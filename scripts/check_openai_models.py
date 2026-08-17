"""列出 OpenAI 账号实际能调用的图像类模型 —— 验证 gpt-image-2 是否真实存在。
不消耗 credit,billing limit 一般不挡 metadata 调用。"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
load_dotenv(SKILL_ROOT / ".env")

key = os.environ.get("OPENAI_API_KEY")
if not key:
    print("[FAIL] OPENAI_API_KEY 未配置")
    sys.exit(1)

base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
print(f"[*] base={base}")

try:
    r = httpx.get(f"{base}/models",
                  headers={"Authorization": f"Bearer {key}"},
                  timeout=60)
except Exception as e:
    print(f"[FAIL] 网络错误: {type(e).__name__}: {e}")
    sys.exit(2)

print(f"[*] status={r.status_code}")
if r.status_code != 200:
    print(f"[FAIL] body={r.text[:800]}")
    sys.exit(3)

models = sorted([m["id"] for m in r.json()["data"]])
print(f"[*] 账号可用模型总数: {len(models)}")

image_like = [m for m in models if "image" in m.lower() or "dall" in m.lower()]
print(f"[*] 图像类模型({len(image_like)} 个):")
for m in image_like:
    print(f"      - {m}")

for target in ["gpt-image-2", "gpt-image-1", "gpt-image-1-mini",
               "gpt-image-1.5", "dall-e-3", "dall-e-2"]:
    present = target in models
    mark = "✅" if present else "❌"
    print(f"  {mark} {target}")
