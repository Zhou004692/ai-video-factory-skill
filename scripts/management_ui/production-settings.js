/* Settings explain dependencies and costs; the existing backend preflight authorizes work. */
(function(global){
  'use strict';
  function decorate(job){
    const modal=document.querySelector('#modal'),actions=modal.querySelector('.modal-actions');if(!actions)return;
    global.AVFComponents.setContext('production:'+job.job_id+':'+(job.production_spec_hash||job.updated_at||''));
    const summary=document.createElement('div');summary.className='production-budget-summary';
    const spent=Number(job.cost_cents||0)/100;const estimate=job.preflight?.projected_total_cost_cents??job.preflight?.cost_estimate?.projected_total_cost_cents;
    summary.innerHTML='<strong>已发生费用 ¥'+spent.toFixed(2)+'</strong><br><span data-budget-summary></span>';actions.prepend(summary);
    const budget=modal.querySelector('#setting-budget'),audio=modal.querySelector('#setting-audio-mode');
    const note=document.createElement('div');note.className='persistent-alert';note.id='settings-dependency-note';modal.querySelector('.production-settings')?.prepend(note);
    const sections=[['人物','#modal .speaker-mapping'],['场景','#setting-scene-anchor-generator'],['声音','#setting-audio-mode'],['输出','#setting-orientation'],['预算','#setting-budget']];
    const tabs=document.createElement('nav');tabs.className='settings-section-nav';tabs.setAttribute('aria-label','制作设置分组');
    sections.forEach(([label,selector])=>{const target=document.querySelector(selector)?.closest('section,label,div');if(!target)return;const heading=document.createElement('h3');heading.textContent=label;heading.className='settings-group-title';target.prepend(heading);const button=document.createElement('button');button.type='button';button.className='mini-action';button.textContent=label;button.onclick=()=>target.scrollIntoView({block:'start',behavior:'smooth'});tabs.append(button)});note.after(tabs);
    const settingsInitial=[...modal.querySelectorAll('input,select')].map(field=>[field,field.type==='checkbox'?field.checked:field.value]);
    const update=()=>{
      const changed=settingsInitial.some(([field,value])=>(field.type==='checkbox'?field.checked:field.value)!==value);
      const max=Number(budget?.value||0);summary.querySelector('[data-budget-summary]').textContent='预算上限 ¥'+max.toFixed(2)+(changed?' · 设置已变更，费用待重新预检':estimate!=null?' · 最近预检估算 ¥'+(Number(estimate)/100).toFixed(2):' · 保存后取得预检估算');
      note.textContent=(audio?.value==='fixed_tts'?'固定声线需要火山 TTS 能力。':'原生声线不要求额外 TTS。')+' 设置变更后重新预检；保存设置不会自动开始付费生成。';
    };
    modal.querySelectorAll('input,select').forEach(field=>field.addEventListener('change',update));budget?.addEventListener('input',update);update();
  }
  global.AVFProductionSettings=Object.freeze({decorate});
})(globalThis);
