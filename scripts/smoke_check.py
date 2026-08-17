"""Low-cost connectivity checks for the Seedance-only production stack."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
results: list[tuple[str, str, str]] = []


def ok(service: str, detail: str = "") -> None:
    results.append((service, "OK", detail))
    print(f"{GREEN}[OK]{RESET}   {service:24s} {detail}")


def fail(service: str, detail: str) -> None:
    results.append((service, "FAIL", detail))
    print(f"{RED}[FAIL]{RESET} {service:24s} {detail}")


def skip(service: str, detail: str) -> None:
    results.append((service, "SKIP", detail))
    print(f"{YELLOW}[SKIP]{RESET} {service:24s} {detail}")


def check_anthropic() -> None:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key or key.startswith("sk-ant-..."):
        skip("ANTHROPIC", "未配置；默认 LLM 使用豆包")
        return
    try:
        from anthropic import Anthropic

        models = Anthropic(api_key=key).models.list(limit=1)
        ok("ANTHROPIC", f"models.list 返回 {len(models.data)} 条")
    except Exception as exc:
        fail("ANTHROPIC", f"{type(exc).__name__}: {str(exc)[:200]}")


def _ark_post(path: str, payload: dict) -> httpx.Response:
    key = os.environ.get("ARK_API_KEY")
    if not key:
        raise RuntimeError("ARK_API_KEY not set")
    with httpx.Client(timeout=30) as client:
        return client.post(
            f"https://ark.cn-beijing.volces.com/api/v3/{path}",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )


def check_ark_llm() -> None:
    model = os.getenv("ARK_LLM_MODEL", "doubao-seed-2-0-pro-260215")
    try:
        response = _ark_post(
            "chat/completions",
            {
                "model": model,
                "max_tokens": 2,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        if response.status_code == 200:
            ok("ARK_LLM", f"model={model}")
        else:
            fail("ARK_LLM", f"{response.status_code}: {response.text[:200]}")
    except Exception as exc:
        fail("ARK_LLM", f"{type(exc).__name__}: {str(exc)[:200]}")


def check_ark_video() -> None:
    """Use an invalid 31-second request so access is checked before billing."""
    model = os.getenv("ARK_VIDEO_MODEL", "doubao-seedance-2-5-260628")
    payload = {
        "model": model,
        "content": [{"type": "text", "text": "connectivity check"}],
        "duration": 31,
        "resolution": "480p",
        "ratio": "9:16",
        "generate_audio": True,
    }
    try:
        response = _ark_post("contents/generations/tasks", payload)
        if response.status_code == 400:
            ok("ARK_VIDEO", f"model={model}；31 秒参数按预期被拒，未创建任务")
        elif response.status_code in {401, 403, 404}:
            fail("ARK_VIDEO", f"{response.status_code}: {response.text[:240]}")
        elif response.status_code < 300:
            fail("ARK_VIDEO", "31 秒无效请求被意外接受；请检查供应商限制")
        else:
            fail("ARK_VIDEO", f"{response.status_code}: {response.text[:240]}")
    except Exception as exc:
        fail("ARK_VIDEO", f"{type(exc).__name__}: {str(exc)[:200]}")


def check_tos() -> None:
    ak = os.environ.get("VOLC_AK")
    sk = os.environ.get("VOLC_SK")
    bucket = os.environ.get("TOS_BUCKET")
    region = os.getenv("TOS_REGION", "cn-beijing")
    endpoint = os.getenv("TOS_ENDPOINT", f"tos-{region}.volces.com")
    if not (ak and sk and bucket):
        fail("TOS", "VOLC_AK / VOLC_SK / TOS_BUCKET 未完整配置")
        return
    try:
        import tos

        tos.TosClientV2(ak, sk, endpoint, region).head_bucket(bucket)
        ok("TOS", f"bucket={bucket} @ {endpoint}")
    except Exception as exc:
        fail("TOS", f"{type(exc).__name__}: {str(exc)[:200]}")


def main() -> None:
    print("\nAI Video Factory · Seedance-only 连通性检查\n")
    check_anthropic()
    check_ark_llm()
    check_ark_video()
    check_tos()
    n_ok = sum(1 for _, status, _ in results if status == "OK")
    n_fail = sum(1 for _, status, _ in results if status == "FAIL")
    n_skip = sum(1 for _, status, _ in results if status == "SKIP")
    print(f"\n汇总: {n_ok} OK · {n_fail} FAIL · {n_skip} SKIP\n")
    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
