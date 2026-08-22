from __future__ import annotations

import asyncio
import inspect
import sqlite3
import unittest

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


if __name__ == "__main__":
    unittest.main()
