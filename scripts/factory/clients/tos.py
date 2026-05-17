"""火山引擎 TOS 对象存储 client。

OmniHuman 要求 image_url / audio_url 是公网 URL,所以本地生成的
character.png / voice.mp3 都要先上传 TOS 拿到 URL。

环境变量:
  VOLC_AK             Access Key ID(同方舟那套,可复用)
  VOLC_SK             Secret Access Key
  TOS_REGION          区域,默认 cn-beijing
  TOS_ENDPOINT        endpoint,默认 tos-cn-beijing.volces.com
  TOS_BUCKET          桶名(需先在控制台创建,公共读私有写)
  TOS_OBJECT_PREFIX   对象 key 前缀,默认 ai-video-factory/
"""
import hashlib
import os
import time
from pathlib import Path

import tos as _tos


def _client() -> "_tos.TosClientV2":
    ak = os.environ.get("VOLC_AK")
    sk = os.environ.get("VOLC_SK")
    region = os.getenv("TOS_REGION", "cn-beijing")
    endpoint = os.getenv("TOS_ENDPOINT", f"tos-{region}.volces.com")
    if not (ak and sk):
        raise RuntimeError("VOLC_AK / VOLC_SK not set in .env")
    return _tos.TosClientV2(ak, sk, endpoint, region)


def upload(local_path: str | Path, key_hint: str | None = None) -> str:
    """上传一个本地文件,返回公网可访问的 URL。

    key_hint: 可读 key 前缀,例如 'character'、'voice'。最终 key:
        <prefix>/<key_hint>-<8 位哈希>.<ext>
    """
    bucket = os.environ.get("TOS_BUCKET")
    if not bucket:
        raise RuntimeError("TOS_BUCKET not set in .env")

    local_path = Path(local_path)
    if not local_path.exists():
        raise FileNotFoundError(local_path)

    prefix = os.getenv("TOS_OBJECT_PREFIX", "ai-video-factory").rstrip("/")
    # 用文件内容哈希做 key 后缀,实现幂等(相同文件不重复上传)
    h = hashlib.sha1(local_path.read_bytes()).hexdigest()[:8]
    ts = int(time.time())
    name = key_hint or local_path.stem
    key = f"{prefix}/{name}-{ts}-{h}{local_path.suffix}"

    cli = _client()
    # 已存在则直接返回 URL(同名跳过)
    try:
        cli.head_object(bucket, key)
    except _tos.exceptions.TosClientError:
        raise
    except _tos.exceptions.TosServerError as e:
        if e.status_code != 404:
            raise
        # 不存在 → 上传
        with open(local_path, "rb") as f:
            cli.put_object(bucket, key, content=f)

    region = os.getenv("TOS_REGION", "cn-beijing")
    endpoint = os.getenv("TOS_ENDPOINT", f"tos-{region}.volces.com")
    return f"https://{bucket}.{endpoint}/{key}"


def signed_url(key_or_url: str, expires: int = 3600) -> str:
    """对私有 bucket,生成限时签名 URL。"""
    bucket = os.environ["TOS_BUCKET"]
    region = os.getenv("TOS_REGION", "cn-beijing")
    endpoint = os.getenv("TOS_ENDPOINT", f"tos-{region}.volces.com")

    # 接受完整 URL 或单纯 key
    if key_or_url.startswith("http"):
        prefix = f"https://{bucket}.{endpoint}/"
        if not key_or_url.startswith(prefix):
            return key_or_url
        key = key_or_url[len(prefix):]
    else:
        key = key_or_url

    cli = _client()
    out = cli.pre_signed_url(_tos.HttpMethodType.Http_Method_Get, bucket, key, expires)
    return out.signed_url
