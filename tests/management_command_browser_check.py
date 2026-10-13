"""Real local command tombstones and browser refresh; never dispatch business work."""
from __future__ import annotations
import argparse
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import sys
import uuid
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'tests')]

def run(root: Path):
    root.mkdir(parents=True,exist_ok=False)
    for key in list(os.environ):
        if key.startswith(('FACTORY_','ARK_','VOLC_','DOUYIN_','SEEDANCE_','PRODUCTION_','VIDEO_QA_')):os.environ.pop(key)
    env=root/'.env';env.write_text('',encoding='utf-8')
    os.environ.update(FACTORY_RUNTIME_HOME=str(root),FACTORY_DATA_DIR=str(root),FACTORY_DB_PATH=str(root/'factory.db'),FACTORY_ENV_FILE=str(env),FACTORY_WORKER_MODE='external',FACTORY_PROVIDER_MODE='replay',PROVIDER_RECONCILE_ENABLED='false')
    from factory import db
    from factory.management_api import ManagementService
    from management_server import ConsoleHandler
    from tests.fixtures.management_factory import seed_management_factory
    from tests.management_workflow_browser_check import _edge_path
    from playwright.sync_api import sync_playwright
    database=db.conn();seed_management_factory(database)
    class Handler(ConsoleHandler):service_lock=threading.RLock()
    Handler.service=ManagementService(database,ROOT,worker_mode='external',data_dir=root,env_path=env)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def business_snapshot():
        return {row[0]:[tuple(r) for r in database.execute('SELECT * FROM "'+row[0]+'"')] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('events','sqlite_sequence') ORDER BY name")}
    before=business_snapshot();errors=[];external=[];captured=[];results=[]
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=str(_edge_path()),args=['--disable-background-networking'])
            try:
                context=browser.new_context(viewport={'width':1440,'height':900});page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                hold=False;held_routes=[];allow_service=False
                def route(request):
                    nonlocal hold
                    req=request.request
                    if not req.url.startswith(base+'/'):external.append(req.url);request.abort();return
                    if req.method=='POST' and (req.url.endswith('/produce') or req.url.endswith('/publish/douyin')):
                        captured.append({'path':req.url[len(base):],'body':req.post_data,'command_id':req.headers['x-avf-command-id'],'request_sha256':req.headers['x-avf-command-hash']})
                        if allow_service:request.continue_();return
                        if not hold:request.abort()
                        else:held_routes.append(request)
                        return
                    request.continue_()
                context.route('**/*',route)
                def ready():page.wait_for_function("() => typeof state!=='undefined' && state.projects")
                page.goto(base+'/',wait_until='domcontentloaded');ready()
                def cancel_and_prove(command,remaining=0):
                    page.locator('[data-cancel-unclaimed]').click();page.wait_for_function('(n) => AVFRequest.pending().length===n',arg=remaining)
                    token=page.evaluate('() => ensureServerCsrf()')
                    response=context.request.post(base+command['path'],data=command['body'],headers={'Content-Type':'application/json','Origin':base,'X-CSRF-Token':token,'X-AVF-Command-Id':command['command_id'],'X-AVF-Command-Hash':command['request_sha256']})
                    assert response.status==409,response.text()
                    assert response.json()['code']=='command_cancelled',response.text()
                    proof=context.request.get(base+'/api/commands/'+command['command_id']).json()
                    assert proof['submission_known'] and proof['outcome']=='rejected'
                    assert proof['request_sha256']==hashlib.sha256(command['body'].encode()).hexdigest()
                for kind in ('generation','publication'):
                    path='/api/videos/J-E2E-SETUP/'+('produce' if kind=='generation' else 'publish/douyin')
                    opening='openModal' if kind=='generation' else 'openDrawer'
                    scope='#modal-body' if kind=='generation' else '#drawer-body'
                    page.evaluate("([opening,path])=>{window[opening]('COMMAND TEST','隔离请求','<input id=retained-command-value value=保留输入><button id=send-test-command>提交</button>');document.querySelector(opening==='openModal'?'#modal-body #send-test-command':'#drawer-body #send-test-command').onclick=()=>api(path,{method:'POST',body:JSON.stringify({confirmed:true,title:'中文宣传',fraction:1e-7})}).catch(()=>{})}",[opening,path])
                    page.locator(scope+' #retained-command-value').fill(kind+' 输入不丢失');page.locator(scope+' #send-test-command').click()
                    page.wait_for_selector('[data-cancel-unclaimed]:not([hidden])')
                    assert page.locator(scope+' #retained-command-value').input_value()==kind+' 输入不丢失'
                    assert len(captured)>0;cancel_and_prove(captured[-1])
                    assert page.locator(scope+' #retained-command-value').input_value()==kind+' 输入不丢失'
                    results.append(kind+' never-arrived cancellation + late original refused + input retained')
                    page.evaluate('() => {AVFComponents.markClean();closeModal();closeDrawer()}')
                # Persist UUID before a response exists; refreshing destroys only this document.
                hold=True
                page.evaluate("() => {openModal('COMMAND TEST','等待中刷新','<button id=send-test-command>提交</button>');document.querySelector('#send-test-command').onclick=()=>api('/api/videos/J-E2E-SETUP/produce',{method:'POST',body:'{\"confirmed\":true}'}).catch(()=>{})}")
                page.locator('#modal-body #send-test-command').click();page.wait_for_function('() => AVFRequest.pending().length===1')
                pending=page.evaluate('() => AVFRequest.pending()[0]')
                page.reload(wait_until='domcontentloaded');ready()
                for held in held_routes:
                    try:held.abort()
                    except Exception:pass  # Reload already cancelled this document's request.
                assert page.evaluate('() => AVFRequest.pending()[0].command_id')==pending['command_id']
                page.wait_for_selector('[data-cancel-unclaimed]:not([hidden])')
                hold=False;cancel_and_prove(pending|{'body':'{"confirmed":true}'})
                results.append('inflight reload restores exact identity and global cancellation')
                # A genuine durable 500 has no automatic no-execution classification.
                allow_service=True
                original_start=Handler.service.start_video_job
                Handler.service.start_video_job=Mock(side_effect=RuntimeError('isolated failure before business effects'))
                page.evaluate("() => {openModal('COMMAND TEST','核对 500 原请求','<input id=unknown-500-input value=保留输入><button id=send-500-command>提交</button>');document.querySelector('#send-500-command').onclick=()=>api('/api/videos/J-E2E-SETUP/produce',{method:'POST',body:'{\"confirmed\":true}'}).catch(()=>{})}")
                page.locator('#unknown-500-input').fill('人工核对期间不丢失')
                page.locator('#send-500-command').click();page.wait_for_selector('[data-command-resolution]')
                uncertain=captured[-1]
                receipt=context.request.get(base+'/api/commands/'+uncertain['command_id']).json()
                assert receipt['outcome']=='unknown' and not receipt['submission_known']
                page.locator('[data-command-resolution] summary').click()
                page.locator('[data-resolution-decision]').select_option('not_executed')
                page.locator('[data-resolution-reason]').fill('处理器在业务写入前失败，已经人工核对')
                page.locator('[data-resolution-evidence]').fill('隔离数据库业务快照未改变；记录本次核对时间')
                page.locator('[data-resolution-confirm]').check();page.locator('[data-resolution-save]').click()
                page.wait_for_function('() => AVFRequest.pending().length===0')
                assert page.locator('#unknown-500-input').input_value()=='人工核对期间不丢失'
                proof=context.request.get(base+'/api/commands/'+uncertain['command_id']).json()
                assert proof['resolution']['source']=='human' and proof['outcome']=='rejected'
                replay=context.request.post(base+uncertain['path'],data=uncertain['body'],headers={'Content-Type':'application/json','Origin':base,'X-AVF-Command-Id':uncertain['command_id'],'X-AVF-Command-Hash':uncertain['request_sha256']})
                assert replay.status==409,replay.text()
                assert Handler.service.start_video_job.call_count==1
                Handler.service.start_video_job=original_start;allow_service=False
                results.append('actual terminal 500 + explicit human resolution preserves input and never replays original')
                page.evaluate('() => {AVFComponents.markClean();closeModal();closeDrawer()}')
                multiple=[{'path':'/api/videos/object-'+letter+'/produce','kind':'generation','command_id':str(uuid.uuid4()),'request_sha256':hashlib.sha256(b'{}').hexdigest()} for letter in ('A','B')]
                page.evaluate("p=>sessionStorage.setItem('avf_unconfirmed_commands_v1',JSON.stringify(p))",multiple)
                page.reload(wait_until='domcontentloaded');ready();page.wait_for_selector('[data-pending-selector]:visible')
                page.locator('[data-pending-selector]').select_option(multiple[1]['command_id'])
                page.wait_for_function('(id) => document.querySelector("[data-uncertain-submit]").dataset.commandId===id',arg=multiple[1]['command_id'])
                cancel_and_prove(multiple[1]|{'body':'{}'},remaining=1)
                page.evaluate("() => api('/api/videos')");page.wait_for_selector('[data-cancel-unclaimed]:not([hidden])')
                assert page.locator('[data-uncertain-submit]').get_attribute('data-command-id')==multiple[0]['command_id']
                cancel_and_prove(multiple[0]|{'body':'{}'})
                results.append('multiple pending requests can be selected and reconciled independently')
                # Pure browser object-isolation boundary: mocked known proof is not a business receipt.
                synthetic={'path':'/api/videos/object-A/produce','kind':'generation','command_id':str(uuid.uuid4()),'request_sha256':hashlib.sha256(b'{}').hexdigest()}
                page.evaluate("p=>sessionStorage.setItem('avf_unconfirmed_commands_v1',JSON.stringify([p]))",synthetic)
                page.reload(wait_until='domcontentloaded');ready();page.wait_for_selector('[data-cancel-unclaimed]:not([hidden])')
                page.evaluate("() => openModal('PRODUCTION PREFLIGHT','尚未提交的对象 B','<button id=produce-confirm data-command-path=/api/videos/object-B/produce>确认 B</button>')")
                context.route(base+'/api/commands/'+synthetic['command_id'],lambda request:request.fulfill(status=200,content_type='application/json',body=json.dumps(synthetic|{'submission_known':True,'outcome':'accepted'})))
                page.evaluate('() => AVFWorkbench.reconcile(state.videos)');page.wait_for_function('() => AVFRequest.pending().length===0')
                assert page.locator('#produce-confirm').is_enabled()
                assert page.locator('#produce-confirm').inner_text()=='确认 B'
                results.append('synthetic exact proof for A preserves unsent B confirmation')
            finally:browser.close()
        assert business_snapshot()==before,'Command bookkeeping changed business state'
        assert not errors and not external,(errors,external)
        report={'status':'passed','scenarios':results,'business_state_unchanged':True,'paid_provider_calls':0,'external_requests':0,'page_errors':errors,'command_ids':[x['command_id'] for x in captured]}
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));return report
    finally:server.shutdown();server.server_close();thread.join(5);database.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work-root',type=Path,required=True);a=p.parse_args();run(a.work_root.resolve());return 0
if __name__=='__main__':raise SystemExit(main())
