/* Shared modal/drawer lifecycle. Successful business handlers close explicitly. */
(function(global){
  'use strict';
  const frames=new Map();let pendingClose=null,viewEpoch=0;
  const modal=()=>document.querySelector('#modal'),drawer=()=>document.querySelector('#drawer');
  const active=()=>!modal()?.hidden?frames.get('modal'):drawer()?.classList.contains('open')?frames.get('drawer'):null;
  const fields=frame=>[...(frame?.body?.querySelectorAll('input,select,textarea')||[])].filter(e=>!e.matches('[data-browse-only]')&&!e.closest('#unsaved-confirm'));
  const safeField=e=>!['password','file'].includes(e.type)&&!e.closest('[data-secret="true"]')&&!/key|token|secret|password/i.test(e.id||e.name||'');
  const fieldKey=e=>e.id||JSON.stringify([e.closest('[data-edit-script-beat]')?.dataset.editScriptBeat,e.closest('[data-edit-brief-angle]')?.dataset.editBriefAngle,e.closest('[data-content-id]')?.dataset.contentId,e.name,e.type==='radio'?e.value:null,Object.entries(e.dataset).filter(([key])=>!['browseOnly'].includes(key)).sort()]);
  const signature=frame=>JSON.stringify(fields(frame).map(e=>[fieldKey(e),e.type,e.type==='checkbox'||e.type==='radio'?e.checked:e.value]));
  const frameDirty=frame=>!!frame&&signature(frame)!==frame.clean;
  const hasDirty=()=>[...frames.values()].some(frameDirty);
  function restoreIsolation(frame){for(const [node,previous] of frame?.isolation||[])node.inert=previous;if(frame)frame.isolation=null}
  function dismissConfirm(){for(const frame of frames.values())restoreIsolation(frame);document.querySelector('#unsaved-confirm')?.remove();pendingClose=null}
  function markClean(){const frame=active();if(frame)frame.clean=signature(frame);for(const item of frames.values())restoreIsolation(item);document.querySelector('#unsaved-confirm')?.remove()}
  const visible=e=>!!e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden'&&!e.closest('[hidden],[inert]');
  const focusables=root=>[...root.querySelectorAll('button:not([disabled]),a[href],video[controls],audio[controls],input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(visible);
  function focusInside(root){if(!root?.isConnected)return;const list=focusables(root),first=list.find(e=>['INPUT','TEXTAREA','SELECT'].includes(e.tagName))||list[0]||root;if(!list.length)first.tabIndex=-1;first.focus({preventScroll:true})}
  function syncInert(){const frame=active(),shell=document.querySelector('.app-shell'),tasks=document.querySelector('#studio-task-drawer');if(shell)shell.inert=!!frame;drawer().inert=!modal().hidden;if(tasks){tasks.inert=!!frame;tasks.classList.toggle('suspended-by-dialog',!!frame)}document.body.classList.toggle('dialog-active',!!frame)}
  function openModal(kicker,title,body){
    viewEpoch++;
    dismissConfirm();
    const backdrop=modal();backdrop.className='modal-backdrop';
    delete document.querySelector('#modal-body')._productionPreflightIdentity;
    delete document.querySelector('#modal-body')._settingsRequestIdentity;
    delete document.querySelector('#modal-body')._nativePreviewIdentity;
    document.querySelector('#modal-kicker').textContent=kicker;document.querySelector('#modal-title').textContent=title;document.querySelector('#modal-body').innerHTML=body;
    backdrop.hidden=false;const frame={root:backdrop.querySelector('.modal'),body:document.querySelector('#modal-body'),trigger:frames.get('modal')?.trigger||document.activeElement,kicker,context:title,clean:''};frames.set('modal',frame);frame.root.tabIndex=-1;
    if(['CONTENT REVIEW','HUMAN REVIEW','CANDIDATE REVIEW','MASTER REVIEW','MASTER CANDIDATE','NATIVE VIDEO REPAIR'].includes(kicker))backdrop.classList.add('review-workspace-open');
    if(kicker==='PRODUCTION SETUP')backdrop.classList.add('settings-workspace-open');
    global.AVFReviewWorkspace?.prepare(kicker);syncInert();
    queueMicrotask(()=>{if(active()!==frame)return;frame.clean=signature(frame);draftHint();focusInside(frame.root)});
  }
  function closeModal({user=false}={}){
    if(modal().hidden)return true;
    if(user&&frameDirty(frames.get('modal'))){confirmLeave(()=>closeModal(),()=>{},frames.get('modal'));return false}
    viewEpoch++;
    const frame=frames.get('modal');restoreIsolation(frame);delete document.querySelector('#modal-body')._productionPreflightIdentity;delete document.querySelector('#modal-body')._settingsRequestIdentity;modal().hidden=true;frames.delete('modal');document.querySelector('#unsaved-confirm')?.remove();pendingClose=null;syncInert();
    if(frame?.trigger?.isConnected&&visible(frame.trigger))frame.trigger.focus({preventScroll:true});else focusInside(active()?.root);return true;
  }
  const draftKey=frame=>'avf_form_draft_v1:'+frame.kicker+':'+frame.context;
  function saveDraft(frame=active()){if(!frame)return false;const items=fields(frame).map((e,index)=>({index,key:fieldKey(e),type:e.type,value:e.type==='checkbox'||e.type==='radio'?e.checked:e.value,safe:safeField(e)})).filter(item=>item.safe);try{sessionStorage.setItem(draftKey(frame),JSON.stringify({savedAt:new Date().toISOString(),items}));return true}catch{return false}}
  function restoreDraft(){const frame=active();if(!frame)return false;let saved;try{saved=JSON.parse(sessionStorage.getItem(draftKey(frame))||'null')}catch{return false}if(!saved)return false;const current=fields(frame);for(const item of saved.items){const e=item.key?current.find(e=>fieldKey(e)===item.key):current[item.index];if(!e||!safeField(e)||e.type!==item.type)continue;if(e.type==='checkbox'||e.type==='radio')e.checked=!!item.value;else e.value=item.value;e.dispatchEvent(new Event('change',{bubbles:true}))}return true}
  function draftHint(){const frame=active();if(!frame)return;let saved;try{saved=JSON.parse(sessionStorage.getItem(draftKey(frame))||'null')}catch{}if(!saved||frame.body.querySelector('#saved-draft-hint'))return;const hint=document.createElement('div');hint.id='saved-draft-hint';hint.className='persistent-alert';hint.innerHTML='<span>此对象有本页保存的草稿。附件需重新选择，密钥不保存。</span><button type="button">恢复草稿</button>';hint.querySelector('button').onclick=()=>{restoreDraft();hint.remove()};frame.body.prepend(hint)}
  function confirmLeave(action,onCancel=()=>{},draftFrame=active()){
    const frame=active();if(!frame)return action();if(document.querySelector('#unsaved-confirm'))return;pendingClose=action;
    const panel=document.createElement('section');panel.id='unsaved-confirm';panel.className='unsaved-confirm';panel.setAttribute('role','alertdialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-labelledby','unsaved-title');
    panel.innerHTML='<h3 id="unsaved-title">未保存的修改</h3><p>当前输入尚未提交。请选择保留草稿、放弃修改，或继续编辑。</p><div><button type="button" data-dirty="stay" class="primary-action">继续编辑</button><button type="button" data-dirty="draft">保留草稿并离开</button><button type="button" data-dirty="discard">放弃修改</button></div>';
    frame.root.append(panel);frame.isolation=[...frame.root.children].filter(node=>node!==panel).map(node=>[node,node.inert]);for(const [node] of frame.isolation)node.inert=true;
    panel.querySelector('[data-dirty=stay]').onclick=()=>{restoreIsolation(frame);panel.remove();pendingClose=null;onCancel();focusInside(frame.root)};
    const proceed=()=>{const next=pendingClose;draftFrame.clean=signature(draftFrame);restoreIsolation(frame);panel.remove();pendingClose=null;guardTransition(next,onCancel)};
    panel.querySelector('[data-dirty=discard]').onclick=proceed;
    panel.querySelector('[data-dirty=draft]').onclick=()=>{if(!saveDraft(draftFrame)){panel.querySelector('p').textContent='草稿保存失败，当前输入仍保留；请继续编辑或明确放弃。';return}proceed()};focusInside(panel);
  }
  const guardTransition=(action,onCancel=()=>{})=>{const dirtyFrame=[...frames.values()].reverse().find(frameDirty);return dirtyFrame?confirmLeave(action,onCancel,dirtyFrame):action?.()};
  function openDrawer(kicker,title,body){
    viewEpoch++;
    if(!modal().hidden)closeModal();dismissConfirm();const panel=drawer();
    document.querySelector('#drawer-kicker').textContent=kicker;document.querySelector('#drawer-title').textContent=title;document.querySelector('#drawer-body').innerHTML=body;
    document.querySelector('#context-drawer').hidden=false;panel.classList.add('open');panel.setAttribute('aria-hidden','false');panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.tabIndex=-1;
    const frame={root:panel,body:document.querySelector('#drawer-body'),trigger:document.activeElement,kicker,context:title,clean:''};frames.set('drawer',frame);syncInert();queueMicrotask(()=>{if(active()!==frame)return;frame.clean=signature(frame);draftHint();focusInside(panel)});
  }
  function closeDrawer({user=false}={}){viewEpoch++;if(user&&frameDirty(frames.get('drawer'))){confirmLeave(()=>closeDrawer(),()=>{},frames.get('drawer'));return false}const frame=frames.get('drawer');dismissConfirm();drawer().classList.remove('open');drawer().setAttribute('aria-hidden','true');document.querySelector('#context-drawer').hidden=true;frames.delete('drawer');syncInert();if(!active()&&frame?.trigger?.isConnected)frame.trigger.focus({preventScroll:true});return true}
  document.addEventListener('keydown',event=>{
    const frame=active();if(!frame)return;
    if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();if(document.querySelector('#unsaved-confirm'))document.querySelector('[data-dirty=stay]').click();else if(!modal().hidden)closeModal({user:true});else closeDrawer({user:true});return}
    if(event.key!=='Tab')return;const root=document.querySelector('#unsaved-confirm')||frame.root,list=focusables(root);if(!list.length){event.preventDefault();root.focus();return}const index=list.indexOf(document.activeElement);if(index<0||(event.shiftKey&&index===0)||(!event.shiftKey&&index===list.length-1)){event.preventDefault();list[event.shiftKey?list.length-1:0].focus()}
  },true);
  global.AVFComponents=Object.freeze({openModal,closeModal,openDrawer,closeDrawer,markClean,guardTransition,draftHint,hasDirty,epoch:()=>viewEpoch,invalidate:()=>{viewEpoch++},setContext:key=>{const frame=active();if(frame){frame.context=String(key);queueMicrotask(()=>{if(active()===frame)draftHint()})}}});
})(globalThis);
