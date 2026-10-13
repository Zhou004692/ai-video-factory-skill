"""Exercise the real bounded JSON client with deterministic transport boundaries."""
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
CLIENT=ROOT/'scripts/management_ui/api-client.js'

class ManagementRequestClientTests(unittest.TestCase):
    def evaluate(self,source):
        self.assertTrue(CLIENT.is_file(),'JSON requests have no bounded client')
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable for JavaScript unit boundary')
        script="globalThis.window=globalThis;const localStore=new Map();globalThis.sessionStorage={getItem:k=>localStore.get(k)??null,setItem:(k,v)=>localStore.set(k,v)};require('vm').runInThisContext(require('fs').readFileSync(process.argv[1],'utf8'));(async()=>{"+source+"})().then(r=>console.log(JSON.stringify(r))).catch(e=>{console.error(e);process.exitCode=1});"
        result=subprocess.run([node,'-e',script,str(CLIENT)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)
    def test_queries_finish_with_recoverable_timeout(self):
        value=self.evaluate("fetch=(_p,o)=>new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))));try{await AVFRequest.request('/api/videos',{timeoutMs:100})}catch(e){return{code:e.code,unknown:e.submissionUnknown}}")
        self.assertEqual(value,{'code':'query_timeout','unknown':False})
    def test_paid_submission_timeout_blocks_duplicate_transport(self):
        value=self.evaluate("let calls=0;fetch=(_p,o)=>{calls++;return new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))))};const errors=[];for(let i=0;i<2;i++){try{await AVFRequest.request('/api/videos/job/start',{method:'POST',body:'{}',timeoutMs:100})}catch(e){errors.push(e.code)}}return{calls,errors}")
        self.assertEqual(value,{'calls':1,'errors':['submission_unknown','submission_unknown']})
    def test_superseded_query_cannot_deliver_stale_result(self):
        value=self.evaluate("let calls=0;fetch=(_p,o)=>new Promise((resolve,reject)=>{const n=++calls;o.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})));setTimeout(()=>resolve({ok:true,status:200,json:async()=>({n})}),n===1?80:5)});const first=AVFRequest.request('/api/videos',{requestKey:'workbench',timeoutMs:1000}).catch(e=>({code:e.code}));const second=await AVFRequest.request('/api/projects',{requestKey:'workbench',timeoutMs:1000});return{first:await first,second}")
        self.assertEqual(value,{'first':{'code':'request_superseded'},'second':{'n':2}})
    def test_rejected_validation_does_not_become_unknown_submission(self):
        value=self.evaluate("fetch=async()=>({ok:false,status:409,json:async()=>({error:'版本已变更'})});try{await AVFRequest.request('/api/videos/job/start',{method:'POST',body:'{}'})}catch(e){return{code:e.code,status:e.status,unknown:e.submissionUnknown,message:e.message}}")
        self.assertEqual(value,{'code':'http_error','status':409,'unknown':False,'message':'版本已变更'})
    def test_reconciliation_requires_known_submission_evidence(self):
        value=self.evaluate("fetch=(_p,o)=>new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))));try{await AVFRequest.request('/api/videos/job/start',{method:'POST',submissionBaseline:{receipt_id:'old'},timeoutMs:100})}catch{};const pendingCommand=AVFRequest.pending()[0];const refused=AVFRequest.markReconciled('/api/videos/job/start',{submission_known:true,submission_evidence:{receipt_id:'new-but-unrelated'}});const accepted=AVFRequest.markReconciled('/api/videos/job/start',{submission_known:true,command_id:pendingCommand.command_id,path:pendingCommand.path,request_sha256:pendingCommand.request_sha256,outcome:'accepted'});return{refused,accepted,pending:AVFRequest.pending().length}")
        self.assertEqual(value,{'refused':False,'accepted':True,'pending':0})
    def test_paid_budget_and_produce_block_other_entry_for_same_job(self):
        for path in ['/api/videos/job/budget-confirmation','/api/videos/job/produce','/api/videos/job/retry-network-failures','/api/videos/job/anchors/approve','/api/videos/job/execution-contract/repair']:
            with self.subTest(path=path):
                value=self.evaluate("let calls=0;fetch=(_p,o)=>{calls++;return new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted'))))};const errors=[];try{await AVFRequest.request("+json.dumps(path)+",{method:'POST',timeoutMs:100})}catch(e){errors.push(e.code)};try{await AVFRequest.request('/api/videos/job/resume',{method:'POST',timeoutMs:100})}catch(e){errors.push(e.code)};return{calls,errors}")
                self.assertEqual(value,{'calls':1,'errors':['submission_unknown','submission_unknown']})
    def test_native_preparation_unknown_does_not_deadlock_paid_confirmation(self):
        value=self.evaluate("let calls=0;fetch=(_p,o)=>{calls++;if(calls>1)return Promise.resolve({ok:true,status:200,json:async()=>({ok:true})});return new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted'))))};let code;try{await AVFRequest.request('/api/videos/job/masters/1/extend',{method:'POST',timeoutMs:100})}catch(e){code=e.code};const accepted=await AVFRequest.request('/api/videos/job/produce',{method:'POST',timeoutMs:100});return{code,calls,accepted,pending:AVFRequest.pending().length}")
        self.assertEqual(value,{'code':'operation_unknown','calls':2,'accepted':{'ok':True},'pending':0})
    def test_publication_disconnect_is_unknown_and_not_reposted(self):
        value=self.evaluate("let calls=0;fetch=(_p,o)=>{calls++;return new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted'))))};const codes=[];for(let i=0;i<2;i++){try{await AVFRequest.request('/api/videos/job/publish/douyin',{method:'POST',timeoutMs:100})}catch(e){codes.push(e.code)}}return{codes,calls}")
        self.assertEqual(value,{'codes':['submission_unknown','submission_unknown'],'calls':1})
    def test_command_identity_hashes_exact_utf8_body(self):
        body='{"title":"同框宣传", "fraction":1e-07,"budget":1234}'
        value=self.evaluate("let headers;fetch=async(_p,o)=>{headers=o.headers;return{ok:true,status:200,json:async()=>({})}};await AVFRequest.request('/api/videos/job/produce',{method:'POST',body:"+json.dumps(body,ensure_ascii=False)+"});return headers")
        self.assertEqual(value['X-AVF-Command-Hash'],hashlib.sha256(body.encode('utf-8')).hexdigest())
        self.assertRegex(value['X-AVF-Command-Id'],r'^[0-9a-f-]{36}$')
    def test_inflight_command_identity_survives_refresh_and_known_result_clears_it(self):
        value=self.evaluate("let stored,finish;globalThis.sessionStorage={getItem:()=>stored?JSON.stringify(stored):null,setItem:(_k,v)=>stored=JSON.parse(v)};fetch=()=>new Promise(resolve=>finish=resolve);const request=AVFRequest.request('/api/videos/job/produce',{method:'POST',body:'{\\\"confirmed\\\":true}',timeoutMs:1000});while(!finish)await new Promise(r=>setTimeout(r,1));const saved=stored?.[0],inflight=AVFRequest.pending().length;finish({ok:true,status:200,json:async()=>({ok:true})});await request;return{inflight,identity:!!saved?.command_id&&!!saved?.request_sha256,bodySaved:!!saved?.body,remaining:AVFRequest.pending().length,stored:stored.length}")
        self.assertEqual(value,{'inflight':1,'identity':True,'bodySaved':False,'remaining':0,'stored':0})
    def test_exact_command_reconciliation_rejects_mismatched_or_unfinished_proof(self):
        value=self.evaluate("fetch=(_p,o)=>new Promise((_r,reject)=>o.signal.addEventListener('abort',()=>reject(Error('aborted'))));try{await AVFRequest.request('/api/videos/job/produce',{method:'POST',timeoutMs:100})}catch{};const p=AVFRequest.pending()[0],base={...p,submission_known:true,outcome:'accepted'};const refused=[{...base,command_id:'other'},{...base,request_sha256:'other'},{...base,path:'/api/videos/other/produce'},{...base,outcome:'pending'}].map(proof=>AVFRequest.markReconciled(p.path,proof));return{refused,remaining:AVFRequest.pending().length}")
        self.assertEqual(value,{'refused':[False]*4,'remaining':1})
    def test_failed_identity_persistence_blocks_paid_transport_before_any_effect(self):
        for kind in ('throw','missing','discard'):
            with self.subTest(kind=kind):
                setup={'throw':"sessionStorage.setItem=()=>{throw Error('quota exceeded')}",'missing':'globalThis.sessionStorage=null','discard':'sessionStorage.setItem=()=>{}'}[kind]
                value=self.evaluate(setup+";let calls=0;fetch=async()=>{calls++;return{ok:true,status:200,json:async()=>({})}};try{await AVFRequest.request('/api/videos/job/produce',{method:'POST',body:'{}'})}catch(e){return{code:e.code,calls,pending:AVFRequest.pending().length,unknown:e.submissionUnknown}};return{code:'success',calls,pending:AVFRequest.pending().length,unknown:false}")
                self.assertEqual(value,{'code':'command_identity_unavailable','calls':0,'pending':0,'unknown':False})

if __name__=='__main__':unittest.main()
