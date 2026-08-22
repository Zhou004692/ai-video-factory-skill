from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class CrossPlatformMediaRuntimeTests(unittest.TestCase):
    def test_explicit_font_must_exist(self):
        from factory.media_runtime import MediaRuntimeError, resolve_subtitle_font

        with self.assertRaisesRegex(MediaRuntimeError, "字幕字体不存在"):
            resolve_subtitle_font(
                environ={"SUBTITLE_FONT": "Z:/missing/font.ttc"},
                platform="win32",
            )

    def test_windows_and_linux_fonts_are_selected_from_existing_candidates(self):
        from factory.media_runtime import resolve_subtitle_font

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            windows = base / "msyhbd.ttc"
            linux = base / "NotoSansCJK-Bold.ttc"
            windows.write_bytes(b"font")
            linux.write_bytes(b"font")

            self.assertEqual(
                resolve_subtitle_font(
                    environ={}, platform="win32", candidates=(windows,)
                ),
                windows.resolve(),
            )
            self.assertEqual(
                resolve_subtitle_font(
                    environ={}, platform="linux", candidates=(linux,)
                ),
                linux.resolve(),
            )

    def test_binary_resolver_honors_explicit_path_and_fails_actionably(self):
        from factory.media_runtime import MediaRuntimeError, resolve_binary

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "ffmpeg.exe"
            binary.write_bytes(b"binary")
            resolved = resolve_binary(
                "ffmpeg",
                environ={"FFMPEG_PATH": str(binary)},
                which=lambda _: None,
            )
            self.assertEqual(resolved, binary.resolve())

        with self.assertRaisesRegex(MediaRuntimeError, "未找到 FFprobe"):
            resolve_binary("ffprobe", environ={}, which=lambda _: None)

    def test_ffmpeg_drawtext_resolves_font_at_call_time(self):
        from factory.clients import ffmpeg

        with tempfile.TemporaryDirectory() as tmp:
            font = Path(tmp) / "font.ttc"
            font.write_bytes(b"font")
            with mock.patch(
                "factory.clients.ffmpeg.resolve_subtitle_font",
                return_value=font,
            ) as resolver:
                rendered = ffmpeg._drawtext(
                    Path(tmp) / "subtitle.txt",
                    fontsize=32,
                    y="100",
                )

        resolver.assert_called_once_with()
        self.assertIn(ffmpeg._esc(str(font)), rendered)

    def test_doctor_reports_runtime_storage_and_media_without_secrets(self):
        from doctor import diagnose

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "runtime-data"
            font = root / "font.ttc"
            binary = root / "ffmpeg.exe"
            font.write_bytes(b"font")
            binary.write_bytes(b"binary")
            (root / ".env").write_text(
                "FACTORY_RUNTIME=desktop\n"
                "FACTORY_DATA_DIR=runtime-data\n"
                "ARK_API_KEY=secret-value\nVOLC_AK=ak\nVOLC_SK=sk\n"
                "TOS_BUCKET=bucket\n",
                encoding="utf-8",
            )
            with mock.patch("doctor.resolve_binary", return_value=binary), mock.patch(
                "doctor.resolve_subtitle_font", return_value=font
            ):
                report = diagnose(root, profile="video")

            self.assertEqual(report["runtime"]["mode"], "desktop")
            self.assertEqual(report["runtime"]["data_dir"], str(data_dir.resolve()))
            self.assertTrue(report["checks"]["data_directory"]["ok"])
            self.assertTrue(report["checks"]["ffmpeg"]["ok"])
            self.assertTrue(report["checks"]["ffprobe"]["ok"])
            self.assertTrue(report["checks"]["subtitle_font"]["ok"])
            self.assertNotIn("secret-value", json.dumps(report, ensure_ascii=False))


class RealMediaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_second_chinese_caption_media_is_playable(self):
        from factory.clients import ffmpeg
        from factory.media_runtime import MediaRuntimeError

        asyncio.get_running_loop().slow_callback_duration = 1.0
        try:
            ffmpeg.executable("ffmpeg")
            ffmpeg.executable("ffprobe")
            ffmpeg.resolve_subtitle_font()
        except MediaRuntimeError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            subtitle = base / "subtitle.txt"
            output = base / "caption-smoke.mp4"
            subtitle.write_text("暴雨抢修\n安全第一", encoding="utf-8")
            await ffmpeg._run([
                "-f", "lavfi", "-i", "color=c=0x315a4a:s=320x240:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-vf", ffmpeg._drawtext(subtitle, fontsize=28, y="h*0.70"),
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(output),
            ])

            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertAlmostEqual(await ffmpeg.probe_duration(output), 2.0, delta=0.2)
            self.assertTrue(await ffmpeg.has_audio_stream(output))


if __name__ == "__main__":
    unittest.main()
