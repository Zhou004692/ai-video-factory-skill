"""Immutable production contract compilation tests."""
from __future__ import annotations

import importlib.util
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import db as factory_db  # noqa: E402
from factory.management_api import ManagementService  # noqa: E402


class ProductionContractTests(unittest.TestCase):
    def test_v2_restores_missing_creative_characters_from_runtime_storyboard(self):
        from factory.production_contract import compile_production_spec

        storyboard = {
            "title": "本地视频高还原剧本",
            "characters": [{
                "id": "女博主",
                "name": "女博主",
                "identity_type": "ai_generated",
                "generation_mode": "ai_generated",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 4,
                "speaker": "女博主",
                "scene": "室内新年主题场景",
                "scene_anchor_key": "new-year-room",
                "cast": ["女博主"],
                "performance_beats": [{
                    "beat": "hook",
                    "speaker": "女博主",
                    "dialogue": "设备租赁，狗都不干！",
                    "duration": 4,
                    "visual_intent": "女博主抬手说话",
                }],
            }],
        }
        stale_creative = {
            "version": 1,
            "title": storyboard["title"],
            "target_duration_sec": 4,
            "characters": [],
            "beats": [{
                "beat_id": "beat-1",
                "kind": "hook",
                "scene": "室内新年主题场景",
                "speakers": ["女博主"],
                "dialogue": "设备租赁，狗都不干！",
                "action": "女博主抬手说话",
                "emotion": "",
                "camera_intent": "中景固定机位",
                "must_preserve": [],
                "may_adapt": [],
                "extensions": {
                    "characters": ["女博主"],
                    "duration_sec": 4,
                },
            }],
            "extensions": {},
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard,"
            "creative_ir_json,creative_ir_version) VALUES(?,?,?,?,?,?,?)",
            (
                "J-V2-STALE-CHARACTERS",
                "T-V2-STALE-CHARACTERS",
                storyboard["title"],
                "STORYBOARDED",
                json.dumps(storyboard, ensure_ascii=False),
                json.dumps(stale_creative, ensure_ascii=False),
                1,
            ),
        )
        self.database.commit()

        result = compile_production_spec(
            self.database,
            "J-V2-STALE-CHARACTERS",
            self.root,
            persist=False,
            target_version=2,
        )

        self.assertTrue(result.payable)
        self.assertIn(
            "creative_characters_restored",
            [issue.code for issue in result.issues],
        )
        self.assertEqual(
            [character["id"] for character in result.spec.characters],
            ["女博主"],
        )

    def test_compile_supports_explicit_v2_without_changing_v1_default(self):
        from factory.production_contract import compile_production_spec

        storyboard = {
            "title": "双人连续场景",
            "characters": [
                {"id": "lin_xia", "name": "林夏", "identity_type": "ai_generated"},
                {"id": "worker", "name": "维修员", "identity_type": "ai_generated"},
            ],
            "beats": [{
                "id": "beat-1",
                "kind": "dialogue",
                "scene": "暴雨抢修现场",
                "speakers": ["lin_xia"],
                "dialogue": "先确认风速，再让设备进场",
                "action": "林夏查看仪表，维修员固定吊篮",
                "characters": ["lin_xia", "worker"],
                "must_preserve": ["完整台词", "双人协作"],
            }],
            "masters": [{
                "idx": 1, "type": "dialogue", "duration": 8,
                "speaker": "lin_xia", "scene": "暴雨抢修现场",
                "scene_anchor_key": "storm-site",
                "cast": ["lin_xia", "worker"],
                "performance_beats": [{
                    "speaker": "lin_xia",
                    "dialogue": "先确认风速，再让设备进场",
                }],
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES(?,?,?,?,?)",
            ("J-V2", "T-V2", "双人连续场景", "STORYBOARDED", json.dumps(storyboard)),
        )
        self.database.commit()

        v1 = compile_production_spec(
            self.database, "J-V2", self.root, persist=False,
        )
        v2 = compile_production_spec(
            self.database, "J-V2", self.root, persist=False, target_version=2,
        )

        self.assertEqual(v1.spec.version, 1)
        self.assertEqual(v2.spec.version, 2)
        self.assertTrue(v2.payable)
        self.assertEqual(v2.spec.masters[0].creative_beat_ids, ("beat-1",))

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(factory_db.DDL)

    def tearDown(self):
        self.database.close()
        self.tempdir.cleanup()

    def test_compile_reports_all_deterministic_issues_in_one_pass(self):
        module_spec = importlib.util.find_spec("factory.production_contract")
        self.assertIsNotNone(
            module_spec,
            "production contract compiler must exist before paid production",
        )
        from factory.production_contract import compile_production_spec

        storyboard = {
            "characters": [{
                "id": "lin_xia",
                "name": "林夏",
                "identity_type": "real_person",
                "generation_mode": "fixed_asset",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 0,
                "speaker": "unknown_worker",
                "performance_beats": [{
                    "speaker": "unknown_worker",
                    "dialogue": "吊篮必须先停稳",
                }],
                "reference_paths": ["references/missing-worker.png"],
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-1", "T-1", "暴雨夜抢修", "STORYBOARDED", json.dumps(storyboard)),
        )
        self.database.commit()

        result = compile_production_spec(self.database, "J-1", self.root)

        self.assertEqual(
            [issue.code for issue in result.issues],
            [
                "character_unmapped",
                "reference_missing",
                "speaker_unknown",
                "duration_invalid",
            ],
        )
        self.assertFalse(result.payable)
        self.assertIsNone(result.spec)
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-1'"
            ).fetchone()[0],
            0,
        )

    def test_compile_persists_stable_immutable_spec_without_mutating_source(self):
        from factory.production_contract import compile_production_spec

        storyboard = {
            "production_settings": {
                "audio_mode": "seedance_native",
                "resolution": "720p",
            },
            "characters": [{
                "id": "lin_xia",
                "name": "林夏",
                "script_character_name": "林夏",
                "identity_type": "ai_generated",
                "generation_mode": "ai_generated",
                "background_description": "年轻的高空设备租赁调度员",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 8,
                "speaker": "lin_xia",
                "scene_anchor_key": "storm-command-room",
                "performance_beats": [{
                    "speaker": "lin_xia",
                    "dialogue": "先确认风速，再让设备进场",
                }],
                "camera": "中景缓慢推进",
            }],
        }
        original = json.dumps(storyboard, ensure_ascii=False, sort_keys=True)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-2", "T-2", "稳定生产契约", "STORYBOARDED", original),
        )
        self.database.execute(
            "INSERT INTO shots(job_id,idx,duration_sec,shot_type,state,cost_cents) "
            "VALUES(?,?,?,?,?,?)",
            ("J-2", 1, 8, "dialogue", "OK", 321),
        )
        self.database.commit()

        first = compile_production_spec(self.database, "J-2", self.root)
        second = compile_production_spec(self.database, "J-2", self.root)

        self.assertTrue(first.payable)
        self.assertEqual(first.issues, ())
        self.assertEqual(first.spec.spec_hash, second.spec.spec_hash)
        self.assertEqual(first.spec.dependency_waves, ((1,),))
        self.assertEqual(first.spec.masters[0].generation_duration_sec, 8)
        with self.assertRaises(FrozenInstanceError):
            first.spec.budget_cents = 1

        row = self.database.execute(
            "SELECT storyboard,production_spec_json,production_spec_hash "
            "FROM jobs WHERE job_id='J-2'"
        ).fetchone()
        self.assertEqual(row["storyboard"], original)
        self.assertEqual(row["production_spec_hash"], first.spec.spec_hash)
        persisted = json.loads(row["production_spec_json"])
        self.assertEqual(persisted["dependency_waves"], [[1]])
        self.assertEqual(persisted["provider_profile"]["video_model"], "doubao-seedance-2-5-260628")
        shot = self.database.execute(
            "SELECT state,cost_cents FROM shots WHERE job_id='J-2' AND idx=1"
        ).fetchone()
        self.assertEqual((shot["state"], shot["cost_cents"]), ("OK", 321))
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-2'"
            ).fetchone()[0],
            0,
        )

    def test_compile_applies_only_safe_repairs_to_the_new_spec(self):
        from factory.production_contract import compile_production_spec

        storyboard = {
            "production_settings": {"audio_mode": "seedance_native"},
            "characters": [{
                "id": "lin_xia",
                "name": "林夏",
                "identity_type": "ai_generated",
                "generation_mode": "ai_generated",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 2.5,
                "speaker": " 林夏 ",
                "performance_beats": [{
                    "speaker": "林夏",
                    "dialogue": "收到，立即执行",
                }],
                "camera": "",
            }],
        }
        original = json.dumps(storyboard, ensure_ascii=False, separators=(",", ":"))
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-3", "T-3", "安全自动修复", "STORYBOARDED", original),
        )
        self.database.commit()

        result = compile_production_spec(self.database, "J-3", self.root)

        self.assertTrue(result.payable)
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["speaker_alias_normalized", "duration_normalized", "camera_defaulted"],
        )
        master = result.spec.masters[0]
        self.assertEqual(master.speaker_id, "lin_xia")
        self.assertEqual(master.editorial_duration_sec, 2.5)
        self.assertEqual(master.generation_duration_sec, 4)
        self.assertEqual(master.camera, "中景，固定机位，保持自然表演")
        self.assertEqual(
            self.database.execute(
                "SELECT storyboard FROM jobs WHERE job_id='J-3'"
            ).fetchone()[0],
            original,
        )

    def test_paid_start_reports_the_complete_contract_before_dispatch(self):
        storyboard = {
            "characters": [{
                "id": "lin_xia",
                "name": "林夏",
                "identity_type": "real_person",
                "generation_mode": "fixed_asset",
            }],
            "masters": [{
                "idx": 1,
                "type": "dialogue",
                "duration": 0,
                "speaker": "unknown_worker",
                "reference_paths": ["references/missing-worker.png"],
                "performance_beats": [{
                    "speaker": "unknown_worker",
                    "dialogue": "先停机再处理",
                }],
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-4", "T-4", "付费前完整校验", "STORYBOARDED", json.dumps(storyboard)),
        )
        self.database.commit()
        service = ManagementService(self.database, self.root)

        with mock.patch.object(service, "dispatch_production_queue") as dispatch:
            with self.assertRaisesRegex(ValueError, "character_unmapped") as caught:
                service.start_video_job("J-4", confirmed=True)

        message = str(caught.exception)
        self.assertIn("reference_missing", message)
        self.assertIn("speaker_unknown", message)
        self.assertIn("duration_invalid", message)
        dispatch.assert_not_called()
        row = self.database.execute(
            "SELECT state,production_spec_hash FROM jobs WHERE job_id='J-4'"
        ).fetchone()
        self.assertEqual(row["state"], "STORYBOARDED")
        self.assertIsNone(row["production_spec_hash"])

    def test_invalid_recompile_clears_a_stale_persisted_spec(self):
        from factory.production_contract import compile_production_spec

        valid = {
            "characters": [],
            "masters": [{
                "idx": 1,
                "type": "broll",
                "duration": 8,
                "camera": "广角固定机位",
            }],
        }
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,storyboard) "
            "VALUES(?,?,?,?,?)",
            ("J-5", "T-5", "规格失效", "STORYBOARDED", json.dumps(valid)),
        )
        self.database.commit()
        compiled = compile_production_spec(self.database, "J-5", self.root)
        self.assertIsNotNone(compiled.spec)

        invalid = {**valid, "masters": [{**valid["masters"][0], "duration": 0}]}
        self.database.execute(
            "UPDATE jobs SET storyboard=? WHERE job_id='J-5'",
            (json.dumps(invalid),),
        )
        self.database.commit()

        result = compile_production_spec(self.database, "J-5", self.root)

        self.assertFalse(result.payable)
        row = self.database.execute(
            "SELECT production_spec_json,production_spec_hash,production_spec_version "
            "FROM jobs WHERE job_id='J-5'"
        ).fetchone()
        self.assertEqual(tuple(row), (None, None, 0))


if __name__ == "__main__":
    unittest.main()
