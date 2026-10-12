"""Pre-registered, single-variable creative experiment tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from factory import (  # noqa: E402
    creative_experiments,
    creative_learning,
    db,
    performance_feedback,
)
import cli as cli_module  # noqa: E402


class CreativeExperimentTests(unittest.TestCase):
    def setUp(self):
        self.memory_db = sqlite3.connect(":memory:")
        self.memory_db.row_factory = sqlite3.Row
        self.memory_db.executescript(db.DDL)

    def tearDown(self):
        self.memory_db.close()

    def _add_job(
        self, suffix: str, *, hook_type: str, audience: str = "设备租赁老板",
        narrative_pattern: str = "problem_solution",
        published: bool = True,
    ) -> str:
        job_id = f"J-EXP-{suffix}"
        brief_id = f"BRIEF-EXP-{suffix}"
        storyboard = {
            "production_profile": "standard",
            "production_mode": "native_master_local_director",
        }
        self.memory_db.execute(
            "INSERT INTO jobs(job_id, topic_id, title, target_audience, state, "
            "storyboard) VALUES(?,?,?,?,?,?)",
            (
                job_id, f"T-EXP-{suffix}", f"实验视频{suffix}", audience,
                "PUBLISHED" if published else "PENDING",
                json.dumps(storyboard, ensure_ascii=False),
            ),
        )
        brief = {
            "source_score": {"overall": 80}, "recommended_index": 0,
            "angles": [
                {"title": f"实验视频{suffix}"},
                {"title": "备选二"}, {"title": "备选三"},
            ],
        }
        self.memory_db.execute(
            "INSERT INTO topic_briefs(brief_id,status,recommended_title,brief_json,"
            "approved_topic_id,approved_job_id,approved_angle_index) "
            "VALUES(?,'approved',?,?,?,?,0)",
            (
                brief_id, f"实验视频{suffix}",
                json.dumps(brief, ensure_ascii=False), f"T-EXP-{suffix}", job_id,
            ),
        )
        self.memory_db.commit()
        creative_learning.tag_angle(
            self.memory_db, brief_id, angle_index=0,
            hook_type=hook_type, narrative_pattern=narrative_pattern,
            proof_type="source_quote", cta_type="consult",
        )
        # Positive experimental observations require captured production intent.
        frozen = {**storyboard,"script_metadata":{"title":job_id,"creative_tags":{
            "hook_type":hook_type,"narrative_pattern":narrative_pattern,
            "proof_type":"source_quote","cta_type":"consult"}}}
        self.memory_db.execute("UPDATE jobs SET production_spec_json=? WHERE job_id=?",
            (json.dumps({"runtime_storyboard":frozen}),job_id))
        self.memory_db.commit()
        if published:
            performance_feedback.register_publication(
                self.memory_db, job_id, platform="douyin",
                item_id=f"ITEM-EXP-{suffix}",
                published_at="2026-08-12T00:00:00Z",
            )
        return job_id

    def _create_experiment(self, **overrides) -> str:
        values = {
            "name": "痛点钩子对反常识钩子",
            "platform": "douyin", "window_hours": 24,
            "variable_dimension": "hook_type",
            "control_value": "pain_point", "treatment_value": "contrarian",
            "primary_metric": "completion_rate",
            "hypothesis": "反常识钩子提升完播率",
            "audience": "设备租赁老板", "production_profile": "standard",
            "min_views": 100, "min_jobs_per_arm": 2,
            "planned_jobs_per_arm": 2,
            "minimum_effect": 0.05,
        }
        values.update(overrides)
        return creative_experiments.create_experiment(self.memory_db, **values)

    def test_contract_is_idempotent_and_validates_controlled_values(self):
        experiment_id = self._create_experiment()
        repeated = self._create_experiment()

        self.assertEqual(experiment_id, repeated)
        row = self.memory_db.execute(
            "SELECT * FROM creative_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        self.assertEqual(row["status"], "draft")
        self.assertEqual(row["primary_metric"], "completion_rate")
        self.assertEqual(row["planned_jobs_per_arm"], 2)
        with self.assertRaisesRegex(ValueError, "must be different"):
            self._create_experiment(treatment_value="pain_point")
        with self.assertRaisesRegex(ValueError, "primary_metric"):
            self._create_experiment(primary_metric="likes")
        with self.assertRaisesRegex(ValueError, "planned_jobs_per_arm"):
            self._create_experiment(
                min_jobs_per_arm=3, planned_jobs_per_arm=2,
            )

    def test_assignment_is_before_results_and_only_one_variable_changes(self):
        experiment_id = self._create_experiment()
        control = self._add_job("CONTROL", hook_type="pain_point")
        treatment = self._add_job("TREAT", hook_type="contrarian")
        creative_experiments.assign_job(
            self.memory_db, experiment_id, control, arm="control",
        )
        creative_experiments.assign_job(
            self.memory_db, experiment_id, treatment, arm="treatment",
        )

        rows = self.memory_db.execute(
            "SELECT job_id, arm FROM creative_experiment_jobs "
            "WHERE experiment_id=? ORDER BY arm", (experiment_id,),
        ).fetchall()
        self.assertEqual([(row["job_id"], row["arm"]) for row in rows], [
            (control, "control"), (treatment, "treatment"),
        ])
        fixed = json.loads(self.memory_db.execute(
            "SELECT fixed_tags_json FROM creative_experiments "
            "WHERE experiment_id=?", (experiment_id,),
        ).fetchone()[0])
        self.assertEqual(fixed["proof_type"], "source_quote")

        wrong_context = self._add_job(
            "CONFOUND", hook_type="contrarian",
            narrative_pattern="misconception_correction",
        )
        with self.assertRaisesRegex(ValueError, "fixed creative tags"):
            creative_experiments.assign_job(
                self.memory_db, experiment_id, wrong_context, arm="treatment",
            )

        late = self._add_job("LATE", hook_type="pain_point")
        performance_feedback.record_snapshot(
            self.memory_db, late, platform="douyin",
            captured_at="2026-08-12T22:00:00Z", views=200,
            completion_rate=0.6,
        )
        with self.assertRaisesRegex(ValueError, "before performance"):
            creative_experiments.assign_job(
                self.memory_db, experiment_id, late, arm="control",
            )

    def test_assignment_requires_expected_tag_audience_and_production_profile(self):
        experiment_id = self._create_experiment()
        wrong_arm = self._add_job("WRONG-ARM", hook_type="contrarian")
        with self.assertRaisesRegex(ValueError, "expects hook_type=pain_point"):
            creative_experiments.assign_job(
                self.memory_db, experiment_id, wrong_arm, arm="control",
            )
        wrong_audience = self._add_job(
            "WRONG-AUDIENCE", hook_type="pain_point", audience="普通消费者",
        )
        with self.assertRaisesRegex(ValueError, "audience"):
            creative_experiments.assign_job(
                self.memory_db, experiment_id, wrong_audience, arm="control",
            )

    def test_assignment_can_be_locked_before_publication(self):
        experiment_id = self._create_experiment()
        job_id = self._add_job(
            "PRE-PUBLISH", hook_type="pain_point", published=False,
        )

        creative_experiments.assign_job(
            self.memory_db, experiment_id, job_id, arm="control",
        )

        row = self.memory_db.execute(
            "SELECT arm FROM creative_experiment_jobs WHERE experiment_id=? "
            "AND job_id=?", (experiment_id, job_id),
        ).fetchone()
        self.assertEqual(row["arm"], "control")

    def test_system_allocates_balanced_idempotent_preproduction_slots(self):
        experiment_id = self._create_experiment()

        first = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-001",
        )
        repeated = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-001",
        )
        second = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-002",
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first["arm"], second["arm"])
        self.assertEqual(
            {first["creative_value"], second["creative_value"]},
            {"pain_point", "contrarian"},
        )
        self.assertEqual(first["allocation_method"], "system_balanced")
        self.assertTrue(first["allocation_hash"])

    def test_system_stops_allocating_after_preregistered_arm_quotas(self):
        experiment_id = self._create_experiment(
            min_jobs_per_arm=1, planned_jobs_per_arm=1,
        )
        first = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="QUOTA-1",
        )
        second = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="QUOTA-2",
        )

        self.assertNotEqual(first["arm"], second["arm"])
        with self.assertRaisesRegex(ValueError, "planned slot quota"):
            creative_experiments.allocate_slot(
                self.memory_db, experiment_id, subject_key="QUOTA-3",
            )
        repeated = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="QUOTA-1",
        )
        self.assertEqual(repeated["slot_id"], first["slot_id"])

    def test_manual_assignment_cannot_bypass_preregistered_arm_quota(self):
        experiment_id = self._create_experiment(
            min_jobs_per_arm=1, planned_jobs_per_arm=1,
        )
        first = self._add_job(
            "MANUAL-QUOTA-1", hook_type="pain_point", published=False,
        )
        second = self._add_job(
            "MANUAL-QUOTA-2", hook_type="pain_point", published=False,
        )
        creative_experiments.assign_job(
            self.memory_db, experiment_id, first, arm="control",
        )

        with self.assertRaisesRegex(ValueError, "planned arm quota"):
            creative_experiments.assign_job(
                self.memory_db, experiment_id, second, arm="control",
            )

    def test_system_slot_binds_only_the_preassigned_creative_value(self):
        experiment_id = self._create_experiment()
        slot = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-BIND",
        )
        correct = self._add_job(
            "SYSTEM-CORRECT", hook_type=slot["creative_value"],
            published=False,
        )

        creative_experiments.bind_slot(
            self.memory_db, slot["slot_id"], correct,
        )

        assignment = self.memory_db.execute(
            "SELECT arm,assignment_method,allocation_token "
            "FROM creative_experiment_jobs WHERE experiment_id=? AND job_id=?",
            (experiment_id, correct),
        ).fetchone()
        self.assertEqual(assignment["arm"], slot["arm"])
        self.assertEqual(assignment["assignment_method"], "system_balanced")
        self.assertEqual(assignment["allocation_token"], slot["slot_id"])

        another = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-WRONG",
        )
        wrong_value = (
            "contrarian" if another["creative_value"] == "pain_point"
            else "pain_point"
        )
        wrong = self._add_job(
            "SYSTEM-WRONG", hook_type=wrong_value, published=False,
        )
        with self.assertRaisesRegex(ValueError, "expects hook_type"):
            creative_experiments.bind_slot(
                self.memory_db, another["slot_id"], wrong,
            )

    def test_system_slot_rejects_a_job_that_is_already_published(self):
        experiment_id = self._create_experiment()
        slot = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="CONTENT-PUBLISHED",
        )
        published = self._add_job(
            "ALREADY-PUBLISHED", hook_type=slot["creative_value"],
            published=True,
        )

        with self.assertRaisesRegex(ValueError, "before publication"):
            creative_experiments.bind_slot(
                self.memory_db, slot["slot_id"], published,
            )

    def test_report_uses_preregistered_window_metric_and_minimum_effect(self):
        experiment_id = self._create_experiment()
        cases = (
            ("C1", "control", "pain_point", 200, 0.5),
            ("C2", "control", "pain_point", 300, 0.6),
            ("T1", "treatment", "contrarian", 200, 0.7),
            ("T2", "treatment", "contrarian", 300, 0.8),
        )
        for suffix, arm, hook, views, completion in cases:
            job_id = self._add_job(suffix, hook_type=hook)
            creative_experiments.assign_job(
                self.memory_db, experiment_id, job_id, arm=arm,
            )
            performance_feedback.record_snapshot(
                self.memory_db, job_id, platform="douyin",
                captured_at="2026-08-12T22:00:00Z", views=views,
                completion_rate=completion, likes=20,
            )

        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )

        self.assertEqual(report["assessment"], "treatment_candidate")
        self.assertEqual(report["primary_metric"], "completion_rate")
        self.assertEqual(report["arms"]["control"]["sample_count"], 2)
        self.assertEqual(report["arms"]["treatment"]["sample_count"], 2)
        self.assertAlmostEqual(
            report["arms"]["control"]["completion_rate"], 0.56,
        )
        self.assertAlmostEqual(
            report["arms"]["treatment"]["completion_rate"], 0.76,
        )
        self.assertAlmostEqual(report["absolute_delta"], 0.2)
        self.assertAlmostEqual(report["relative_lift"], 0.2 / 0.56)
        self.assertIn("directional", report["evidence_policy"])

    def test_report_refuses_a_decision_until_both_arms_are_mature(self):
        experiment_id = self._create_experiment()
        job_id = self._add_job("ONLY-CONTROL", hook_type="pain_point")
        creative_experiments.assign_job(
            self.memory_db, experiment_id, job_id, arm="control",
        )
        performance_feedback.record_snapshot(
            self.memory_db, job_id, platform="douyin",
            captured_at="2026-08-12T10:00:00Z", views=200,
            completion_rate=0.8,
        )

        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )

        self.assertEqual(report["assessment"], "insufficient_data")
        self.assertIsNone(report["absolute_delta"])
        self.assertTrue(report["excluded"])

    def test_report_audits_system_allocation_and_downgrades_manual_assignment(self):
        experiment_id = self._create_experiment(min_jobs_per_arm=1)
        for subject in ("AUDIT-1", "AUDIT-2"):
            slot = creative_experiments.allocate_slot(
                self.memory_db, experiment_id, subject_key=subject,
            )
            job_id = self._add_job(
                subject, hook_type=slot["creative_value"], published=False,
            )
            creative_experiments.bind_slot(
                self.memory_db, slot["slot_id"], job_id,
            )
        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )
        self.assertEqual(
            report["allocation_audit"]["evidence_grade"],
            "controlled_directional",
        )
        self.assertEqual(report["allocation_audit"]["system_allocated"], 2)
        self.assertEqual(report["allocation_audit"]["manual"], 0)

        manual_experiment = self._create_experiment(name="人工分组实验")
        manual_job = self._add_job(
            "MANUAL-AUDIT", hook_type="pain_point", published=False,
        )
        creative_experiments.assign_job(
            self.memory_db, manual_experiment, manual_job, arm="control",
        )
        manual_report = creative_experiments.experiment_report(
            self.memory_db, manual_experiment,
        )
        self.assertEqual(
            manual_report["allocation_audit"]["evidence_grade"],
            "observational_directional",
        )

    def _publish_with_snapshot(
        self, job_id: str, suffix: str, *, views: int, completion_rate: float,
        captured_at: str = "2026-08-12T22:00:00Z",
    ) -> None:
        self.memory_db.execute(
            "UPDATE jobs SET state='PUBLISHED' WHERE job_id=?", (job_id,),
        )
        self.memory_db.commit()
        performance_feedback.register_publication(
            self.memory_db, job_id, platform="douyin",
            item_id=f"ITEM-COHORT-{suffix}",
            published_at="2026-08-12T00:00:00Z",
        )
        performance_feedback.record_snapshot(
            self.memory_db, job_id, platform="douyin",
            captured_at=captured_at, views=views,
            completion_rate=completion_rate,
        )

    def test_unresolved_system_slot_pauses_an_otherwise_eligible_result(self):
        experiment_id = self._create_experiment(
            min_jobs_per_arm=1, planned_jobs_per_arm=2,
        )
        for subject, completion in (("FULL-1", 0.5), ("FULL-2", 0.8)):
            slot = creative_experiments.allocate_slot(
                self.memory_db, experiment_id, subject_key=subject,
            )
            job_id = self._add_job(
                subject, hook_type=slot["creative_value"], published=False,
            )
            creative_experiments.bind_slot(
                self.memory_db, slot["slot_id"], job_id,
            )
            self._publish_with_snapshot(
                job_id, subject, views=300, completion_rate=completion,
            )
        pending = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="FULL-UNBOUND",
        )

        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )

        self.assertEqual(report["assessment"], "cohort_incomplete")
        self.assertIn(
            report["directional_assessment"],
            {"treatment_candidate", "control_candidate"},
        )
        self.assertFalse(report["cohort_audit"]["complete"])
        self.assertEqual(report["cohort_audit"]["issued_slots"], 3)
        attrition = {item["slot_id"]: item for item in report["cohort_audit"]["attrition"]}
        self.assertEqual(attrition[pending["slot_id"]]["reason"], "slot_unbound")

    def test_cohort_audit_classifies_every_missing_stage(self):
        experiment_id = self._create_experiment(
            min_jobs_per_arm=1, planned_jobs_per_arm=3,
        )

        unbound = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="DROP-UNBOUND",
        )

        unpublished = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="DROP-UNPUBLISHED",
        )
        unpublished_job = self._add_job(
            "DROP-UNPUBLISHED", hook_type=unpublished["creative_value"],
            published=False,
        )
        creative_experiments.bind_slot(
            self.memory_db, unpublished["slot_id"], unpublished_job,
        )

        no_snapshot = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="DROP-NO-SNAPSHOT",
        )
        no_snapshot_job = self._add_job(
            "DROP-NO-SNAPSHOT", hook_type=no_snapshot["creative_value"],
            published=False,
        )
        creative_experiments.bind_slot(
            self.memory_db, no_snapshot["slot_id"], no_snapshot_job,
        )
        self.memory_db.execute(
            "UPDATE jobs SET state='PUBLISHED' WHERE job_id=?", (no_snapshot_job,),
        )
        self.memory_db.commit()
        performance_feedback.register_publication(
            self.memory_db, no_snapshot_job, platform="douyin",
            item_id="ITEM-DROP-NO-SNAPSHOT",
            published_at="2026-08-12T00:00:00Z",
        )

        immature = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="DROP-IMMATURE",
        )
        immature_job = self._add_job(
            "DROP-IMMATURE", hook_type=immature["creative_value"],
            published=False,
        )
        creative_experiments.bind_slot(
            self.memory_db, immature["slot_id"], immature_job,
        )
        self._publish_with_snapshot(
            immature_job, "IMMATURE", views=300, completion_rate=0.7,
            captured_at="2026-08-12T10:00:00Z",
        )

        low_views = creative_experiments.allocate_slot(
            self.memory_db, experiment_id, subject_key="DROP-LOW-VIEWS",
        )
        low_views_job = self._add_job(
            "DROP-LOW-VIEWS", hook_type=low_views["creative_value"],
            published=False,
        )
        creative_experiments.bind_slot(
            self.memory_db, low_views["slot_id"], low_views_job,
        )
        self._publish_with_snapshot(
            low_views_job, "LOW-VIEWS", views=50, completion_rate=0.9,
        )

        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )

        reasons = {
            item["slot_id"]: item["reason"]
            for item in report["cohort_audit"]["attrition"]
        }
        self.assertEqual(reasons[unbound["slot_id"]], "slot_unbound")
        self.assertEqual(reasons[unpublished["slot_id"]], "publication_missing")
        self.assertEqual(reasons[no_snapshot["slot_id"]], "snapshot_missing")
        self.assertEqual(reasons[immature["slot_id"]], "window_not_mature")
        self.assertEqual(reasons[low_views["slot_id"]], "below_min_views")

    def test_report_pauses_until_all_preregistered_slots_are_issued(self):
        experiment_id = self._create_experiment(
            min_jobs_per_arm=1, planned_jobs_per_arm=2,
        )
        for subject, completion in (("PLAN-1", 0.5), ("PLAN-2", 0.8)):
            slot = creative_experiments.allocate_slot(
                self.memory_db, experiment_id, subject_key=subject,
            )
            job_id = self._add_job(
                subject, hook_type=slot["creative_value"], published=False,
            )
            creative_experiments.bind_slot(
                self.memory_db, slot["slot_id"], job_id,
            )
            self._publish_with_snapshot(
                job_id, subject, views=300, completion_rate=completion,
            )

        report = creative_experiments.experiment_report(
            self.memory_db, experiment_id,
        )

        self.assertEqual(report["assessment"], "cohort_incomplete")
        self.assertEqual(report["cohort_audit"]["planned_slots"], 4)
        self.assertEqual(report["cohort_audit"]["issued_slots"], 2)
        self.assertEqual(report["cohort_audit"]["unissued_slots"], 2)
        self.assertFalse(report["cohort_audit"]["plan_fully_issued"])

    def test_cli_creates_assigns_and_reports_experiments(self):
        runner = CliRunner()
        with patch.object(
            cli_module.creative_experiments, "create_experiment",
            return_value="EXP-CLI",
        ) as create:
            created = runner.invoke(cli_module.cli, [
                "experiment-create", "--name", "钩子实验",
                "--platform", "douyin", "--window-hours", "24",
                "--dimension", "hook_type", "--control", "pain_point",
                "--treatment", "contrarian", "--primary-metric",
                "completion_rate", "--hypothesis", "提升完播",
                "--audience", "设备租赁老板", "--production-profile",
                "standard", "--minimum-effect", "0.05",
                "--planned-jobs-per-arm", "3",
            ])
        self.assertEqual(created.exit_code, 0, created.output)
        self.assertIn("EXP-CLI", created.output)
        self.assertEqual(create.call_args.kwargs["variable_dimension"], "hook_type")
        self.assertEqual(create.call_args.kwargs["planned_jobs_per_arm"], 3)

        with patch.object(
            cli_module.creative_experiments, "assign_job",
        ) as assign:
            assigned = runner.invoke(
                cli_module.cli,
                ["experiment-assign", "EXP-CLI", "J-1", "--arm", "control"],
            )
        self.assertEqual(assigned.exit_code, 0, assigned.output)
        self.assertEqual(assign.call_args.args[1:], ("EXP-CLI", "J-1"))

        with patch.object(
            cli_module.creative_experiments, "experiment_report",
            return_value={"experiment_id": "EXP-CLI", "assessment": "insufficient_data"},
        ):
            reported = runner.invoke(
                cli_module.cli, ["experiment-report", "EXP-CLI"],
            )
        self.assertEqual(reported.exit_code, 0, reported.output)
        self.assertIn("insufficient_data", reported.output)

    def test_cli_allocates_and_binds_preproduction_slots(self):
        runner = CliRunner()
        slot = {
            "slot_id": "SLOT-1", "arm": "control",
            "creative_value": "pain_point",
        }
        with patch.object(
            cli_module.creative_experiments, "allocate_slot",
            return_value=slot,
        ) as allocate:
            allocated = runner.invoke(
                cli_module.cli,
                ["experiment-allocate", "EXP-1", "CONTENT-1"],
            )
        self.assertEqual(allocated.exit_code, 0, allocated.output)
        self.assertIn("SLOT-1", allocated.output)
        self.assertEqual(allocate.call_args.args[1:], ("EXP-1",))
        self.assertEqual(allocate.call_args.kwargs["subject_key"], "CONTENT-1")

        with patch.object(
            cli_module.creative_experiments, "bind_slot",
        ) as bind:
            bound = runner.invoke(
                cli_module.cli,
                ["experiment-bind", "SLOT-1", "J-1"],
            )
        self.assertEqual(bound.exit_code, 0, bound.output)
        self.assertEqual(bind.call_args.args[1:], ("SLOT-1", "J-1"))


if __name__ == "__main__":
    unittest.main()
