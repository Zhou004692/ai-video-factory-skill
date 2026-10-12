"""Real Edge + isolated HTTP/supervisor/FFmpeg content delivery and restart.

Provider generation is explicitly synthetic; observed content is measured red
test-pattern pixels. Tone audio is never represented as recognized speech.
"""
from __future__ import annotations
import argparse
import array
from contextlib import contextmanager, closing, ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
from factory import db, creative_learning, full_flow_regression, script_drafts, topic_briefs
from factory.management_api import ManagementService
from factory.flow_guard.evidence import BROWSER_EVIDENCE_PREFIX
from tests.management_full_flow_browser_check import _open_advanced_page, _open_job, _wait_for_job_state
from tests.management_workflow_browser_check import _free_port, _edge_path, _stop_process_tree, _wait_until_ready
from tests.test_creative_delivery_journey import TAGS_A, TAGS_B
from tests.zero_cost_release_gate import _safe_environment, _guard_source

REQ = {'id': 'red-pattern', 'description': 'Technical regression pattern contains a visible red region', 'priority': 'critical'}

def seed(root):
    """Actual preapproval inputs, no frozen/spec/publication JSON injection."""
    database = db.conn()
    try:
        service = ManagementService(database, ROOT, worker_mode='external')
        source = service.add_source(source_type='manual', title='Regression pattern; no business claim', content='Synthetic test pattern has red pixels and sine tone; no actual human face or spoken dialogue. The scripted text is fixture intent only.')
        source_id = source['source_id']
        brief = topic_briefs.generate_brief(database, [source_id], generate_json=lambda *a, **kw: (full_flow_regression._brief_result(full_flow_regression._source_record(database, source_id)), {}))
        topic_briefs.approve_brief(database, brief['brief_id'], topic_id='T-CREATIVE-JOURNEY', angle_index=0)
        creative_learning.tag_angle(database, brief['brief_id'], angle_index=0, **TAGS_A)
        beat = {'beat_id': 'pattern-beat', 'beat': 'evidence', 'speaker': 'fixture_actor', 'dialogue': '回归夹具', 'duration_sec': 4,
                'visual_intent': 'Regression test pattern with a measurable red region', 'source_ids': [source_id],
                'speech_mode': 'off_screen', 'characters':['fixture_actor'], 'scene': 'technical fixture', 'continuity_id': 'pattern',
                'visual_requirements': [REQ], 'broll_hint': ''}
        result = {'variants': [{'title': 'Technical content delivery regression', 'creative_thesis': 'Technical red-pixel intent; no business or speech verification', 'characters':[{'id':'fixture_actor','name':'Technical fixture identity','look':'Synthetic test-pattern role; not a real person'}], 'target_duration_sec': 4, 'beats': [beat]}], 'recommended_index': 0}
        drafts = script_drafts.generate_drafts(database, brief['brief_id'], generate_json=lambda *a, **kw: (result, {}))
        (root / 'ids.json').write_text(json.dumps({'source_id': source_id, 'brief_id': brief['brief_id'], 'draft_id': drafts['drafts'][0]['draft_id']}), encoding='utf-8')
    finally:
        database.close()

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def red_measure(path, at=0.5):
    frame = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', str(path), '-frames:v', '1', '-vf', 'scale=80:80', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout
    assert len(frame) == 80*80*3
    fraction = sum(frame[n] > 150 and frame[n] > frame[n+1]*1.5 and frame[n] > frame[n+2]*1.5 for n in range(0,len(frame),3)) / 6400
    assert fraction > .03, f'No measured red region: {fraction}'
    return {'frame_sec': at, 'red_fraction': fraction, 'decoded_rgb_sha256': hashlib.sha256(frame).hexdigest()}

def tone_measure(path):
    pcm=subprocess.run(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-ac','1','-ar','8000','-f','s16le','-'],check=True,capture_output=True).stdout
    samples=array.array('h');samples.frombytes(pcm)
    duration=len(samples)/8000
    crossings=sum(samples[n]<0<=samples[n+1] for n in range(len(samples)-1))
    frequency=crossings/duration
    assert 500<frequency<540 and 3.8<duration<4.2,(frequency,duration)
    return {'duration_sec':duration,'zero_crossing_frequency_hz':frequency,'pcm_sha256':hashlib.sha256(pcm).hexdigest(),'speech_verified':False}

def http(base, path, payload=None):
    req = Request(base+path, data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type':'application/json','Origin':base})
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read())

class Journey:
    def __init__(self, root):
        self.root=root
        guard=root/'guard'; guard.mkdir()
        (guard/'sitecustomize.py').write_text(_guard_source(),encoding='utf-8')
        self.env=_safe_environment(guard,root/'external.jsonl')
        for key in list(self.env):
            if key.startswith(('FACTORY_', 'ARK_', 'PRODUCTION_', 'VIDEO_QA_', 'SEEDANCE_', 'APPEND_', 'GEN_')):
                self.env.pop(key)
        self.env.update(FACTORY_RUNTIME_HOME=str(root), FACTORY_ENV_FILE=str(root/'.env'), FACTORY_DB_PATH=str(root/'factory.db'), FACTORY_DATA_DIR=str(root), FACTORY_PROVIDER_MODE='replay', FACTORY_REPLAY_DISABLE_WORKERS='0',
                        FACTORY_FLOW_GUARD_ZERO_COST_WORKER='1', FACTORY_FLOW_GUARD_MEDIA_ROOT=str(root/'assets'), PRODUCTION_EXECUTION_ENGINE='graph_v2',
                        PROVIDER_RECONCILE_ENABLED='false', FACTORY_ZERO_COST_SOCKET_TELEMETRY=str(root/'external.jsonl'), APPEND_CTA='false', GEN_THUMBNAIL='false', GEN_MARKETING_THUMBNAIL='false')
        self.server_pids=[]; self.worker_pids=set(); self.requests=[]; self.errors=[]; self.external=[]

    @contextmanager
    def server(self):
        base=f'http://127.0.0.1:{_free_port()}'
        with (self.root/f'server-{len(self.server_pids)}.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,str(ROOT/'scripts/management_server.py'),'--host','127.0.0.1','--port',urlsplit(base).port.__str__(),'--no-browser'],cwd=ROOT,env=self.env,stdout=log,stderr=log)
            self.server_pids.append(process.pid)
            try:
                deadline=time.monotonic()+30
                while True:
                    assert process.poll() is None, 'HTTP service exited; see server log'
                    try: http(base,'/api/videos'); break
                    except OSError:
                        assert time.monotonic()<deadline, 'HTTP startup timeout'
                        time.sleep(.1)
                yield base
            finally: _stop_process_tree(process)

    def navigate(self,page,base):
        page.goto(base,wait_until='networkidle')

    def content(self,page,job, *, save=True):
        page.reload(wait_until='networkidle')
        _open_job(page,job)
        page.locator('#drawer-content-review').click()
        page.wait_for_selector('[data-content-id="red-pattern"]')
        if save:
            page.locator('[data-content-status]').select_option('satisfied')
            page.locator('[data-content-start]').fill('0.2')
            page.locator('[data-content-end]').fill('0.8')
            page.locator('[data-content-description]').fill('FFmpeg decoded frame measured visible red test-pattern pixels; technical fixture only, no human or business-action claim')
            page.locator('[data-content-reason]').fill('回归夹具；音频仅为正弦波，未识别真实台词或人物身份')
            with page.expect_response(lambda r: r.request.method=='POST' and r.url.endswith('/content-review')) as saved:
                page.locator('#save-content-review').click()
            response=saved.value.json(); assert saved.value.ok, response
            return response

    def execute(self):
        subprocess.run([sys.executable,str(Path(__file__).resolve()),'--seed',str(self.root)],cwd=ROOT,env=self.env,check=True)
        ids=json.loads((self.root/'ids.json').read_text())
        database=sqlite3.connect(self.root/'factory.db'); database.row_factory=sqlite3.Row
        from playwright.sync_api import sync_playwright
        # sqlite's transaction context does not close the connection. Close
        # it explicitly on success/failure before deleting the isolated root.
        with closing(database), database, sync_playwright() as playwright, ExitStack() as resources:
            browser=playwright.chromium.launch(headless=True,executable_path=_edge_path(),args=['--disable-background-networking','--disable-component-update','--no-first-run'])
            resources.callback(browser.close)
            self.browser_pid=None
            page=browser.new_page(viewport={'width':1440,'height':980})
            page.on('pageerror',lambda e:self.errors.append(str(e)))
            page.on('request', lambda r:self.requests.append({'method':r.method,'url':r.url,'body':r.post_data}) if r.method=='POST' else None)
            def local(route):
                if urlsplit(route.request.url).hostname not in {'127.0.0.1','localhost'}:
                    self.external.append(route.request.url);route.abort()
                else:route.continue_()
            page.route('**/*',local)
            with self.server() as base:
                self.navigate(page,base)
                _open_advanced_page(page,'scripts')
                page.locator(f'[data-approve-script="{ids["draft_id"]}"]').click()
                with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/approve')) as approval:
                    page.locator('#drawer-approve').click()
                assert approval.value.ok,approval.value.text()
                page.wait_for_function("() => state.scripts?.drafts?.some(item=>item.status==='approved')")
                job=database.execute('SELECT job_id FROM jobs').fetchone()[0]; ids['job_id']=job
                approved=json.loads(database.execute('SELECT storyboard FROM jobs').fetchone()[0])
                assert approved['script_metadata']['creative_tags']==TAGS_A
                # An authored dual-track fixture edit is input to compilation,
                # never an injected frozen spec or evidence/approval record.
                opening={**approved['shots'][0], 'idx':1,'duration':1.5,'video_start':2.5,'audio_start':0,'speech_mode':'off_screen','subtitle':''}
                approved['shots']=[opening,{**opening,'idx':2,'duration':2.5,'video_start':1.5,'audio_start':1.5,'continuity_relation':'same_master_continuous'}]
                approved['hook']=''
                database.execute('UPDATE jobs SET storyboard=? WHERE job_id=?',(json.dumps(approved),job)); database.commit()
                _open_job(page,job)
                page.wait_for_selector('#save-production-settings')
                page.locator('#setting-anchor-review-mode').select_option('manual')
                page.locator('#setting-asr').uncheck();page.locator('#setting-budget').fill('100')
                page.locator('#save-production-settings').click();page.wait_for_selector('#produce-confirm')
                if page.locator('#high-risk-master-confirm').count():page.locator('#high-risk-master-confirm').check()
                assert page.locator('#produce-confirm').is_enabled(), page.locator('#modal-body').inner_text()
                with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/produce')) as started:
                    page.locator('#produce-confirm').click()
                assert started.value.ok,started.value.text()
                self.worker_pids|=_wait_for_job_state(database,job,'ANCHOR_REVIEW_REQUIRED')
                frozen=database.execute('SELECT production_spec_json,production_spec_hash FROM jobs').fetchone()
                assert json.loads(frozen[0])['runtime_storyboard']['script_metadata']['creative_tags']==TAGS_A
                self.spec_hash=frozen[1]; self.spec_bytes=frozen[0]
            # Hard-stop the actual HTTP process at a durable human gate, then
            # restart a separate service. GET must not create work/reservations.
            with self.server() as base:
                before=database.execute('SELECT COUNT(*) FROM production_nodes').fetchone()[0]
                self.navigate(page,base);_open_job(page,job)
                page.wait_for_selector('#anchor-generation-status')
                assert database.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
                assert database.execute('SELECT COUNT(*) FROM production_nodes').fetchone()[0]==before
                page.locator('#drawer-next-action').click()
                self.worker_pids|=_wait_for_job_state(database,job,'PRODUCTION_REVIEW_REQUIRED')
                source=database.execute('SELECT video_path FROM shots').fetchone()[0]
                physical=red_measure(source)
                source_audio=tone_measure(source)
                source_hash=sha(source); bgm=database.execute('SELECT bgm_path FROM jobs').fetchone()[0]; bgm_hash=sha(bgm)
                page.reload(wait_until='networkidle')
                first=self.content(page,job)
                page.locator('#modal-close').click()
                duplicate=self.content(page,job)
                assert duplicate['already_recorded']
                page.locator('#modal-close').click()
                self.content(page,job,save=False)
                page.wait_for_selector('#preview-free-recut')
                page.locator('#preview-free-recut').click();page.wait_for_selector('#confirm-free-recut')
                with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/local-recut')) as cut:
                    page.locator('#confirm-free-recut').click()
                assert cut.value.ok,cut.value.text()
                self.recut_response=cut.value.json()
                self.worker_pids|=_wait_for_job_state(database,job,'COMPOSED')
                assert sha(source)==source_hash and sha(bgm)==bgm_hash
                assert database.execute('SELECT production_spec_json FROM jobs').fetchone()[0]==self.spec_bytes
                actual=json.loads(database.execute('SELECT storyboard FROM jobs').fetchone()[0])
                assert [s['video_start'] for s in actual['shots']] != [s['video_start'] for s in approved['shots']]
                assert [(s['audio_start'],s['duration']) for s in actual['shots']] == [(s['audio_start'],s['duration']) for s in approved['shots']]
                repeat_body=json.loads(next(r['body'] for r in reversed(self.requests) if r['url'].endswith('/local-recut')))
                assert http(base,f'/api/videos/{job}/masters/1/local-recut',repeat_body)['already_queued']
                final=database.execute('SELECT final_path FROM jobs').fetchone()[0]
                offset=0
                for edit in actual['shots']:
                    if edit['video_start'] <= .5 < edit['video_start']+edit['duration']:
                        measured_final_sec=offset+.5-edit['video_start'];break
                    offset+=edit['duration']
                final_physical=red_measure(final,measured_final_sec)
                final_audio=tone_measure(final)
                assert abs(source_audio['zero_crossing_frequency_hz']-final_audio['zero_crossing_frequency_hz'])<6
                page.reload(wait_until='networkidle');_open_job(page,job)
                page.wait_for_selector('#review-check-all');page.locator('#review-check-all').click()
                page.locator('#review-notes').fill('回归夹具：仅技术像素/音轨/保留证据通过，不验证台词或人物身份')
                page.locator('#review-submit').click();page.wait_for_selector('#drawer-next-action');page.locator('#drawer-next-action').click()
                page.wait_for_function("id=>state.videos?.jobs?.some(j=>j.job_id===id&&j.state==='READY_TO_PUBLISH')",arg=job)
                # Change live tags/draft after production. Registered feedback
                # must retain the actual frozen A intent, never associate B.
                creative_learning.tag_angle(database,ids['brief_id'],angle_index=0,**TAGS_B)
                database.execute("UPDATE script_drafts SET script_json='{}' WHERE draft_id=?",(ids['draft_id'],));database.commit()
                publication=http(base,'/api/publications',{'job_id':job,'platform':'douyin','item_id':'offline-creative-fixture','title':'Local association only'})
                self.publication=publication
                pub_id=database.execute('SELECT publication_id FROM platform_publications').fetchone()[0]
                metric=http(base,f'/api/publications/{pub_id}/performance',{'captured_at':datetime.now(timezone.utc).isoformat(),'views':0,'likes':0})
                assert metric['latest_snapshot']['views']==0 and metric['latest_snapshot']['comments'] is None
            with self.server() as base:
                self.navigate(page,base)
                restored=http(base,'/api/videos')
                assert len(restored['jobs'])==1 and restored['jobs'][0]['job_id']==job
                pub=database.execute('SELECT * FROM platform_publications').fetchone()
                context=json.loads(pub['context_json'])
                assert context['tags']==TAGS_A and context['tags_provenance']=='frozen_production_input',context
                assert context['final_sha256']==sha(final)
                assert context['declared_association']['tags']==TAGS_B
                assert json.loads(database.execute('SELECT context_json FROM performance_snapshots').fetchone()[0])['tags']==TAGS_A
                retention=json.loads((Path(final).parent/'content_retention.json').read_text(encoding='utf-8'))
                assert retention['passed'] is True
                assert database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0]==0
                assert database.execute('SELECT cost_cents FROM jobs').fetchone()[0]==0
            browser.close()
            assert len(self.worker_pids)>=3, 'Three supervisor-owned worker process executions were not observed'
            assert not self.errors and not self.external, (self.errors,self.external)
            external=self.root/'external.jsonl'; assert not external.exists() or not external.read_text().strip()
            worker_nodes=sorted({r[0] for r in database.execute("SELECT DISTINCT node_type FROM production_nodes WHERE state='SUCCEEDED'")})
            report={'status':'passed','ids':ids,'server_pids':self.server_pids,'worker_pids':sorted(self.worker_pids),'worker_node_types':worker_nodes,
                    'source_sha256':source_hash,'bgm_sha256':bgm_hash,'final_sha256':sha(final),'production_spec_hash':self.spec_hash,
                    'pixel_measurements':{'source':physical,'final':final_physical},'audio_measurements':{'source':source_audio,'final':final_audio},'content_retention':retention,'publication_context':context,
                    'http_posts':self.requests,'paid_provider_calls':0,'external_requests':0,
                    'limitations':['Synthetic high-level provider handlers; no actual generated person or business action','No ASR measurement: sine-tone audio; fixture text is authored intent','Process interrupt at durable review gate; no provider in-flight interrupt in this slice']}
            (self.root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            return report

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--seed',type=Path);parser.add_argument('--work-root',type=Path);args=parser.parse_args()
    if args.seed:seed(args.seed);return 0
    if args.work_root:
        args.work_root.mkdir(parents=True,exist_ok=False);root=args.work_root
        report=Journey(root).execute()
    else:
        with tempfile.TemporaryDirectory(prefix='creative-delivery-browser-') as name: report=Journey(Path(name)).execute()
    print(BROWSER_EVIDENCE_PREFIX+json.dumps({'ui_dispatch_actions':['review_continuity_anchors','review_visual_content','review_final_video','submit_publish'],
          'integrated_backend_actions':['review_continuity_anchors','review_visual_content','review_final_video','submit_publish'],
          'worker_actions':['review_continuity_anchors','review_visual_content'],'worker_node_types':report['worker_node_types'],'source_types':['manual'],'provider_response_types':['synthetic_worker_fixture']},sort_keys=True))
    print('CREATIVE_DELIVERY_BROWSER_CHECK_OK');return 0

if __name__=='__main__':raise SystemExit(main())
