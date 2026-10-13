/* Bounded JSON requests. A browser timeout never means a paid command was cancelled. */
(function(global){
  'use strict';
  const reads=new Map(), writes=new Map(), uncertain=new Map();
  let observer=()=>{};
  const storageKey='avf_unconfirmed_commands_v1';
  try{for(const item of JSON.parse(global.sessionStorage?.getItem(storageKey)||'[]'))if(item.path&&(paidPath(item.path)||publicationPath(item.path)))uncertain.set(item.path,item)}catch{}
  function persist(){try{global.sessionStorage?.setItem(storageKey,JSON.stringify([...uncertain.values()]))}catch{}}
  function notify(record,phase,extra={}){if(record.feedback===false)return;try{observer({path:record.path,method:record.method,phase,at:new Date().toISOString(),...extra})}catch{}}
  class RequestError extends Error{
    constructor(message,code,{status=0,submissionUnknown=false,details=null}={}){super(message);this.name='RequestError';this.code=code;this.status=status;this.submissionUnknown=submissionUnknown;this.details=details}
  }
  const jobScope=path=>path.match(/^\/api\/videos\/[^/]+(?:\/|$)/)?.[0].replace(/\/$/,'');
  function paidPath(path){return /^\/api\/videos\/[^/]+\/(?:start|produce|resume|budget-confirmation|retry|retry-network-failures|anchors\/approve|execution-contract\/repair|doctor\/repair)(?:\/|$)/.test(path)}
  function publicationPath(path){return /^\/api\/videos\/[^/]+\/publish\/douyin$/.test(path)}
  async function commandDigest(body){const bytes=new TextEncoder().encode(String(body??''));const hash=await global.crypto.subtle.digest('SHA-256',bytes);return [...new Uint8Array(hash)].map(byte=>byte.toString(16).padStart(2,'0')).join('')}
  function request(path,options={}){
    const method=String(options.method||'GET').toUpperCase(),mutation=!['GET','HEAD'].includes(method);
    const pathname=new URL(path,global.location?.href||'http://local/').pathname;
    const kind=publicationPath(pathname)?'publication':'generation',paid=mutation&&(options.paidCommand===true||paidPath(pathname)||publicationPath(pathname));
    if(paid&&[...uncertain.keys()].some(path=>jobScope(path)===jobScope(pathname))){const failure=new RequestError('提交结果仍待核对，请先核对当前任务；不会重复发送生成或投稿请求。','submission_unknown',{submissionUnknown:true});notify({path:pathname,method},'unknown',{message:failure.message});return Promise.reject(failure)}
    if(paid&&[...writes.values()].some(record=>record.paid&&jobScope(record.path)===jobScope(pathname)))return Promise.reject(new RequestError('当前任务正在提交，请等待结果。','submission_pending'));
    if(mutation&&writes.has(pathname))return Promise.reject(new RequestError('当前操作正在提交，请等待结果。','submission_pending'));
    const key=options.requestKey,controller=new AbortController();
    if(key&&reads.has(key)){const older=reads.get(key);older.superseded=true;older.controller.abort()}
    const record={path:pathname,method,paid,kind,feedback:options.feedback,controller,superseded:false,timedOut:false};
    if(key)reads.set(key,record);if(mutation)writes.set(pathname,record);
    const {timeoutMs=mutation?60000:15000,requestKey,paidCommand,submissionBaseline,feedback,...init}=options;
    const externalSignal=init.signal;
    const cancel=()=>controller.abort();if(externalSignal){if(externalSignal.aborted)controller.abort();else externalSignal.addEventListener('abort',cancel,{once:true})}
    init.signal=controller.signal;
    let deadline,waiting;notify(record,'loading');
    const timeout=new Promise((_resolve,reject)=>{deadline=setTimeout(()=>{record.timedOut=true;controller.abort();reject(new Error('request deadline exceeded'))},Math.max(1,Number(timeoutMs)||15000))});
    waiting=setTimeout(()=>notify(record,'waiting'),2000);
    const transport=async()=>{
      if(paid){record.command_id=global.crypto.randomUUID();record.request_sha256=await commandDigest(init.body);if(record.timedOut||record.controller.signal.aborted)throw new RequestError('请求尚未发送，请重新确认当前参数。','command_not_sent');init.headers={...(init.headers||{}),'X-AVF-Command-Id':record.command_id,'X-AVF-Command-Hash':record.request_sha256};uncertain.set(pathname,{path:pathname,kind,command_id:record.command_id,request_sha256:record.request_sha256,at:new Date().toISOString(),baseline_captured:submissionBaseline!==undefined,baseline_receipt_id:submissionBaseline?.receipt_id??null});persist()}
      record.transportStarted=true;
      const response=await global.fetch(path,init);
      const data=await response.json();
      if(record.superseded)throw new RequestError('当前读取已被新的对象选择替代。','request_superseded');
      if(!response.ok){
        if(paid&&response.status>=500)throw new RequestError('服务返回异常，付费提交结果需要核对。','submission_unknown',{status:response.status,submissionUnknown:true});
        throw new RequestError(data.error||'请求失败','http_error',{status:response.status,details:data});
      }
      return data;
    };
    const clearKnown=()=>{if(uncertain.get(pathname)?.command_id===record.command_id){uncertain.delete(pathname);persist()}};
    return Promise.race([transport(),timeout]).then(data=>{clearKnown();notify(record,'success');return data}).catch(error=>{
      if(record.superseded)throw new RequestError('当前读取已被新的对象选择替代。','request_superseded');
      if(error.code==='http_error'){clearKnown();notify(record,'error',{message:error.message});throw error}
      if(paid&&!record.transportStarted){const failure=new RequestError('请求尚未发送，请重新确认当前参数。','command_not_sent');notify(record,'error',{message:failure.message});throw failure}
      if(paid){
        uncertain.set(pathname,{path:pathname,kind,command_id:record.command_id,request_sha256:record.request_sha256,at:new Date().toISOString(),baseline_captured:submissionBaseline!==undefined,baseline_receipt_id:submissionBaseline?.receipt_id??null});persist();
        const failure=new RequestError(kind==='publication'?'投稿结果尚未确认，请核对当前渠道记录；不会自动重复投稿。':'提交结果尚未确认，请核对当前任务后再决定下一步；不会自动重新扣费。','submission_unknown',{status:error.status||0,submissionUnknown:true});
        notify(record,'unknown',{message:failure.message});throw failure;
      }
      const failure=error instanceof RequestError?error:mutation?new RequestError('操作结果尚未确认，请先核对已有记录；当前输入已保留。','operation_unknown'):new RequestError(record.timedOut?'读取超时，已保留当前内容，请重新读取。':'连接中断，已保留当前内容，请重新读取。',record.timedOut?'query_timeout':'query_network');
      notify(record,'error',{message:failure.message});throw failure;
    }).finally(()=>{
      clearTimeout(deadline);clearTimeout(waiting);externalSignal?.removeEventListener('abort',cancel);
      if(key&&reads.get(key)===record)reads.delete(key);if(writes.get(pathname)===record)writes.delete(pathname);
    });
  }
  function markReconciled(path,proof){const pending=uncertain.get(path);if(!pending||proof?.submission_known!==true)return false;if(pending.command_id){if(proof.command_id!==pending.command_id||proof.path!==pending.path||proof.request_sha256!==pending.request_sha256||!['accepted','rejected'].includes(proof.outcome))return false}else if(pending.baseline_captured!==true||!proof.submission_evidence?.receipt_id||proof.submission_evidence.receipt_id===pending.baseline_receipt_id)return false;const changed=uncertain.delete(path);persist();return changed}
  global.AVFRequest=Object.freeze({request,RequestError,markReconciled,pending:()=>[...uncertain.values()],setObserver:callback=>{observer=typeof callback==='function'?callback:()=>{}}});
})(globalThis);
