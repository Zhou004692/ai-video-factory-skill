/* Publication evidence and metrics always belong to one immutable work/version. */
(function(global){
  'use strict';
  function label(item={}){
    const status=String(item.metadata?.review_status||item.verification_status||'').toLowerCase();
    if(['rejected','failed','denied'].includes(status))return '平台审核未通过';
    if(['pending','reviewing','submitted','pending_review'].includes(status))return '平台审核中';
    if(item.binding_status==='server_verified_douyin_upload'&&item.verification_status==='verified')return '已发布 · 上传成片已核实';
    if(item.verification_status==='verified')return '已登记 · 平台成片待核实';
    return '历史状态待核实';
  }
  function decorate(item){
    const body=document.querySelector('#drawer-body'),context=item.creative_context||{};if(!body)return;
    const evidence=document.createElement('section');evidence.className='detail-section';const title=document.createElement('h3');title.textContent='作品与成片依据';const note=document.createElement('p');note.textContent=label(item)+'。'+(context.intent?'冻结创作意图：'+context.intent:'历史记录未保存冻结创作意图。');
    const details=document.createElement('details'),summary=document.createElement('summary'),hash=document.createElement('p');summary.textContent='查看版本依据';hash.textContent='成片摘要：'+(context.final_sha256||'待核实')+'；生产规格：'+(context.production_spec_hash||'历史未保存')+'；发布修订：'+(context.publication_revision||'历史未保存');hash.className='version-evidence';details.append(summary,hash);evidence.append(title,note,details);body.prepend(evidence);
    const form=body.querySelector('.performance-form');if(form){const field=document.createElement('label');field.textContent='采集来源（说明数据来自哪里）';const input=document.createElement('input');input.id='performance-observation-source';input.value='平台后台人工录入';field.append(input);form.prepend(field)}
    const history=document.createElement('section');history.className='detail-section';const heading=document.createElement('h3');heading.textContent='历史累计快照';history.append(heading);
    const snapshots=item.snapshot_history||[];
    if(!snapshots.length){const note=document.createElement('p');note.textContent='暂无可展示的历史快照；当前最新数据不代表历史所有观察。';history.append(note)}
    snapshots.forEach(snapshot=>{const row=document.createElement('details'),label=document.createElement('summary'),content=document.createElement('p');label.textContent=(snapshot.captured_at||'未注明时间')+' · 播放 '+(snapshot.views??'未提供');content.textContent='采集来源：'+(snapshot.metadata?.observation_source||'历史未注明')+'；完播 '+(snapshot.completed_views??'未提供')+'；点赞 '+(snapshot.likes??'未提供')+'；线索 '+(snapshot.leads??'未提供')+'。均为该作品在此时间点的累计值。';row.append(label,content);history.append(row)});
    if(item.snapshot_history_total>snapshots.length){const note=document.createElement('small');note.textContent='显示最近 '+snapshots.length+' 条，共 '+item.snapshot_history_total+' 条；完整历史保留在本机。';history.append(note)}body.append(history);
  }
  global.AVFPublicationWorkspace=Object.freeze({label,decorate});
})(globalThis);
