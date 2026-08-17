"""gpt-image-2 烟测:出一张图,验证 key + 网络 + 模型 + 文字渲染。
重点考察文字渲染——这是 gpt-image-2 比 Seedream 的核心差异点。
成本约 ¥0.40(medium 1088x1920)。"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SCRIPTS))

from factory.clients import gpt_image  # noqa: E402

PROMPT = """A 45-year-old Asian male construction site boss sitting at a wooden desk in a construction container office, deep gray suit and white shirt, examining a price sheet with intense focus. The price sheet on the desk clearly shows readable Chinese text: "高空车租赁价目表", with two lines below: "日租 ¥800" and "月租 ¥200". Industrial documentary cinematic style, 35mm film grain, natural golden side light from window with visible dust motes in the light beam, soft fill on face, shallow depth of field background bokeh, vertical 9:16 portrait composition."""


async def main():
    out_dir = SKILL_ROOT / "data" / "ab_still"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[gpt-image-2] generating with model={gpt_image._model()} "
          f"size={gpt_image._default_size()} quality={gpt_image._default_quality()}",
          flush=True)
    try:
        img = await gpt_image.generate(PROMPT)
    except gpt_image.ContentPolicyError as e:
        print(f"[FAIL] 内容审核拒绝: {e}", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {str(e)[:500]}", flush=True)
        sys.exit(2)
    out = out_dir / "gpt_image_test.png"
    out.write_bytes(img)
    print(f"[OK] {len(img) // 1024} KB -> {out}", flush=True)
    print(f"[cost] 估算 ¥{gpt_image.estimate_cost_cents() / 100:.2f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
