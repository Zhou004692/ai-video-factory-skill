/* Only format backend facts. No frontend state machine or inferred active leases. */
(function(global){
  'use strict';
  const value=n=>Math.max(0,Number(n)||0);
  function activity(job){const facts=job?.presentation?.activity;if(!facts)return null;return{queued:value(facts.queued),running:value(facts.running),remote:value(facts.remote_accepted),unknown:!!facts.unknown}}
  function activeRows(jobs){const result=[];for(const job of jobs||[]){const facts=activity(job);if(!facts||facts.queued+facts.running+facts.remote===0)continue;result.push({job,master:null,title:job.title||job.job_id,meta:`母片 ${value(job.presentation.production?.completed)}/${value(job.presentation.production?.total)} 完成 · ${facts.remote?'远端已受理 '+facts.remote+' 条':facts.running?'本地执行 '+facts.running+' 条':'实际队列 '+facts.queued+' 条'}`,status:facts.remote?'远端已受理':facts.running?'执行中':'已排队'})}return result}
  function stage(project,video,key){return project?.stage_gates?.[key]||(key==='video'?video?.presentation?.gate:null)}
  function error(job){const item=job?.error_summary;return item?.message||item?.summary||item?.label||''}
  global.AVFPresentation=Object.freeze({activity,activeRows,stage,error});
})(globalThis);
