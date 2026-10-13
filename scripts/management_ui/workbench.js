/* Workspace chrome and persistent request feedback, independent of production state. */
(function(global){
  'use strict';
  const failures=new Map(),checkingCommands=new Map();let lastSync='';
  const status=document.createElement('div');status.id='request-status';status.className='request-status';status.setAttribute('role','status');status.setAttribute('aria-live','polite');status.hidden=true;
  status.innerHTML='<span></span><button type="button" hidden>重新读取</button>';document.body.prepend(status);
  status.querySelector(':scope > button').onclick=()=>{if(typeof loadPage==='function')loadPage(state.page,true)};
  function selectedPending(){const all=global.AVFRequest.pending(),id=document.querySelector('[data-uncertain-submit]')?.dataset.commandId;return all.find(p=>p.command_id===id)||all[0]}
  function pendingSelector(alert){
    let label=alert.querySelector('[data-pending-label]');if(!label){label=document.createElement('label');label.dataset.pendingLabel='true';label.textContent='选择待核对请求 ';const select=document.createElement('select');select.dataset.pendingSelector='true';select.dataset.browseOnly='true';select.setAttribute('aria-label','选择待核对请求');label.append(select);alert.append(label);
      select.onchange=()=>{const details=alert.querySelector('[data-command-resolution]'),dirty=details&&[...details.querySelectorAll('input,select,textarea')].some(e=>e.type==='checkbox'?e.checked:!!e.value.trim());if(dirty){select.value=alert.dataset.commandId;details.querySelector('[data-resolution-error]').textContent='请先保存核对结论或清空当前核对输入，再切换请求；现有输入保留。';return}alert.dataset.commandId=select.value;details?.remove();checkCommands()};
    }
    const all=global.AVFRequest.pending(),select=label.querySelector('select');label.hidden=all.length<2;select.replaceChildren();for(const pending of all){const id=decodeURIComponent(pending.path.match(/^\/api\/videos\/([^/]+)/)?.[1]||'');const job=typeof state!=='undefined'?(state.videos?.jobs||[]).find(j=>j.job_id===id):null;const option=document.createElement('option');option.value=pending.command_id;option.textContent=(job?.title||id||'已保存请求')+' · '+(pending.kind==='publication'?'投稿':'生成');select.append(option)}select.value=alert.dataset.commandId;
  }
  function modalUnknown(message,path){const body=!document.querySelector('#modal')?.hidden?document.querySelector('#modal-body'):document.querySelector('#drawer')?.classList.contains('open')?document.querySelector('#drawer-body'):status;let alert=document.querySelector('[data-uncertain-submit]');if(alert){if(!body.contains(alert))body.prepend(alert);delete alert.dataset.resolved;alert.querySelector('[data-cancel-unclaimed]').hidden=true}if(!alert){alert=document.createElement('div');alert.dataset.uncertainSubmit='true';alert.className='persistent-alert';const text=document.createElement('span'),button=document.createElement('button'),cancel=document.createElement('button');button.type=cancel.type='button';button.className=cancel.className='mini-action';button.textContent='核对已有任务与渠道记录';cancel.textContent='取消未受理请求';cancel.hidden=true;cancel.dataset.cancelUnclaimed='true';button.onclick=async()=>{button.disabled=true;try{await checkCommands();const videos=await api('/api/videos');reconcile(videos);const publishing=await api('/api/publishing');reconcilePublications(publishing)}catch{}finally{if(button.isConnected)button.disabled=false}};cancel.onclick=async()=>{const pending=global.AVFRequest.pending().find(p=>p.command_id===alert.dataset.commandId);if(!pending)return;cancel.disabled=true;try{const proof=await api('/api/commands/'+encodeURIComponent(pending.command_id)+'/cancel-unclaimed',{method:'POST',feedback:false,body:JSON.stringify({path:pending.path,request_sha256:pending.request_sha256})});if(global.AVFRequest.markReconciled(pending.path,proof)){failures.delete(pending.path);clearResolvedAliases();alert.querySelector('span').textContent='此请求尚未受理，已阻止它今后执行。当前输入保留；重新生成或投稿仍需明确确认。';cancel.hidden=true}}catch(error){alert.querySelector('span').textContent=error.details?.code==='submission_pending'?'请求已受理，不能取消；请核对已有任务，当前素材和输入保留。':error.message}finally{if(cancel.isConnected)cancel.disabled=false}};alert.append(text,button,cancel);body.prepend(alert)}alert.querySelector('span').textContent=message;const scope=path?.match(/^\/api\/videos\/[^/]+/)?.[0],pending=selectedPending()||global.AVFRequest.pending().find(p=>!scope||p.path.startsWith(scope+'/'));if(pending?.command_id)alert.dataset.commandId=pending.command_id;pendingSelector(alert)}
  global.AVFRequest.setObserver(event=>{
    if(event.phase==='success'){
      failures.delete(event.path);lastSync=new Date().toLocaleTimeString('zh-CN',{hour12:false});
      if(!failures.size){status.hidden=true;document.querySelectorAll('.studio-save-state').forEach(e=>e.textContent='最近同步 '+lastSync)}
    }
    if(event.phase==='waiting'){status.hidden=false;status.dataset.state='loading';status.querySelector(':scope > span').textContent=event.method==='GET'?'正在读取，当前内容已保留。':'正在提交，请等待结果；关闭页面不会取消已受理任务。';status.querySelector(':scope > button').hidden=true}
    if(event.phase==='error'||event.phase==='unknown'){
      failures.set(event.path,event);status.hidden=false;status.dataset.state=event.phase;status.querySelector(':scope > span').textContent=event.message+(lastSync?' 最近同步 '+lastSync:'');status.querySelector(':scope > button').hidden=false;status.querySelector(':scope > button').textContent=event.phase==='unknown'?'核对当前任务':'重新读取';
      if(event.phase==='unknown'){modalUnknown(event.message,event.path);checkCommands()}
      document.querySelectorAll('.studio-save-state').forEach(e=>e.textContent='读取未完成 · 保留已有内容');
    }
  });
  const mobile=document.createElement('section');mobile.id='narrow-screen-message';mobile.innerHTML='<h2>请使用桌面窗口</h2><p>制作、审核和预算确认需要更大的工作区。请将窗口宽度调整到 1024 像素以上。</p><p>当前任务与已受理的生成会继续保留，缩小窗口不会取消任务。</p>';document.querySelector('main').append(mobile);
  const controls=document.createElement('div');controls.className='workspace-controls';controls.innerHTML='<button type="button" id="toggle-project-rail" aria-expanded="false">项目列表</button><button type="button" id="toggle-stage-inspector" aria-expanded="false">检查与建议</button>';
  document.querySelector('.studio-head-actions')?.prepend(controls);
  for(const [id,cls] of [['toggle-project-rail','project-rail-open'],['toggle-stage-inspector','inspector-open']])document.querySelector('#'+id)?.addEventListener('click',event=>{const open=document.body.classList.toggle(cls);event.currentTarget.setAttribute('aria-expanded',String(open))});
  document.addEventListener('click',event=>{if(event.target.closest('[data-global-project]')){document.body.classList.remove('project-rail-open');document.querySelector('#toggle-project-rail')?.setAttribute('aria-expanded','false')}});
  function reconcile(data){
    const jobs=data?.jobs||[];
    for(const pending of global.AVFRequest.pending()){
      const match=pending.path.match(/^\/api\/videos\/([^/]+)\//);const job=match&&jobs.find(j=>j.job_id===decodeURIComponent(match[1]));
      if(pending.kind!=='publication'&&global.AVFRequest.markReconciled(pending.path,job?.presentation))failures.delete(pending.path);
    }
    clearResolvedAliases();
    checkCommands();
    if(!failures.size&&status.dataset.state==='unknown'){status.hidden=true;if(!document.querySelector('[data-uncertain-submit]')?.dataset.resolved)document.querySelector('[data-uncertain-submit]')?.remove()}
  }
  function clearResolvedAliases(){const scopes=new Set(global.AVFRequest.pending().map(p=>p.path.match(/^\/api\/videos\/[^/]+/)?.[0]));for(const [path,failure] of failures){if(failure.phase==='unknown'&&!scopes.has(path.match(/^\/api\/videos\/[^/]+/)?.[0]))failures.delete(path)}}
  function reconcilePublications(data){for(const pending of global.AVFRequest.pending()){if(pending.kind!=='publication')continue;const id=decodeURIComponent(pending.path.match(/^\/api\/videos\/([^/]+)/)?.[1]||'');const receipt=data?.submission_evidence?.[id],proof={submission_known:receipt?.submission_known,submission_evidence:receipt};if(global.AVFRequest.markReconciled(pending.path,proof))failures.delete(pending.path)}clearResolvedAliases();checkCommands();if(!failures.size&&status.dataset.state==='unknown'){status.hidden=true;if(!document.querySelector('[data-uncertain-submit]')?.dataset.resolved)document.querySelector('[data-uncertain-submit]')?.remove()}}
  function manualCommandResolution(alert,pending){
    if(!alert||alert.dataset.commandId!==pending.command_id)return;
    if(alert.querySelector('[data-command-resolution]')?.dataset.commandId===pending.command_id)return;
    alert.querySelector('[data-command-resolution]')?.remove();
    const details=document.createElement('details');details.dataset.commandResolution='true';details.dataset.commandId=pending.command_id;
    details.innerHTML='<summary>人工核对结果</summary><p>仅记录本次请求的人工核对结论，不会生成、投稿或重复执行原请求。请先核对现有任务、成本和渠道记录；证据不足时保留未知。</p><label>核对结论<select data-resolution-decision><option value="">请选择</option><option value="executed">已执行，保留已有结果</option><option value="not_executed">确认未执行，重新确认后再处理</option></select></label><label>核对说明<textarea data-resolution-reason maxlength="1000" rows="2" placeholder="填写这次请求的核对结论"></textarea></label><label>核对依据<textarea data-resolution-evidence maxlength="1000" rows="2" placeholder="填写核对过的任务或渠道记录及时间，不填写密钥"></textarea></label><label class="check-line"><input type="checkbox" data-resolution-confirm>我已核对本次请求；此结论不会把任务或渠道标记为成功</label><button type="button" data-resolution-save>保存人工核对结论</button><small data-resolution-error></small>';
    details.querySelectorAll('input,select,textarea').forEach(field=>field.dataset.commandId=pending.command_id);const button=details.querySelector('[data-resolution-save]');
    button.onclick=async()=>{
      const decision=details.querySelector('[data-resolution-decision]').value,reason=details.querySelector('[data-resolution-reason]').value.trim(),evidence=details.querySelector('[data-resolution-evidence]').value.trim();
      const error=details.querySelector('[data-resolution-error]');
      if(!decision||!reason||!evidence||!details.querySelector('[data-resolution-confirm]').checked){error.textContent='请选择结论、填写说明和依据，并确认已经核对。';return}
      button.disabled=true;error.textContent='';
      try{const proof=await api('/api/commands/'+encodeURIComponent(pending.command_id)+'/resolve',{method:'POST',feedback:false,body:JSON.stringify({path:pending.path,request_sha256:pending.request_sha256,decision,reason,evidence,confirmed:true})});if(global.AVFRequest.markReconciled(pending.path,proof)){resolvedCommand(pending,proof);details.remove();if(!global.AVFRequest.pending().length)status.hidden=true}}
      catch(failure){const messages={command_resolution_blocked:'已有任务仍在执行或远端结果尚未核实，请先核对已有任务；不能确认本次请求未执行。',command_resolution_not_eligible:'这条请求仍在处理或已有确定结果，请重新读取已有记录。',command_resolution_conflict:'这条请求已保存另一份人工结论，请重新读取记录。',command_identity_conflict:'请求身份与原记录不一致，请重新读取当前任务。'};error.textContent=(messages[failure.details?.code]||failure.message)+' 当前输入保留，已有任务不会重新执行。'}
      finally{if(button.isConnected)button.disabled=false}
    };
    alert.append(details);
  }
  function resolvedCommand(pending,proof){
    failures.delete(pending.path);clearResolvedAliases();
    const alert=document.querySelector('[data-uncertain-submit]');
    const accepted=proof.outcome==='accepted';
    const message=proof.resolution?.source==='human'?'已记录人工核对结论，原请求不会再次执行。请按当前任务状态继续；当前输入保留。':accepted?'服务端已受理这条请求，请查看现有任务或渠道记录；当前输入保留，不再重复提交。':'已确认这条请求未执行。当前输入保留；请核对错误原因后重新确认。';
    if(alert?.dataset.commandId===pending.command_id){
      alert.dataset.resolved=proof.outcome;alert.querySelector('span').textContent=message;alert.querySelector('[data-cancel-unclaimed]').hidden=true;
      const button=alert.querySelector('button');button.textContent='重新读取已有记录';button.onclick=async()=>{button.disabled=true;try{const data=await api(pending.kind==='publication'?'/api/publishing':'/api/videos');if(pending.kind==='publication')reconcilePublications(data);else reconcile(data)}catch{}finally{if(button.isConnected)button.disabled=false}};
      if(accepted)for(const id of ['produce-confirm','native-budget-confirm','confirm-video-budget','publish-douyin-direct']){const submit=document.getElementById(id);if(submit?.dataset.commandPath===pending.path){submit.disabled=true;submit.textContent=proof.resolution?.source==='human'?'已核对，请查看现有记录':'已受理，请查看现有记录'}}
    }
    if(typeof toast==='function')toast(message);
  }
  async function checkCommands(){await Promise.allSettled(global.AVFRequest.pending().filter(p=>p.command_id).map(async pending=>{
    if(checkingCommands.has(pending.command_id))return checkingCommands.get(pending.command_id);
    const read=(async()=>{try{
      const proof=await api('/api/commands/'+encodeURIComponent(pending.command_id),{feedback:false});
      if(global.AVFRequest.markReconciled(pending.path,proof))resolvedCommand(pending,proof);else if(proof.outcome==='unknown'&&selectedPending()?.command_id===pending.command_id){modalUnknown('这条请求的服务端结果尚未确定，请先核对实际任务与渠道记录；不会自动重复执行。',pending.path);manualCommandResolution(document.querySelector('[data-uncertain-submit]'),pending);}
    }catch(error){if(error.status===404&&error.details?.code==='command_not_found'&&selectedPending()?.command_id===pending.command_id){
      modalUnknown('未找到这条请求的受理记录。可取消未受理请求，已受理任务不会被终止；当前输入保留。',pending.path);
      const alert=document.querySelector('[data-uncertain-submit]');if(alert?.dataset.commandId===pending.command_id)alert.querySelector('[data-cancel-unclaimed]').hidden=false;
    }}finally{checkingCommands.delete(pending.command_id);clearResolvedAliases();if(!global.AVFRequest.pending().length){status.hidden=true;if(!document.querySelector('[data-uncertain-submit]')?.dataset.resolved)document.querySelector('[data-uncertain-submit]')?.remove()}}})();
    checkingCommands.set(pending.command_id,read);return read;
  }));}
  for(const pending of global.AVFRequest.pending()){failures.set(pending.path,pending);status.hidden=false;status.dataset.state='unknown';status.querySelector(':scope > span').textContent='上次生成或投稿的结果仍待核对，不会重复发送。';status.querySelector(':scope > button').hidden=false;status.querySelector(':scope > button').textContent='核对当前任务'}
  const selectionKey='avf_workspace_selection_v1';
  const selectionFields=['studioProjectId','studioStage','studioOutlineIndex','studioVideoView','studioTimelineScope','studioMasterIndex','globalProjectFilter','videoFilter'];
  function saveSelection(){try{if(typeof state==='undefined')return;const selection={};for(const key of selectionFields)selection[key]=state[key];sessionStorage.setItem(selectionKey,JSON.stringify(selection))}catch{}}
  function restore(){try{const saved=JSON.parse(sessionStorage.getItem(selectionKey)||'{}'),result={};for(const key of selectionFields){const value=saved[key];if(key.endsWith('Index')){if(Number.isInteger(value)&&value>=-1&&value<100000)result[key]=value}else if(typeof value==='string'&&value.length<200)result[key]=value}if(result.studioStage&&!['source','brief','script','video','review','publish'].includes(result.studioStage))delete result.studioStage;return result}catch{return {}}}
  // Local draft partition only; backend SHA/version guards authorize writes.
  function contextVersion(value){const text=JSON.stringify(value??null);let hash=2166136261;for(let i=0;i<text.length;i++)hash=Math.imul(hash^text.charCodeAt(i),16777619)>>>0;return text.length+':'+hash.toString(16)}
  document.addEventListener('click',()=>queueMicrotask(saveSelection));global.addEventListener('beforeunload',saveSelection);
  global.AVFWorkbench=Object.freeze({reconcile,reconcilePublications,restore,saveSelection,contextVersion});
})(globalThis);
