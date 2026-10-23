"""Run generation UI handlers with deterministic timers and transport, without a browser."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

APP = Path(__file__).resolve().parents[1] / 'scripts/management_ui/app.js'


class ScriptGenerationUiTests(unittest.TestCase):
    def evaluate(self, source):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        harness = r"""
const fs=require('fs'),vm=require('vm');
const app=fs.readFileSync(process.argv[1],'utf8');
globalThis.state={page:'scripts',studioProjectId:'project-a',loadSequence:1,scripts:{generation_runs:[]}};
const timers=new Map(),elements={},calls=[],messages=[];let timerId=0,modalHtml='',epoch=1;
globalThis.setTimeout=(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id};
globalThis.clearTimeout=id=>timers.delete(id);
globalThis.document={visibilityState:'visible'};
globalThis.AVFComponents={epoch:()=>epoch,hasDirty:()=>false};
globalThis.$=id=>elements[id]||null;globalThis.$$=()=>[];
globalThis.esc=v=>String(v??'');globalThis.summarizeError=v=>v;
globalThis.toast=(message,error)=>messages.push({message,error:!!error});
globalThis.openModal=(_label,_title,html)=>{modalHtml=html;elements['#resume-script-generation']={};elements['#script-unknown-confirm']={checked:false}};
globalThis.closeModal=()=>{};globalThis.loadPage=async()=>true;
globalThis.renderScripts=()=>{};globalThis.renderStudio=()=>{};globalThis.renderGlobalProjects=()=>{};
globalThis.api=async(url,options={})=>{calls.push({url,options});return {}};
vm.runInThisContext(app.slice(app.indexOf('function scriptGenerationRuns('),app.indexOf('const confirmBriefAndGenerateScripts=')));
"""
        result = subprocess.run([node, '-e', harness + '(async()=>{' + source + '})().then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1})', str(APP)], capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_refresh_restores_progress_and_polls_only_get_without_overlap(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'persisted',brief_id:'b',status:'running',completed_count:1,target_count:3,active_variant:2}];
let finish;api=(url,options={})=>{calls.push({url,method:options.method||'GET'});return new Promise(resolve=>finish=resolve)};
scheduleScriptGenerationPoll();const scheduled=[...timers.values()][0];
const first=pollScriptGenerationRuns();await pollScriptGenerationRuns();
finish({...state.scripts.generation_runs[0],active_variant:3});await first;
return {calls,delay:scheduled.ms,text:scriptGenerationProgress(state.scripts.generation_runs[0]),timers:timers.size};
""")
        self.assertEqual(value['calls'], [{'url': '/api/script-generation-runs/persisted', 'method': 'GET'}])
        self.assertGreaterEqual(value['delay'], 3000)
        self.assertIn('1/3', value['text'])
        self.assertIn('3/3', value['text'])
        self.assertEqual(value['timers'], 1)

    def test_status_failure_stops_polling_and_never_resubmits(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'r',brief_id:'b',status:'running',completed_count:1,target_count:3}];
api=async(url,options={})=>{calls.push({url,method:options.method||'GET'});throw Error('network unavailable')};
await pollScriptGenerationRuns();scheduleScriptGenerationPoll();
return {calls,timers:timers.size,error:state.scriptGenerationPollError};
""")
        self.assertEqual(len(value['calls']), 1)
        self.assertEqual(value['calls'][0]['method'], 'GET')
        self.assertEqual(value['timers'], 0)
        self.assertIn('network unavailable', value['error'])

    def test_unknown_resume_requires_explicit_fee_confirmation(self):
        value = self.evaluate(r"""
const run={run_id:'r',brief_id:'b',status:'unknown',completed_count:1,target_count:3,can_resume:true,requires_unknown_confirmation:true};
state.scripts.generation_runs=[run];api=async(url,options={})=>{calls.push({url,body:JSON.parse(options.body)});return {...run,status:'queued'}};
confirmResumeScriptGeneration('r');const warning=modalHtml.includes('重复')&&modalHtml.includes('费用');
await elements['#resume-script-generation'].onclick();const before=calls.length;
elements['#script-unknown-confirm'].checked=true;await elements['#resume-script-generation'].onclick();
return {warning,before,calls};
""")
        self.assertTrue(value['warning'])
        self.assertEqual(value['before'], 0)
        self.assertEqual(value['calls'], [{'url': '/api/script-generation-runs/r/resume', 'body': {'confirmed': True, 'confirm_unknown': True}}])

    def test_partial_resume_only_requests_missing_versions(self):
        value = self.evaluate(r"""
const run={run_id:'r',brief_id:'b',status:'partial',completed_count:2,target_count:3,can_resume:true,error_message:'第三版失败'};
state.scripts.generation_runs=[run];api=async(url,options={})=>{calls.push(JSON.parse(options.body));return {...run,status:'queued'}};
confirmResumeScriptGeneration('r');const saved=modalHtml.includes('2/3');await elements['#resume-script-generation'].onclick();
return {saved,calls};
""")
        self.assertEqual(value, {'saved': True, 'calls': [{'confirmed': True}]})

    def test_partial_run_is_not_labeled_fully_generated_or_recommended(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'r',brief_id:'b',status:'partial',completed_count:1,target_count:3,recommendation_ready:false}];
return {label:scriptGenerationBriefLabel({brief_id:'b',status:'approved'}),recommended:scriptRecommendationIndex([{brief_id:'b',variant_index:0,recommended_index:null}])};
""")
        self.assertIn('1/3', value['label'])
        self.assertNotIn('已生成剧本', value['label'])
        self.assertEqual(value['recommended'], -1)

    def test_navigation_discards_stale_status_response(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'r',brief_id:'b',status:'running',completed_count:0,target_count:3}];
let finish;api=()=>new Promise(resolve=>finish=resolve);const pending=pollScriptGenerationRuns();
state.studioProjectId='project-b';epoch++;finish({run_id:'r',brief_id:'b',status:'completed',completed_count:3,target_count:3});await pending;
return {completed:state.scripts.generation_runs[0].completed_count};
""")
        self.assertEqual(value, {'completed': 0})

    def test_terminal_run_refreshes_saved_drafts_then_stops_polling(self):
        value = self.evaluate(r"""
const run={run_id:'r',brief_id:'b',status:'running',completed_count:2,target_count:3};state.scripts.generation_runs=[run];
api=async(url,options={})=>{calls.push({url,method:options.method||'GET'});if(url.includes('/script-generation-runs/'))return {...run,status:'completed',completed_count:3};if(url==='/api/scripts')return {generation_runs:[{...run,status:'completed',completed_count:3}],drafts:[1,2,3]};return {projects:[]}};
await pollScriptGenerationRuns();return {calls,timers:timers.size,drafts:state.scripts.drafts.length};
""")
        self.assertEqual(value['drafts'], 3)
        self.assertEqual(value['timers'], 0)
        self.assertEqual([call['url'] for call in value['calls']], ['/api/script-generation-runs/r', '/api/scripts', '/api/projects'])
        self.assertTrue(all(call['method'] == 'GET' for call in value['calls']))

    def test_submission_timeout_refresh_never_reposts_or_reopens_paid_submission(self):
        value = self.evaluate(r"""
const button={disabled:false};elements['#modal']={classList:{add(){}}};elements['#confirm-generate-scripts']=button;
for(const id of ['duration','shots','characters'])elements['#script-suggest-'+id]={value:''};
let refreshes=0;loadPage=async()=>{refreshes++;return true};api=async(url,options)=>{calls.push({url,body:JSON.parse(options.body),timeout:options.timeoutMs});throw Error('操作结果尚未确认')};
confirmGenerateScripts('b',0,1);await button.onclick();await button.onclick();confirmGenerateScripts('b',0,1);
return {calls,refreshes,unknown:state.scriptGenerationSubmissions?.b};
""")
        self.assertEqual(len(value['calls']), 1)
        self.assertEqual(value['calls'][0]['timeout'], 15000)
        self.assertEqual(value['refreshes'], 1)
        self.assertEqual(value.get('unknown'), 'unknown')

    def test_edited_brief_opens_fresh_confirmation_despite_old_failed_run(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'old',brief_id:'b',brief_revision:1,status:'failed',can_resume:false}];
elements['#modal']={classList:{add(){}}};elements['#confirm-generate-scripts']={};
confirmGenerateScripts('b',0,2);return {guidance:modalHtml.includes('script-suggest-duration'),calls};
""")
        self.assertEqual(value, {'guidance': True, 'calls': []})

    def test_switching_project_restores_terminal_progress_without_a_page_load(self):
        value = self.evaluate(r"""
state.page='overview';state.studioStage='brief';state.projects={projects:[{project_id:'project-b',brief:{brief_id:'b'},source:{source_type:'manual'},quality_gate:{score:100}}]};
state.scripts.generation_runs=[{run_id:'r',brief_id:'b',status:'failed',completed_count:1,target_count:3,error_message:'模型服务暂不可用',can_resume:true}];
let html='';elements['#studio-stage-body']={querySelector:()=>null,insertAdjacentHTML:(_where,value)=>html=value};
for(const id of ['#studio-project-code','#studio-project-title','#studio-project-state','#studio-project-meta'])elements[id]={};
globalThis.studioRelated=()=>({});globalThis.stageAvailability=()=>({brief:true});globalThis.sourceTypeLabel=v=>v;globalThis.projectStageLabel=v=>v;
globalThis.renderStudioStage=()=>{};globalThis.renderStudioInspector=()=>{};globalThis.renderStudioPrimaryAction=()=>{};globalThis.renderStudioDoctorAction=()=>{};
vm.runInThisContext(app.slice(app.indexOf('function renderStudioProject('),app.indexOf('function studioTextPanel(')));
state.studioProjectId='project-b';renderStudioProject(state.projects.projects[0]);
return {saved:html.includes('1/3'),failure:html.includes('模型服务暂不可用'),resume:html.includes('data-script-run-resume'),calls};
""")
        self.assertEqual(value, {'saved': True, 'failure': True, 'resume': True, 'calls': []})

    def test_old_completed_run_is_hidden_after_brief_revision_changes(self):
        value = self.evaluate(r"""
const brief={brief_id:'b',revision:1,approved_angle_index:0,status:'approved'};
state.scripts.briefs=[brief];state.scripts.generation_runs=[{run_id:'old',brief_id:'b',brief_revision:1,angle_index:0,status:'completed',completed_count:3,target_count:3}];
let html='';elements['#page-scripts']={querySelector:()=>({remove:()=>html=''}),insertAdjacentHTML:(_where,value)=>html=value};
renderScriptGenerationProgress();const previous=html.includes('3/3');brief.revision=2;brief.status='candidate';renderScriptGenerationProgress();
return {previous,html,label:scriptGenerationBriefLabel(brief)};
""")
        self.assertTrue(value['previous'])
        self.assertEqual(value['html'], '')
        self.assertNotIn('3/3', value['label'])

    def test_recommendations_are_matched_to_each_guidance_fingerprint(self):
        value = self.evaluate(r"""
state.scripts.generation_runs=[{run_id:'new',brief_id:'b',request_fingerprint:'new-guidance',recommendation_ready:false},{run_id:'old',brief_id:'b',request_fingerprint:'old-guidance',recommendation_ready:true}];
return {old:scriptRecommendationIndex([{brief_id:'b',request_fingerprint:'old-guidance',variant_index:0,recommended_index:0}]),fresh:scriptRecommendationIndex([{brief_id:'b',request_fingerprint:'new-guidance',variant_index:0,recommended_index:0}])};
""")
        self.assertEqual(value, {'old': 0, 'fresh': -1})


if __name__ == '__main__':
    unittest.main()
