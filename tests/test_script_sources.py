"""Script-source ingestion, provenance and topic-link tests."""
from pathlib import Path
import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


from factory import (  # noqa: E402
    db,
    media_sources,
    prompts,
    script_sources,
    source_analysis,
    topic_briefs,
)
from factory.clients import bigasr, ffmpeg  # noqa: E402
import cli as cli_module  # noqa: E402


class ScriptSourceTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()

    def test_ingest_text_normalizes_content_and_deduplicates_by_fingerprint(self):
        first = script_sources.ingest_text(
            self.memory_db,
            "设备租赁\r\n\r\n  客户最怕信息不透明。  ",
            title="行业观察",
            source_uri="manual://meeting-note",
        )
        second = script_sources.ingest_text(
            self.memory_db,
            "设备租赁\n\n客户最怕信息不透明。",
            title="重复导入",
            source_uri="manual://copy",
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.source_id, second.source_id)
        row = self.memory_db.execute(
            "SELECT * FROM script_sources WHERE source_id=?",
            (first.source_id,),
        ).fetchone()
        self.assertEqual(row["content"], "设备租赁\n\n客户最怕信息不透明。")
        self.assertEqual(row["source_type"], "text")
        self.assertEqual(row["source_uri"], "manual://meeting-note")

    def test_ingest_file_keeps_file_provenance_and_removes_srt_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "competitor.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:02,100\n设备租赁狗都不做\n\n"
                "2\n00:00:02,200 --> 00:00:04,000\n狗不做，我做\n",
                encoding="utf-8",
            )

            result = script_sources.ingest_file(self.memory_db, path)

        row = self.memory_db.execute(
            "SELECT * FROM script_sources WHERE source_id=?",
            (result.source_id,),
        ).fetchone()
        self.assertEqual(row["source_type"], "subtitle")
        self.assertEqual(row["title"], "competitor")
        self.assertEqual(row["content"], "设备租赁狗都不做\n狗不做，我做")
        self.assertTrue(row["source_uri"].endswith("competitor.srt"))

    def test_link_source_to_topic_is_idempotent_and_exposes_prompt_context(self):
        result = script_sources.ingest_text(
            self.memory_db,
            "客户要找设备，但不同业务员的信息都散在聊天记录里。",
            title="客户找设备痛点",
            source_uri="manual://interview-01",
        )

        script_sources.link_source_to_topic(
            self.memory_db, result.source_id, "T-SOURCE-01"
        )
        script_sources.link_source_to_topic(
            self.memory_db, result.source_id, "T-SOURCE-01"
        )
        context = script_sources.topic_source_context(
            self.memory_db, "T-SOURCE-01"
        )

        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM topic_script_sources"
            ).fetchone()[0],
            1,
        )
        self.assertIn("客户找设备痛点", context)
        self.assertIn("manual://interview-01", context)
        self.assertIn("仅作为创作素材", context)

    def test_private_or_loopback_urls_are_rejected_before_fetch(self):
        for url in (
            "http://127.0.0.1/admin",
            "http://localhost:8080/secret",
            "http://10.0.0.8/internal",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(
                ValueError, "public HTTP"
            ):
                script_sources.validate_public_url(url)

    def test_redirect_target_is_validated_before_the_next_request(self):
        with self.assertRaisesRegex(ValueError, "public HTTP"):
            script_sources.redirect_target(
                "https://example.com/article", "http://127.0.0.1/admin"
            )

        self.assertEqual(
            script_sources.redirect_target(
                "https://example.com/news/article", "../final"
            ),
            "https://example.com/final",
        )

    def test_storyboard_prompt_includes_only_the_linked_source_context(self):
        source = script_sources.ingest_text(
            self.memory_db,
            "客户真正担心的是设备到了以后没人服务。",
            title="服务访谈",
            source_uri="manual://service-interview",
        )
        script_sources.link_source_to_topic(
            self.memory_db, source.source_id, "T-LINKED"
        )

        linked = script_sources.topic_source_context(
            self.memory_db, "T-LINKED"
        )
        prompt = prompts.storyboard_prompt(
            "设备服务", "租赁", source_context=linked
        )
        unlinked = prompts.storyboard_prompt(
            "另一个选题", "租赁", source_context=""
        )

        self.assertIn("客户真正担心的是设备到了以后没人服务", prompt)
        self.assertNotIn("客户真正担心的是设备到了以后没人服务", unlinked)

    def test_cli_source_add_and_link_produce_traceable_records(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("note.txt").write_text("租赁销售需要统一客户记录。", encoding="utf-8")
            with patch.object(cli_module, "conn", return_value=self.memory_db):
                added = runner.invoke(
                    cli_module.cli,
                    ["source-add", "--file", "note.txt", "--title", "访谈"],
                )
                source_id = self.memory_db.execute(
                    "SELECT source_id FROM script_sources"
                ).fetchone()[0]
                linked = runner.invoke(
                    cli_module.cli,
                    ["source-link", source_id, "T-CLI-01"],
                )

        self.assertEqual(added.exit_code, 0, added.output)
        self.assertIn(source_id, added.output)
        self.assertEqual(linked.exit_code, 0, linked.output)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT topic_id FROM topic_script_sources"
            ).fetchone()[0],
            "T-CLI-01",
        )


class MediaSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)
        self.tmp = tempfile.TemporaryDirectory()
        self.media_dir = Path(self.tmp.name) / "archive"

    def tearDown(self):
        self.memory_db.close()
        self.tmp.cleanup()

    async def test_audio_ingest_transcribes_and_deduplicates_the_same_media(self):
        source = Path(self.tmp.name) / "interview.mp3"
        source.write_bytes(b"same-media-bytes")
        transcription = bigasr.Transcription(
            "客户找设备时，最怕信息不透明。",
            120,
            2680,
            (bigasr.TimedUtterance(
                "客户找设备时，最怕信息不透明。", 120, 2680
            ),),
        )
        with patch.object(
            media_sources.bigasr, "transcribe_detailed",
            new=Mock(return_value=transcription),
        ) as transcribe:
            first = await media_sources.ingest_media(
                self.memory_db, source, archive_dir=self.media_dir,
                topic_id="T-AUDIO-01",
            )
            second = await media_sources.ingest_media(
                self.memory_db, source, archive_dir=self.media_dir,
                topic_id="T-AUDIO-01",
            )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.source_id, second.source_id)
        self.assertEqual(transcribe.call_count, 1)
        row = self.memory_db.execute(
            "SELECT * FROM script_sources WHERE source_id=?",
            (first.source_id,),
        ).fetchone()
        metadata = __import__("json").loads(row["metadata_json"])
        self.assertEqual(row["source_type"], "audio")
        self.assertEqual(row["content"], "客户找设备时，最怕信息不透明。")
        self.assertEqual(metadata["media_sha256"], first.media_sha256)
        self.assertEqual(metadata["utterances"][0]["start_ms"], 120)
        self.assertEqual(
            self.memory_db.execute(
                "SELECT topic_id FROM topic_script_sources"
            ).fetchone()[0],
            "T-AUDIO-01",
        )
        context = script_sources.topic_source_context(
            self.memory_db, "T-AUDIO-01"
        )
        self.assertIn("[0.120s—2.680s]", context)

    async def test_video_ingest_extracts_audio_and_samples_frame_manifest(self):
        source = Path(self.tmp.name) / "competitor.mp4"
        source.write_bytes(b"video-bytes")
        transcription = bigasr.Transcription("设备租赁为什么越来越难做？")
        expected_frames = [
            self.media_dir / "expected" / f"frame-{index:03d}.jpg"
            for index in range(1, 5)
        ]
        with patch.object(
            media_sources.ffmpeg, "probe_duration",
            new=AsyncMock(return_value=11.2),
        ), patch.object(
            media_sources.ffmpeg, "extract_audio_wav", new=AsyncMock()
        ) as extract_audio, patch.object(
            media_sources.ffmpeg, "extract_frames",
            new=AsyncMock(return_value=expected_frames)
        ) as extract_frames, patch.object(
            media_sources.bigasr, "transcribe_detailed",
            new=Mock(return_value=transcription),
        ):
            result = await media_sources.ingest_media(
                self.memory_db, source, archive_dir=self.media_dir,
                frame_interval_sec=4,
            )

        extract_audio.assert_awaited_once()
        extract_frames.assert_awaited_once()
        timestamps = extract_frames.await_args.args[2]
        self.assertEqual(timestamps, [0.0, 4.0, 8.0, 11.0])
        row = self.memory_db.execute(
            "SELECT * FROM script_sources WHERE source_id=?",
            (result.source_id,),
        ).fetchone()
        metadata = __import__("json").loads(row["metadata_json"])
        self.assertEqual(row["source_type"], "video")
        self.assertEqual(metadata["duration_sec"], 11.2)
        self.assertEqual(len(metadata["frames"]), 4)

    def test_media_context_exposes_frame_manifest_without_binary_content(self):
        result = script_sources.ingest_text(
            self.memory_db, "设备租赁为什么越来越难做？",
            title="竞品视频", source_uri="C:/videos/competitor.mp4",
            source_type="video",
            metadata={
                "duration_sec": 11.2,
                "utterances": [],
                "frames": [
                    {"index": 1, "timestamp_sec": 0.0,
                     "path": "C:/archive/frame-001.jpg"},
                    {"index": 2, "timestamp_sec": 4.0,
                     "path": "C:/archive/frame-002.jpg"},
                ],
            },
        )
        script_sources.link_source_to_topic(
            self.memory_db, result.source_id, "T-VIDEO-CONTEXT"
        )

        context = script_sources.topic_source_context(
            self.memory_db, "T-VIDEO-CONTEXT"
        )

        self.assertIn("时长: 11.200s", context)
        self.assertIn("抽帧 1 @ 0.000s", context)
        self.assertIn("C:/archive/frame-001.jpg", context)

    async def test_media_without_recognizable_speech_is_rejected(self):
        source = Path(self.tmp.name) / "silent.wav"
        source.write_bytes(b"silent")
        with patch.object(
            media_sources.bigasr, "transcribe_detailed",
            new=Mock(return_value=bigasr.Transcription("")),
        ):
            with self.assertRaisesRegex(ValueError, "recognizable speech"):
                await media_sources.ingest_media(
                    self.memory_db, source, archive_dir=self.media_dir
                )
        self.assertFalse(any(self.media_dir.glob("*/source.wav")))

    async def test_cli_media_add_links_transcript_to_topic(self):
        runner = CliRunner()
        with runner.isolated_filesystem():
            source = Path("interview.wav")
            source.write_bytes(b"wav")
            result = media_sources.MediaIngestResult(
                "SRC-media", True, "abc123", Path("archive.wav"), ()
            )
            with patch.object(cli_module, "conn", return_value=self.memory_db), \
                    patch.object(
                        cli_module.media_sources, "ingest_media",
                        new=AsyncMock(return_value=result),
                    ) as ingest:
                invoked = await asyncio.to_thread(
                    runner.invoke, cli_module.cli,
                    ["media-add", "interview.wav", "--topic-id", "T-MEDIA"],
                )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertIn("SRC-media", invoked.output)
        self.assertEqual(ingest.await_args.kwargs["topic_id"], "T-MEDIA")

    async def test_frame_sampler_uses_explicit_timestamps_and_stable_names(self):
        with patch.object(ffmpeg, "_run", new=AsyncMock()) as run:
            paths = await ffmpeg.extract_frames(
                Path("source.mp4"), self.media_dir / "frames", [0.0, 4.0]
            )

        self.assertEqual(
            [path.name for path in paths], ["frame-001.jpg", "frame-002.jpg"]
        )
        self.assertEqual(run.await_count, 2)
        first = run.await_args_list[0].args[0]
        self.assertEqual(first[first.index("-ss") + 1], "0.000")
        self.assertEqual(first[-1], str(self.media_dir / "frames" / "frame-001.jpg"))


class SourceAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)
        self.tmp = tempfile.TemporaryDirectory()
        self.frames = []
        for index in range(1, 4):
            path = Path(self.tmp.name) / f"frame-{index:03d}.jpg"
            path.write_bytes(f"frame-{index}".encode())
            self.frames.append(path)
        self.source = script_sources.ingest_text(
            self.memory_db,
            "设备租赁狗都不做。狗不做，我做。客户难找，回款又慢。",
            title="竞品视频",
            source_uri="C:/videos/competitor.mp4",
            source_type="video",
            metadata={
                "duration_sec": 9.0,
                "utterances": [
                    {"text": "设备租赁狗都不做。", "start_ms": 0,
                     "end_ms": 2200},
                    {"text": "狗不做，我做。", "start_ms": 2300,
                     "end_ms": 3900},
                ],
                "frames": [
                    {"index": index, "timestamp_sec": float((index - 1) * 4),
                     "path": str(path)}
                    for index, path in enumerate(self.frames, start=1)
                ],
            },
            deduplication_key="analysis-fixture",
        )

    def tearDown(self):
        self.memory_db.close()
        self.tmp.cleanup()

    def test_normalizes_frame_observation_contract(self):
        observation = source_analysis.normalize_frame_observation(
            {
                "ocr_text": ["设备租赁", "狗都不做"],
                "visual": "女销售面对镜头讲解",
                "people": "1",
                "objects": ["办公桌", "安全帽"],
                "confidence": 1.4,
            },
            frame_index=2,
            timestamp_sec=4.0,
            path=self.frames[1],
        )

        self.assertEqual(observation["ocr_text"], "设备租赁\n狗都不做")
        self.assertEqual(observation["people_count"], 1)
        self.assertEqual(observation["confidence"], 1.0)
        self.assertEqual(observation["timestamp_sec"], 4.0)

    def test_structure_analysis_preserves_full_video_script_timeline(self):
        raw = {
            "summary": "用冲突开场，再通过现场演示完成反转",
            "segments": [{
                "role": "hook",
                "start_sec": 0,
                "end_sec": 3.2,
                "scene": "设备仓库入口",
                "characters": ["女销售", "客户"],
                "speaker": "女销售",
                "action": "女销售拦住准备离开的客户并指向设备",
                "camera": "中景手持跟拍后快速推近",
                "dialogue": "这台设备一天能省多少钱？",
                "on_screen_text": "一天省 800 元",
                "audio": "环境机械声，人物同期声",
                "evidence": "抽帧 1-2 与 0-3.2 秒转写",
            }],
            "catchphrases": ["先算账，再租设备"],
            "claims_to_verify": ["一天省 800 元"],
        }

        report = source_analysis.normalize_structure_report(
            raw, duration_sec=12.0, observations=[]
        )

        segment = report["segments"][0]
        self.assertEqual(segment["scene"], "设备仓库入口")
        self.assertEqual(segment["characters"], ["女销售", "客户"])
        self.assertEqual(segment["speaker"], "女销售")
        self.assertEqual(segment["action"], "女销售拦住准备离开的客户并指向设备")
        self.assertEqual(segment["camera"], "中景手持跟拍后快速推近")
        self.assertEqual(segment["dialogue"], "这台设备一天能省多少钱？")
        self.assertEqual(segment["on_screen_text"], "一天省 800 元")
        self.assertEqual(segment["audio"], "环境机械声，人物同期声")
        prompt = source_analysis._structure_prompt(
            "原始转写", {"duration_sec": 12, "utterances": []}, []
        )
        for key in (
            '"scene"', '"characters"', '"speaker"', '"action"', '"camera"',
            '"dialogue"', '"on_screen_text"', '"audio"',
        ):
            self.assertIn(key, prompt)
        self.assertIn(
            "同一人物在所有分段中必须使用完全相同的 speaker", prompt
        )

    def test_visual_analysis_samples_the_entire_video_timeline(self):
        self.assertGreaterEqual(
            source_analysis.DEFAULT_MAX_ANALYSIS_FRAMES, 16
        )
        frames = [
            {"index": index + 1, "timestamp_sec": index * 4.0,
             "path": f"frame-{index + 1}.jpg"}
            for index in range(20)
        ]

        selected = source_analysis._select_timeline_frames(frames, max_frames=5)

        self.assertEqual(len(selected), 5)
        self.assertEqual(selected[0]["timestamp_sec"], 0.0)
        self.assertEqual(selected[-1]["timestamp_sec"], 76.0)
        self.assertEqual(
            [item["index"] for item in selected],
            sorted({item["index"] for item in selected}),
        )

    def test_analysis_merges_frame_observations_and_validates_story_roles(self):
        observations = [
            {"frame_index": 1, "timestamp_sec": 0.0,
             "ocr_text": "设备租赁狗都不做", "visual": "女销售看镜头",
             "people_count": 1, "objects": [], "confidence": 0.98},
            {"frame_index": 2, "timestamp_sec": 4.0,
             "ocr_text": "客户难找 回款又慢", "visual": "办公室讲解",
             "people_count": 1, "objects": [], "confidence": 0.96},
        ]
        raw = {
            "summary": "用反差句引出设备租赁痛点",
            "segments": [
                {"role": "hook", "start_sec": 0, "end_sec": 2.2,
                 "evidence": "设备租赁狗都不做"},
                {"role": "pain", "start_sec": 2.3, "end_sec": 5.0,
                 "evidence": "客户难找，回款又慢"},
                {"role": "cta", "start_sec": 7.0, "end_sec": 9.0,
                 "evidence": "关注了解更多"},
            ],
            "catchphrases": ["狗不做，我做"],
            "claims_to_verify": ["设备租赁都不赚钱"],
        }

        report = source_analysis.normalize_structure_report(
            raw, duration_sec=9.0, observations=observations
        )

        self.assertEqual(
            [segment["role"] for segment in report["segments"]],
            ["hook", "pain", "cta"],
        )
        self.assertEqual(report["catchphrases"], ["狗不做，我做"])
        with self.assertRaisesRegex(ValueError, "unsupported story role"):
            source_analysis.normalize_structure_report(
                {**raw, "segments": [{"role": "made_up", "start_sec": 0,
                                       "end_sec": 1, "evidence": "x"}]},
                duration_sec=9.0, observations=[],
            )

    def test_analyze_source_persists_visual_and_structure_results(self):
        frame_results = [
            {"ocr_text": "设备租赁狗都不做", "visual": "女销售面对镜头",
             "people_count": 1, "objects": ["办公桌"], "confidence": 0.98},
            {"ocr_text": "客户难找 回款又慢", "visual": "办公室讲解",
             "people_count": 1, "objects": [], "confidence": 0.95},
            {"ocr_text": "关注了解更多", "visual": "片尾卡",
             "people_count": 0, "objects": [], "confidence": 0.99},
        ]
        structure = {
            "summary": "反差钩子后陈述痛点并转入 CTA",
            "segments": [
                {"role": "hook", "start_sec": 0, "end_sec": 2.2,
                 "evidence": "设备租赁狗都不做"},
                {"role": "cta", "start_sec": 7, "end_sec": 9,
                 "evidence": "关注了解更多"},
            ],
            "catchphrases": ["狗不做，我做"],
            "claims_to_verify": [],
        }
        analyzer = Mock(side_effect=[
            *((item, {"input_tokens": 10, "output_tokens": 5})
              for item in frame_results),
            (structure, {"input_tokens": 40, "output_tokens": 20}),
        ])

        report = source_analysis.analyze_source(
            self.memory_db, self.source.source_id, analyze_json=analyzer
        )

        self.assertEqual(report["summary"], structure["summary"])
        row = self.memory_db.execute(
            "SELECT metadata_json FROM script_sources WHERE source_id=?",
            (self.source.source_id,),
        ).fetchone()
        metadata = __import__("json").loads(row["metadata_json"])
        self.assertEqual(len(metadata["visual_observations"]), 3)
        self.assertEqual(metadata["structure_analysis"]["segments"][0]["role"],
                         "hook")
        self.assertEqual(metadata["analysis_usage"]["input_tokens"], 70)
        self.assertEqual(analyzer.call_count, 4)

        cached_analyzer = Mock(side_effect=AssertionError("must use cache"))
        cached = source_analysis.analyze_source(
            self.memory_db, self.source.source_id,
            analyze_json=cached_analyzer,
        )
        self.assertEqual(cached, report)
        cached_analyzer.assert_not_called()

    def test_force_analysis_recomputes_cached_result(self):
        row = self.memory_db.execute(
            "SELECT metadata_json FROM script_sources WHERE source_id=?",
            (self.source.source_id,),
        ).fetchone()
        metadata = __import__("json").loads(row["metadata_json"])
        metadata["structure_analysis"] = {
            "summary": "old", "segments": [], "catchphrases": [],
            "claims_to_verify": [], "visual_observation_count": 0,
        }
        self.memory_db.execute(
            "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
            (__import__("json").dumps(metadata), self.source.source_id),
        )
        self.memory_db.commit()
        analyzer = Mock(side_effect=[
            *(({"ocr_text": "", "visual": "画面", "people_count": 0,
                "objects": [], "confidence": 1}, {}) for _ in range(3)),
            ({"summary": "new", "segments": [], "catchphrases": [],
              "claims_to_verify": []}, {}),
        ])

        report = source_analysis.analyze_source(
            self.memory_db, self.source.source_id, force=True,
            analyze_json=analyzer,
        )

        self.assertEqual(report["summary"], "new")
        self.assertEqual(analyzer.call_count, 4)

    def test_source_context_includes_structure_but_labels_it_as_machine_analysis(self):
        row = self.memory_db.execute(
            "SELECT metadata_json FROM script_sources WHERE source_id=?",
            (self.source.source_id,),
        ).fetchone()
        metadata = __import__("json").loads(row["metadata_json"])
        metadata["structure_analysis"] = {
            "summary": "反差钩子切入租赁痛点",
            "segments": [{"role": "hook", "start_sec": 0,
                          "end_sec": 2.2, "evidence": "狗都不做"}],
            "catchphrases": ["狗不做，我做"],
            "claims_to_verify": ["设备租赁都不赚钱"],
        }
        self.memory_db.execute(
            "UPDATE script_sources SET metadata_json=? WHERE source_id=?",
            (__import__("json").dumps(metadata, ensure_ascii=False),
             self.source.source_id),
        )
        self.memory_db.commit()
        script_sources.link_source_to_topic(
            self.memory_db, self.source.source_id, "T-ANALYZED"
        )

        context = script_sources.topic_source_context(
            self.memory_db, "T-ANALYZED"
        )

        self.assertIn("机器结构分析（需人工复核）", context)
        self.assertIn("hook 0.000s—2.200s", context)
        self.assertIn("待核验声明", context)

    def test_cli_source_analyze_runs_analysis_for_archived_source(self):
        runner = CliRunner()
        report = {"summary": "完成分析", "segments": []}
        with patch.object(cli_module, "conn", return_value=self.memory_db), \
                patch.object(
                    cli_module.source_analysis, "analyze_source",
                    new=Mock(return_value=report),
                ) as analyze:
            invoked = runner.invoke(
                cli_module.cli,
                ["source-analyze", self.source.source_id, "--force"],
            )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertIn("完成分析", invoked.output)
        analyze.assert_called_once()
        self.assertTrue(analyze.call_args.kwargs["force"])


class TopicBriefTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)
        self.source = script_sources.ingest_text(
            self.memory_db,
            "设备租赁狗都不做。客户难找，回款又慢。狗不做，我做。",
            title="竞品来源",
            source_uri="manual://brief-fixture",
            source_type="video",
            metadata={
                "structure_analysis": {
                    "summary": "反差钩子切入租赁痛点",
                    "segments": [
                        {"role": "hook", "start_sec": 0, "end_sec": 2,
                         "evidence": "设备租赁狗都不做"},
                        {"role": "pain", "start_sec": 2, "end_sec": 5,
                         "evidence": "客户难找，回款又慢"},
                    ],
                    "catchphrases": ["狗不做，我做"],
                    "claims_to_verify": [],
                }
            },
            deduplication_key="brief-fixture",
        )

    def _raw_brief(self):
        return {
            "audience": "设备租赁公司销售负责人",
            "source_score": {
                "relevance": 92,
                "story_potential": 88,
                "evidence_quality": 76,
                "production_fit": 90,
                "reason": "痛点明确且适合真人口播",
            },
            "angles": [
                {
                    "title": "设备租赁不是狗都不做，是你还在靠腿跑",
                    "hook": "狗不做，我做，但别再靠腿做",
                    "promise": "讲清客户与设备信息统一管理的价值",
                    "story_arc": "反差钩子→传统销售痛点→效率反转→行动建议",
                    "evidence": ["客户难找，回款又慢"],
                    "risk_notes": [],
                    "production_notes": "小玲真人母片加设备 B-roll",
                },
                {
                    "title": "设备租赁销售，最贵的不是油费",
                    "hook": "每天跑工地，真正浪费的是信息",
                    "promise": "说明信息分散造成的隐形成本",
                    "story_arc": "成本误区→信息断层→解决方式→金句",
                    "evidence": ["客户难找"],
                    "risk_notes": [],
                    "production_notes": "办公室口播",
                },
                {
                    "title": "为什么你的好设备总遇不到好客户",
                    "hook": "不是没需求，是你看不见需求",
                    "promise": "解释需求匹配的经营价值",
                    "story_arc": "错配现象→根因→匹配机制→CTA",
                    "evidence": ["客户难找"],
                    "risk_notes": ["不得虚构匹配成功率"],
                    "production_notes": "场地 B-roll 加画外音",
                },
            ],
            "recommended_index": 0,
            "recommendation_reason": "钩子继承来源记忆点，但表达方向不同",
        }

    def test_normalizes_three_angles_and_weighted_source_score(self):
        brief = topic_briefs.normalize_brief(self._raw_brief())

        self.assertEqual(len(brief["angles"]), 3)
        self.assertEqual(brief["recommended_index"], 0)
        self.assertEqual(brief["source_score"]["overall"], 86.5)
        self.assertEqual(brief["angles"][0]["evidence"], ["客户难找，回款又慢"])

    def test_brief_fact_ledger_reports_uncovered_must_keep_facts(self):
        raw = self._raw_brief()
        raw["source_digest"] = {
            "facts": [
                {
                    "fact_id": "F1", "statement": "客户信息分散",
                    "quote": "客户难找", "source_id": "SRC-1",
                    "importance": "must_keep",
                },
                {
                    "fact_id": "F2", "statement": "回款周期较长",
                    "quote": "回款又慢", "source_id": "SRC-1",
                    "importance": "must_keep",
                },
            ]
        }
        raw["angles"][0]["fact_ids"] = ["F1"]

        brief = topic_briefs.normalize_brief(
            raw, allowed_source_ids={"SRC-1"}
        )

        self.assertEqual(brief["source_digest"]["coverage"]["must_keep_total"], 2)
        self.assertEqual(brief["source_digest"]["coverage"]["covered"], 1)
        self.assertEqual(
            brief["source_digest"]["coverage"]["missing_fact_ids"], ["F2"]
        )

    def test_brief_fact_ledger_rejects_unknown_source(self):
        raw = self._raw_brief()
        raw["source_digest"] = {
            "facts": [{
                "fact_id": "F1", "statement": "未经证实的事实",
                "quote": "未知来源", "source_id": "SRC-OTHER",
                "importance": "must_keep",
            }]
        }

        with self.assertRaisesRegex(ValueError, "unknown source_id"):
            topic_briefs.normalize_brief(raw, allowed_source_ids={"SRC-1"})

    def test_accepts_available_angles_and_rejects_out_of_range_scores(self):
        raw = self._raw_brief()
        raw["angles"] = raw["angles"][:2]
        brief = topic_briefs.normalize_brief(raw)
        self.assertEqual(len(brief["angles"]), 2)
        raw = self._raw_brief()
        raw["source_score"]["relevance"] = 101
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            topic_briefs.normalize_brief(raw)

    def test_unknown_model_creative_tags_fall_back_to_safe_vocabulary(self):
        raw = self._raw_brief()
        raw["angles"][0]["creative_tags"] = {
            "hook_type": "情绪冲突",
            "narrative_pattern": "观点反转",
            "proof_type": "观点论证",
            "cta_type": "私信咨询",
        }

        brief = topic_briefs.normalize_brief(raw)

        self.assertEqual(brief["angles"][0]["creative_tags"], {
            "hook_type": "pain_point",
            "narrative_pattern": "problem_solution",
            "proof_type": "none",
            "cta_type": "none",
        })

    def test_duplicate_detection_compares_existing_jobs_and_approved_briefs(self):
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title) VALUES(?,?,?)",
            ("J-OLD", "OLD", "设备租赁不是狗都不做，是你还在靠腿跑"),
        )
        self.memory_db.commit()

        duplicate = topic_briefs.find_similar_topic(
            self.memory_db,
            "设备租赁不是狗都不做，而是你还在用腿跑",
            threshold=0.72,
        )
        novel = topic_briefs.find_similar_topic(
            self.memory_db,
            "高空车进场前必须检查哪三项",
            threshold=0.72,
        )

        self.assertEqual(duplicate["kind"], "job")
        self.assertGreaterEqual(duplicate["similarity"], 0.72)
        self.assertIsNone(novel)

    def test_generate_brief_persists_candidate_and_duplicate_warning(self):
        raw = self._raw_brief()
        for index, angle in enumerate(raw["angles"]):
            angle["creative_tags"] = {
                "hook_type": ("contrarian", "pain_point", "question")[index],
                "narrative_pattern": "problem_solution",
                "proof_type": "source_quote",
                "cta_type": "consult",
            }
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title) VALUES(?,?,?)",
            ("J-OLD", "OLD", raw["angles"][0]["title"]),
        )
        self.memory_db.commit()
        generator = Mock(return_value=(
            raw, {"input_tokens": 100, "output_tokens": 50}
        ))

        result = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], generate_json=generator
        )

        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["duplicate_warning"]["kind"], "job")
        row = self.memory_db.execute(
            "SELECT * FROM topic_briefs WHERE brief_id=?", (result["brief_id"],)
        ).fetchone()
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["recommended_title"], raw["angles"][0]["title"])
        self.assertEqual(row["input_tokens"], 100)
        self.assertTrue(result["brief"]["source_digest"]["facts"])
        self.assertEqual(
            result["brief"]["source_digest"]["facts"][0]["source_id"],
            self.source.source_id,
        )
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM topic_brief_sources"
            ).fetchone()[0],
            1,
        )
        tags = self.memory_db.execute(
            "SELECT angle_index, hook_type FROM creative_angle_tags "
            "WHERE brief_id=? ORDER BY angle_index", (result["brief_id"],),
        ).fetchall()
        self.assertEqual(
            [tuple(tag) for tag in tags],
            [(0, "contrarian"), (1, "pain_point"), (2, "question")],
        )

        cached_generator = Mock(side_effect=AssertionError("must reuse brief"))
        cached = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id],
            generate_json=cached_generator,
        )
        self.assertEqual(cached["brief_id"], result["brief_id"])
        cached_generator.assert_not_called()

    def test_generate_brief_keeps_two_usable_angles_without_padding(self):
        incomplete = self._raw_brief()
        incomplete["angles"] = incomplete["angles"][:2]
        generator = Mock(return_value=(
            incomplete, {"input_tokens": 80, "output_tokens": 30}
        ))

        result = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], generate_json=generator
        )

        self.assertEqual(len(result["brief"]["angles"]), 2)
        self.assertEqual(generator.call_count, 1)
        row = self.memory_db.execute(
            "SELECT input_tokens, output_tokens FROM topic_briefs WHERE brief_id=?",
            (result["brief_id"],),
        ).fetchone()
        self.assertEqual(tuple(row), (80, 30))

    def test_generate_brief_accepts_two_strong_angles_without_second_model_call(self):
        available = self._raw_brief()
        available["angles"] = available["angles"][:2]
        available["recommended_index"] = 1
        generator = Mock(return_value=(
            available, {"input_tokens": 80, "output_tokens": 30}
        ))

        result = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], generate_json=generator
        )

        self.assertEqual(generator.call_count, 1)
        self.assertEqual(len(result["brief"]["angles"]), 2)
        self.assertEqual(result["brief"]["recommended_index"], 1)

    def test_long_source_is_stored_and_prompt_compaction_samples_full_span(self):
        content = "HEAD\n" + ("A" * 70000) + "\nMIDDLE\n" + ("B" * 70000) + "\nTAIL"

        normalized = script_sources.normalize_content(content)
        compacted = script_sources.compact_text_for_prompt(normalized, max_chars=1200)

        self.assertGreater(len(normalized), 100000)
        self.assertLessEqual(len(compacted), 1200)
        self.assertIn("HEAD", compacted)
        self.assertIn("MIDDLE", compacted)
        self.assertIn("TAIL", compacted)
        self.assertIn("omitted", compacted.casefold())

    def test_generate_brief_retries_truncated_json_with_larger_output_budget(self):
        calls = []

        def generator(prompt, max_tokens):
            calls.append(max_tokens)
            if len(calls) == 1:
                raise json.JSONDecodeError(
                    "LLM JSON output truncated", '{"angles":[', 11
                )
            return self._raw_brief(), {
                "input_tokens": 90, "output_tokens": 40
            }

        result = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], generate_json=generator
        )

        self.assertEqual(calls, [3000, 6000])
        self.assertEqual(len(result["brief"]["angles"]), 3)

    def test_generate_brief_preserves_available_angles_after_best_effort_repair(self):
        incomplete = self._raw_brief()
        incomplete["angles"] = incomplete["angles"][:1]
        incomplete["recommended_index"] = 0
        generator = Mock(return_value=(incomplete, {}))

        result = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], generate_json=generator
        )

        self.assertEqual(generator.call_count, 1)
        self.assertEqual(len(result["brief"]["angles"]), 1)
        self.assertEqual(
            self.memory_db.execute("SELECT COUNT(*) FROM topic_briefs").fetchone()[0],
            1,
        )

    def test_force_brief_generation_recomputes_existing_source_set(self):
        first_generator = Mock(return_value=(self._raw_brief(), {}))
        first = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id],
            generate_json=first_generator,
        )
        changed = self._raw_brief()
        changed["recommendation_reason"] = "重新评估后的推荐理由"
        forced_generator = Mock(return_value=(changed, {}))

        forced = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id], force=True,
            generate_json=forced_generator,
        )

        self.assertNotEqual(forced["brief_id"], first["brief_id"])
        forced_generator.assert_called_once()

    def test_brief_cache_is_independent_of_source_argument_order(self):
        second = script_sources.ingest_text(
            self.memory_db, "回款慢会拖垮设备租赁现金流",
            title="回款补充素材", deduplication_key="brief-order-fixture",
        )
        first = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id, second.source_id],
            generate_json=Mock(return_value=(self._raw_brief(), {})),
        )
        cached_generator = Mock(side_effect=AssertionError("must reuse brief"))

        cached = topic_briefs.generate_brief(
            self.memory_db, [second.source_id, self.source.source_id],
            generate_json=cached_generator,
        )

        self.assertEqual(cached["brief_id"], first["brief_id"])
        self.assertTrue(cached["cached"])
        cached_generator.assert_not_called()

    def test_database_migrates_legacy_topic_briefs_before_creating_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "factory.db"
            legacy = sqlite3.connect(db_path)
            legacy.execute(
                "CREATE TABLE topic_briefs ("
                "brief_id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "recommended_title TEXT NOT NULL, brief_json TEXT NOT NULL, "
                "duplicate_json TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, approved_topic_id TEXT, "
                "approved_job_id TEXT, created_at TIMESTAMP, "
                "updated_at TIMESTAMP)"
            )
            legacy.commit()
            legacy.close()

            with patch.object(db, "_DB_PATH", db_path):
                migrated = db.conn()
            try:
                columns = {
                    row[1] for row in migrated.execute(
                        "PRAGMA table_info(topic_briefs)"
                    ).fetchall()
                }
                indexes = {
                    row[1] for row in migrated.execute(
                        "PRAGMA index_list(topic_briefs)"
                    ).fetchall()
                }
                self.assertIn("request_fingerprint", columns)
                self.assertIn("idx_topic_briefs_request", indexes)
            finally:
                migrated.close()

    def test_approve_brief_reserves_job_id_without_creating_video_job(self):
        generated = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id],
            generate_json=Mock(return_value=(self._raw_brief(), {})),
        )

        job_id = topic_briefs.approve_brief(
            self.memory_db, generated["brief_id"], topic_id="T-BRIEF-01",
            track="设备租赁", target_audience="租赁公司销售负责人",
        )
        repeated = topic_briefs.approve_brief(
            self.memory_db, generated["brief_id"], topic_id="T-BRIEF-01",
            track="设备租赁", target_audience="租赁公司销售负责人",
        )

        self.assertEqual(job_id, "J-T-BRIEF-01")
        self.assertEqual(repeated, job_id)
        job = self.memory_db.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertIsNone(job)
        brief = self.memory_db.execute(
            "SELECT status,approved_job_id FROM topic_briefs WHERE brief_id=?",
            (generated["brief_id"],),
        ).fetchone()
        self.assertEqual(brief["status"], "approved")
        self.assertIsNone(brief["approved_job_id"])
        self.assertEqual(
            self.memory_db.execute(
                "SELECT COUNT(*) FROM topic_script_sources "
                "WHERE topic_id='T-BRIEF-01'"
            ).fetchone()[0],
            1,
        )

    def test_approve_brief_can_select_a_nonrecommended_angle(self):
        generated = topic_briefs.generate_brief(
            self.memory_db, [self.source.source_id],
            generate_json=Mock(return_value=(self._raw_brief(), {})),
        )

        job_id = topic_briefs.approve_brief(
            self.memory_db, generated["brief_id"], topic_id="T-ANGLE-02",
            angle_index=1,
        )

        stored = self.memory_db.execute(
            "SELECT approved_angle_index,approved_job_id FROM topic_briefs WHERE brief_id=?",
            (generated["brief_id"],),
        ).fetchone()
        self.assertEqual(stored["approved_angle_index"], 1)
        self.assertIsNone(stored["approved_job_id"])
        self.assertIsNone(self.memory_db.execute(
            "SELECT 1 FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone())

    def test_cli_brief_generate_prints_three_angles_without_starting_job(self):
        runner = CliRunner()
        result = {
            "brief_id": "BRIEF-123", "status": "candidate",
            "brief": self._raw_brief(), "duplicate_warning": None,
        }
        with patch.object(cli_module, "conn", return_value=self.memory_db), \
                patch.object(
                    cli_module.topic_briefs, "generate_brief",
                    new=Mock(return_value=result),
                ) as generate:
            invoked = runner.invoke(
                cli_module.cli,
                ["brief-generate", self.source.source_id, "--force"],
            )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertIn("BRIEF-123", invoked.output)
        self.assertIn("为什么你的好设备", invoked.output)
        self.assertEqual(
            self.memory_db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0
        )
        generate.assert_called_once()
        self.assertTrue(generate.call_args.kwargs["force"])

    def test_cli_brief_approve_accepts_one_based_angle(self):
        runner = CliRunner()
        with patch.object(cli_module, "conn", return_value=self.memory_db), \
                patch.object(
                    cli_module.topic_briefs, "approve_brief",
                    new=Mock(return_value="J-T-ANGLE-03"),
                ) as approve:
            invoked = runner.invoke(
                cli_module.cli,
                ["brief-approve", "BRIEF-123", "--topic-id", "T-ANGLE-03",
                 "--angle", "3"],
            )

        self.assertEqual(invoked.exit_code, 0, invoked.output)
        self.assertEqual(approve.call_args.kwargs["angle_index"], 2)


if __name__ == "__main__":
    unittest.main()
