/* Explain revision impact from changed content; frozen production remains authoritative. */
(function(global){
  'use strict';
  const text=value=>String(value??'');
  const normalize=raw=>(Array.isArray(raw)?raw:[raw]).flatMap(value=>text(value?.id??value).split(/[,，、]/)).map(v=>v.trim()).filter(v=>v&&!['narration','narrator','voiceover','voice_over','旁白','画外音','none','off_screen'].includes(v.toLowerCase()));
  const ids=beat=>normalize(beat?.speakers?.length?beat.speakers:beat?.speaker);
  const cast=beat=>normalize((Array.isArray(beat?.characters)?beat.characters.length:beat?.characters)?beat.characters:beat?.cast||[]);
  function impact(before={},after={}){
    const changes=[],characters=new Set(),a=before.beats||[],b=after.beats||[];
    for(let i=0;i<Math.max(a.length,b.length);i++){
      const old=a[i]||{},next=b[i]||{},keys=new Set([...Object.keys(old),...Object.keys(next)]);
      const fields=[...keys].filter(key=>JSON.stringify(old[key]??null)!==JSON.stringify(next[key]??null));
      if(fields.length||!a[i]||!b[i]){changes.push({beat:i+1,fields});[...ids(old),...ids(next),...cast(old),...cast(next)].forEach(id=>characters.add(id))}
    }
    const sum=items=>Number(items.reduce((n,beat)=>n+(Number(beat.duration_sec)||0),0).toFixed(2));
    const metadata_changed=['title','creative_thesis','duration_mode'].some(key=>JSON.stringify(before[key]??null)!==JSON.stringify(after[key]??null));
    return {changes,affected_beats:changes.map(item=>item.beat),character_ids:[...characters],before_seconds:sum(a),after_seconds:sum(b),metadata_changed,needs_preflight:changes.length>0};
  }
  function decorate(draft){
    const body=document.querySelector('#modal-body'),editor=body.querySelector('.script-editor');if(!editor)return;
    const current=draft.script||{};global.AVFComponents.setContext('script:'+draft.draft_id+':'+draft.revision);
    document.querySelector('#modal').classList.add('content-workspace-open');
    const panel=document.createElement('aside');panel.className='content-impact-summary';panel.setAttribute('aria-live','polite');panel.id='script-change-impact';editor.prepend(panel);
    const roleNames=new Map((current.characters||[]).map(c=>[c.id,c.name||c.id]));
    body.querySelectorAll('[data-edit-script-beat]').forEach(section=>{
      const beat=current.beats?.[Number(section.dataset.editScriptBeat)]||{};
      const facts=document.createElement('p');facts.className='scene-content-facts';
      const names=cast(beat).map(id=>roleNames.get(id)||id),voices=ids(beat).map(id=>roleNames.get(id)||id);
      const requirements=beat.visual_requirements||[];
      facts.textContent='入镜人物：'+(names.join('、')||(Array.isArray(beat.characters)?'无固定出镜人物':'未单独声明，请核对画面意图'))+'；声音说话人：'+(voices.join('、')||'旁白 / 无角色声线')+'；来源依据：'+((beat.source_ids||[]).join('、')||'未关联来源')+'；关键内容：'+(requirements.map(r=>r.description||r.text||r.requirement_id||r.id).filter(Boolean).join('；')||'未声明额外关键要求');section.querySelector('header')?.after(facts);
    });
    const update=()=>{
      const next={...current,title:body.querySelector('#edit-script-title').value,creative_thesis:body.querySelector('#edit-script-thesis').value,beats:[...(current.beats||[])]};
      body.querySelectorAll('[data-edit-script-beat]').forEach(section=>{
        const index=Number(section.dataset.editScriptBeat),beat={...next.beats[index]};
        section.querySelectorAll('[data-name]').forEach(field=>{const key=field.dataset.name;beat[key]=key==='duration_sec'?Number(field.value):field.value.trim()});if(beat.speaker!==current.beats[index].speaker)beat.speakers=text(beat.speaker).split(/[,，]/).map(v=>v.trim()).filter(Boolean);next.beats[index]=beat;
      });
      const result=impact(current,next);
      panel.replaceChildren();const title=document.createElement('strong');title.textContent='修改影响 · 基于当前输入';const details=document.createElement('p');
      details.textContent=result.affected_beats.length?'涉及场次 '+result.affected_beats.join('、')+'；人物 '+(result.character_ids.map(id=>roleNames.get(id)||id).join('、')||'无固定人物')+'；时长 '+result.before_seconds+' → '+result.after_seconds+' 秒。新草稿须重新审核导演意图，角色/场景锚点、母片和时间线是否复用由重新预检决定，费用尚未重估。':'未改变场次内容；标题和命题的修改仍保存为新修订。';
      const binding=document.createElement('small');binding.textContent='编辑基于修订 '+draft.revision+'；保存不会重建旧任务、替换成功素材或开始付费生成。';panel.append(title,details,binding);
    };
    editor.addEventListener('input',update);editor.addEventListener('change',update);update();
  }
  function conflict(error){
    const content=error.details?.current_script||error.details?.current_brief;if(error.status!==409||!content)return;
    const body=document.querySelector('#modal-body');body.querySelector('#script-version-conflict')?.remove();
    const panel=document.createElement('section');panel.id='script-version-conflict';panel.className='persistent-alert';const title=document.createElement('strong');title.textContent='内容版本已变化，当前编辑仍保留';
    const note=document.createElement('p');note.textContent='服务端当前修订 '+error.details.current_revision+'，本页基于 '+error.details.expected_revision+'。请保留草稿、退出后重新打开当前版本，再对照修改；不会覆盖新版本。';
    const diff=document.createElement('details'),label=document.createElement('summary'),pre=document.createElement('pre');label.textContent='查看服务端当前内容';pre.textContent=JSON.stringify(content,null,2);diff.append(label,pre);panel.append(title,note,diff);body.prepend(panel);
  }
  global.AVFContentWorkspace=Object.freeze({impact,decorate,conflict});
})(globalThis);
