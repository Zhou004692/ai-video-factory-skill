from __future__ import annotations

import asyncio
import inspect
import sqlite3
import unittest
from unittest import mock

from scripts.factory import provider_executor, provider_tasks
from scripts.factory.db import DDL
from scripts.factory.provider_executor import (
    PaidTaskOwner,
    ProviderExecutor,
    ProviderSubmissionOutcomeUnknown,
)
from scripts.factory.steps import s3_shots, s5_bgm


class ProviderExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.row_factory = sqlite3.Row
        self.database.executescript(DDL)
        self.database.execute(
            "INSERT INTO jobs(job_id,topic_id,title,state,cost_cents) "
            "VALUES('J-1','T-1','provider','PRODUCTION_RUNNING',0)"
        )
        self.database.commit()
        self.owner = PaidTaskOwner(
            job_id="J-1", shot_idx=1, provider="seedance", intent="generate"
        )

    def tearDown(self) -> None:
        self.database.close()

    def test_unknown_submit_result_never_posts_twice(self):
        posts = 0

        async def ambiguous(_created):
            nonlocal posts
            posts += 1
            raise ProviderSubmissionOutcomeUnknown("ambiguous")

        executor = ProviderExecutor(self.database, self.owner)
        with self.assertRaises(ProviderSubmissionOutcomeUnknown):
            asyncio.run(executor.submit_once(ambiguous))
        with self.assertRaises(provider_tasks.ProviderTaskGateError):
            asyncio.run(ProviderExecutor(
                self.database, self.owner
            ).submit_once(ambiguous))
        self.assertEqual(posts, 1)
        self.assertEqual(
            provider_tasks.unresolved_task_ids(self.database, "J-1"),
            ["local-submit:seedance:J-1:1:generate"],
        )

    def test_remote_id_is_durable_before_submit_returns(self):
        async def accepted(created):
            created("remote-1")
            row = self.database.execute(
                "SELECT task_id FROM provider_calls WHERE task_id='remote-1'"
            ).fetchone()
            self.assertIsNotNone(row)
            return "accepted"

        result = asyncio.run(
            ProviderExecutor(self.database, self.owner).submit_once(accepted)
        )
        self.assertEqual(result.value, "accepted")
        self.assertEqual(result.task_id, "remote-1")

    def test_definite_pre_submit_failure_releases_reservation(self):
        class BeforeSubmit(RuntimeError):
            pre_submit = True

        async def rejected(_created):
            raise BeforeSubmit("missing credential")

        with self.assertRaises(BeforeSubmit):
            asyncio.run(
                ProviderExecutor(self.database, self.owner).submit_once(rejected)
            )
        self.assertEqual(
            provider_tasks.unresolved_task_ids(self.database, "J-1"), []
        )

    def test_paid_steps_do_not_bypass_provider_executor_lifecycle(self):
        for module in (s3_shots, s5_bgm):
            with self.subTest(module=module.__name__):
                self.assertIs(module.provider_tasks, provider_executor)

    def test_frozen_owner_gate_accepts_only_matching_ready_node(self):
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-1", "master:1", "master", 1, "READY", "spec-1", "{}",
                "idem-1",
            ),
        )
        self.database.commit()
        provider_executor.assert_frozen_owner(
            self.database, job_id="J-1", owner_id="master:1", spec_hash="spec-1",
        )
        with self.assertRaises(provider_executor.ProductionContractRequiredError):
            provider_executor.assert_frozen_owner(
                self.database, job_id="J-1", owner_id="master:1",
                spec_hash="wrong",
            )

    def test_enforced_gate_rejects_before_reservation_without_owner(self):
        with mock.patch.dict("os.environ", {"FACTORY_REQUIRE_V2_CONTRACT": "1"}):
            with self.assertRaises(provider_executor.ProductionContractRequiredError):
                provider_executor.reserve_paid_submit(
                    self.database,
                    job_id="J-1", shot_idx=1, provider="seedance",
                    intent="generate",
                )
        self.assertEqual(
            self.database.execute(
                "SELECT COUNT(*) FROM provider_calls WHERE job_id='J-1'"
            ).fetchone()[0],
            0,
        )

    def test_node_context_owns_nested_paid_reservation(self):
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-1", "master:1", "master", 1, "RUNNING", "spec-1",
                "{}", "idem-context",
            ),
        )
        self.database.commit()

        with provider_executor.paid_task_owner_context(
            job_id="J-1", node_id="master:1", spec_hash="spec-1",
        ):
            reservation = provider_executor.reserve_paid_submit(
                self.database,
                job_id="J-1", shot_idx=1, provider="seedance",
                intent="generate",
            )
            provider_executor.persist_remote_task_id(
                self.database,
                reservation,
                remote_task_id="remote-context-1",
                provider="seedance",
                intent="generate",
            )
            provider_executor.promote_reservation(
                self.database,
                reservation,
                task_id="remote-context-1",
                job_id="J-1",
                shot_idx=1,
                provider="seedance",
                intent="generate",
            )
            provider_executor.upsert_final_call(
                self.database,
                task_id="remote-context-1",
                job_id="J-1",
                shot_idx=1,
                provider="seedance",
                intent="generate",
                status="completed",
                usage={},
                cost_cents=123,
            )

        self.assertTrue(reservation)
        node = self.database.execute(
            "SELECT provider_task_id,actual_cost_cents FROM production_nodes "
            "WHERE job_id='J-1' AND node_id='master:1'"
        ).fetchone()
        self.assertEqual(node["provider_task_id"], "remote-context-1")
        self.assertEqual(node["actual_cost_cents"], 123)

    def test_non_paid_node_cannot_own_paid_reservation(self):
        self.database.execute(
            "INSERT INTO production_nodes("
            "job_id,node_id,node_type,paid,state,spec_hash,input_json,"
            "idempotency_key) VALUES(?,?,?,?,?,?,?,?)",
            (
                "J-1", "local_compose", "local_compose", 0, "RUNNING",
                "spec-1", "{}", "idem-local",
            ),
        )
        self.database.commit()

        with provider_executor.paid_task_owner_context(
            job_id="J-1", node_id="local_compose", spec_hash="spec-1",
        ):
            with self.assertRaises(
                provider_executor.ProductionContractRequiredError
            ):
                provider_executor.reserve_paid_submit(
                    self.database,
                    job_id="J-1", shot_idx=0, provider="music",
                    intent="bgm",
                )


if __name__ == "__main__":
    unittest.main()
