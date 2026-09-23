"""Zero-cost, read-only transport preflight with sanitized phase evidence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import time
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class ProbeTarget:
    target_id: str
    label: str
    host: str
    path: str = "/"
    port: int = 443
    method: str = "HEAD"

    @property
    def url(self) -> str:
        suffix = self.path if str(self.path).startswith("/") else f"/{self.path}"
        port = "" if self.port == 443 else f":{self.port}"
        return f"https://{self.host}{port}{suffix}"


class TransportProbeFailure(RuntimeError):
    def __init__(self, phase: str, cause: BaseException):
        super().__init__(phase)
        self.phase = str(phase or "unknown")
        self.cause = cause


class TransportPreflightError(RuntimeError):
    stable_code = "transient_network"
    code = stable_code
    error_code = stable_code
    retryable = True
    non_retryable = False
    submit_result_unknown = False

    def __init__(self, report: dict):
        super().__init__(
            "模型连接预检未通过，尚未创建远端任务；请查看连接诊断后重试。"
        )
        self.transport_diagnostic = report


def _normalized_host(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        host = parsed.hostname
    except ValueError:
        host = None
    return str(host or fallback).strip().lower()


def default_targets(environment: Mapping[str, str] | None = None) -> tuple[ProbeTarget, ...]:
    env = environment if environment is not None else os.environ
    tos_host = _normalized_host(
        env.get("TOS_ENDPOINT"),
        f"tos-{str(env.get('TOS_REGION') or 'cn-beijing').strip()}.volces.com",
    )
    download_host = _normalized_host(
        env.get("SEEDANCE_DOWNLOAD_PROBE_HOST"),
        "ark-acg-cn-beijing.tos-cn-beijing.volces.com",
    )
    return (
        ProbeTarget("ark_api", "模型 API", "ark.cn-beijing.volces.com", "/api/v3"),
        ProbeTarget("tos_upload", "素材上传", tos_host, "/"),
        ProbeTarget("provider_download", "成片下载", download_host, "/"),
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _direct_probe(target: ProbeTarget, timeout: float) -> list[dict]:
    phases: list[dict] = []
    phase = "dns"
    started = time.monotonic()
    raw_socket = None
    tls_socket = None
    try:
        addresses = socket.getaddrinfo(
            target.host, target.port, type=socket.SOCK_STREAM
        )
        if not addresses:
            raise socket.gaierror("no address records")
        phases.append({
            "phase": "dns", "status": "ok", "latency_ms": _elapsed_ms(started),
            "address_count": len({item[4][0] for item in addresses}),
        })

        phase = "tcp"
        started = time.monotonic()
        raw_socket = socket.create_connection(
            (target.host, target.port), timeout=float(timeout)
        )
        raw_socket.settimeout(float(timeout))
        phases.append({
            "phase": "tcp", "status": "ok", "latency_ms": _elapsed_ms(started),
        })

        phase = "tls"
        started = time.monotonic()
        tls_socket = ssl.create_default_context().wrap_socket(
            raw_socket, server_hostname=target.host
        )
        raw_socket = None
        phases.append({
            "phase": "tls", "status": "ok", "latency_ms": _elapsed_ms(started),
            "certificate_verified": True,
        })

        phase = "http"
        started = time.monotonic()
        request = (
            f"HEAD {target.path or '/'} HTTP/1.1\r\n"
            f"Host: {target.host}\r\n"
            "User-Agent: ai-video-factory-transport-preflight/1\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii", errors="strict")
        tls_socket.sendall(request)
        status_line = tls_socket.recv(512).split(b"\r\n", 1)[0]
        parts = status_line.split(b" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise httpx.RemoteProtocolError("invalid HTTP status line")
        phases.append({
            "phase": "http", "status": "ok", "latency_ms": _elapsed_ms(started),
            "http_status": int(parts[1]),
        })
        return phases
    except TransportProbeFailure:
        raise
    except Exception as exc:
        raise TransportProbeFailure(phase, exc) from exc
    finally:
        if tls_socket is not None:
            tls_socket.close()
        if raw_socket is not None:
            raw_socket.close()


def _route_probe(
    target: ProbeTarget,
    timeout: float,
    environment: Mapping[str, str],
) -> dict:
    started = time.monotonic()
    try:
        proxy = None
        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            if str(environment.get(key) or "").strip():
                proxy = str(environment.get(key)).strip()
                break
        with httpx.Client(
            timeout=float(timeout),
            follow_redirects=False,
            trust_env=False,
            proxy=proxy,
        ) as client:
            response = client.request(
                target.method,
                target.url,
                headers={"User-Agent": "ai-video-factory-transport-preflight/1"},
            )
        return {
            "phase": "application_route", "status": "ok",
            "latency_ms": _elapsed_ms(started),
            "http_status": int(response.status_code),
        }
    except Exception as exc:
        raise TransportProbeFailure("application_route", exc) from exc


def _proxy_summary(environment: Mapping[str, str]) -> dict:
    value = ""
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        if str(environment.get(key) or "").strip():
            value = str(environment.get(key)).strip()
            break
    if not value:
        return {"mode": "direct", "proxy": None}
    try:
        parsed = urlsplit(value)
        host = str(parsed.hostname or "").strip()
        port = parsed.port
    except ValueError:
        host = ""
        port = None
    safe = f"{host}:{port}" if host and port else host or "configured"
    return {"mode": "environment_proxy", "proxy": safe}


def _phase_for_exception(error: BaseException, fallback: str) -> str:
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, (ssl.SSLError, ssl.CertificateError)):
        return "tls"
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
        return "tcp"
    if isinstance(error, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)):
        return "http_response"
    if isinstance(error, TimeoutError):
        return "tcp" if fallback in {"dns", "tcp", "connect", "submit_connect"} else fallback
    return str(fallback or "unknown")


def _reason_code(phase: str, error: BaseException) -> str:
    if phase == "dns":
        return "dns_resolution_failed"
    if phase == "tls":
        return "tls_handshake_failed"
    if phase == "tcp":
        return "tcp_connect_timeout" if isinstance(
            error, (TimeoutError, httpx.ConnectTimeout)
        ) else "tcp_connect_failed"
    if phase in {"http", "http_response", "application_route"}:
        return "http_response_timeout" if isinstance(
            error, (TimeoutError, httpx.TimeoutException)
        ) else "http_transport_failed"
    return "transport_failed"


def _safe_host(endpoint: object) -> str:
    value = str(endpoint or "").strip()
    if not value:
        return "unknown"
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        host = parsed.hostname
    except ValueError:
        host = None
    return str(host or "unknown").lower()


def exception_diagnostic(
    error: BaseException,
    *,
    stage: str,
    endpoint: object,
    submit_result_unknown: bool,
    phase: str = "",
    submit_state: str | None = None,
) -> dict:
    resolved_phase = _phase_for_exception(error, phase or stage)
    resolved_submit_state = (
        str(submit_state)
        if submit_state in {"not_submitted", "unknown", "submitted_known"}
        else "unknown" if submit_result_unknown else "not_submitted"
    )
    return {
        "stage": str(stage or "unknown"),
        "phase": resolved_phase,
        "host": _safe_host(endpoint),
        "error_type": type(error).__name__,
        "reason_code": _reason_code(resolved_phase, error),
        "retryable": resolved_submit_state == "not_submitted",
        "submit_state": resolved_submit_state,
    }


def probe_target(
    target: ProbeTarget,
    *,
    timeout: float = 4.0,
    environment: Mapping[str, str] | None = None,
    direct_probe: Callable[[ProbeTarget, float], list[dict]] | None = None,
    route_probe: Callable[..., dict] | None = None,
) -> dict:
    env = environment if environment is not None else os.environ
    direct = direct_probe or _direct_probe
    routed = route_probe or _route_probe
    report = {
        "target_id": target.target_id,
        "label": target.label,
        "host": target.host,
        "port": target.port,
        "method": target.method,
        "route": _proxy_summary(env),
        "ready": False,
        "status": "failed",
        "phases": [],
    }
    try:
        report["phases"].extend(direct(target, float(timeout)))
        report["phases"].append(routed(target, float(timeout), env))
    except TransportProbeFailure as failure:
        diagnostic = exception_diagnostic(
            failure.cause,
            stage="transport_preflight",
            endpoint=target.url,
            submit_result_unknown=False,
            phase=failure.phase,
        )
        report["failure"] = diagnostic
        report["phases"].append({
            "phase": diagnostic["phase"],
            "status": "failed",
            "reason_code": diagnostic["reason_code"],
        })
        return report
    report["ready"] = True
    report["status"] = "ok"
    return report


def _atomic_write_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def load_report(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def run_preflight(
    *,
    targets: Iterable[ProbeTarget] | None = None,
    timeout: float = 4.0,
    environment: Mapping[str, str] | None = None,
    probe: Callable[..., dict] = probe_target,
    report_path: Path | None = None,
) -> dict:
    env = environment if environment is not None else os.environ
    resolved_targets = tuple(targets or default_targets(env))
    results = [
        probe(target, timeout=timeout, environment=env)
        for target in resolved_targets
    ]
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest_source = json.dumps(
        [{"target_id": item.get("target_id"), "ready": item.get("ready"),
          "failure": item.get("failure")} for item in results],
        sort_keys=True,
    )
    report = {
        "diagnostic_id": "transport-" + hashlib.sha256(
            f"{checked_at}:{digest_source}".encode("utf-8")
        ).hexdigest()[:16],
        "checked_at": checked_at,
        "ready": bool(results) and all(bool(item.get("ready")) for item in results),
        "paid_provider_calls": 0,
        "provider_task_created": False,
        "targets": results,
    }
    if report_path is not None:
        _atomic_write_json(Path(report_path), report)
    return report


def require_ready(**kwargs) -> dict:
    report = run_preflight(**kwargs)
    if not report["ready"]:
        raise TransportPreflightError(report)
    return report
