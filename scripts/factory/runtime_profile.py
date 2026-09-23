"""Validated runtime boundaries shared by desktop and server deployments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import os
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from runtime_paths import resolve_data_dir


class RuntimeProfileError(RuntimeError):
    """The requested runtime configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class RuntimeProfile:
    mode: Literal["desktop", "server"]
    data_dir: Path
    public_base_url: str | None
    trusted_origins: tuple[str, ...]
    worker_mode: Literal["child_process", "external"]
    bind_host: str

    def as_public_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["data_dir"] = str(self.data_dir)
        return payload


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _writable_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".runtime-write-probe"
    try:
        probe.write_bytes(b"ok")
    except OSError as exc:
        raise RuntimeProfileError(f"数据目录不可写：{path}") from exc
    finally:
        probe.unlink(missing_ok=True)
    return path


def resolve_runtime_profile(
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeProfile:
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    values = dict(os.environ if environ is None else environ)
    mode = str(values.get("FACTORY_RUNTIME") or "desktop").strip().casefold()
    if mode not in {"desktop", "server"}:
        raise RuntimeProfileError("FACTORY_RUNTIME 只能是 desktop 或 server")

    bind_host = str(
        values.get("FACTORY_BIND_HOST")
        or ("127.0.0.1" if mode == "desktop" else "0.0.0.0")
    ).strip()
    data_value = str(values.get("FACTORY_DATA_DIR") or "").strip()

    if mode == "desktop":
        if not _is_loopback(bind_host):
            raise RuntimeProfileError("本地模式只允许回环地址，禁止直接暴露公网")
        data_dir = resolve_data_dir(root, values)
        replay_without_workers = (
            str(values.get("FACTORY_PROVIDER_MODE") or "").strip().casefold()
            == "replay"
            and str(values.get("FACTORY_REPLAY_DISABLE_WORKERS") or "")
            .strip().casefold() in {"1", "true", "yes", "on"}
        )
        return RuntimeProfile(
            mode="desktop",
            data_dir=_writable_directory(data_dir.resolve()),
            public_base_url=None,
            trusted_origins=(),
            worker_mode="external" if replay_without_workers else "child_process",
            bind_host=bind_host,
        )

    public_base_url = str(values.get("FACTORY_PUBLIC_BASE_URL") or "").strip()
    if not public_base_url:
        raise RuntimeProfileError("server 模式必须配置 FACTORY_PUBLIC_BASE_URL")
    parsed = urlsplit(public_base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeProfileError("FACTORY_PUBLIC_BASE_URL 必须是 HTTPS 站点根地址")
    if not data_value:
        raise RuntimeProfileError("server 模式必须配置 FACTORY_DATA_DIR")
    secret = str(values.get("FACTORY_SESSION_SECRET") or "")
    if len(secret) < 32:
        raise RuntimeProfileError("server 模式必须配置至少 32 位 FACTORY_SESSION_SECRET")
    password_hash = str(values.get("FACTORY_ADMIN_PASSWORD_HASH") or "").strip()
    if not password_hash:
        raise RuntimeProfileError("server 模式必须配置 FACTORY_ADMIN_PASSWORD_HASH")
    data_dir = Path(data_value).expanduser()
    if not data_dir.is_absolute():
        raise RuntimeProfileError("server 模式的 FACTORY_DATA_DIR 必须使用绝对路径")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return RuntimeProfile(
        mode="server",
        data_dir=_writable_directory(data_dir.resolve()),
        public_base_url=origin,
        trusted_origins=(origin,),
        worker_mode="external",
        bind_host=bind_host,
    )
