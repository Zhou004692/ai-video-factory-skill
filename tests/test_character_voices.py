from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import wave
from unittest.mock import AsyncMock, Mock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


from factory import character_voices, db, production_planner, video_routing  # noqa: E402
from factory.clients import bigasr, volc_tts  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402
from factory.steps import s3_shots  # noqa: E402


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    character_voices.ensure_schema(database)
    return database


class CharacterVoiceMatchingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = [
            {
                "VoiceType": "zh_female_sweet_uranus_bigtts",
                "Name": "甜美少女",
                "Gender": "女",
                "Age": "青年",
                "Languages": ["中文"],
                "Categories": ["角色扮演"],
                "NormalLabels": ["甜美", "活泼"],
                "Description": "甜美活泼，适合娱乐内容",
                "TrialURL": "https://example.com/sweet.mp3",
                "ResourceID": "seed-tts-2.0",
            },
            {
                "VoiceType": "zh_female_professional_uranus_bigtts",
                "Name": "知性干练",
                "Gender": "女",
                "Age": "青年",
                "Languages": ["中文"],
                "Categories": ["视频配音", "职场"],
                "NormalLabels": ["专业", "沉稳", "清晰", "有力量"],
                "SpecialLabels": ["纪实", "工程"],
                "Description": "青年女性，专业沉稳，适合工程纪实与应急报道",
                "TrialURL": "https://example.com/professional.mp3",
                "ResourceID": "seed-tts-2.0",
            },
            {
                "VoiceType": "zh_male_professional_uranus_bigtts",
                "Name": "沉稳男声",
                "Gender": "男",
                "Age": "青年",
                "Languages": ["中文"],
                "Categories": ["视频配音"],
                "NormalLabels": ["专业", "沉稳"],
                "Description": "专业工程解说",
                "TrialURL": "https://example.com/male.mp3",
                "ResourceID": "seed-tts-2.0",
            },
        ]

    def test_linxia_profile_selects_professional_young_female_voice(self):
        character = {
            "id": "linxia",
            "name": "林夏",
            "gender": "female",
            "age": 27,
            "background_description": "高空设备租赁公司的项目经理，暴雨抢修现场冷静果断",
            "voice_directive": "青年女性，专业、沉稳、清晰、有力量，不甜腻",
        }

        match = character_voices.match_voice(character, self.catalog)

        self.assertEqual(
            match.voice_type, "zh_female_professional_uranus_bigtts"
        )
        self.assertEqual(match.resource_id, "seed-tts-2.0")
        self.assertIn("性别匹配", match.reasons)
        self.assertGreater(match.score, 0)

    def test_explicit_gender_is_a_hard_filter(self):
        character = {
            "id": "linxia", "gender": "female",
            "voice_directive": "专业沉稳",
        }
        only_male = [self.catalog[-1]]

        with self.assertRaisesRegex(
            character_voices.VoiceMatchError, "没有符合角色性别"
        ):
            character_voices.match_voice(character, only_male)

    def test_match_tie_breaker_is_deterministic(self):
        speakers = [
            {**self.catalog[1], "VoiceType": "voice-z"},
            {**self.catalog[1], "VoiceType": "voice-a"},
        ]
        character = {"id": "host", "gender": "female"}

        first = character_voices.match_voice(character, speakers)
        second = character_voices.match_voice(character, list(reversed(speakers)))

        self.assertEqual(first.voice_type, "voice-a")
        self.assertEqual(first.voice_type, second.voice_type)

    def test_binding_is_reused_and_not_rematched_per_master(self):
        database = _database()
        client = Mock()
        client.list_speakers.return_value = self.catalog
        character = {
            "id": "linxia", "name": "林夏", "gender": "female",
            "voice_directive": "专业沉稳",
        }

        first = character_voices.resolve_voice_binding(
            database, character, client=client
        )
        client.list_speakers.reset_mock()
        second = character_voices.resolve_voice_binding(
            database, {**character, "voice_directive": "甜美"}, client=client
        )

        self.assertEqual(first["voice_type"], second["voice_type"])
        client.list_speakers.assert_not_called()

    def test_missing_catalog_fails_closed_instead_of_random_seedance_voice(self):
        database = _database()
        character = {"id": "linxia", "name": "林夏", "gender": "female"}

        with self.assertRaisesRegex(
            character_voices.VoiceBindingRequiredError, "火山声线"
        ):
            character_voices.resolve_voice_binding(
                database, character, client=None
            )

    def test_manual_voice_override_replaces_auto_match(self):
        database = _database()
        character_voices.save_manual_binding(
            database,
            character_id="linxia",
            display_name="林夏",
            voice_type="zh_female_custom_uranus_bigtts",
            voice_name="自选女声",
            resource_id="seed-tts-2.0",
            preview_url="https://example.com/custom.mp3",
        )

        row = character_voices.get_binding(database, "linxia")

        self.assertEqual(row["voice_type"], "zh_female_custom_uranus_bigtts")
        self.assertEqual(row["source"], "manual")

    def test_pipeline_resolves_only_speaking_roles_and_enriches_characters(self):
        database = _database()
        client = Mock()
        client.list_speakers.return_value = self.catalog
        characters = [
            {
                "id": "linxia", "name": "林夏", "gender": "female",
                "voice_directive": "专业沉稳",
            },
            {"id": "silent_worker", "name": "无台词工人", "gender": "male"},
        ]

        changed = s3_shots._resolve_speaking_voice_bindings(
            database,
            characters,
            [{"type": "dialogue", "speaker": "linxia"}],
            client=client,
        )

        self.assertTrue(changed)
        self.assertEqual(
            characters[0]["voice_type"],
            "zh_female_professional_uranus_bigtts",
        )
        self.assertNotIn("voice_type", characters[1])
        client.list_speakers.assert_called_once()


class VolcSpeakerCatalogTests(unittest.TestCase):
    def test_list_speakers_uses_tts2_catalog_and_normalizes_result(self):
        client = volc_tts.VolcSpeechClient(ak="ak", sk="sk")
        client._call = Mock(return_value={
            "Total": 1,
            "Speakers": [{
                "VoiceType": "zh_female_vv_uranus_bigtts",
                "Name": "Vivi 2.0",
                "Gender": "女",
                "Age": "青年",
                "Languages": ["中文"],
                "TrialURL": "https://example.com/vivi.mp3",
                "ResourceID": "seed-tts-2.0",
            }],
        })

        speakers = client.list_speakers()

        client._call.assert_called_once_with("ListSpeakers", {
            "ResourceIDs": ["seed-tts-2.0"], "Page": 1, "Limit": 100,
        })
        self.assertEqual(speakers[0]["VoiceType"], "zh_female_vv_uranus_bigtts")


class CharacterVoiceManagementTests(unittest.TestCase):
    def test_character_library_exposes_and_can_auto_match_voice(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ManagementService(database, Path(temp_dir))
            service.create_custom_character(
                character_id="linxia", display_name="林夏", gender="female",
                background_description="青年女性项目经理，专业、沉稳、清晰",
            )
            client = Mock()
            client.list_speakers.return_value = [{
                "VoiceType": "voice-linxia", "Name": "知性干练",
                "Gender": "女", "Age": "青年", "Languages": ["中文"],
                "NormalLabels": ["专业", "沉稳", "清晰"],
                "TrialURL": "https://example.com/linxia.mp3",
                "ResourceID": "seed-tts-2.0",
            }]

            with patch.object(
                service, "_volc_speech_client", return_value=client
            ):
                matched = service.auto_match_character_voice("linxia")
            listed = service.characters()["characters"][0]

            self.assertEqual(matched["voice_type"], "voice-linxia")
            self.assertEqual(
                listed["voice_binding"]["voice_name"], "知性干练"
            )
            self.assertEqual(listed["voice_binding"]["source"], "auto")


class VolcTTSClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_uses_fixed_voice_and_parses_concatenated_json(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            stream = (
                '{"code":0,"data":"YWJj","sequence":1}'
                '{"code":20000000,"data":"ZA==","sequence":-1,'
                '"addition":{"duration":1234,"timestamps":[{"text":"你好",'
                '"start_time":0,"end_time":1234}]}}'
            )
            return httpx.Response(200, text=stream)

        result = await volc_tts.synthesize(
            text="你好",
            voice_type="zh_female_vv_uranus_bigtts",
            app_id="app-id",
            api_key="api-key",
            resource_id="seed-tts-2.0",
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(result.audio, b"abcd")
        self.assertEqual(result.duration_ms, 1234)
        self.assertEqual(result.timestamps[0]["text"], "你好")
        self.assertEqual(captured["headers"]["x-api-key"], "api-key")
        self.assertEqual(
            captured["headers"]["x-api-resource-id"], "seed-tts-2.0"
        )
        self.assertEqual(
            captured["body"]["req_params"]["speaker"],
            "zh_female_vv_uranus_bigtts",
        )
        self.assertTrue(captured["body"]["req_params"]["with_timestamp"])

    async def test_missing_credentials_fails_before_http_request(self):
        transport = AsyncMock()
        with self.assertRaisesRegex(volc_tts.VolcTTSConfigError, "VOLC_TTS"):
            await volc_tts.synthesize(
                text="你好", voice_type="voice", app_id="", api_key="",
                transport=transport,
            )
        transport.handle_async_request.assert_not_called()


class FixedVoiceVideoRoutingTests(unittest.TestCase):
    def test_dialogue_defaults_to_seedance_native_voice(self):
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"narrative_type": "dialogue"},
            env={},
        )

        self.assertTrue(route.generate_audio)
        self.assertEqual(
            video_routing.normalize_audio_mode(None), "seedance_native"
        )

    def test_dialogue_route_disables_seedance_native_voice(self):
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"narrative_type": "dialogue", "audio_mode": "fixed_tts"},
        )
        self.assertFalse(route.generate_audio)

    def test_dialogue_route_can_use_seedance_native_voice(self):
        route = video_routing.select_route(
            {"narrative_type": "dialogue"},
            {"narrative_type": "dialogue", "audio_mode": "seedance_native"},
        )
        self.assertTrue(route.generate_audio)

    def test_broll_keeps_seedance_ambient_audio_in_both_voice_modes(self):
        for mode in ("fixed_tts", "seedance_native"):
            with self.subTest(mode=mode):
                route = video_routing.select_route(
                    {"narrative_type": "broll"}, {"audio_mode": mode}
                )
                self.assertTrue(route.generate_audio)

    def test_multi_speaker_master_builds_one_fixed_voice_segment_per_beat(self):
        shot = {
            "speaker": "linxia",
            "voiceover": "先停机。现在复检。",
            "duration_sec": 8,
            "performance_beats": [
                {
                    "speaker": "linxia", "start": 0, "duration": 3,
                    "dialogue": "先停机。",
                },
                {
                    "speaker": "engineer", "start": 4, "duration": 3,
                    "dialogue": "现在复检。",
                },
            ],
        }
        characters = {
            "linxia": {"id": "linxia", "voice_type": "voice-linxia"},
            "engineer": {"id": "engineer", "voice_type": "voice-engineer"},
        }

        segments = s3_shots._fixed_voice_segments(shot, characters)

        self.assertEqual(
            [(item.speaker_id, item.voice_type, item.start_ms, item.text)
             for item in segments],
            [
                ("linxia", "voice-linxia", 0, "先停机。"),
                ("engineer", "voice-engineer", 4000, "现在复检。"),
            ],
        )

    def test_missing_role_voice_fails_before_seedance(self):
        with self.assertRaisesRegex(
            character_voices.VoiceBindingRequiredError, "林夏.*固定声线"
        ):
            s3_shots._fixed_voice_segments(
                {
                    "speaker": "linxia", "voiceover": "先停机。",
                    "duration_sec": 4,
                },
                {"linxia": {"id": "linxia", "name": "林夏"}},
            )

    def test_dialogue_request_uses_fixed_voice_as_reference_audio(self):
        request = s3_shots._dialogue_request_kwargs(
            {"duration_sec": 8}, None,
            fixed_voice_audio=Path("C:/voice/linxia.wav"),
        )

        self.assertEqual(
            request["reference_audios"], ["C:\\voice\\linxia.wav"]
        )

    def test_preflight_exposes_bound_and_auto_matched_role_voice_plan(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)
        character_voices.save_manual_binding(
            database, character_id="linxia", display_name="林夏",
            voice_type="voice-linxia", voice_name="知性干练",
        )

        report = production_planner.analyze_storyboard(database, {
            "production_settings": {"audio_mode": "fixed_tts"},
            "characters": [
                {"id": "linxia", "name": "林夏", "gender": "female"},
                {"id": "engineer", "name": "工程师", "gender": "male"},
            ],
            "masters": [{
                "idx": 1, "type": "dialogue", "duration": 8,
                "performance_beats": [
                    {"speaker": "linxia", "dialogue": "先停机"},
                    {"speaker": "engineer", "dialogue": "马上复检"},
                ],
            }],
        })

        self.assertEqual(report["estimated_tts_calls"], 2)
        self.assertEqual(report["voice_plan"]["bound_role_count"], 1)
        self.assertEqual(report["voice_plan"]["auto_match_role_count"], 1)
        self.assertEqual(
            report["voice_plan"]["roles"][0]["voice_type"], "voice-linxia"
        )

    def test_seedance_native_preflight_has_no_tts_calls_or_bindings(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(db.DDL)

        report = production_planner.analyze_storyboard(database, {
            "production_settings": {"audio_mode": "seedance_native"},
            "characters": [{"id": "linxia", "name": "林夏"}],
            "masters": [{
                "idx": 1, "type": "dialogue", "duration": 4,
                "speaker": "linxia", "voiceover": "先停机",
            }],
        })

        self.assertEqual(report["voice_plan"]["mode"], "seedance_native")
        self.assertEqual(report["estimated_tts_calls"], 0)
        self.assertEqual(report["voice_plan"]["roles"], [])


class FixedVoiceGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesizes_each_role_voice_and_uses_tts_timestamps(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY,state TEXT DEFAULT 'STORYBOARDED',cost_cents INTEGER DEFAULT 0);"
            "CREATE TABLE shots(job_id TEXT,idx INTEGER,cost_cents INTEGER DEFAULT 0);"
            "CREATE TABLE provider_calls(task_id TEXT PRIMARY KEY,job_id TEXT,"
            "shot_idx INTEGER,provider TEXT,intent TEXT,usage_json TEXT,"
            "cost_cents INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO jobs(job_id) VALUES('J-VOICE');"
            "INSERT INTO shots(job_id,idx) VALUES('J-VOICE',1);"
        )
        results = [
            volc_tts.TTSResult(
                b"a", 900,
                ({"text": "先停机", "start_time": 0, "end_time": 900},),
                "tts-a", {},
            ),
            volc_tts.TTSResult(
                b"b", 800,
                ({"text": "现在复检", "start_time": 0, "end_time": 800},),
                "tts-b", {},
            ),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            s3_shots, "ASSETS_DIR", Path(temp_dir)
        ), patch.object(
            s3_shots.volc_tts, "synthesize",
            new=AsyncMock(side_effect=results),
        ) as synthesize, patch.object(
            s3_shots.ffmpeg, "assemble_timed_audio", new=AsyncMock()
        ) as assemble:
            track = await s3_shots._synthesize_fixed_voice_track(
                database,
                job_id="J-VOICE",
                shot={
                    "idx": 1, "speaker": "linxia", "duration_sec": 6,
                    "performance_beats": [
                        {"speaker": "linxia", "start": 0, "duration": 2,
                         "dialogue": "先停机"},
                        {"speaker": "engineer", "start": 3, "duration": 2,
                         "dialogue": "现在复检"},
                    ],
                },
                characters={
                    "linxia": {"id": "linxia", "voice_type": "voice-a"},
                    "engineer": {"id": "engineer", "voice_type": "voice-b"},
                },
                paid_batch_id="batch-voice",
            )

            self.assertEqual(
                [call.kwargs["voice_type"] for call in synthesize.await_args_list],
                ["voice-a", "voice-b"],
            )
            assemble.assert_awaited_once()
            self.assertEqual(track.transcription.text, "先停机现在复检")
            self.assertEqual(
                [(u.text, u.start_ms, u.end_ms)
                 for u in track.transcription.utterances],
                [("先停机", 0, 900), ("现在复检", 3000, 3800)],
            )
            rows = database.execute(
                "SELECT provider,usage_json FROM provider_calls ORDER BY rowid"
            ).fetchall()
            self.assertEqual([row["provider"] for row in rows], [
                "volc_tts", "volc_tts"
            ])
            self.assertTrue(all(
                json.loads(row["usage_json"])["status"] == "completed"
                for row in rows
            ))

    async def test_mix_failure_reuses_paid_role_clips_without_reposting_tts(self):
        database = sqlite3.connect(":memory:")
        database.row_factory = sqlite3.Row
        database.executescript(
            "CREATE TABLE jobs(job_id TEXT PRIMARY KEY,state TEXT DEFAULT 'STORYBOARDED',cost_cents INTEGER DEFAULT 0);"
            "CREATE TABLE shots(job_id TEXT,idx INTEGER,cost_cents INTEGER DEFAULT 0);"
            "CREATE TABLE provider_calls(task_id TEXT PRIMARY KEY,job_id TEXT,"
            "shot_idx INTEGER,provider TEXT,intent TEXT,usage_json TEXT,"
            "cost_cents INTEGER,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
            "INSERT INTO jobs(job_id) VALUES('J-VOICE-REUSE');"
            "INSERT INTO shots(job_id,idx) VALUES('J-VOICE-REUSE',1);"
        )
        result = volc_tts.TTSResult(
            b"voice", 800,
            ({"text": "先停机", "start_time": 0, "end_time": 800},),
            "tts-once", {},
        )
        shot = {
            "idx": 1, "speaker": "linxia", "duration_sec": 3,
            "voiceover": "先停机",
        }
        characters = {
            "linxia": {"id": "linxia", "voice_type": "voice-linxia"},
        }
        async def finish_mix(_clips, output, *, duration_sec):
            output.write_bytes(b"mixed")

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            s3_shots, "ASSETS_DIR", Path(temp_dir)
        ), patch.object(
            s3_shots.volc_tts, "synthesize", new=AsyncMock(return_value=result)
        ) as synthesize, patch.object(
            s3_shots.ffmpeg, "assemble_timed_audio",
            new=AsyncMock(side_effect=[RuntimeError("mix failed"), None]),
        ):
            with self.assertRaisesRegex(RuntimeError, "mix failed"):
                await s3_shots._synthesize_fixed_voice_track(
                    database, job_id="J-VOICE-REUSE", shot=shot,
                    characters=characters, paid_batch_id="batch-voice",
                )
            # The second attempt must consume the durable segment artifact,
            # not submit another paid TTS request.
            track = await s3_shots._synthesize_fixed_voice_track(
                database, job_id="J-VOICE-REUSE", shot=shot,
                characters=characters, paid_batch_id="batch-voice",
            )

            self.assertEqual(synthesize.await_count, 1)
            self.assertEqual(track.transcription.text, "先停机")
            self.assertEqual(
                database.execute(
                    "SELECT cost_cents FROM jobs WHERE job_id='J-VOICE-REUSE'"
                ).fetchone()[0],
                volc_tts.estimate_cost_cents("先停机"),
            )

    async def test_seedance_visual_is_muxed_with_exact_fixed_voice_track(self):
        transcription = bigasr.Transcription(
            "先停机。",
            speech_start_ms=0,
            speech_end_ms=1200,
            utterances=(bigasr.TimedUtterance("先停机。", 0, 1200),),
        )
        metadata = {
            "task_id": "task-fixed-voice",
            "usage": {"cost_cents": 7},
        }

        async def fake_video(**kwargs):
            kwargs["metadata_out"].update(metadata)
            return b"visual-only"

        async def fake_mux(_video, _audio, out_path):
            out_path.write_bytes(b"visual-with-fixed-voice")

        with self.subTest("fixed voice replaces provider audio"):
            with patch.object(
                s3_shots.seedance, "text_to_video", side_effect=fake_video
            ) as video_call, patch.object(
                s3_shots.tos, "upload", return_value="https://cdn/final.mp4"
            ) as upload, patch.object(
                s3_shots.ffmpeg, "mux_video_audio",
                new=AsyncMock(side_effect=fake_mux),
            ) as mux, patch.object(
                s3_shots.video_qa, "analyze", return_value=({}, None)
            ):
                output = ROOT / "artifacts" / "fixed-voice-test.mp4"
                voice = ROOT / "artifacts" / "fixed-voice-test.wav"
                voice.parent.mkdir(parents=True, exist_ok=True)
                # A real 1.2s PCM silence fixture lets the production preflight
                # probe duration. The transcript above is authored test data,
                # not speech recognized from this synthetic audio.
                with wave.open(str(voice), "wb") as audio_fixture:
                    audio_fixture.setnchannels(1)
                    audio_fixture.setsampwidth(2)
                    audio_fixture.setframerate(16000)
                    audio_fixture.writeframes(b"\x00\x00" * 19200)
                try:
                    result = await s3_shots._generate_seedance_video(
                        prompt="visual",
                        route=video_routing.VideoRoute(
                            model="seedance", tier="seedance25",
                            generate_audio=False, qa_required=False,
                        ),
                        request_kwargs={
                            "duration": 4, "resolution": "720p",
                            "ratio": "9:16", "reference_audios": [str(voice)],
                        },
                        out_path=output,
                        key_hint="fixed-voice-test",
                        fixed_audio_path=voice,
                        fixed_transcription=transcription,
                    )

                    self.assertFalse(
                        video_call.call_args.kwargs["generate_audio"]
                    )
                    mux.assert_awaited_once()
                    upload.assert_called_once()
                    self.assertEqual(result.audio_path, voice)
                    self.assertEqual(result.transcription, transcription)
                finally:
                    output.unlink(missing_ok=True)
                    voice.unlink(missing_ok=True)
