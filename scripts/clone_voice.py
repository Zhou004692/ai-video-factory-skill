"""一键声音复刻 — 上传音频到火山,等训练完成,提示填回 roster。

前置:
  1. 火山控制台 https://console.volcengine.com/speech/service/9 创建 speaker
     (拿到 S_xxx 格式的 speaker_id)
  2. 录 30s+ 单人清晰音频(规范见 references/voice_cloning_guide.md)

用法:
  py scripts/clone_voice.py S_laozhou01 path/to/laozhou_30s.mp3
  py scripts/clone_voice.py S_laozhou01 clip1.mp3 clip2.mp3 clip3.mp3  # 多段更稳

完成后:
  把 speaker_id 填到 references/characters/roster.yaml 对应角色的 voice_id
  下次跑 job 时 seedtts_v2 自动识别 S_ 前缀切 seed-icl-2.0 资源,管线透明支持。
"""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

SKILL_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(SKILL_ROOT / ".env")
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from factory.clients import seedtts_clone  # noqa: E402


async def main(speaker_id: str, audio_paths: list[str]) -> None:
    # 校验音频文件
    for p in audio_paths:
        if not Path(p).exists():
            raise SystemExit(f"音频文件不存在: {p}")
        size_mb = Path(p).stat().st_size / 1024 / 1024
        if size_mb > 50:
            print(f"[clone] WARN {p} 体积 {size_mb:.1f}MB 偏大,建议 <10MB",
                  flush=True)

    print(f"[clone] 上传 {len(audio_paths)} 段音频到 {speaker_id}...",
          flush=True)
    res = await seedtts_clone.upload_audio(
        speaker_id, audio_paths, model_type=4
    )
    print(f"[clone] 上传响应: {res}", flush=True)

    print(f"[clone] 等待训练完成 (ICL 2.0 秒级,服务端可能排队几分钟)...",
          flush=True)
    await seedtts_clone.wait_until_ready(speaker_id, timeout=600)

    print("")
    print(f"✅ 复刻完成!speaker_id = {speaker_id}")
    print("")
    print("下一步:")
    print(f"  1. 编辑 references/characters/roster.yaml")
    print(f"  2. 把对应角色(如 boss_laozhou)的 voice_id 改成 {speaker_id}")
    print(f"  3. 下次 job 自动用专属音色(老周声音 IP 完整)")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    speaker = sys.argv[1]
    audios = sys.argv[2:]
    asyncio.run(main(speaker, audios))
