"""Real SQLite/process crash boundaries; no external providers or credentials."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from factory import db


CHILD = r'''
import asyncio,json,os,sqlite3,sys
from datetime import datetime,timedelta,timezone
sys.path.insert(0,sys.argv[1])
from factory import production_executor as ex, node_state_machine as sm
c=sqlite3.connect(sys.argv[2],timeout=10);c.row_factory=sqlite3.Row
mode=sys.argv[3]
if mode=='recover':
    result=ex.recover_expired_nodes(c,'J',now=datetime.now(timezone.utc)+timedelta(hours=1))
    print(json.dumps(result))
elif mode=='claim':
    print(json.dumps(bool(ex.acquire_lease(c,'J','master:1'))))
else:
    async def handler(row):
        c.execute("INSERT INTO provider_calls(task_id,job_id,shot_idx,provider,intent,cost_cents,usage_json) VALUES('remote-1','J',1,'fixed','master',123,'{\"status\":\"completed\"}')")
        c.commit()
        return ex.NodeOutcome.require_review({'candidate_path':'retained.mp4','provider_task_id':'remote-1'})
    if mode=='checkpoint-crash':
        def crash(database, point, **kwargs):
            if point=='after_handler_completion_before_node_update': os._exit(29)
        ex.fault_injection.hit=crash
    elif mode=='transaction-crash':
        original=sm.transition_node_in_transaction
        def crash(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[3]=='require_review': os._exit(30)
            return result
        sm.transition_node_in_transaction=crash
    asyncio.run(ex.run_ready_wave(c,'J',configured_limit=1,handlers={'master':handler}))
'''


class ProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'factory.db'
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript(db.DDL)
            c.execute("INSERT INTO jobs(job_id,topic_id,title,state) VALUES('J','T','test','PRODUCTION_RUNNING')")
            c.execute("INSERT INTO production_nodes(job_id,node_id,node_type,state,spec_hash,input_json,idempotency_key,paid) VALUES('J','master:1','master','READY','s','{}','i',1)")
            c.commit()

    def command(self,mode):
        return [sys.executable,'-c',CHILD,str(ROOT/'scripts'),str(self.path),mode]

    def run_child(self,mode):
        return subprocess.run(self.command(mode),capture_output=True,text=True,timeout=30,
                              env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('API_KEY','TOKEN','SECRET'))})

    def assert_crash_recovery(self,mode,code):
        crashed=self.run_child(mode)
        self.assertEqual(crashed.returncode,code,crashed.stderr)
        with closing(sqlite3.connect(self.path)) as c:
            state,lease,raw=c.execute('SELECT state,lease_id,output_json FROM production_nodes').fetchone()
            self.assertEqual(state,'RUNNING');self.assertTrue(lease)
            self.assertIn('_executor_checkpoint',json.loads(raw))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM production_node_events WHERE event='require_review'").fetchone()[0],0)
        recovered=self.run_child('recover')
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout),{'master:1':'REVIEW_REQUIRED'})
        repeated=self.run_child('recover')
        self.assertEqual(repeated.returncode,0,repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout),{})
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*),SUM(cost_cents) FROM provider_calls').fetchone(),(1,123))
            row=c.execute('SELECT state,lease_id,provider_task_id,output_json FROM production_nodes').fetchone()
            self.assertEqual(row[:3],('REVIEW_REQUIRED',None,'remote-1'))
            self.assertEqual(json.loads(row[3])['candidate_path'],'retained.mp4')
            self.assertEqual(c.execute("SELECT COUNT(*) FROM production_node_events WHERE event='require_review'").fetchone()[0],1)

    def test_exit_after_handler_checkpoint_replays_without_new_submission(self):
        self.assert_crash_recovery('checkpoint-crash',29)

    def test_exit_inside_outcome_transaction_rolls_back_then_recovers(self):
        self.assert_crash_recovery('transaction-crash',30)

    def test_two_processes_cannot_claim_the_same_master(self):
        processes=[subprocess.Popen(self.command('claim'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        results=[]
        for process in processes:
            stdout,stderr=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,stderr)
            results.append(json.loads(stdout))
        self.assertEqual(sorted(results),[False,True])
