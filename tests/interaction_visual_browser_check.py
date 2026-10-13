"""Isolated real UI contract checks; no paid work or production service access."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts'),str(ROOT/'tests')]

def run(work_root: Path,batch: str) -> dict:
    work_root.mkdir(parents=True,exist_ok=True)
    runtime=work_root/'runtime';runtime.mkdir(exist_ok=True)
    env_path=runtime/'.env';env_path.write_text('',encoding='utf-8')
    for key in list(os.environ):
        if key.startswith(('FACTORY_','ARK_','VOLC_','DOUYIN_','SEEDANCE_','VIDEO_QA_','PRODUCTION_')):os.environ.pop(key)
    os.environ.update(FACTORY_RUNTIME_HOME=str(runtime),FACTORY_DATA_DIR=str(runtime),FACTORY_DB_PATH=str(runtime/'factory.db'),FACTORY_ENV_FILE=str(env_path),FACTORY_WORKER_MODE='external',FACTORY_PROVIDER_MODE='replay',PROVIDER_RECONCILE_ENABLED='false')
    from factory import db
    from factory.management_api import ManagementService
    from management_server import ConsoleHandler
    from tests.fixtures.management_factory import seed_management_factory
    from tests.management_workflow_browser_check import _edge_path
    from playwright.sync_api import sync_playwright
    database=db.conn();seed_management_factory(database)
    database.execute("INSERT INTO jobs(job_id,topic_id,title,state,storyboard) VALUES('J-PAUSED-UI','T-PAUSED-UI','等待确认人物','ANCHOR_REVIEW_REQUIRED',?)",(json.dumps({'shots':[{'idx':1,'duration_sec':4,'speaker':'经营者','dialogue':'测试台词','visual_intent':'测试场景'}]}),))
    database.execute("INSERT INTO shots(job_id,idx,state) VALUES('J-PAUSED-UI',1,'PENDING')")
    media=runtime/'assets/J-E2E-REVIEW/final.mp4';media.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=320x180:r=24:d=4','-f','lavfi','-i','anullsrc=r=16000:cl=mono','-t','4','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-y',str(media)],check=True,capture_output=True)
    database.execute("UPDATE jobs SET final_path=? WHERE job_id='J-E2E-REVIEW'",(str(media),));database.commit()
    service=ManagementService(database,ROOT,worker_mode='external',data_dir=runtime,env_path=env_path)
    class Handler(ConsoleHandler):
        service_lock=threading.RLock()
    Handler.service=service
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    results=[];errors=[];requests=[];external=[];dimensions=[];screenshots=[]
    before_dump='\n'.join(database.iterdump())
    (work_root/'before.sql').write_text(before_dump,encoding='utf-8')
    def case(name,action):
        try:
            action();results.append({'name':name,'status':'passed'})
            if name.startswith(('viewport-','readable-')) or name in ('settings-wide-fixed-budget-action','script-change-impact-wide-workspace','same-screen-final-review-six-items-no-bulk-pass'):
                guard=Path(r'D:\WORK\github\my-claude-ai\tools\Assert-RemoteDesktopNotVisible.ps1')
                checked=subprocess.run(['powershell','-NoProfile','-File',str(guard)],capture_output=True,text=True) if guard.is_file() else None
                if checked is not None and checked.returncode==0:
                    image=work_root/(name+'.png');page.screenshot(path=str(image));screenshots.append({'scenario':name,'path':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'status':'captured','surface':'isolated headless local app only'})
                else:screenshots.append({'scenario':name,'status':'blocked','reason':'privacy guard missing or visible remote session; no pixels captured'})
        except Exception as exc:results.append({'name':name,'status':'failed','error':str(exc)[:1800]})
        finally:
            try:page.evaluate("() => {window.AVFComponents?.markClean();closeModal();closeDrawer()}")
            except Exception:pass
    def check(condition,message):
        if not condition:raise AssertionError(message)
    def go(page,key):
        page.goto(base+'/#'+key,wait_until='domcontentloaded');page.wait_for_function("() => typeof state!=='undefined' && state.projects",timeout=15000)
        page.wait_for_timeout(250)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=str(_edge_path()),headless=True,args=['--disable-background-networking'])
            try:
                context=browser.new_context(viewport={'width':1440,'height':900},locale='zh-CN')
                def routing(route):
                    req=route.request;u=urlsplit(req.url)
                    if u.scheme in ('http','https') and u.hostname not in ('127.0.0.1','localhost'):
                        external.append({'method':req.method,'path':u.path});return route.abort()
                    requests.append({'method':req.method,'path':u.path})
                    # This suite only opens/edits local UI. Submit journeys use separately guarded fixtures.
                    if req.method not in ('GET','HEAD'):
                        return route.fulfill(status=409,content_type='application/json',body=json.dumps({'error':'隔离测试：明确拒绝提交，表单应保留'}))
                    route.continue_()
                context.route('**/*',routing)
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                go(page,'overview')
                if batch in ('m0','all'):
                    def activity():
                        facts=page.evaluate("() => studioBackgroundTasks().map(t=>({parent:t.job.state,status:t.status}))")
                        check(not any(f['status']=='排队中' and f['parent'] in ('STORYBOARDED','ANCHOR_REVIEW_REQUIRED','PROVIDER_ACTION_REQUIRED') for f in facts),'planned or paused work appears queued')
                    case('activity-does-not-invent-running',activity)
                    def counts():
                        page.evaluate("() => navigate('todos')");page.wait_for_timeout(500)
                        value=page.evaluate("() => ({badge:document.querySelector('#nav-todo-count').textContent,total:state.todos?.item_count??state.todos?.total,hidden:document.querySelector('#nav-todo-count').hidden})")
                        check(int(value['badge'] or 0)==int(value['total'] or 0),str(value))
                    case('todo-badge-same-scope',counts)
                if batch in ('m1','all'):
                    for width,height in ((1920,1080),(1440,900),(1366,768),(1280,800),(1024,768),(549,970)):
                        def viewport(w=width,h=height):
                            go(page,'overview');page.set_viewport_size({'width':w,'height':h});page.wait_for_timeout(200)
                            if w<1024:
                                check(page.get_by_text('请使用桌面窗口').is_visible(),'narrow screen lacks visible message')
                            else:
                                check(page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'horizontal clipping')
                                button=page.locator('#studio-primary-action');r=button.bounding_box()
                                check(r is not None and r['x']+r['width']<=w,'primary action outside viewport')
                                main=page.locator('.studio-canvas').bounding_box()
                                check(main and main['width']>=320 and main['x']+main['width']<=w+1,'main content clipped')
                            dimensions.append({'scenario':f'viewport-{w}','viewport':[w,h],'body_scroll_width':page.evaluate('document.documentElement.scrollWidth'),'main_action':page.locator('#studio-primary-action').bounding_box() if w>=1024 else None})
                        case(f'viewport-{width}',viewport)
                    page.set_viewport_size({'width':1440,'height':900});go(page,'overview')
                    def modal_focus():
                        page.locator('#open-advanced-nav').click()
                        check(page.evaluate("!!document.activeElement.closest('[role=dialog]')"),'opening modal leaves focus outside')
                        for _ in range(12):
                            page.keyboard.press('Tab');check(page.evaluate("!!document.activeElement.closest('[role=dialog]')"),'Tab escaped modal')
                        for _ in range(12):
                            page.keyboard.press('Shift+Tab');check(page.evaluate("!!document.activeElement.closest('[role=dialog]')"),'Shift Tab escaped modal')
                        page.keyboard.press('Escape');check(page.locator('#modal').evaluate('(e)=>e.hidden'),'Esc failed')
                        check(page.evaluate("document.activeElement.id==='open-advanced-nav'"),'focus not returned')
                    case('modal-focus-tab-escape-return',modal_focus)
                    def dirty():
                        page.locator('#global-new-project').click();page.locator('#source-title').fill('保留输入')
                        page.keyboard.press('Escape')
                        check(page.locator('#source-title').count()>0 and page.locator('#source-title').input_value()=='保留输入','dirty source input silently lost')
                        check(page.get_by_text('继续编辑',exact=True).count()>0,'no dirty-close choice')
                        page.get_by_text('继续编辑',exact=True).click();page.evaluate("() => {window.AVFComponents?.markClean();closeModal()}")
                    case('dirty-form-esc-preserves-input',dirty)
                    def draft_restore():
                        page.locator('#global-new-project').click();page.locator('#source-title').fill('需恢复的来源')
                        page.keyboard.press('Escape');page.get_by_text('保留草稿并离开',exact=True).click()
                        page.locator('#global-new-project').click();page.get_by_text('恢复草稿',exact=True).click()
                        check(page.locator('#source-title').input_value()=='需恢复的来源','saved source draft lacks restore')
                        page.evaluate('() => AVFComponents.markClean()')
                    case('source-draft-save-reopen-restore',draft_restore)
                    def drawer_nested():
                        page.evaluate("() => {openDrawer('TEST DRAWER','抽屉测试','<label>绩效<input id=drawer-value></label><button id=nested-open>打开复核</button>');document.querySelector('#nested-open').onclick=()=>openModal('TEST','嵌套复核','<input id=nested-value>')}")
                        page.locator('#drawer-value').fill('999');page.keyboard.press('Escape')
                        check(page.locator('#unsaved-confirm').is_visible(),'drawer dirty silently lost')
                        page.get_by_text('继续编辑',exact=True).click();page.locator('#nested-open').click();page.keyboard.press('Escape')
                        check(page.evaluate("document.querySelector('.app-shell').inert && document.activeElement.id==='nested-open'"),'nested modal lost underlying drawer focus/inert')
                        for _ in range(8):
                            page.keyboard.press('Tab');check(page.evaluate("!!document.activeElement.closest('#drawer')"),'Tab escaped restored drawer')
                        page.keyboard.press('Escape');check(page.locator('#unsaved-confirm').is_visible(),'restored drawer dirty lost')
                    case('drawer-dirty-nested-modal-focus-restored',drawer_nested)
                    def dirty_navigation():
                        page.locator('#global-new-project').click();page.locator('#source-title').fill('导航保留')
                        page.evaluate("() => navigate('videos',{historyMode:'push'})")
                        check(page.locator('#unsaved-confirm').is_visible(),'navigation lost dirty source')
                        page.get_by_text('继续编辑',exact=True).click();check(page.locator('#source-title').input_value()=='导航保留','navigation overwrote form')
                    case('dirty-navigation-preserves-form',dirty_navigation)
                    def rejected_save():
                        page.locator('#global-new-project').click();page.locator('#source-title').fill('保存失败后保留');page.locator('#source-content').fill('真实表单输入用于隔离明确拒绝测试')
                        page.locator('#source-submit').click();page.wait_for_function("!document.querySelector('#source-submit').disabled")
                        check(page.locator('#source-title').input_value()=='保存失败后保留','rejection lost title')
                        check(page.locator('#source-content').input_value().startswith('真实表单'),'rejection lost content')
                    case('explicit-rejection-keeps-form-and-enables-save',rejected_save)
                    def footer_overlap():
                        page.evaluate("() => openDrawer('TEST','固定底部','<p>抽屉内容</p><div class=drawer-footer><button id=footer-submit class=primary-action>提交</button></div>')")
                        footer=page.locator('#footer-submit').bounding_box();check(footer and footer['y']+footer['height']<=900,'drawer footer outside viewport')
                        check(not page.locator('#studio-task-drawer').is_visible(),'task float competes with dialog')
                        page.evaluate("() => toast('隔离明确失败：不能遮挡固定提交',true)")
                        popup=page.locator('#toast').bounding_box()
                        check(popup and (popup['x']+popup['width']<=footer['x'] or footer['x']+footer['width']<=popup['x'] or popup['y']+popup['height']<=footer['y'] or footer['y']+footer['height']<=popup['y']),'toast covers fixed submit')
                    case('drawer-footer-visible-without-task-float',footer_overlap)
                    def restore_selection():
                        target=page.evaluate("() => {const p=state.projects.projects[1]||state.projects.projects[0];state.studioProjectId=p.project_id;state.studioStage='script';state.studioMasterIndex=1;AVFWorkbench.saveSelection();return p.project_id}")
                        page.reload(wait_until='domcontentloaded');page.wait_for_function("() => state.projects && state.projects.projects.length>0")
                        check(page.evaluate('state.studioProjectId')==target,'refresh lost project identity')
                        check(page.evaluate('state.studioStage')=='script','refresh lost selected stage')
                    case('workspace-selection-refresh-restored',restore_selection)
                    def error_details_guard():
                        page.locator('#global-new-project').click();page.locator('#source-title').fill('错误详情之前的修改')
                        page.evaluate("() => toast('隔离明确失败',true)");page.locator('.toast-detail').click()
                        check(page.locator('#unsaved-confirm').is_visible(),'error detail destroyed dirty editor')
                        check(page.locator('#source-title').input_value()=='错误详情之前的修改','error detail lost input')
                    case('error-details-preserve-dirty-editor',error_details_guard)
                    def nested_dirty_navigation():
                        page.evaluate("() => {openDrawer('TEST','底层表单','<input id=underlying-dirty>');document.querySelector('#underlying-dirty').value='dirty';openModal('READONLY','只读说明','<p>说明</p>')}")
                        # Wait for initial clean signatures, then change the underlying field.
                        page.evaluate("() => document.querySelector('#underlying-dirty').value='still dirty'")
                        page.evaluate("() => navigate('videos',{historyMode:'push'})")
                        check(page.locator('#unsaved-confirm').is_visible(),'clean overlay masks dirty drawer')
                        check(page.locator('#underlying-dirty').input_value()=='still dirty','nested navigation lost drawer input')
                    case('nested-dirty-drawer-navigation-protected',nested_dirty_navigation)
                    def brief_draft():
                        page.evaluate("() => editBrief(state.scripts.briefs.find(b=>b.status!=='approved'))")
                        headings=page.locator('[data-edit-brief-angle] [data-name=title]');check(headings.count()>=2,'fixture needs independent angles')
                        headings.nth(0).fill('恢复角度甲');headings.nth(1).fill('恢复角度乙')
                        radios=page.locator('[data-edit-brief-angle] input[type=radio]');radios.nth(1).check()
                        page.keyboard.press('Escape');page.get_by_text('保留草稿并离开',exact=True).click()
                        page.evaluate("() => editBrief(state.scripts.briefs.find(b=>b.status!=='approved'))")
                        page.get_by_text('恢复草稿',exact=True).click()
                        check(page.locator('[data-edit-brief-angle] [data-name=title]').nth(0).input_value()=='恢复角度甲','angle A restored into wrong field')
                        check(page.locator('[data-edit-brief-angle] [data-name=title]').nth(1).input_value()=='恢复角度乙','angle B restored into wrong field')
                        check(page.locator('[data-edit-brief-angle] input[type=radio]').nth(1).is_checked(),'recommended angle lost')
                    case('brief-multiple-angle-draft-restores-distinct-fields',brief_draft)
                    def character_scope():
                        page.evaluate("() => editCharacterBackground({character_id:'isolated-A',updated_at:'v1',background_description:'A original'})")
                        page.locator('#edit-character-background').fill('A draft');page.keyboard.press('Escape');page.get_by_text('保留草稿并离开',exact=True).click()
                        page.evaluate("() => editCharacterBackground({character_id:'isolated-B',updated_at:'v1',background_description:'B original'})")
                        check(page.locator('#saved-draft-hint').count()==0,'character A draft exposed for B')
                        check(page.locator('#edit-character-background').input_value()=='B original','cross-character draft overwrote B')
                    case('character-draft-isolated-by-object',character_scope)
                    def poll_draft():
                        page.evaluate("() => openVideoDrawer(state.videos.jobs.find(j=>j.job_id==='J-PAUSED-UI'))")
                        page.locator('#anchor-review-note').fill('轮询期间保留审核输入')
                        page.evaluate("() => {const data=structuredClone(state.videos);data.jobs.find(j=>j.job_id==='J-PAUSED-UI').updated_at='changed';patchVideoProductionData(data,state.productionHealth)}")
                        check(page.locator('#anchor-review-note').input_value()=='轮询期间保留审核输入','poll refresh erased dirty drawer')
                        page.evaluate("() => openModal('TEST','较新编辑器','<input id=newer-edit value=保留>')")
                        page.locator('#newer-edit').fill('较新编辑不丢失')
                        page.evaluate("() => {const data=structuredClone(state.videos);data.jobs.find(j=>j.job_id==='J-PAUSED-UI').updated_at='again';patchVideoProductionData(data,state.productionHealth)}")
                        check(not page.locator('#modal').evaluate('(e)=>e.hidden'),'poll closed newer modal')
                        check(page.locator('#newer-edit').input_value()=='较新编辑不丢失','poll erased newer modal')
                    case('poll-refresh-keeps-dirty-drawer-and-newer-modal',poll_draft)
                    def late_poll():
                        value=page.evaluate("""async()=>{const originalApi=api,originalVideos=state.videos;const waiting=[];try{state.videos=structuredClone(originalVideos);state.videos.jobs[0].state=state.videos.jobs[0].display_state='PRODUCTION_RUNNING';const initial=state.videos;api=path=>new Promise(resolve=>waiting.push({path,resolve}));const polling=pollActiveProduction();openModal('TEST','迟到轮询之后的编辑','<input id=late-poll-input value=原始>');await Promise.resolve();document.querySelector('#late-poll-input').value='保留较新输入';for(const r of waiting)r.resolve(r.path==='/api/videos'?{...initial,jobs:initial.jobs.map(j=>({...j,title:'迟到快照'}))}:state.productionHealth);await polling;return{visible:!document.querySelector('#modal').hidden,value:document.querySelector('#late-poll-input')?.value,unchanged:state.videos===initial,reads:waiting.length}}finally{api=originalApi;state.videos=originalVideos;clearTimeout(state.productionPollTimer)}}""")
                        check(value=={'visible':True,'value':'保留较新输入','unchanged':True,'reads':2},str(value))
                    case('late-poll-response-cannot-replace-newer-editor',late_poll)
                if batch in ('m2','all'):
                    go(page,'overview')
                    def settings():
                        page.evaluate("() => openProductionSettings(state.videos.jobs.find(j=>j.job_id==='J-E2E-SETUP'))")
                        page.wait_for_timeout(300)
                        panel=page.locator('#modal .modal');r=panel.bounding_box();check(r and r['width']>=800,'settings still small modal')
                        footer=page.locator('#save-production-settings');r=footer.bounding_box();check(r and r['y']+r['height']<=900,'settings action not fixed/visible')
                        check(page.get_by_text('已发生费用',exact=False).count()>0,'cost history absent')
                    case('settings-wide-fixed-budget-action',settings)
                    def content_impact():
                        page.evaluate("() => editScript(state.scripts.drafts.find(d=>d.status!=='approved'))")
                        page.locator('[data-name=visual_intent]').first.fill('改为仓库调度场景')
                        check('涉及场次' in page.locator('#script-change-impact').inner_text(),'script change impact missing')
                        check('费用尚未重估' in page.locator('#script-change-impact').inner_text(),'changes falsely reuse budget')
                        panel=page.locator('#modal .modal').bounding_box();check(panel and panel['width']>=880,'editor still narrow')
                    case('script-change-impact-wide-workspace',content_impact)
                    def preflight_rejection():
                        page.evaluate("() => confirmProduction(state.videos.jobs.find(j=>j.job_id==='J-E2E-SETUP'))")
                        check(page.locator('#preflight-retry').is_visible(),'failed preflight lacks recovery')
                        check(page.locator('#produce-confirm').count()==0,'failed preflight permits paid generate')
                        check('当前费用与执行规格尚未确认' in page.locator('#modal-body').inner_text(),'failed preflight not explicit')
                    case('failed-offline-preflight-blocks-paid-confirmation',preflight_rejection)
                if batch in ('m3','all'):
                    go(page,'overview')
                    def final_review():
                        page.evaluate("() => openVideoReview(state.videos.jobs.find(j=>j.job_id==='J-E2E-REVIEW'),'approved')")
                        page.wait_for_timeout(300);check(page.locator('#modal video').count()==1,'no same-screen final player')
                        check(page.locator('#review-check-all').count()==0,'formal review still has all-pass shortcut')
                        check(page.locator('#modal [data-review-check]').count()==6,'quality review items missing')
                        check(page.locator('#modal video').get_attribute('src').startswith('/api/videos/'),'player not bound to actual job')
                        page.wait_for_function("document.querySelector('#modal video').readyState>=2")
                        page.locator('#modal video').evaluate('(v)=>v.currentTime=1')
                        for i in range(6):page.locator('[data-review-check]').nth(i).check()
                        page.locator('#review-submit').click();page.wait_for_timeout(200)
                        check(page.locator('#modal video').evaluate('(v)=>v.currentTime')>=.9,'rejection reset playback')
                        check(page.locator('[data-review-check]:checked').count()==6,'rejection lost quality checks')
                    case('same-screen-final-review-six-items-no-bulk-pass',final_review)
                    page.evaluate("() => {window.AVFComponents?.markClean();closeModal()}")
                if batch=='all':
                    for key in ('overview','todos','projects','scripts','videos','publishing','characters','config'):
                        def readable(k=key):
                            go(page,k)
                            check(page.locator('body').inner_text().strip()!='','empty page')
                            sizes=page.locator('button,label,input,select,textarea').evaluate_all("els=>els.filter(e=>{const r=e.getBoundingClientRect();return r.width&&r.height&&r.x>=0&&r.right<=innerWidth&&r.y>=0&&r.bottom<=innerHeight&&((e.innerText||'').trim()||['INPUT','SELECT','TEXTAREA'].includes(e.tagName))}).map(e=>parseFloat(getComputedStyle(e).fontSize)).filter(n=>n>0)")
                            check(not any(n<14 for n in sizes),f'tiny interactive text {sizes}')
                        case('readable-'+key,readable)
            finally:browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        costs=database.execute('SELECT COALESCE(SUM(cost_cents),0) FROM provider_calls').fetchone()[0]
        calls=database.execute('SELECT COUNT(*) FROM provider_calls').fetchone()[0];after_dump='\n'.join(database.iterdump());(work_root/'after.sql').write_text(after_dump,encoding='utf-8');database.close()
    head=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
    summary={'batch':batch,'source_head':head,'isolated_runtime':str(runtime),'paid_provider_calls':calls,'provider_cost_cents':costs,'external_requests':len(external),'business_invariant_changes':before_dump!=after_dump,'page_errors':errors,'business_mutations_attempted':[r for r in requests if r['method'] not in ('GET','HEAD')],'mutation_result':'all rejected by isolated browser route; no command reached server','dimensions':dimensions,'screenshots':screenshots,'scenarios':results,'status':'passed' if results and all(r['status']=='passed' for r in results) and not errors and not calls and not external and before_dump==after_dump else 'failed'}
    (work_root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work-root',type=Path,required=True);p.add_argument('--batch',choices=('m0','m1','m2','m3','all'),default='all');args=p.parse_args()
    result=run(args.work_root.resolve(),args.batch);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['status']=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
