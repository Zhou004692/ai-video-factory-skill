const state={page:'overview',overview:null,todos:null,projects:null,scripts:null,sourceFeeds:null,sourceCandidates:null,videos:null,productionHealth:null,publishing:null,config:null,characters:null,recentEvents:[],videoFilter:'all',videoSearch:'',scriptStage:'all',globalProjectFilter:'all',studioProjectId:'',studioStage:'source',studioOutlineIndex:0,studioVideoView:'timeline',studioTimelineScope:'master',studioMasterIndex:0,studioFocus:null,studioTaskDrawerOpen:false,lastTechnicalError:'',loadSequence:0,characterLoadSequence:0,openVideoJobId:'',pendingMusicResumeJobId:'',polling:false,productionPollTimer:null};
const $=(s,r=document)=>r.querySelector(s);const $$=(s,r=document)=>[...r.querySelectorAll(s)];
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const money=c=>`¥${((Number(c)||0)/100).toFixed(2)}`;const short=v=>String(v||'').slice(0,18);const empty=t=>`<div class="empty">${t}</div>`;
function costObservationRows(job){const observation=job.cost_observation||{};if(observation.estimated_cost_cents==null)return `${detailRow('实际累计',money(observation.actual_cost_cents??job.cost_cents))}${detailRow('成本对账','历史任务未保存确认预估')}`;const variance=Number(observation.variance_cents||0);const percent=observation.variance_percent==null?'—':`${variance>0?'+':''}${Number(observation.variance_percent).toFixed(1)}%`;const signed=`${variance>0?'+':variance<0?'-':''}¥${(Math.abs(variance)/100).toFixed(2)}`;return `${detailRow('确认预估',money(observation.estimated_cost_cents))}${detailRow('实际累计',money(observation.actual_cost_cents))}${detailRow('成本偏差',`${signed}（${percent}）${observation.is_final?'':' · 生成中'}`)}`}
function estimateDialogueDuration(dialogue){const text=String(dialogue||'').trim();if(!text)return 1.5;const cjk=(text.match(/[\u3400-\u9fff]/g)||[]).length;const latin=(text.match(/[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*/g)||[]).length;const light=(text.match(/[，、,:：]/g)||[]).length;const strong=(text.match(/[。！？!?；;]/g)||[]).length;const seconds=cjk/3.6+latin/2.6+light*.12+strong*.28+.55;return Math.max(1.5,Math.ceil(seconds*2)/2)}
const videoFileUrl=(job,download=false)=>`/api/videos/${encodeURIComponent(job.job_id)}/file?v=${encodeURIComponent(job.final_version||job.updated_at||Date.now())}${download?'&download=1':''}`;
const videoCoverUrl=job=>`/api/videos/${encodeURIComponent(job.job_id)}/cover?v=${encodeURIComponent(job.final_version||job.updated_at||Date.now())}`;
const isServerRuntime=()=>location.protocol==='https:';
async function ensureServerCsrf(){if(!isServerRuntime())return'';const cached=sessionStorage.getItem('avf_csrf');if(cached)return cached;const res=await fetch('/api/auth/session',{headers:{Accept:'application/json'}});if(res.status===401){location.href='/login';throw new Error('请先登录')}const data=await res.json();if(!res.ok||!data.csrf_token)throw new Error(data.error||'会话校验失败');sessionStorage.setItem('avf_csrf',data.csrf_token);return data.csrf_token}
async function api(path,options={}){const method=String(options.method||'GET').toUpperCase();const headers={'Content-Type':'application/json',...(options.headers||{})};if(['POST','PUT','PATCH','DELETE'].includes(method)){const csrf=await ensureServerCsrf();if(csrf)headers['X-CSRF-Token']=csrf}const res=await fetch(path,{...options,headers});if(res.status===401&&isServerRuntime()){sessionStorage.removeItem('avf_csrf');location.href='/login'}const data=await res.json();if(!res.ok)throw new Error(data.error||'请求失败');return data}
function summarizeError(message){const text=String(message||'请求失败');if(/ReplayFixtureMissing|replay fixture missing/i.test(text))return'零费用回放未找到匹配夹具，未调用外部模型；请补录夹具后重试';if(/AccountOverdueError|overdue balance|账号欠费/i.test(text))return'火山方舟账号欠费，请充值后继续生成剩余母片';if(/PrivacyInformation|real person|隐私/i.test(text))return'角色参考图未通过火山隐私校验，请检查角色资产授权';if(/budget|cost.*limit|成本|预算/i.test(text))return'生成成本达到保护条件，请查看任务详情后确认下一步';if(/exactly\s*3|three angles|三个角度/i.test(text))return'AI 返回的选题格式不完整，请重新生成';if(/quality gate|manual review|质检/i.test(text))return'母片需要人工复核，请在视频任务中查看具体镜头';if(/Seedance|provider|submit 4\d\d/i.test(text))return'视频模型调用失败，请查看技术详情';if(text.length>96)return`${text.slice(0,92)}…`;return text}
function toast(message,error=false){const el=$('#toast');clearTimeout(toast.timer);if(error){state.lastTechnicalError=String(message||'请求失败');const summary=summarizeError(state.lastTechnicalError);el.innerHTML=`<span>${esc(summary)}</span><button type="button" class="toast-detail">查看技术详情</button>`;el.querySelector('.toast-detail').onclick=()=>openModal('ERROR DETAILS','错误技术详情',`<pre class="technical-error">${esc(state.lastTechnicalError)}</pre>`)}else{el.textContent=message}el.className=`toast show${error?' error':''}`;toast.timer=setTimeout(()=>el.className='toast',error?12000:2600)}
function tick(){const d=new Date();$('#clock-time').textContent=d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});$('#clock-date').textContent=d.toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit',weekday:'short'})}tick();setInterval(tick,30000);
const pageMeta={overview:['CONTENT STUDIO / 00','内容项目工作室'],todos:['UNIFIED TODO / 01','统一待办中心'],projects:['CONTENT PROJECTS / 02','内容项目'],scripts:['CONTENT & SCRIPT / 03','内容与剧本'],videos:['VIDEO PRODUCTION / 04','视频制作'],publishing:['PUBLISH & DATA / 05','发布与数据'],characters:['CHARACTER ASSETS / 06','角色资产库'],config:['SYSTEM CONFIG / 07','配置中心']};
function placeGlobalStage(page){const strip=$('#global-stage-strip');if(page==='overview'){document.querySelector('main').insertBefore(strip,$('#page-overview'));return}const intro=$(`#page-${page} .section-intro`);if(intro)intro.after(strip)}
function navigate(page,{historyMode='replace'}={}){if(!pageMeta[page])page='overview';if(page!==state.page){closeDrawer();closeModal()}if(historyMode!=='none'&&location.hash!==`#${page}`)window.history[historyMode==='push'?'pushState':'replaceState'](null,'',`#${page}`);state.page=page;document.body.dataset.page=page;$$('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));$$('.page').forEach(x=>x.classList.toggle('active',x.id===`page-${page}`));$('#page-kicker').textContent=pageMeta[page][0];$('#page-title').textContent=pageMeta[page][1];placeGlobalStage(page);syncGlobalStage(page);scheduleActiveProductionPoll();return loadPage(page)}
function syncGlobalStage(page){const stageByPage={overview:state.studioStage||'source',todos:'',projects:'source',scripts:'script',videos:'video',publishing:'publish',characters:'video',config:''};const active=stageByPage[page]||'';$$('[data-global-stage]').forEach(button=>{const stage=button.dataset.globalStage;button.classList.toggle('current',stage===active);button.classList.toggle('done',studioStageOrder.indexOf(stage)<studioStageOrder.indexOf(active))})}
function globalProjectCollections(projects=state.projects?.projects||[]){
  const jobs=state.videos?.jobs||[];
  const jobsById=new Map(jobs.map(job=>[job.job_id,job]));
  const reviewJobs=jobs.filter(job=>job.shots?.some(shot=>shot.manual_review_required));
  const reviewJobIds=new Set(reviewJobs.map(job=>job.job_id));
  const interventionStates=new Set(['FAILED','BLOCKED','PROVIDER_ACTION_REQUIRED','BUDGET_CONFIRMATION_REQUIRED','PRODUCTION_REVIEW_REQUIRED','ANCHOR_REVIEW_REQUIRED','PRODUCTION_PAUSED']);
  const needsAttention=project=>{
    const job=jobsById.get(project.video?.job_id);
    return project.stage==='review'||reviewJobIds.has(project.video?.job_id)||Boolean(job&&interventionStates.has(job.state));
  };
  return {
    all:{label:'全部项目',description:'不限制阶段',items:projects},
    review:{label:'待内容决策',description:'来源、选题或剧本需要确认',items:projects.filter(project=>['source','brief','script'].includes(project.stage))},
    production:{label:'生产处理中',description:'等待设置、排队或生成母片',items:projects.filter(project=>['production_setup','production'].includes(project.stage))},
    'video-review':{label:'需人工处理',description:'异常、锚点、母片或成片待复核',items:projects.filter(needsAttention),reviewJobs},
    publish:{label:'待发布成片',description:'成片已就绪，等待选择渠道',items:projects.filter(project=>project.stage==='publish')}
  };
}
function globalProjectCollectionReason(project,filter){
  const job=(state.videos?.jobs||[]).find(item=>item.job_id===project.video?.job_id);
  if(filter==='review')return `待处理 · ${projectActionLabel(project.next_action)}`;
  if(filter==='production')return job?videoStateLabel(job.display_state||job.state):projectStageLabel(project.stage);
  if(filter==='video-review'){
    if(job?.shots?.some(shot=>shot.manual_review_required))return '母片需要人工复核';
    if(job)return videoStateLabel(job.display_state||job.state);
    return project.stage==='review'?'成片等待验收':'需要人工判断';
  }
  if(filter==='publish')return '成片已就绪 · 等待选择渠道';
  return `${projectStageLabel(project.stage)} · ${project.quality_gate.score} 分`;
}
function renderGlobalProjects(){
  const projects=state.projects?.projects||[];
  const list=$('#global-project-list');if(!list)return;
  const collections=globalProjectCollections(projects);
  if(!collections[state.globalProjectFilter])state.globalProjectFilter='all';
  const selectedCollection=collections[state.globalProjectFilter];
  $('#global-review-count').textContent=collections.review.items.length;
  $('#global-production-count').textContent=collections.production.items.length;
  $('#global-video-review-count').textContent=collections['video-review'].items.length;
  $('#global-publish-count').textContent=collections.publish.items.length;
  const navTodo=$('#nav-todo-count');
  if(navTodo){const count=collections.review.items.length;navTodo.textContent=count>99?'99+':String(count);navTodo.hidden=count===0}
  const query=($('#global-project-search')?.value||'').trim().toLowerCase();
  const visible=selectedCollection.items.filter(project=>!query||`${project.title} ${project.project_id}`.toLowerCase().includes(query));
  const title=$('#global-project-list-title');if(title)title.textContent=selectedCollection.label;
  const count=$('#global-project-list-count');if(count)count.textContent=`${selectedCollection.items.length} 项`;
  list.innerHTML=visible.map(project=>`<button class="${project.project_id===state.studioProjectId?'selected':''}" data-global-project="${esc(project.project_id)}"><span class="global-project-cover">${esc((project.title||'项').slice(0,1))}</span><span><b>${esc(project.title)}</b><small>${esc(globalProjectCollectionReason(project,state.globalProjectFilter))}</small></span></button>`).join('')||`<div class="global-collection-empty"><b>当前集合为空</b><span>${esc(query?'没有匹配搜索条件的项目':selectedCollection.description)}</span></div>`;
  $$('[data-global-project]').forEach(button=>button.onclick=()=>{const project=projects.find(item=>item.project_id===button.dataset.globalProject);if(project)openProjectStudio(project,studioStageOrder[studioStageIndex(project.stage)])});
  $$('[data-global-filter]').forEach(button=>{
    const filter=button.dataset.globalFilter;
    button.classList.toggle('active',filter===state.globalProjectFilter);
    button.classList.toggle('has-items',collections[filter]?.items.length>0);
    button.setAttribute('aria-pressed',String(filter===state.globalProjectFilter));
    button.onclick=()=>{state.globalProjectFilter=state.globalProjectFilter===filter?'all':filter;renderGlobalProjects()};
  });
  const reset=$('#global-filter-reset');if(reset){reset.classList.toggle('active',state.globalProjectFilter==='all');reset.onclick=()=>{state.globalProjectFilter='all';renderGlobalProjects()}}
}
function syncGlobalProjectSelection(){
  $$('[data-global-project]').forEach(button=>button.classList.toggle('selected',button.dataset.globalProject===state.studioProjectId))
}
function studioBackgroundTasks(){
  const activeStates=new Set(['PENDING','QUEUED','SUBMITTED','RUNNING','PROCESSING','GENERATING','IN_PROGRESS','RETRY_PENDING']);
  const tasks=[];
  for(const job of state.videos?.jobs||[]){
    const masters=studioVideoMasters(job);
    const active=masters.filter(master=>activeStates.has(String(master.state||'').toUpperCase()));
    active.forEach(master=>tasks.push({job,master,title:`M${String(master.idx).padStart(2,'0')} · ${studioMasterTitle(master)}`,meta:`${master.provider||'Seedance 2.5'} · ${Number(master.duration_sec||master.duration||0)} 秒`,status:['PENDING','QUEUED','RETRY_PENDING'].includes(String(master.state||'').toUpperCase())?'排队中':'生成中'}));
    if(!active.length&&['PRODUCTION_QUEUED','PRODUCTION_RUNNING','GENERATING','IN_PROGRESS'].includes(String(job.display_state||job.state||'').toUpperCase()))tasks.push({job,master:null,title:job.title||job.job_id,meta:`${job.completed_master_count||0}/${job.master_count||job.planned_masters?.length||0} 条母片完成`,status:'处理中'});
  }
  return tasks;
}
function renderStudioTaskDrawer(){
  const drawer=$('#studio-task-drawer');if(!drawer)return;
  const tasks=studioBackgroundTasks();
  drawer.classList.toggle('open',state.studioTaskDrawerOpen);
  drawer.hidden=state.page!=='overview'&&!tasks.length;
  $('#studio-task-toggle').setAttribute('aria-expanded',String(state.studioTaskDrawerOpen));
  $('#studio-task-chevron').textContent=state.studioTaskDrawerOpen?'⌄':'⌃';
  $('#studio-task-title').textContent=`后台任务 · ${tasks.length} 项进行中`;
  $('#studio-task-list').innerHTML=tasks.length?tasks.slice(0,6).map((task,index)=>`<button data-background-job="${esc(task.job.job_id)}"><span><b>${esc(task.title)}</b><small>${esc(task.meta)}</small></span><em class="${task.status==='排队中'?'queued':'running'}">${esc(task.status)}</em></button>`).join(''):'<p>当前没有进行中的模型任务</p>';
  $$('[data-background-job]',drawer).forEach(button=>button.onclick=()=>{const job=(state.videos?.jobs||[]).find(item=>item.job_id===button.dataset.backgroundJob);if(job)openVideoDrawer(job)});
}
$('#studio-task-toggle').onclick=()=>{state.studioTaskDrawerOpen=!state.studioTaskDrawerOpen;renderStudioTaskDrawer()};
$$('.nav-item').forEach(x=>x.addEventListener('click',()=>navigate(x.dataset.page,{historyMode:'push'})));$('#refresh').addEventListener('click',()=>loadPage(state.page,true));
function openAdvancedNavigation(){openModal('PRO TOOLS','项目工具',`<div class="advanced-nav-grid"><button data-advanced-page="projects"><b>项目档案</b><span>查看跨阶段生产记录</span></button><button data-advanced-page="scripts"><b>内容与剧本列表</b><span>批量管理来源、简报和候选剧本</span></button><button data-advanced-page="videos"><b>视频任务队列</b><span>集中诊断生成、复核和恢复任务</span></button><button data-advanced-page="publishing"><b>发布与数据</b><span>管理渠道授权、投稿和绩效回流</span></button></div>`);$$('[data-advanced-page]').forEach(button=>button.onclick=()=>{closeModal();navigate(button.dataset.advancedPage,{historyMode:'push'})})}
$('#open-advanced-nav').onclick=openAdvancedNavigation;
function navigateFromLocation(){const page=location.hash.replace(/^#/,"");if(pageMeta[page]&&page!==state.page)navigate(page,{historyMode:'none'})}
window.addEventListener('hashchange',navigateFromLocation);
window.addEventListener('popstate',navigateFromLocation);
function setPageLoadState(page,mode,message=''){const pageElement=$(`#page-${page}`);if(!pageElement)return;let banner=pageElement.querySelector('.page-load-state');if(mode==='idle'){banner?.remove();return}if(!banner){banner=document.createElement('div');banner.className='page-load-state';pageElement.prepend(banner)}banner.className=`page-load-state ${mode}`;if(mode==='loading'){banner.innerHTML='<i></i><span>正在加载最新数据</span>';return}banner.innerHTML=`<span><b>刷新失败，已保留当前内容</b><small>${esc(summarizeError(message))}</small></span><button type="button" class="mini-action" data-page-retry="${esc(page)}">重试</button>`;banner.querySelector('[data-page-retry]').onclick=()=>loadPage(page,true)}
const pageHasLoadedData=page=>Boolean({overview:state.overview,todos:state.todos,projects:state.projects,scripts:state.scripts,videos:state.videos,publishing:state.publishing,characters:state.characters,config:state.config}[page]);
async function loadPage(page,notify=false){
  if(state.page==='overview'&&page!=='overview')page='overview';
  const loadToken=++state.loadSequence;
  const stale=()=>loadToken!==state.loadSequence||state.page!==page;
  if(notify||!pageHasLoadedData(page))setPageLoadState(page,'loading');
  try{
    if(page==='overview'){
      const [o,e,p,s,v,pub]=await Promise.all([api('/api/overview'),api('/api/events?limit=12'),api('/api/projects'),api('/api/scripts'),api('/api/videos'),api('/api/publishing')]);
      if(stale())return false;
      state.overview=o;state.projects=p;state.scripts=s;state.videos=v;state.publishing=pub;state.recentEvents=e.events||[];
      renderOverview(o,e);renderStudio();
    }
    if(page==='todos'){
      const todos=await api('/api/todos');
      if(stale())return false;
      state.todos=todos;renderTodoCenter(todos);
    }
    if(page==='projects'){
      const projects=await api('/api/projects');
      if(stale())return false;
      state.projects=projects;renderProjects(projects);
    }
    if(page==='scripts'){
      const [scripts,projects,feeds,candidates]=await Promise.all([api('/api/scripts'),api('/api/projects'),api('/api/source-feeds'),api('/api/source-candidates')]);
      if(stale())return false;
      state.scripts=scripts;state.projects=projects;state.sourceFeeds=feeds;state.sourceCandidates=candidates;renderSourceCandidates();renderScripts(scripts);
    }
    if(page==='videos'){
      const [videos,overview,projects,productionHealth]=await Promise.all([api('/api/videos'),api('/api/overview'),api('/api/projects'),api('/api/production/health')]);
      if(stale())return false;
      state.videos=videos;state.overview=overview;state.projects=projects;state.productionHealth=productionHealth;renderVideos();
    }
    if(page==='publishing'){
      const [publishing,config,projects]=await Promise.all([api('/api/publishing'),api('/api/config'),api('/api/projects')]);
      if(stale())return false;
      state.publishing=publishing;state.config=config;state.projects=projects;renderPublishing(publishing);
    }
    if(page==='characters'){
      const characterLoadToken=++state.characterLoadSequence;
      const [characters,projects]=await Promise.all([fetchCharacters(),state.projects?Promise.resolve(null):api('/api/projects')]);
      if(stale()||characterLoadToken!==state.characterLoadSequence)return false;
      state.characters=characters;if(projects)state.projects=projects;renderCharacters();
    }
    if(page==='config'){
      const [config,projects]=await Promise.all([api('/api/config'),state.projects?Promise.resolve(null):api('/api/projects')]);
      if(stale())return false;
      state.config=config;if(projects)state.projects=projects;renderConfig();
    }
    if(stale())return false;
    renderGlobalProjects();syncGlobalStage(page);setPageLoadState(page,'idle');scheduleActiveProductionPoll();if(notify)toast('数据已刷新');
    return true;
  }catch(e){
    if(!stale())setPageLoadState(page,'error',e.message);
    if(!stale())toast(e.message,true);
    return false;
  }
}
function renderOverview(o,eventData){const cards=[['剧本资产',o.scripts.drafts,`${o.scripts.sources} 个来源 · ${o.scripts.briefs} 个选题`,'#1677ff'],['视频任务',o.videos.jobs,`${o.videos.active} 进行中 · ${o.videos.review_required} 待复核 · ${o.videos.failed} 其他异常`,'#1677ff'],['待发布',o.publishing.ready,`${o.publishing.mapped} 个平台作品已映射`,'#1677ff'],['模型成本',money(o.videos.cost_cents),'历史累计支出','#1677ff']];$('#overview-metrics').innerHTML=cards.map(c=>`<article class="metric-card" style="--accent:${c[3]}"><span class="metric-label">${c[0]}</span><div class="metric-value">${c[1]}</div><span class="metric-note">${c[2]}</span></article>`).join('');const f=[['01',`${o.scripts.sources} 来源`,'输入归档'],['02',`${o.scripts.drafts} 剧本`,'创意决策'],['03',`${o.videos.active} 任务`,'母片与剪辑'],['04',`${o.publishing.ready} 待发`,'分发与回流']];$('#factory-flow').innerHTML=f.map(x=>`<div class="flow-stage"><b>${x[0]}</b><span>${x[2]} · ${x[1]}</span></div>`).join('');$('#recent-events').innerHTML=eventData.events.length?eventData.events.map(e=>`<button class="event-row ${esc(e.level).toLowerCase()}" data-event-id="${esc(e.id)}" data-event-job="${esc(e.job_id||'')}"><i class="event-dot"></i><div><div class="activity-meta"><span>${esc(e.category)}</span><strong>${esc(e.title||'')}</strong></div><p>${esc(e.message)}</p></div><small>${short(e.ts)}</small></button>`).join(''):empty('暂无业务动态')}
function renderReliability(data){
  const target=$('#page-todos .todo-page-shell');
  if(!target)return;
  let host=$('#reliability-overview');
  if(!host){host=document.createElement('details');host.id='reliability-overview';host.className='reliability-overview'}
  if(host.parentElement!==target)target.insertBefore(host,target.firstElementChild);
  const reliability=data?.reliability||{};
  const seven=reliability.windows?.['7d']||{};
  const thirty=reliability.windows?.['30d']||{};
  const gate=reliability.release_gate||{status:'insufficient_data'};
  const scorecard=reliability.canary_scorecard||{};
  const v2=reliability.production_compiler_v2||{};
  const v2Gate=v2.switch_gate||{eligible:false,failed:['total_real_jobs']};
  const percent=value=>`${(Number(value||0)*100).toFixed(1)}%`;
  const gateText={pass:'发布门通过',freeze:'发布冻结',insufficient_data:'样本积累中'}[gate.status]||'样本积累中';
  const card=(title,report)=>`<article><span>${title}</span><b>${percent(report.first_round_progression_rate)}</b><small>重复付费 ${Number(report.duplicate_paid_submissions||0)} · 未知卡住 ${Number(report.unknown_stuck_jobs||0)} · 付费后确定失败 ${Number(report.deterministic_post_paid_failures||0)}</small></article>`;
  const qualityLabels={character_consistency:'人物一致性',scene_continuity:'场景连续性',staging:'人物站位',performance:'表演',lip_sync:'口型',subtitle_sync:'字幕同步',readable_text:'异常文字',editing_rhythm:'剪辑节奏'};
  const topFailure=(scorecard.top_failure_dimensions||[])[0];
  const qualityRate=Number(scorecard.reviewed_runs||0)?percent(scorecard.first_pass_usable_rate):'—';
  const qualityNote=`质量样本覆盖 ${Number(scorecard.covered_profiles||0)}/${Number(scorecard.required_profiles||0)} · 已复核 ${Number(scorecard.reviewed_runs||0)} 条${topFailure?` · 优先优化 ${qualityLabels[topFailure.dimension]||topFailure.dimension}`:''}`;
  const v2Status=v2Gate.eligible?'已满足切换门槛':`仍缺 ${Number((v2Gate.failed||[]).length)} 项`;
  host.innerHTML=`<summary><div><p class="section-no">RELIABILITY GATE</p><h3>生产可靠性</h3></div><div class="reliability-summary-meta"><span class="reliability-gate ${esc(gate.status)}">${gateText}</span><small>7 天 ${percent(seven.first_round_progression_rate)} · 首遍可用 ${qualityRate}</small></div></summary><div class="reliability-details"><div class="reliability-detail-actions"><span>展开查看可靠性、恢复与灰度门槛</span><button class="mini-action" id="start-quality-canary">人工质量金丝雀</button></div><div class="reliability-metrics">${card('7 天可靠性',seven)}${card('30 天趋势',thirty)}<article><span>恢复与人工复核</span><b>${percent(seven.recovery_success_rate)}</b><small>7 天恢复成功率 · 人工复核率 ${percent(seven.manual_review_rate)} · 耗时 P95 ${Math.round(Number(seven.duration_seconds?.p95||0))} 秒</small></article><article><span>首遍成片可用率</span><b>${qualityRate}</b><small>${esc(qualityNote)}</small></article><article class="v2-rollout-card ${v2Gate.eligible?'eligible':'accumulating'}"><span>生产编译器 V2 灰度</span><b>${Number(v2.total_real_jobs||0)} / 10</b><small>${esc(v2Status)} · 未知卡住 ${Number(v2.unknown_stuck_jobs||0)} · 重复付费 ${Number(v2.duplicate_paid_submissions||0)}</small></article></div></div>`;
  host.querySelector('#start-quality-canary').onclick=()=>openQualityCanary(reliability.canary_profiles||[]);
}
function openQualityCanary(profiles){
  if(!profiles.length)return toast('没有可用的固定金丝雀脚本',true);
  const options=profiles.map(profile=>`<option value="${esc(profile.id)}" data-budget="${Number(profile.budget_cents)}">${esc(profile.id)} · ${Number(profile.duration_sec)} 秒 · ${Number(profile.character_count)} 人 · 上限 ${money(profile.budget_cents)}</option>`).join('');
  openModal('EXPLICIT QUALITY CANARY','创建人工质量金丝雀',`<div class="form-stack"><p class="canary-warning">此入口只创建隔离的虚构角色验收任务，不读取生产真人资产，也不会由后台定时运行。创建后仍需在视频任务中再次确认制作预算</p><label>固定验收脚本<select id="quality-canary-profile">${options}</select></label><label>本次独立预算（分）<input id="quality-canary-budget" type="number" min="1" value="${Number(profiles[0].budget_cents)}"></label><label class="check-line"><input id="quality-canary-confirm" type="checkbox"> 我确认这是一次真实 Provider 小成本验收，费用使用上述独立预算</label></div><div class="modal-actions"><button class="mini-action" onclick="closeModal()">取消</button><button class="primary-action" id="quality-canary-create">创建隔离任务</button></div>`);
  const profile=$('#quality-canary-profile');
  profile.onchange=()=>{$('#quality-canary-budget').value=profile.selectedOptions[0].dataset.budget};
  $('#quality-canary-create').onclick=async()=>{
    if(!$('#quality-canary-confirm').checked)return toast('请先明确确认真实 Provider 金丝雀预算',true);
    try{
      const result=await api('/api/quality-canary/jobs',{method:'POST',body:JSON.stringify({profile_id:profile.value,budget_cents:Number($('#quality-canary-budget').value),confirmed:true})});
      closeModal();toast('隔离金丝雀任务已创建，尚未发生模型费用');await navigate('videos',{historyMode:'push'});const job=state.videos?.jobs?.find(item=>item.job_id===result.job_id);if(job)openVideoDrawer(job);
    }catch(error){toast(error.message,true)}
  };
}

async function openRecentEvent(event){
  if(event.draft_id)return openEntityInProject(event.draft_id,'draft','script');
  if(event.publication_id)return openEntityInProject(event.publication_id,'performance','publish');
  if(event.job_id){
    const job=state.videos?.jobs.find(item=>item.job_id===event.job_id);
    const stage=job&&(job.final_exists||['COMPOSED','READY_TO_PUBLISH','PUBLISHED','PUBLISHED_VERIFIED'].includes(job.state))?'review':'video';
    return openEntityInProject(event.job_id,'master_review',stage,{videoView:'sessions'});
  }
  toast('对应记录已不存在或不在当前列表中',true)
}

function bindRecentEvents(eventData){$$('[data-event-id]').forEach(button=>button.onclick=()=>openRecentEvent(eventData.events.find(event=>event.id===button.dataset.eventId)||{}))}

const renderOverviewBase=renderOverview;
renderOverview=function(o,eventData){renderOverviewBase(o,eventData);renderReliability(o);bindRecentEvents(eventData);const costCard=$$('#overview-metrics .metric-card').at(-1);if(costCard){const calibration=o.videos.cost_estimation||{};const segments=calibration.segments||{};const note=costCard.querySelector('.metric-note');const signed=value=>`${Number(value)>0?'+':''}${Number(value||0).toFixed(1)}%`;note.textContent=!calibration.sample_count?'暂无估算样本':calibration.status==='calibrating'?`估算样本 ${calibration.sample_count} · 总体偏差 ${signed(calibration.variance_percent)} · 样本不足，仅积累数据`:`估算样本 ${calibration.sample_count} · 总体偏差 ${signed(calibration.variance_percent)} · 平均绝对偏差 ${Number(calibration.mean_absolute_percentage_error||0).toFixed(1)}%`;note.title=`已归集调用明细 ${money(o.videos.tracked_call_cost_cents)} · 未分解成本 ${money(o.videos.unallocated_cost_cents)}；${o.videos.reconciliation_status==='reconciled'?'成本明细已对齐':'部分历史成本尚未能拆分到具体模型调用'}`;const group=(label,items)=>`<div><b>${label}</b><span>${(items||[]).length?(items||[]).map(item=>`${esc(item.key)} ${signed(item.variance_percent)}`).join(' · '):'积累中'}</span></div>`;const detail=document.createElement('div');detail.className='cost-calibration-groups';detail.innerHTML=group('分辨率校准',segments.resolution)+group('时长校准',segments.duration_bucket)+group('参考素材校准',segments.reference_type);costCard.append(detail)}};

const todoGroupLabels={content_decision:'需内容决策',production_setup:'需制作设置',manual_review:'需人工复核',provider_action:'需模型核对',final_acceptance:'需成片验收',publish:'需发布',performance:'需绩效录入'};
function renderTodoCenter(data){const target=$('#action-queue');if(!target)return;const groups=data?.groups||[];target.innerHTML=`<div class="action-queue-title"><div><p class="section-no">DECISION QUEUE</p><h3>全流程决策队列</h3></div><span>${data?.total||0} 项需要人工处理</span></div><div class="todo-groups">${groups.map(group=>`<section class="todo-group ${group.count?'has-items':''}"><header><span>${esc(todoGroupLabels[group.key]||group.title)}</span><b>${group.count}</b></header><div>${group.items.length?group.items.map(item=>`<button data-todo-group="${esc(group.key)}" data-todo-kind="${esc(item.kind)}" data-todo-id="${esc(item.id)}" data-todo-stage="${esc(item.stage||'')}"><span><strong>${esc(item.title)}</strong><small>${esc(item.id)}${item.window_hours?` · ${item.window_hours} 小时窗口`:''}</small></span><i>›</i></button>`).join(''):'<p>当前已清空</p>'}</div></section>`).join('')}</div>`;$$('[data-todo-id]').forEach(button=>button.onclick=()=>openTodoTarget({id:button.dataset.todoId,kind:button.dataset.todoKind,stage:button.dataset.todoStage,group:button.dataset.todoGroup}))}
function missingTodoTarget(){toast('待办对应记录已不存在，请刷新后重试',true)}
async function openTodoTarget(todo){
  if(todo.kind==='candidate'){
    if(!state.sourceCandidates)state.sourceCandidates=await api('/api/source-candidates');
    const item=(state.sourceCandidates?.candidates||[]).find(row=>row.candidate_id===todo.id);
    return item?openSourceCandidateDrawer(item):missingTodoTarget();
  }
  const stage={source:'source',brief:'brief',draft:'script',production_setup:'video',anchor_review:'video',master_review:'video',provider_action:'video',final_acceptance:'review',publish:'publish',performance:'publish'}[todo.kind];
  if(!stage)return missingTodoTarget();
  const project=findProjectForEntity(todo.id,todo.kind);
  if(!project)return missingTodoTarget();
  const focus=todo.kind==='anchor_review'?{videoView:'anchors'}:['master_review','provider_action'].includes(todo.kind)?{videoView:'sessions'}:{};
  return openEntityInProject(todo.id,todo.kind,stage,focus);
}

const studioStageOrder=['source','brief','script','video','review','publish'];
const studioPublishPlatforms=['douyin','weixin_channels'];
const studioStageIndex=stage=>Math.max(0,studioStageOrder.indexOf(stage==='production_setup'||stage==='production'?'video':stage==='performance'?'publish':stage));
const projectPublicationRows=project=>(state.publishing?.publications||[]).filter(item=>item.job_id===project.video?.job_id);
function studioRelated(project){const source=state.scripts?.sources.find(item=>item.source_id===project.source?.source_id)||project.source;const brief=state.scripts?.briefs.find(item=>item.brief_id===project.brief?.brief_id)||project.brief;const drafts=(state.scripts?.drafts||[]).filter(item=>item.brief_id===brief?.brief_id);const selected=drafts.find(item=>item.draft_id===project.selected_script_id)||drafts.find(item=>item.status==='approved');const video=state.videos?.jobs.find(item=>item.job_id===project.video?.job_id)||project.video;return{source,brief,drafts,selected,video,publications:projectPublicationRows(project)}}
function stageAvailability(project){const r=studioRelated(project);return{source:!!r.source,brief:!!r.brief,script:!!r.drafts.length,video:!!r.video,review:!!r.video,publish:!!r.video&&(r.video.state==='READY_TO_PUBLISH'||r.video.state==='PUBLISHED'||r.publications.length>0)}}
function studioStageInspection(project,r,stage){
  const pipelineStage=studioStageOrder[studioStageIndex(project.stage)];
  const viewedIndex=studioStageOrder.indexOf(stage);
  const pipelineIndex=studioStageOrder.indexOf(pipelineStage);
  const available=stageAvailability(project)[stage];
  const sourceText=sourceStudioContent(r.source||{}).trim();
  const brief=r.brief?.brief||{};
  const angles=Array.isArray(brief.angles)?brief.angles:[];
  const facts=Array.isArray(brief.fact_ledger)?brief.fact_ledger:Array.isArray(brief.key_facts)?brief.key_facts:[];
  const script=r.selected?.script||{};
  const beats=Array.isArray(script.beats)?script.beats:[];
  const video=r.video;
  const masters=video?studioVideoMasters(video):[];
  const characterAnchors=video?.character_makeup_plan||[];
  const sceneAnchors=video?.scene_anchor_plan||[];
  const qualityIssues=studioReviewIssues(video);
  const review=video?.latest_review;
  const checksByStage={
    source:[
      {label:'原始内容已归档',passed:Boolean(r.source)},
      {label:'来源正文可供后续阶段引用',passed:Boolean(sourceText)},
      {label:'来源类型和创建信息可追溯',passed:Boolean(r.source?.source_type)}
    ],
    brief:[
      {label:'至少存在一个可比较的选题角度',passed:angles.length>0},
      {label:'选题保留来源证据',passed:facts.length>0||Boolean(sourceText)},
      {label:'目标受众与传播承诺已明确',passed:Boolean(brief.audience&&angles.some(item=>item.promise||item.hook))}
    ],
    script:[
      {label:'已有明确选中的剧本版本',passed:Boolean(r.selected)},
      {label:'每个场次都有画面与时长',passed:beats.length>0&&beats.every(item=>item.visual_intent&&Number(item.duration_sec)>0)},
      {label:'有台词的场次均标明说话角色',passed:beats.filter(item=>item.dialogue).every(item=>item.speaker)}
    ],
    video:[
      {label:'母片计划与内部场次关系已建立',passed:masters.length>0&&Boolean(video?.director_timeline?.length)},
      {label:'所有出镜人物均有身份锚点',passed:Boolean(video)&&(!video.characters?.length||characterAnchors.length>=video.characters.length)},
      {label:'跨母片核心场景已有连续性锚点',passed:Boolean(video)&&(!video.scene_anchor_required||sceneAnchors.length>0)}
    ],
    review:[
      {label:'成片文件可完整播放',passed:Boolean(video?.final_exists)},
      {label:'自动质检没有未处理问题',passed:Boolean(video?.final_exists)&&qualityIssues.length===0},
      {label:'成片已完成人工验收',passed:Boolean(review&&['approved','accepted','pass'].includes(String(review.decision||'').toLowerCase()))}
    ],
    publish:[
      {label:'成片已进入待发布状态',passed:Boolean(video&&['READY_TO_PUBLISH','PUBLISHED'].includes(video.state))},
      {label:'至少一个渠道已完成发布',passed:r.publications.length>0},
      {label:'发布结果可追溯到平台作品',passed:r.publications.some(item=>item.item_id)}
    ]
  };
  const checks=checksByStage[stage]||[];
  const passedCount=checks.filter(item=>item.passed).length;
  const score=checks.length?Math.round(passedCount/checks.length*100):0;
  const completed=viewedIndex<pipelineIndex||(viewedIndex===pipelineIndex&&stage==='publish'&&project.stage==='performance');
  const status=completed?'completed':viewedIndex===pipelineIndex?'current':available?'available':'locked';
  const statusLabels={completed:'已完成',current:'当前阶段',available:'可查看',locked:'未开始'};
  const failed=checks.filter(item=>!item.passed);
  return{
    viewed_stage:stage,
    pipeline_stage:pipelineStage,
    status,
    status_label:statusLabels[status],
    score,
    checks,
    summary:failed.length?`${failed.length} 项尚未满足`:'当前阶段检查已满足',
    suggestion:stage===pipelineStage?(project.quality_gate?.suggestion||'使用顶部主操作继续项目流程'):`当前正在查看${statusLabels[status]}阶段；项目下一步仍是“${projectActionLabel(project.next_action)}”`
  };
}
function findProjectForEntity(id,kind=''){
  if(!id)return null;
  const projects=state.projects?.projects||[];
  const publication=(state.publishing?.publications||[]).find(item=>item.publication_id===id);
  const ready=(state.publishing?.ready||[]).find(item=>item.job_id===id);
  const jobId=publication?.job_id||ready?.job_id||(kind.includes('video')||['production_setup','anchor_review','master_review','provider_action','final_acceptance','publish'].includes(kind)?id:'');
  return projects.find(project=>project.project_id===id
    ||project.source?.source_id===id
    ||project.brief?.brief_id===id
    ||project.selected_script_id===id
    ||project.video?.job_id===jobId
    ||studioRelated(project).drafts.some(item=>item.draft_id===id));
}
function studioFocusForEntity(project,id,kind,focus={}){
  const resolved={...focus};
  const related=studioRelated(project);
  if(resolved.outlineIndex==null&&kind==='draft'){
    const draftIndex=related.drafts.findIndex(item=>item.draft_id===id);
    if(draftIndex>=0)resolved.outlineIndex=draftIndex;
  }
  if(resolved.outlineIndex==null&&kind==='performance'){
    const publication=related.publications.find(item=>item.publication_id===id);
    const platformIndex=studioPublishPlatforms.indexOf(publication?.platform);
    if(platformIndex>=0)resolved.outlineIndex=platformIndex;
  }
  return resolved;
}
async function openProjectStudio(project,stage,focus={}){
  if(!project)return false;
  const projectChanged=state.studioProjectId!==project.project_id;
  state.studioProjectId=project.project_id;
  if(projectChanged)state.studioTimelineScope='master';
  syncGlobalProjectSelection();
  state.studioStage=stage||studioStageOrder[studioStageIndex(project.stage)];
  state.studioOutlineIndex=focus.outlineIndex==null?-1:Number(focus.outlineIndex);
  state.studioFocus=focus;
  if(focus.videoView)state.studioVideoView=focus.videoView;
  state.studioMasterIndex=focus.masterIndex==null?0:Number(focus.masterIndex);
  if(state.page!=='overview'){
    const loaded=await navigate('overview',{historyMode:'push'});
    if(!loaded)return false;
  }
  const fresh=(state.projects?.projects||[]).find(item=>item.project_id===project.project_id)||project;
  renderStudioProject(fresh);
  return true;
}
async function openEntityInProject(id,kind,stage,focus={},fallback){
  const project=findProjectForEntity(id,kind);
  if(project)return openProjectStudio(project,stage,studioFocusForEntity(project,id,kind,focus));
  if(fallback)return fallback();
  return missingTodoTarget();
}
function renderStudio(){const projects=state.projects?.projects||[];renderStudioTaskDrawer();if(!projects.length){$('#studio-stage-body').innerHTML='<div class="studio-empty"><b>还没有内容项目</b><span>从左侧工作空间创建第一个内容项目</span></div>';return}if(!projects.some(item=>item.project_id===state.studioProjectId)){const initialProject=projects[0];state.studioProjectId=initialProject.project_id;state.studioStage=studioStageOrder[studioStageIndex(initialProject.stage)];state.studioOutlineIndex=-1}renderGlobalProjects();const project=projects.find(item=>item.project_id===state.studioProjectId);renderStudioProject(project)}
function studioReviewIssues(video){
  return(video?.shots||[]).flatMap(shot=>{
    const resolved=shot.state==='OK'&&!shot.error&&!shot.manual_review_required&&(
      ['candidate_accepted','human_review_approved'].includes(shot.quality_status)
      ||(shot.quality_status==='candidate_rejected'&&shot.human_decision==='keep_prior'));
    return resolved?[]:(shot.quality_reasons||[]).map(reason=>({masterIndex:shot.idx,reason}));
  });
}
function studioPreferredDraftIndex(r){const drafts=r.drafts||[];const approvedIndex=drafts.findIndex(item=>item.draft_id===r.selected?.draft_id||item.status==='approved');const recommendedIndex=drafts.findIndex((item,index)=>Number(item.variant_index??index)===Number(item.recommended_index));return approvedIndex>=0?approvedIndex:recommendedIndex>=0?recommendedIndex:0}
function studioDefaultPublishIndex(project,r){const ready=(state.publishing?.ready||[]).find(item=>item.job_id===project.video?.job_id);const candidates=project.next_action==='record_performance'?(r.publications||[]).map(item=>item.platform):(ready?.remaining_platforms||studioPublishPlatforms.filter(platform=>!(r.publications||[]).some(item=>item.platform===platform)));const index=studioPublishPlatforms.indexOf(candidates[0]);return index>=0?index:0}
function studioPrimaryActionLabel(project,r){
  if(r.video?.network_retry_indexes?.length||r.video?.network_retry_anchor_ids?.length)return r.video.next_action.label;
  if(project.next_action==='generate_scripts')return state.studioStage==='brief'?'使用当前角度生成剧本':'使用推荐角度生成剧本';
  if(project.next_action==='review_script'){
    const draftIndex=state.studioStage==='script'?state.studioOutlineIndex:studioPreferredDraftIndex(r);
    const draft=r.drafts?.[draftIndex]||r.selected||r.drafts?.[0];
    return draft?`审核当前版本 V${Number(draft.variant_index||0)+1}`:'生成候选剧本';
  }
  if(project.next_action==='view_progress'){
    if(r.video?.next_action?.id==='review_continuity_anchors')return r.video.next_action.label;
    const master=r.video?studioSelectedMaster(r.video):null;
    return master?`查看当前母片 M${String(master.idx).padStart(2,'0')}`:'查看生成进度';
  }
  if(project.next_action==='review_video'){
    const issueIndex=state.studioStage==='review'?state.studioOutlineIndex:0;
    const issue=studioReviewIssues(r.video)[issueIndex];
    return issue?`处理当前问题 M${String(issue.masterIndex).padStart(2,'0')}`:'验收成片';
  }
  if(project.next_action==='continue_publish')return state.studioStage==='publish'?'发布到当前渠道':'发布到下一渠道';
  if(project.next_action==='record_performance')return state.studioStage==='publish'?'录入当前渠道绩效':'录入渠道绩效';
  return projectActionLabel(project.next_action);
}
function renderStudioPrimaryAction(project,r){
  const primary=$('#studio-primary-action');
  if(!primary)return;
  const currentStage=studioStageOrder[studioStageIndex(project.stage)];
  if(state.studioStage!==currentStage){
    primary.hidden=true;
    primary.disabled=true;
    primary.textContent='';
    primary.onclick=null;
    primary.setAttribute('aria-hidden','true');
    return;
  }
  primary.hidden=false;
  primary.disabled=false;
  primary.removeAttribute('aria-hidden');
  primary.textContent=studioPrimaryActionLabel(project,r);
  primary.onclick=()=>routeProjectAction(project)
}
function renderStudioDoctorAction(project,r){
  const button=$('#studio-doctor-action');
  if(!button)return;
  const video=r?.video;
  const visible=Boolean(video)&&['video','review'].includes(state.studioStage);
  button.hidden=!visible;
  button.disabled=!visible;
  button.onclick=visible?()=>openTaskDoctor(video):null;
}
document.addEventListener('click',event=>{if(!event.target.closest?.('[data-studio-outline],[data-studio-master]'))return;queueMicrotask(()=>{const project=(state.projects?.projects||[]).find(item=>item.project_id===state.studioProjectId);if(!project)return;const related=studioRelated(project);renderStudioInspector(project,related);renderStudioPrimaryAction(project,related)})})
function renderStudioProject(project){if(!project)return;const r=studioRelated(project);const available=stageAvailability(project);if(!available[state.studioStage]){state.studioStage=studioStageOrder[Math.min(studioStageIndex(project.stage),studioStageOrder.length-1)];state.studioOutlineIndex=-1}$('#studio-project-code').textContent=`项目 / ${project.project_id}`;$('#studio-project-title').textContent=project.title;$('#studio-project-state').textContent=projectStageLabel(project.stage);$('#studio-project-meta').textContent=`${sourceTypeLabel(project.source.source_type)} · ${projectStageLabel(project.stage)} · ${project.quality_gate.score} 分`;const buttons=$$('#studio-stages button');buttons.forEach(button=>{const stage=button.dataset.studioStage;const inspection=studioStageInspection(project,r,stage);button.classList.toggle('active',stage===state.studioStage);button.classList.toggle('done',inspection.status==='completed');button.disabled=!available[stage]});buttons.forEach(button=>button.onclick=()=>{state.studioStage=button.dataset.studioStage;state.studioOutlineIndex=-1;state.studioMasterIndex=0;state.studioFocus=null;renderStudioProject(project)});$$('[data-global-stage]').forEach(button=>{const stage=button.dataset.globalStage;const inspection=studioStageInspection(project,r,stage);button.disabled=!available[stage];button.classList.toggle('current',stage===state.studioStage);button.classList.toggle('done',inspection.status==='completed')});renderStudioStage(project,r);renderStudioInspector(project,r);renderStudioPrimaryAction(project,r);renderStudioDoctorAction(project,r)}
function studioTextPanel(label,title,body,meta=''){if(label==='SOURCE MATERIAL'){const project=(state.projects?.projects||[]).find(item=>item.project_id===state.studioProjectId);const source=project?studioRelated(project).source:null;if(source)body=sourceStudioContent(source)}return `<article class="studio-content-card"><p class="section-no">${esc(label)}</p><h3>${esc(title)}</h3><div class="studio-reading">${esc(body||'暂无内容')}</div>${meta?`<small>${esc(meta)}</small>`:''}</article>`}
function studioVideoMasters(video){const actual=new Map((video.shots||[]).map(item=>[Number(item.idx),item]));return (video.planned_masters||[]).map((item,index)=>{const idx=Number(item.idx||index+1);return{...item,...(actual.get(idx)||{}),idx}})}
function studioMasterTitle(master){if(master.type==='broll')return master.scene_anchor_description||master.scene||'证据 B-roll';const speakers=master.speakers?.length?master.speakers:[master.speaker];return speakers.filter(Boolean).join('、')||master.scene_anchor_description||'对白母片'}
function studioMasterSubtitle(master){return master.type==='broll'?'场景与证据画面':master.render_mode==='cinematic_character'?'电影化连续情境表演':master.shot_type||'连续对白表演'}
function studioMasterScenes(video,master){return (video.director_timeline||[]).filter(item=>Number(item.video_master_idx)===Number(master.idx)||Number(item.audio_master_idx)===Number(master.idx))}
function studioSelectedMaster(video){const masters=studioVideoMasters(video);if(!masters.length)return null;if(!masters.some(item=>Number(item.idx)===Number(state.studioMasterIndex)))state.studioMasterIndex=Number(masters[0].idx);return masters.find(item=>Number(item.idx)===Number(state.studioMasterIndex))||masters[0]}
function studioMasterStatus(master){if(master.manual_review_required)return'待人工复核';if(master.error)return'生成异常';return masterStateLabel(master.state||'PENDING')}
function studioTimelineRows(video,selected){
  let cursor=0;
  return (video.director_timeline||[]).map((item,index)=>{
    const startSec=cursor;
    cursor+=Number(item.duration_sec||0);
    return {...item,timelineIndex:index,startSec,endSec:cursor,related:!selected||Number(item.video_master_idx)===Number(selected.idx)||Number(item.audio_master_idx)===Number(selected.idx)};
  });
}
function studioTimelineView(video,selected){
  const allItems=studioTimelineRows(video,selected);
  if(!allItems.length)return'<div class="studio-empty"><b>导演时间线尚未生成</b><span>完成导演规划后，将展示场次、画面轨、声音轨与衔接策略</span></div>';
  const showAll=state.studioTimelineScope==='all'||!selected;
  const items=showAll?allItems:allItems.filter(item=>item.related);
  const totalDuration=Number(allItems.at(-1)?.endSec||0);
  const referencedDuration=Number(items.reduce((sum,item)=>sum+Number(item.duration_sec||0),0).toFixed(1));
  const summary=showAll
    ?`<span>全片时间线</span><span>${allItems.length} 个场次</span><span>总时长 ${totalDuration.toFixed(1)} 秒</span>`
    :`<span>当前母片 · ${items.length} 个关联片段</span><span>引用时长 ${referencedDuration} 秒</span><span>全片 ${allItems.length} 个场次 · ${totalDuration.toFixed(1)} 秒</span>`;
  const scopeAction=selected?`<button type="button" class="mini-action" id="studio-timeline-scope">${showAll?'仅看当前母片':'查看全片时间线'}</button>`:'';
  return `<div class="studio-timeline-summary">${summary}${scopeAction}</div><div class="studio-director-timeline">${items.map(item=>{
    const videoMaster=Number(item.video_master_idx||0);
    const audioMaster=Number(item.audio_master_idx||0);
    const split=videoMaster&&audioMaster&&videoMaster!==audioMaster;
    const dependency=split?`<strong class="studio-track-dependency">跨母片 · 画面 M${String(videoMaster).padStart(2,'0')} / 声音 M${String(audioMaster).padStart(2,'0')}</strong>`:'';
    return `<article class="studio-timeline-beat related" data-studio-scene="${item.idx}"><span class="studio-timecode">${item.startSec.toFixed(1)}–${item.endSec.toFixed(1)}</span><div><header><b>${String(item.timelineIndex+1).padStart(2,'0')} ${esc(beatDisplayLabel(item.beat))}</b><i>${Number(item.duration_sec||0)}s</i></header><p>${esc(item.visual_intent||item.broll_hint||'按剧本节拍执行')}</p><footer><span>画面 · ${esc(item.video_source_label)}</span><span>声音 · ${esc(item.audio_source_label)}</span>${dependency}<em>${esc(transitionDisplay(item,item.timelineIndex))}</em></footer></div></article>`;
  }).join('')}</div>`;
}
function openStudioAnchorPreview(video,kind,index){
  const item=(kind==='character'?video.character_makeup_plan:video.scene_anchor_plan)?.[Number(index)];
  if(!item?.preview_url)return toast('当前锚点还没有可预览图片',true);
  const character=kind==='character';
  const title=character?(item.display_name||item.identity_key||'人物锚点'):(item.description||item.anchor_key||'场景锚点');
  const relation=character
    ?`${makeupStatusLabel(item.status)} · ${anchorApprovalLabel(item)}`
    :`关联母片 ${(item.master_indexes||[]).map(master=>`#${master}`).join('、')||'待识别'} · ${anchorApprovalLabel(item)}`;
  openModal(character?'IDENTITY ANCHOR':'SCENE ANCHOR',`${title} · ${character?'人物锚点':'场景锚点'}`,`<div class="studio-anchor-lightbox ${character?'character':'scene'}"><figure><img src="${esc(item.preview_url)}" alt="${esc(title)} ${character?'人物定妆照':'场景锚点图'}"></figure><aside><p class="section-no">ANCHOR DETAILS</p><h3>${esc(title)}</h3><dl><div><dt>锚点类型</dt><dd>${character?'人物身份与服装':'空间、光线与人物调度'}</dd></div><div><dt>生成方式</dt><dd>${esc(anchorGeneratorLabel(item.generator||item.source))}</dd></div><div><dt>当前状态</dt><dd>${esc(relation)}</dd></div></dl><a class="mini-action" href="${esc(item.preview_url)}" target="_blank" rel="noopener">在新窗口打开原图</a></aside></div>`);
}
function studioAnchorView(video,selected){
  const cast=new Set([...(selected?.cast||[]),...(selected?.speakers||[]),selected?.speaker].filter(Boolean).map(String));
  const people=video.character_makeup_plan||[];
  const scenes=video.scene_anchor_plan||[];
  const peopleMarkup=people.map((item,index)=>{
    const ids=[item.display_name,item.identity_key,...(item.character_ids||[])].filter(Boolean).map(String);
    const related=!selected||!cast.size||ids.some(id=>cast.has(id));
    const name=item.display_name||item.identity_key||'人物';
    const media=item.preview_url?`<button type="button" class="studio-anchor-media character" data-studio-anchor-preview="character" data-anchor-index="${index}" aria-label="预览 ${esc(name)} 人物锚点"><img src="${esc(item.preview_url)}" alt="${esc(name)} 定妆照" loading="lazy"><span>查看大图</span></button>`:`<span class="studio-anchor-placeholder character">${esc(name.slice(0,1))}</span>`;
    return `<article class="studio-anchor-card character ${related?'related':'muted'}">${media}<div><b>${esc(name)}</b><small>${esc(anchorGeneratorLabel(item.generator||item.source))}</small><em>${esc(makeupStatusLabel(item.status))} · ${esc(anchorApprovalLabel(item))}</em>${item.preview_url?'<span class="studio-anchor-hint">点击图片检查面部、服装和整体轮廓</span>':''}</div></article>`;
  }).join('')||'<p class="empty-inline">当前任务没有人物锚点</p>';
  const sceneMarkup=scenes.map((item,index)=>{
    const related=!selected||(item.master_indexes||[]).map(Number).includes(Number(selected.idx));
    const name=item.description||item.anchor_key||'场景';
    const media=item.preview_url?`<button type="button" class="studio-anchor-media scene" data-studio-anchor-preview="scene" data-anchor-index="${index}" aria-label="预览 ${esc(name)} 场景锚点"><img src="${esc(item.preview_url)}" alt="${esc(name)} 场景锚点" loading="lazy"><span>查看大图</span></button>`:'<span class="studio-anchor-placeholder scene">景</span>';
    return `<article class="studio-anchor-card scene ${related?'related':'muted'}">${media}<div><b>${esc(name)}</b><small>关联母片 ${(item.master_indexes||[]).map(master=>`#${master}`).join('、')||'待识别'}</small><em>${esc(anchorGeneratorLabel(item.generator))} · ${esc(anchorApprovalLabel(item))}</em>${item.preview_url?'<span class="studio-anchor-hint">点击图片检查空间、光线和构图基线</span>':''}</div>${sceneBlockingStage(item)}</article>`;
  }).join('')||'<p class="empty-inline">当前任务没有跨母片场景锚点</p>';
  const spatialModes=`<div class="studio-spatial-mode-legend" aria-label="空间连续性策略说明"><article class="free"><i></i><div><b>自由调度</b><small>允许进出画与自由走位</small></div></article><article class="continuity"><i></i><div><b>连续调度</b><small>承接上一母片结束状态</small></div></article><article class="locked"><i></i><div><b>精确锁定</b><small>仅明确构图时锁定</small></div></article></div>`;
  return `<section class="studio-anchor-section"><header><div><p class="section-no">IDENTITY ANCHORS</p><h3>角色定妆</h3></div><small>锁定人物身份与服装基线 · 点击图片可预览原图</small></header><div class="studio-anchor-grid">${peopleMarkup}</div></section><section class="studio-anchor-section"><header><div><p class="section-no">SCENE & BLOCKING</p><h3>场景与人物调度</h3></div><small>场景图锁定空间基线；人物按自由、连续或精确锁定策略运动</small></header>${spatialModes}<div class="studio-anchor-grid scenes">${sceneMarkup}</div></section>`;
}
function studioSessionsView(video,selected){const masters=studioVideoMasters(video);return `<div class="studio-session-list">${masters.map(master=>{const active=selected&&Number(master.idx)===Number(selected.idx);const scenes=studioMasterScenes(video,master);return `<article class="studio-session-card ${active?'active':''} ${master.error?'failed':''}" data-studio-master="${master.idx}"><span class="studio-session-index">M${String(master.idx).padStart(2,'0')}</span><div><header><b>${esc(studioMasterTitle(master))}</b><i>${esc(studioMasterStatus(master))}</i></header><p>${esc(studioMasterSubtitle(master))} · ${Number(master.duration_sec||master.duration||0)} 秒 · ${scenes.length} 个场次</p><footer><span>${esc(master.provider||'等待模型调度')}</span><span>${money(master.cost_cents)}</span>${master.error?`<em>${esc(summarizeError(master.error))}</em>`:''}</footer></div></article>`}).join('')||'<div class="studio-empty"><b>尚未创建生成会话</b></div>'}</div>${selected?.video_url?`<div class="studio-session-preview"><video controls preload="metadata" src="${esc(selected.video_url)}"></video><small>当前母片 M${String(selected.idx).padStart(2,'0')} 预览</small></div>`:''}`}
function studioVideoView(video,selected){if(state.studioVideoView==='anchors')return studioAnchorView(video,selected);if(state.studioVideoView==='sessions')return studioSessionsView(video,selected);return studioTimelineView(video,selected)}
function renderStudioVideoStage(video){
  const body=$('#studio-stage-body');
  const masters=studioVideoMasters(video);
  const selected=studioSelectedMaster(video);
  const selectedStatus=selected?studioMasterStatus(selected):'待规划';
  const editable=video.state==='STORYBOARDED'?'可调整制作设置':selected?.manual_review_required||selected?.error?'可单条返修':'制作过程只读';
  const workState={current:selected?`M${String(selected.idx).padStart(2,'0')} / ${masters.length}`:'尚未选择母片',business:selectedStatus,businessTone:selected?.error?'warning':selected?.manual_review_required?'warning':['COMPLETED','OK','ACCEPTED'].includes(String(selected?.state||'').toUpperCase())?'selected':'neutral',editability:editable,editTone:editable.includes('可')?'editable':'locked',note:'左侧选择母片；中央默认只显示引用当前母片的场次，并明确画面轨、声音轨和跨母片依赖'};
  body.innerHTML=`<div class="studio-video-workspace"><aside class="studio-master-plan"><header><div><p class="studio-pane-role">01 · 选择对象</p><h3>母片计划</h3></div><span>${masters.length} 条</span></header><div>${masters.map(master=>{const scenes=studioMasterScenes(video,master);return `<button class="studio-master-card ${selected&&Number(master.idx)===Number(selected.idx)?'active':''}" data-studio-master="${master.idx}"><span>M${String(master.idx).padStart(2,'0')}</span><div><b>${esc(studioMasterTitle(master))}</b><small>${esc(studioMasterSubtitle(master))}</small><em>参与场次 ${scenes.map(item=>String(item.idx).padStart(2,'0')).join('、')||'待规划'} · ${esc(studioMasterStatus(master))}</em></div><i>${Number(master.duration_sec||master.duration||0)}s</i></button>`}).join('')||'<p class="empty-inline">尚未规划母片</p>'}</div></aside><section class="studio-video-editor"><header><div><p class="studio-pane-role">02 · 内容工作区</p><h3>${selected?`M${String(selected.idx).padStart(2,'0')} · ${esc(studioMasterTitle(selected))}`:'导演工作区'}</h3><small>${selected?`${studioMasterScenes(video,selected).length} 个场次引用当前母片`:'选择母片查看关联场次'}</small></div><button class="mini-action" id="studio-open-video">打开当前母片检查器</button></header>${studioWorkState(workState)}<nav class="studio-video-tabs" aria-label="视频制作视图"><button class="${state.studioVideoView==='timeline'?'active':''}" data-studio-video-view="timeline">导演时间线</button><button class="${state.studioVideoView==='anchors'?'active':''}" data-studio-video-view="anchors">连续性锚点</button><button class="${state.studioVideoView==='sessions'?'active':''}" data-studio-video-view="sessions">生成会话</button></nav><div class="studio-video-view" id="studio-video-view">${studioVideoView(video,selected)}</div></section></div>`;
  $$('[data-studio-master]',body).forEach(button=>button.onclick=()=>{state.studioMasterIndex=Number(button.dataset.studioMaster);renderStudioVideoStage(video);const project=(state.projects?.projects||[]).find(item=>item.project_id===state.studioProjectId);if(project)renderStudioInspector(project,studioRelated(project))});
  $$('[data-studio-video-view]',body).forEach(button=>button.onclick=()=>{state.studioVideoView=button.dataset.studioVideoView;renderStudioVideoStage(video)});
  const timelineScope=body.querySelector('#studio-timeline-scope');
  if(timelineScope)timelineScope.onclick=()=>{state.studioTimelineScope=state.studioTimelineScope==='all'?'master':'all';renderStudioVideoStage(video)};
  $$('[data-studio-anchor-preview]',body).forEach(button=>button.onclick=()=>openStudioAnchorPreview(video,button.dataset.studioAnchorPreview,button.dataset.anchorIndex));
  $('#studio-open-video').onclick=()=>selected?openVideoDrawerAt(video,selected.idx):openVideoDrawer(video);
}
function studioWorkContract(stage){return{source:{title:'来源正文',hint:'阅读、核对并编辑当前来源'},brief:{title:'选题论证',hint:'比较角度并确认内容策略'},script:{title:'剧本审阅',hint:'审阅场次、角色、对白与时长'},review:{title:'成片与问题',hint:'播放成片并定位具体返修项'},publish:{title:'渠道与绩效',hint:'管理渠道发布及效果记录'}}[stage]||{title:'当前内容',hint:'处理当前选中对象'}}
function studioWorkState(value={}){
  const businessTone=value.businessTone||'neutral';
  const editTone=value.editTone||'neutral';
  return `<div class="studio-work-state" role="status">
    <span><small>当前对象</small><b>${esc(value.current||'当前内容')}</b></span>
    <span><small>业务状态</small><b class="tone-${esc(businessTone)}">${esc(value.business||'待处理')}</b></span>
    <span><small>编辑权限</small><b class="tone-${esc(editTone)}">${esc(value.editability||'只读')}</b></span>
    ${value.note?`<p>${esc(value.note)}</p>`:''}
  </div>`
}
function studioWorkspace(title,items,content,foot='',workState={}){const work=studioWorkContract(state.studioStage);return `<div class="studio-stage-workspace"><aside class="studio-outline"><header><div><p class="studio-pane-role">01 · 选择对象</p><b>${esc(title)}</b></div><span>${items.length} 项</span></header><div>${items.map((item,index)=>{const tag=item.interactive===false?'div':'button';const selected=index===state.studioOutlineIndex&&item.interactive!==false;const contextAttributes=item.reviewIssue?` data-studio-review-issue="${index}"`:item.publishChannel?` data-studio-publish-channel="${esc(item.publishChannel)}"`:'';return `<${tag} class="studio-outline-item ${selected?'active':''} ${item.interactive===false?'static':''}"${item.interactive===false?'':` data-studio-outline="${index}"${contextAttributes}`}><span>${item.code||String(index+1).padStart(2,'0')}</span><div><b>${esc(item.title)}</b><small>${esc(item.meta||'')}</small></div>${item.badge?`<em class="studio-outline-badge ${esc(item.badgeTone||'neutral')}">${esc(item.badge)}</em>`:''}</${tag}>`}).join('')}</div>${foot?`<footer>${foot}</footer>`:''}</aside><section class="studio-document"><header class="studio-document-head"><div><p class="studio-pane-role">02 · 内容工作区</p><b>${esc(work.title)}</b></div><small>${esc(work.hint)}</small></header>${studioWorkState(workState)}<div class="studio-document-scroll"><div class="studio-document-inner">${content}</div></div></section></div>`}
function bindStudioOutline(render,project,r){$$('[data-studio-outline]',$('#studio-stage-body')).forEach(button=>button.onclick=()=>{state.studioOutlineIndex=Number(button.dataset.studioOutline);render();renderStudioInspector(project,r);renderStudioPrimaryAction(project,r)})}
function studioSourceStage(project,r){const source=r.source||{};const items=[{code:'SRC',title:source.title||'原始来源',meta:`${sourceTypeLabel(source.source_type)} · 已归档`,badge:'已归档',badgeTone:'selected',interactive:false}];const reviewState=source.source_type==='video'&&source.metadata?.structure_analysis?sourcePerformanceReviewState(source):null;const reviewMarkup=reviewState?`<section class="studio-source-review ${reviewState.tone}"><header><div><small>解析后复核</small><b>确认说话人与表演</b></div><span class="performance-review-status ${reviewState.tone}">${esc(reviewState.label)}</span></header><p>${esc(reviewState.summary)}。核对结果会作为后续选题和剧本的人物依据。</p><div><span>${reviewState.total} 个片段</span><span>${reviewState.required} 个待确认</span><button class="mini-action" id="studio-review-performance">${esc(reviewState.action)}</button></div></section>`:'';const content=`<p class="studio-doc-kicker">SOURCE MATERIAL</p><h2>${esc(source.title||'未命名来源')}</h2><div class="studio-doc-meta"><span>${esc(sourceTypeLabel(source.source_type))}</span><span>${esc(source.source_display||'人工创建')}</span><span>${esc(source.created_at||'')}</span></div><div class="studio-doc-callout"><b>来源保真</b><p>原始内容完整保留，选题和剧本均可回溯到当前来源，不在此阶段改写事实。</p></div>${reviewMarkup}<div class="studio-doc-reading">${esc(sourceStudioContent(source)||'暂无正文')}</div><div class="studio-doc-actions"><button class="mini-action" id="studio-open-source">查看来源详情</button></div>`;const workState={current:'来源 1 / 1',business:'已归档',businessTone:'selected',editability:'只读归档',note:reviewState&&reviewState.required?`仍有 ${reviewState.required} 段人物信息待确认；建议复核后再生成简报`:'来源正文作为事实底稿保留；后续创作修改不会覆盖原始内容'};$('#studio-stage-body').innerHTML=studioWorkspace('来源对象',items,content,'<p>左侧定位来源对象；中央用于阅读、复核解析结果并继续创作。</p>',workState);$('#studio-open-source').onclick=()=>openSourceDrawer(source);const review=$('#studio-review-performance');if(review)review.onclick=()=>openSourcePerformanceReview(source)}
function studioBriefStage(project,r){const brief=r.brief?.brief||{};const angles=brief.angles||[];const recommendedIndex=Number(brief.recommended_index||0);const approvedIndex=r.brief?.approved_angle_index==null?null:Number(r.brief.approved_angle_index);const locked=(r.drafts||[]).some(item=>item.status==='approved'||item.approved_job_id);const hasDrafts=(r.drafts||[]).length>0;if(state.studioOutlineIndex<0||state.studioOutlineIndex>=angles.length)state.studioOutlineIndex=approvedIndex??recommendedIndex;const selected=angles[state.studioOutlineIndex]||angles[0]||{};const items=angles.map((angle,index)=>{const approved=index===approvedIndex;const recommended=index===recommendedIndex;return{code:String(index+1).padStart(2,'0'),title:angle.title||`创意角度 ${index+1}`,meta:approved?'已选定并用于生成剧本':recommended?'系统推荐方向':angle.promise||'备选方向',badge:approved?'已选定':recommended?'推荐':'备选',badgeTone:approved?'selected':recommended?'recommended':'neutral'}});const facts=(brief.fact_ledger||brief.key_facts||[]);const factMarkup=Array.isArray(facts)&&facts.length?`<div class="studio-evidence-list">${facts.slice(0,8).map((fact,index)=>`<article><span>${String(index+1).padStart(2,'0')}</span><p>${esc(typeof fact==='string'?fact:(fact.fact||fact.claim||fact.text||JSON.stringify(fact)))}</p></article>`).join('')}</div>`:'<p class="empty-inline">当前简报没有独立证据账本，生成时仍会引用完整来源。</p>';const content=`<p class="studio-doc-kicker">TOPIC DECISION</p><h2>${esc(selected.title||r.brief?.recommended_title||'选题简报')}</h2><div class="studio-doc-meta"><span>目标受众 · ${esc(brief.audience||'待补充')}</span><span>推荐方向 · ${recommendedIndex+1}</span><span>来源质量 · ${project.quality_gate.score} 分</span></div><div class="studio-doc-callout"><b>传播钩子</b><p>${esc(selected.hook||'待补充')}</p></div><section class="studio-doc-section"><h3>内容承诺</h3><p>${esc(selected.promise||brief.recommendation_reason||'待补充')}</p></section><section class="studio-doc-section"><h3>来源证据</h3>${factMarkup}</section><div class="studio-doc-actions"><button class="mini-action" id="studio-open-brief">${locked?'查看完整简报':'查看与编辑完整简报'}</button></div>`;const currentBusiness=state.studioOutlineIndex===approvedIndex?'已选定':state.studioOutlineIndex===recommendedIndex?'系统推荐':'备选方向';const workState={current:`角度 ${String(state.studioOutlineIndex+1).padStart(2,'0')} / ${angles.length}`,business:currentBusiness,businessTone:currentBusiness==='已选定'?'selected':currentBusiness==='系统推荐'?'recommended':'neutral',editability:locked?'已锁定':hasDrafts?'可编辑（将影响候选剧本）':'可编辑',editTone:locked?'locked':'editable',note:approvedIndex==null?'尚未选定正式方向；左侧“推荐”仅代表系统建议':'左侧“已选定”是实际用于剧本生成的方向，与当前查看项相互独立'};$('#studio-stage-body').innerHTML=studioWorkspace('候选角度',items,content,'<p>高亮表示当前查看；“已选定”表示实际进入剧本生成的方向。</p>',workState);bindStudioOutline(()=>studioBriefStage(project,r),project,r);$('#studio-open-brief').onclick=()=>openBriefDrawer(r.brief,{angleIndex:state.studioOutlineIndex})}
function scriptSpeakerIds(beat){const raw=Array.isArray(beat.speakers)&&beat.speakers.length?beat.speakers:[beat.speakers||beat.speaker];return [...new Set(raw.flatMap(value=>String(value?.id||value||'').split(/[,\uFF0C\u3001]/)).map(value=>value.trim()).filter(Boolean))]}
function scriptSpeakerLabel(beat,script){const names=new Map((script.characters||[]).map(character=>[character.id,character.name||character.id]));return scriptSpeakerIds(beat).map(id=>names.get(id)||id).join('\u3001')}
function studioScriptStage(project,r){const drafts=r.drafts||[];const approvedIndex=drafts.findIndex(item=>item.draft_id===r.selected?.draft_id||item.status==='approved');const recommendedIndex=Math.max(0,drafts.findIndex((item,index)=>Number(item.variant_index??index)===Number(item.recommended_index)));if(state.studioOutlineIndex<0||state.studioOutlineIndex>=drafts.length)state.studioOutlineIndex=approvedIndex>=0?approvedIndex:recommendedIndex;const selected=drafts[state.studioOutlineIndex]||r.selected||drafts[0];const script=selected?.script||{};const items=drafts.map((item,index)=>{const approved=index===approvedIndex;const recommended=index===recommendedIndex;return{code:`V${Number(item.variant_index??index)+1}`,title:item.script?.title||`剧本版本 ${index+1}`,meta:`${approved?'已选定':recommended?'系统推荐':'候选版本'} · ${Number(item.script?.total_duration_sec||item.script?.target_duration_sec||0)} 秒`,badge:approved?'已选定':recommended?'推荐':'候选',badgeTone:approved?'selected':recommended?'recommended':'neutral'}});const beats=(script.beats||[]).map((beat,index)=>`<article class="studio-scene-card"><header><span>${String(index+1).padStart(2,'0')}</span><b>${esc(beat.beat||`场次 ${index+1}`)}</b><i>${Number(beat.duration_sec||0)} 秒</i></header><div><section><small>画面</small><p>${esc(beat.visual_intent||'待补充')}</p></section><section><small>声音 / 连续性</small><p>${esc(beat.broll_hint||'沿用当前场景和角色设定')}</p></section></div><blockquote><span>${esc(scriptSpeakerLabel(beat,script)||'角色')}</span>${esc(beat.dialogue||'')}</blockquote></article>`).join('');const content=`<p class="studio-doc-kicker">SCRIPT · ${esc(selected?.status||'DRAFT')}</p><h2>${esc(script.title||'未命名剧本')}</h2><div class="studio-doc-meta"><span>${Number(script.total_duration_sec||script.target_duration_sec||0)} 秒</span><span>${new Set((script.beats||[]).flatMap(scriptSpeakerIds)).size} 位角色</span><span>${(script.beats||[]).length} 个场次</span></div>${script.creative_thesis?`<div class="studio-doc-callout"><b>创作意图</b><p>${esc(script.creative_thesis)}</p></div>`:''}<div class="studio-scene-list">${beats||'<p class="empty-inline">当前版本没有剧本场次</p>'}</div><div class="studio-doc-actions"><button class="mini-action" id="studio-open-script">${selected?.status==='candidate'?'查看、编辑与审核此版本':'查看已锁定版本'}</button></div>`;const currentBusiness=state.studioOutlineIndex===approvedIndex?'已选定':state.studioOutlineIndex===recommendedIndex?'系统推荐':'候选版本';const editable=selected?.status==='candidate';const workState={current:`${items[state.studioOutlineIndex]?.code||'V1'} / ${drafts.length}`,business:currentBusiness,businessTone:currentBusiness==='已选定'?'selected':currentBusiness==='系统推荐'?'recommended':'neutral',editability:editable?'可编辑并审核':'已锁定',editTone:editable?'editable':'locked',note:approvedIndex<0?'尚未审核选定正式剧本；系统推荐不等于已通过':'左侧“已选定”版本是视频生产唯一输入，其他版本仅供比较'};$('#studio-stage-body').innerHTML=studioWorkspace('剧本版本',items,content,'<p>高亮表示当前查看；“已选定”表示实际进入视频生产的剧本版本。</p>',workState);bindStudioOutline(()=>studioScriptStage(project,r),project,r);$('#studio-open-script').onclick=()=>openDraftDrawer(selected)}
function studioReviewStage(project,r){const video=r.video;if(!video){$('#studio-stage-body').innerHTML='<div class="studio-empty"><b>尚未创建成片任务</b></div>';return}const shotIssues=studioReviewIssues(video).map(({masterIndex,reason})=>{const shot=video.shots.find(item=>item.idx===masterIndex);return {code:`M${shot.idx}`,title:qualityReasonLabel(reason),meta:`母片 M${shot.idx} · ${shot.manual_review_required?'待确认':'已记录'}`,badge:shot.manual_review_required?'待处理':'已记录',badgeTone:shot.manual_review_required?'warning':'neutral',masterIndex:shot.idx,reason,reviewIssue:true}});const items=shotIssues.length?shotIssues:[{code:'✓',title:video.final_exists?'未发现阻塞问题':'等待成片生成',meta:video.display_state_label||videoStateLabel(video.display_state||video.state),badge:video.final_exists?'可验收':'等待中',badgeTone:video.final_exists?'selected':'neutral',interactive:false}];if(state.studioOutlineIndex>=items.length)state.studioOutlineIndex=0;const selectedIssue=shotIssues[state.studioOutlineIndex];const review=video.latest_review;const issueFocus=selectedIssue?`<div class="studio-doc-callout studio-review-focus" data-focused-review-issue="${state.studioOutlineIndex}"><b>${esc(selectedIssue.title)}</b><p>${esc(selectedIssue.meta)}。打开后将直接定位对应母片，避免误处理其他问题。</p></div>`:'';const content=`<p class="studio-doc-kicker">FINAL REVIEW</p><h2>成片验收</h2><div class="studio-doc-meta"><span>${esc(video.display_state_label||videoStateLabel(video.display_state||video.state))}</span><span>${video.master_count||0} 条母片</span><span>累计 ${money(video.cost_cents)}</span></div>${issueFocus}${video.final_exists?`<div class="studio-review-player"><video controls preload="metadata" src="${videoFileUrl(video)}"></video></div>`:`<div class="studio-empty compact"><b>成片尚未就绪</b><span>${video.completed_master_count||0}/${video.master_count||0} 条母片已完成，可先检查镜头阶段</span></div>`}<div class="studio-review-grid"><article><b>字幕与语音</b><small>按 ASR 时间轴逐句验收</small></article><article><b>人物连续性</b><small>对照定妆和跨母片引用</small></article><article><b>场景与站位</b><small>对照场景锚点和 180° 轴线</small></article><article><b>剪辑完整性</b><small>检查对白结尾与首尾衔接</small></article></div>${review?`<div class="studio-doc-callout"><b>最近验收 · ${esc(reviewDecisionLabel(review.decision))}</b><p>${esc(review.notes||'未填写备注')}</p></div>`:''}<div class="studio-doc-actions"><button class="mini-action" id="studio-review-video">${selectedIssue?'定位并处理该母片':video.final_exists?'打开验收工作区':'查看制作进度'}</button><a class="mini-action" href="${videoFileUrl(video,true)}" ${video.final_exists?'':'hidden'}>下载成片</a></div>`;const approved=review?.decision==='approved';const workState={current:selectedIssue?`${selectedIssue.code} · ${selectedIssue.title}`:'完整成片',business:approved?'成片已通过':shotIssues.length?'存在返修项':video.final_exists?'待验收':'生成中',businessTone:approved?'selected':shotIssues.length?'warning':'neutral',editability:video.final_exists?'可验收 / 退回返修':'等待成片',editTone:video.final_exists?'editable':'locked',note:selectedIssue?'当前查看的是具体问题；验收结论作用于完整成片':'问题列表为空不等于已通过，仍需人工完成成片验收'};$('#studio-stage-body').innerHTML=studioWorkspace('验收问题',items,content,'<p>左侧定位具体问题；中央播放和核对；右侧执行阶段决策。</p>',workState);$$('[data-studio-review-issue]',$('#studio-stage-body')).forEach(button=>button.onclick=()=>{state.studioOutlineIndex=Number(button.dataset.studioReviewIssue);studioReviewStage(project,r)});$('#studio-review-video').onclick=()=>selectedIssue?openVideoDrawerAt(video,selectedIssue.masterIndex):video.final_exists?openVideoReview(video,'approved'):openVideoDrawer(video)}
function studioPublishStage(project,r){const rows=r.publications||[];const platforms=studioPublishPlatforms;const items=platforms.map((platform,index)=>{const row=rows.find(item=>item.platform===platform);return{code:String(index+1).padStart(2,'0'),title:platformName(platform),meta:row?`已发布 · ${row.item_id}`:'等待选择与授权',badge:row?'已发布':'待发布',badgeTone:row?'selected':'neutral',publishChannel:platform}});if(state.studioOutlineIndex>=platforms.length)state.studioOutlineIndex=0;const selectedPlatform=platforms[state.studioOutlineIndex]||platforms[0];const selectedRow=rows.find(item=>item.platform===selectedPlatform);const content=`<p class="studio-doc-kicker">DISTRIBUTION</p><h2>发布与绩效</h2><div class="studio-doc-meta"><span>${rows.length} 个已发布渠道</span><span>${platforms.length} 个支持渠道</span><span>成片版本保持可追溯</span></div><div class="studio-doc-callout studio-publish-focus"><b>当前渠道 · ${esc(platformName(selectedPlatform))}</b><p>${selectedRow?`平台作品 ${esc(selectedRow.item_id)} 已登记，可继续录入绩效数据。`:'发布前检查账号授权、标题、封面、安全区和平台审核要求。'}</p></div><div class="studio-publish-cards">${platforms.map(platform=>{const row=rows.find(item=>item.platform===platform);return `<article class="${row?'ready':''} ${platform===selectedPlatform?'selected':''}" data-publish-card="${esc(platform)}"><span class="platform-logo ${platform}">${platformName(platform)[0]}</span><div><b>${platformName(platform)}</b><small>${row?`作品 ${row.item_id}`:'尚未发布'}</small><p>${row?'可继续登记播放、互动与转化数据':'发布前检查渠道授权、标题、封面和安全区'}</p></div><i>${row?'✓':'—'}</i></article>`}).join('')}</div><div class="studio-doc-actions"><button class="mini-action" id="studio-publish-action">${selectedRow?'录入当前渠道绩效':'发布到当前渠道'}</button></div>`;const workState={current:`${platformName(selectedPlatform)} · 渠道 ${state.studioOutlineIndex+1}/${platforms.length}`,business:selectedRow?'已发布':'待发布',businessTone:selectedRow?'selected':'neutral',editability:selectedRow?'可登记绩效':'可配置发布信息',editTone:'editable',note:'左侧切换当前渠道；每个渠道独立发布、独立记录结果'};$('#studio-stage-body').innerHTML=studioWorkspace('发布渠道',items,content,'<p>同一成片可多渠道发布；单渠道失败不会阻塞其他渠道。</p>',workState);$$('[data-studio-publish-channel]',$('#studio-stage-body')).forEach(button=>button.onclick=()=>{state.studioOutlineIndex=platforms.indexOf(button.dataset.studioPublishChannel);studioPublishStage(project,r)});$('#studio-publish-action').onclick=()=>openStudioPublishTarget(project,r,selectedPlatform)}
function openStudioPublishTarget(project,r,platform){const publication=(r.publications||[]).find(item=>item.platform===platform);if(publication)return openPublicationDrawer(publication);const ready=(state.publishing?.ready||[]).find(item=>item.job_id===project.video?.job_id);if(ready)return openPublishFlow(ready,platform);return toast(`当前成片还不能发布到${platformName(platform)}`,true)}
function renderStudioStage(project,r){
  if(state.studioStage==='source')return studioSourceStage(project,r);
  if(state.studioStage==='brief')return studioBriefStage(project,r);
  if(state.studioStage==='script')return studioScriptStage(project,r);
  if(state.studioStage==='video'){
    if(!r.video){$('#studio-stage-body').innerHTML='<div class="studio-empty"><b>尚未创建视频任务</b><span>审核剧本后即可进入制作设置</span></div>';return}
    return renderStudioVideoStage(r.video);
  }
  if(state.studioStage==='review'){if(state.studioOutlineIndex<0)state.studioOutlineIndex=0;return studioReviewStage(project,r)}
  if(state.studioOutlineIndex<0)state.studioOutlineIndex=studioDefaultPublishIndex(project,r);
  return studioPublishStage(project,r);
}
function studioStageContract(project,r,stage){
  const source=r.source||project.source||{};
  const brief=r.brief?.brief||{};
  const angles=brief.angles||[];
  const angle=angles[state.studioOutlineIndex]||angles[Number(brief.recommended_index||0)]||{};
  const drafts=r.drafts||[];
  const draft=drafts[state.studioOutlineIndex]||r.selected||drafts[0];
  const video=r.video;
  const master=video?studioSelectedMaster(video):null;
  const platforms=['douyin','weixin_channels'];
  const platform=platforms[state.studioOutlineIndex]||platforms[0];
  return {
    source:{name:'来源归档',purpose:'确认原始材料完整、可追溯，并能作为后续创作的事实底稿',object:source.title||'未命名来源',output:'可追溯的来源与事实底稿',downstream:'选题简报与剧本生成'},
    brief:{name:'选题决策',purpose:'在保留来源事实的前提下，选定受众、钩子和创意方向',object:angle.title||r.brief?.recommended_title||'待选择创意方向',output:'一个确认的选题方向',downstream:'候选剧本生成'},
    script:{name:'剧本定稿',purpose:'选定可拍摄版本并确认角色、场次、对白与时长',object:draft?.script?.title||'待选择剧本版本',output:'审核通过的剧本版本',downstream:'制作设置与母片规划'},
    video:{name:'镜头制作',purpose:'锁定角色、场景、站位和导演时间线，完成母片生产',object:master?`M${String(master.idx).padStart(2,'0')} · ${studioMasterTitle(master)}`:(video?.title||'待规划母片'),output:'母片、连续性锚点与导演时间线',downstream:'成片合成与验收'},
    review:{name:'成片验收',purpose:'逐项确认字幕、语音、连续性和剪辑完整性',object:video?.title||r.selected?.script?.title||'待生成成片',output:'已验收成片或明确返修项',downstream:'渠道发布'},
    publish:{name:'渠道发布',purpose:'选择渠道、完成发布并登记平台作品和绩效数据',object:platformName(platform),output:'平台作品与绩效记录',downstream:'效果复盘'}
  }[stage]||{name:'阶段判断',purpose:'确认当前阶段是否可以进入下一步',object:project.title,output:'阶段产物',downstream:'下一阶段'};
}
function renderStudioInspector(project,r){
  const inspection=studioStageInspection(project,r,state.studioStage);
  const contract=studioStageContract(project,r,state.studioStage);
  $('#studio-inspector-title').textContent='阶段判断';
  $('#studio-stage-status').textContent=inspection.status_label;$('#studio-stage-status').className=`tag stage-${inspection.status}`;
  const currentStage=studioStageOrder[studioStageIndex(project.stage)];
  const returnToCurrent=state.studioStage!==currentStage?`<button class="text-action studio-return-current" id="studio-return-current">返回当前阶段 · ${esc(projectStageLabel(project.stage))}</button>`:'';
  $('#studio-inspector-body').innerHTML=`<section class="studio-inspector-section studio-stage-summary"><p class="section-no">CURRENT STAGE</p><h4>${esc(contract.name)}</h4><p>${esc(contract.purpose)}</p>${returnToCurrent}</section><section class="studio-inspector-section"><header><b>进入下一阶段前需满足</b><span>${inspection.score} / 100</span></header><div class="studio-quality-bar"><i style="width:${Math.max(0,Math.min(100,inspection.score))}%"></i></div><p>${esc(inspection.summary)}</p><div class="studio-stage-checks">${inspection.checks.map(item=>`<span class="${item.passed?'passed':'pending'}">${esc(item.label)}</span>`).join('')}</div></section><section class="studio-inspector-section studio-stage-output"><b>本阶段产物</b>${detailRow('当前对象',contract.object)}${detailRow('交付内容',contract.output)}${detailRow('影响下一阶段',contract.downstream)}</section><section class="studio-inspector-section studio-stage-advice"><b>系统建议</b><p>${esc(inspection.suggestion)}</p></section>`;
  const returnButton=$('#studio-return-current');if(returnButton)returnButton.onclick=()=>openProjectStudio(project,currentStage);
}
const previewText=(value,n=76)=>{const t=String(value||'').replace(/\s+/g,' ').trim();return t.length>n?`${t.slice(0,n)}…`:t};
const sourceTypeLabel=value=>({manual:'人工文本',text:'人工文本',url:'公开网页',competitor:'竞品视频',video:'本地视频',audio:'本地音频'})[value]||value;
const projectStageLabel=value=>({source:'内容来源',brief:'选题决策',script:'剧本决策',production_setup:'制作设置',production:'视频生成',review:'成片验收',publish:'渠道发布',performance:'效果复盘'})[value]||value;
const projectActionLabel=value=>({generate_brief:'生成选题简报',generate_scripts:'确认选题并生成剧本',review_script:'审核剧本版本',configure_video:'完成制作设置',view_progress:'查看生成进度',review_video:'验收成片',submit_publish:'提交发布',continue_publish:'继续渠道发布',record_performance:'录入渠道绩效'})[value]||'查看详情';
function renderProjects(data){const projects=data.projects||[];const counts=projects.reduce((acc,p)=>{acc[p.stage]=(acc[p.stage]||0)+1;return acc},{});$('#project-summary').innerHTML=[['全部项目',projects.length],['待做内容决策',(counts.source||0)+(counts.brief||0)+(counts.script||0)],['制作与验收',(counts.production_setup||0)+(counts.production||0)+(counts.review||0)],['发布与复盘',(counts.publish||0)+(counts.performance||0)]].map(x=>`<article><small>${x[0]}</small><strong>${x[1]}</strong></article>`).join('');$('#project-list').innerHTML=projects.length?projects.map(p=>`<button class="project-row" data-project-id="${esc(p.project_id)}"><span class="project-title"><strong>${esc(p.title)}</strong><small>${esc(sourceTypeLabel(p.source.source_type))} · ${esc(p.project_id)}</small></span><span><i class="tag">${esc(projectStageLabel(p.stage))}</i></span><span><i class="quality-dot ${esc(p.quality_gate.level)}"></i>${esc(p.quality_gate.score)} 分</span><span>${p.published_platforms.length?p.published_platforms.map(platformName).join('、'):'尚未发布'}</span><span class="next-action">${esc(projectActionLabel(p.next_action))} ›</span></button>`).join(''):empty('还没有内容项目，请先创建内容来源');$$('[data-project-id]').forEach(b=>b.onclick=()=>{const project=projects.find(p=>p.project_id===b.dataset.projectId);if(project)openProjectStudio(project,studioStageOrder[studioStageIndex(project.stage)])})}
function openProjectDrawer(project){const q=project.quality_gate;const stages=[['来源',true],['选题',!!project.brief],['剧本',!!project.selected_script_id],['视频',!!project.video],['发布',!!project.published_platforms.length]];openDrawer('CONTENT PROJECT',project.title,`<div class="drawer-summary"><span class="tag">${esc(projectStageLabel(project.stage))}</span><p>${esc(q.reason)}。${esc(q.suggestion)}</p></div><div class="project-timeline">${stages.map(s=>`<div class="${s[1]?'done':''}"><i></i><span>${s[0]}</span></div>`).join('')}</div><section class="detail-section"><h3>项目档案</h3>${detailRow('来源',project.source.title||'未命名来源')}${detailRow('选题',project.brief?.recommended_title||'尚未生成')}${detailRow('已选剧本',project.selected_script_id||'尚未选择')}${detailRow('视频任务',project.video?.job_id||'尚未创建')}${detailRow('发布渠道',project.published_platforms.map(platformName).join('、')||'尚未发布')}</section><section class="detail-section"><h3>输入质量</h3><div class="quality-card ${esc(q.level)}"><strong>${q.score} 分 · ${q.passed?'可继续':'需要补充'}</strong><p>${esc(q.reason)}</p></div></section><div class="drawer-footer"><button class="primary-action" id="project-next-action">${esc(projectActionLabel(project.next_action))}</button></div>`);$('#project-next-action').onclick=()=>routeProjectAction(project)}
async function routeProjectAction(project){
  closeDrawer();
  const actionStage={generate_brief:'source',generate_scripts:'brief',review_script:'script',configure_video:'video',view_progress:'video',review_video:'review',submit_publish:'review',continue_publish:'publish',record_performance:'publish'}[project.next_action]||studioStageOrder[studioStageIndex(project.stage)];
  const preserveCurrent=state.page==='overview'&&state.studioProjectId===project.project_id&&state.studioStage===actionStage;
  const focus=preserveCurrent?{outlineIndex:state.studioOutlineIndex,masterIndex:state.studioMasterIndex,videoView:state.studioVideoView}:{};
  if(!await openProjectStudio(project,actionStage,focus))return;
  const activeProject=(state.projects?.projects||[]).find(item=>item.project_id===project.project_id)||project;
  const related=studioRelated(activeProject);
  if(related.video?.network_retry_indexes?.length||related.video?.network_retry_anchor_ids?.length)return confirmNetworkRetry(related.video);
  if(project.next_action==='generate_brief'){
    const source=related.source;
    if(source)return openSourceDrawer(source);
    return toast('对应内容来源已不存在',true);
  }
  if(project.next_action==='generate_scripts'){
    const brief=related.brief;
    if(brief)return openBriefDrawer(brief,{angleIndex:state.studioOutlineIndex});
    return toast('对应选题简报已不存在',true);
  }
  if(project.next_action==='review_script'){
    const drafts=related.drafts||[];
    if(!drafts.length)return toast('当前项目还没有可审核剧本',true);
    const draft=drafts[state.studioOutlineIndex]||related.selected||drafts[0];
    return openDraftDrawer(draft);
  }
  if(['configure_video','view_progress','review_video','submit_publish'].includes(project.next_action)){
    const job=related.video;
    if(!job)return toast('对应视频任务已不存在',true);
    if(project.next_action==='configure_video')return runVideoNextAction(job);
    if(project.next_action==='view_progress'){
      if(job.next_action?.id==='review_continuity_anchors')return openVideoDrawer(job);
      const master=studioSelectedMaster(job);
      return master?openVideoDrawerAt(job,master.idx):openVideoDrawer(job);
    }
    if(project.next_action==='review_video'){
      const issue=studioReviewIssues(job)[state.studioOutlineIndex];
      if(issue)return openVideoDrawerAt(job,issue.masterIndex);
      return job.final_exists?openVideoReview(job,'approved'):openVideoDrawer(job);
    }
    return openVideoDrawer(job);
  }
  if(['continue_publish','record_performance'].includes(project.next_action)){
    const platform=studioPublishPlatforms[state.studioOutlineIndex]||studioPublishPlatforms[0];
    return openStudioPublishTarget(project,related,platform);
  }
  return toast('当前项目还没有可处理的发布记录',true);
}
function renderScripts(d){const draftBriefIds=new Set(d.drafts.map(x=>x.brief_id));$('#source-count').textContent=d.sources.length;$('#brief-count').textContent=d.briefs.length;$('#draft-count').textContent=d.drafts.length;$('#source-list').innerHTML=d.sources.length?d.sources.map(x=>`<button class="record-row interactive" data-source-id="${esc(x.source_id)}"><div class="record-title"><strong>${esc(x.title||'未命名来源')}</strong><span class="tag">${esc(sourceTypeLabel(x.source_type))}</span></div><p class="content-preview">${esc(previewText(x.content)||'暂无正文')}</p><small>${esc(x.source_display||x.source_id)} · ${esc(x.created_at)}</small><i class="row-arrow">›</i></button>`).join(''):empty('尚未归档内容来源');$('#brief-list').innerHTML=d.briefs.length?d.briefs.map(x=>{const generationFailed=x.status==='approved'&&!draftBriefIds.has(x.brief_id);return `<button class="record-row interactive" data-brief-id="${esc(x.brief_id)}"><div class="record-title"><strong>${esc(x.recommended_title)}</strong><span class="tag ${generationFailed?'failed':esc(x.status)}">${generationFailed?'生成未完成，点击重试':x.status==='approved'?'已生成剧本':'待分析'}</span></div><p class="content-preview">${esc(previewText(briefSummary(x.brief))||'暂无简报摘要')}</p><small>${esc(x.brief_id)} · ${esc(x.created_at)}</small><i class="row-arrow">›</i></button>`}).join(''):empty('暂无选题简报');$('#draft-list').innerHTML=d.drafts.length?d.drafts.map(x=>`<div class="record-row interactive"><button class="record-main" data-draft-id="${esc(x.draft_id)}"><div class="record-title"><strong>${esc(x.script?.title||`版本 ${Number(x.variant_index)+1}`)}</strong><span class="tag ${esc(x.status)}">${esc(scriptStatus(x.status))}</span></div><p class="content-preview">${esc(previewText(scriptDialogue(x.script))||'暂无剧本台词')}</p><small>${x.recommended_index===x.variant_index?'系统推荐 · ':''}${esc(x.created_at)}</small></button>${x.status==='candidate'?`<div class="record-actions"><button class="mini-action primary" data-approve-script="${esc(x.draft_id)}">审核并提交视频</button><button class="mini-action danger" data-delete-script="${esc(x.draft_id)}">删除</button></div>`:''}</div>`).join(''):empty('暂无剧本候选');$$('[data-source-id]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.sourceId,'source','source',{},()=>openSourceDrawer(d.sources.find(x=>x.source_id===b.dataset.sourceId))));$$('[data-brief-id]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.briefId,'brief','brief',{},()=>openBriefDrawer(d.briefs.find(x=>x.brief_id===b.dataset.briefId))));$$('[data-draft-id]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.draftId,'draft','script',{},()=>openDraftDrawer(d.drafts.find(x=>x.draft_id===b.dataset.draftId))));$$('[data-approve-script]').forEach(b=>b.onclick=e=>{e.stopPropagation();openDraftDrawer(d.drafts.find(x=>x.draft_id===b.dataset.approveScript))});$$('[data-delete-script]').forEach(b=>b.onclick=e=>{e.stopPropagation();confirmDeleteScript(b.dataset.deleteScript)});applyScriptStage()}
const sourceKindLabel=kind=>({hotspot:'热点信息',competitor:'竞品内容'})[kind]||kind;
function renderSourceCandidates(){
  const candidates=state.sourceCandidates?.candidates||[];
  const count=$('#candidate-count'),list=$('#candidate-list');
  if(!count||!list)return;
  count.textContent=candidates.length;
  list.innerHTML=candidates.length?candidates.map(item=>`<div class="record-row interactive candidate-row"><button class="record-main" data-candidate-id="${esc(item.candidate_id)}"><div class="record-title"><strong>${esc(item.title||'未命名候选')}</strong><span class="tag">${esc((item.source_kinds||[item.source_kind]).map(sourceKindLabel).join(' + '))}</span></div><p class="content-preview">${esc(previewText(item.excerpt)||'暂无摘要')}</p><small>${esc(item.feed_name)} · ${Number(item.occurrence_count||1)} 个来源 · ${esc(item.published_at||item.created_at)}</small></button><div class="record-actions"><button class="mini-action primary" data-promote-candidate="${esc(item.candidate_id)}">晋级为内容来源</button><button class="mini-action" data-reject-candidate="${esc(item.candidate_id)}">不采用</button><button class="mini-action danger" data-delete-candidate="${esc(item.candidate_id)}">删除</button></div></div>`).join(''):empty('暂无自动选题候选；可添加已授权 RSS、Atom 或 JSON Feed');
  $$('[data-candidate-id]').forEach(button=>button.onclick=()=>openSourceCandidateDrawer(candidates.find(item=>item.candidate_id===button.dataset.candidateId)));
  $$('[data-promote-candidate]').forEach(button=>button.onclick=event=>{event.stopPropagation();promoteSourceCandidate(button.dataset.promoteCandidate)});
  $$('[data-reject-candidate]').forEach(button=>button.onclick=event=>{event.stopPropagation();rejectSourceCandidate(button.dataset.rejectCandidate)});
  $$('[data-delete-candidate]').forEach(button=>button.onclick=event=>{event.stopPropagation();confirmDeleteSourceCandidate(button.dataset.deleteCandidate)});
}
function openSourceCandidateDrawer(item){
  if(!item)return;
  const occurrences=item.occurrences||[];
  const occurrenceRows=occurrences.map(row=>`<article class="source-occurrence"><span><b>${esc(row.feed_name)}</b><small>${esc(sourceKindLabel(row.source_kind))} · ${esc(row.published_at||row.first_seen_at||'时间未知')}</small></span>${row.source_url?`<a href="${esc(row.source_url)}" target="_blank" rel="noreferrer">查看原文</a>`:'<em>无原文地址</em>'}</article>`).join('');
  openDrawer('AUTO TOPIC CANDIDATE',item.title||'未命名候选',`<div class="drawer-summary"><span class="tag">${esc((item.source_kinds||[item.source_kind]).map(sourceKindLabel).join(' + '))}</span><p>已合并 ${Number(item.occurrence_count||1)} 条来源证据。人工确认后才会进入正式内容项目</p></div><section class="detail-section source-content"><h3>候选摘要</h3><div class="reading-content">${esc(item.excerpt||'暂无摘要')}</div></section><section class="detail-section"><h3>来源证据</h3><div class="source-occurrence-list">${occurrenceRows||'<p class="empty-inline">暂无来源证据</p>'}</div></section><div class="drawer-footer"><button class="mini-action danger" id="drawer-delete-candidate">删除候选</button><button class="mini-action" id="drawer-reject-candidate">不采用</button><button class="primary-action" id="drawer-promote-candidate">晋级为内容来源</button></div>`);
  $('#drawer-delete-candidate').onclick=()=>{closeDrawer();confirmDeleteSourceCandidate(item.candidate_id)};
  $('#drawer-reject-candidate').onclick=()=>{closeDrawer();rejectSourceCandidate(item.candidate_id)};
  $('#drawer-promote-candidate').onclick=()=>{closeDrawer();promoteSourceCandidate(item.candidate_id)};
}
async function promoteSourceCandidate(candidateId){
  try{
    const result=await api(`/api/source-candidates/${encodeURIComponent(candidateId)}/promote`,{method:'POST',body:'{}'});
    toast('候选已晋级为内容来源，可继续生成选题简报');
    if(!await loadPage('scripts'))return;
    const source=state.scripts.sources.find(item=>item.source_id===result.source_id);
    if(source)openSourceDrawer(source);
  }catch(error){toast(error.message,true)}
}
function rejectSourceCandidate(candidateId){
  openModal('REVIEW DECISION','标记为不采用',`<div class="form-stack"><label>审核备注（可选）<textarea id="candidate-reject-reason" rows="3" placeholder="例如：与近期选题重复、证据不足"></textarea></label></div><div class="impact-callout"><strong>只影响该候选</strong><span>不会生成内容项目、剧本或视频，也不会触发任何付费模型调用</span></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-reject-candidate">确认不采用</button></div>`);
  $('#confirm-reject-candidate').onclick=async()=>{try{await api(`/api/source-candidates/${encodeURIComponent(candidateId)}/reject`,{method:'POST',body:JSON.stringify({reason:$('#candidate-reject-reason').value})});closeModal();toast('候选已标记为不采用');loadPage('scripts')}catch(error){toast(error.message,true)}};
}
function confirmDeleteSourceCandidate(candidateId){
  openModal('DELETE CANDIDATE','删除自动选题候选',`<p class="confirm-copy">仅删除候选池记录，不会影响已经晋级的内容来源</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-candidate">确认删除</button></div>`);
  $('#confirm-delete-candidate').onclick=async()=>{try{await api(`/api/source-candidates/${encodeURIComponent(candidateId)}`,{method:'DELETE'});closeModal();toast('候选已删除');loadPage('scripts')}catch(error){toast(error.message,true)}};
}
function openSourceFeedManager(){
  const feeds=state.sourceFeeds?.feeds||[];
  const statusLabel=feed=>feed.last_status==='error'?'采集异常':feed.last_status==='success'?'运行正常':'等待首次抓取';
  openModal('AUTHORIZED FEEDS','管理自动选题来源',`<div class="form-stack source-feed-form"><label>来源名称<input id="feed-name" placeholder="例如：行业政策授权源"></label><label>内容类型<select id="feed-source-kind"><option value="hotspot">热点信息</option><option value="competitor">竞品内容</option></select></label><label>Feed 格式<select id="feed-type"><option value="rss">RSS / Atom</option><option value="json_feed">JSON Feed</option></select></label><label>Feed 地址<input id="feed-endpoint" type="url" placeholder="https://example.com/feed.xml"><small>仅填写不含账号、Token 或签名密钥的 HTTPS 地址</small></label><label>抓取周期<select id="feed-interval-minutes"><option value="30">每 30 分钟</option><option value="60" selected>每 1 小时</option><option value="180">每 3 小时</option><option value="360">每 6 小时</option><option value="1440">每天</option></select></label><label class="check-line"><input id="feed-authorization-confirmed" type="checkbox"> 我确认已获得该来源内容的采集与使用授权</label><button class="primary-action" id="feed-create">添加授权来源</button></div><section class="feed-manager-list"><h3>已配置来源</h3>${feeds.length?feeds.map(feed=>`<article class="${feed.enabled?'':'disabled'}"><span><b>${esc(feed.name)}</b><small>${esc(sourceKindLabel(feed.source_kind))} · ${Number(feed.interval_minutes||60)} 分钟 · ${statusLabel(feed)}</small><small>下次抓取：${esc(feed.next_collect_at||'等待调度')} · 上次新增 ${Number(feed.last_created_count||0)} / 去重 ${Number(feed.last_duplicate_count||0)}</small>${feed.last_error?`<small><em>${esc(feed.last_error)}</em></small>`:''}</span><button class="mini-action" data-toggle-feed="${esc(feed.feed_id)}" data-feed-enabled="${feed.enabled?1:0}" data-feed-interval="${Number(feed.interval_minutes||60)}">${feed.enabled?'暂停':'启用'}</button><button class="mini-action" data-collect-feed="${esc(feed.feed_id)}">立即抓取</button><button class="mini-action danger" data-delete-feed="${esc(feed.feed_id)}">删除</button></article>`).join(''):empty('尚未配置自动来源')}</section><p class="modal-hint">系统只抓取已授权且到期的来源；结果先跨源去重并进入候选池，必须人工审核后才能转为内容项目</p>`);
  $('#feed-create').onclick=async()=>{try{await api('/api/source-feeds',{method:'POST',body:JSON.stringify({name:$('#feed-name').value,source_kind:$('#feed-source-kind').value,feed_type:$('#feed-type').value,endpoint:$('#feed-endpoint').value,interval_minutes:Number($('#feed-interval-minutes').value),authorization_confirmed:$('#feed-authorization-confirmed').checked})});closeModal();toast('授权来源已添加，将按周期自动抓取');if(!await loadPage('scripts'))return;openSourceFeedManager()}catch(error){toast(error.message,true)}};
  $$('[data-toggle-feed]').forEach(button=>button.onclick=async()=>{try{await api(`/api/source-feeds/${encodeURIComponent(button.dataset.toggleFeed)}/settings`,{method:'POST',body:JSON.stringify({enabled:button.dataset.feedEnabled!=='1',interval_minutes:Number(button.dataset.feedInterval)})});closeModal();toast(button.dataset.feedEnabled==='1'?'自动抓取已暂停':'自动抓取已启用');if(!await loadPage('scripts'))return;openSourceFeedManager()}catch(error){toast(error.message,true)}});
  $$('[data-collect-feed]').forEach(button=>button.onclick=async()=>{button.disabled=true;try{const result=await api(`/api/source-feeds/${encodeURIComponent(button.dataset.collectFeed)}/collect`,{method:'POST',body:'{}'});closeModal();toast(`采集完成，新增 ${result.created} 条候选`);await loadPage('scripts');state.scriptStage='candidates';applyScriptStage()}catch(error){button.disabled=false;toast(error.message,true)}});
  $$('[data-delete-feed]').forEach(button=>button.onclick=async()=>{try{await api(`/api/source-feeds/${encodeURIComponent(button.dataset.deleteFeed)}`,{method:'DELETE'});closeModal();toast('自动来源已删除');if(!await loadPage('scripts'))return;openSourceFeedManager()}catch(error){toast(error.message,true)}});
}
const scriptStatus=s=>({candidate:'待审核',approved:'已提交视频',rejected:'未选用'})[s]||s;
const videoStateLabel=s=>({STORYBOARDED:'待设置并生成',PENDING:'生成中',PRODUCTION_QUEUED:'已排队',PRODUCTION_RUNNING:'生成中',BUDGET_CONFIRMATION_REQUIRED:'预算待确认',ANCHOR_REVIEW_REQUIRED:'锚点待确认',PRODUCTION_REVIEW_REQUIRED:'母片待复核',PROVIDER_ACTION_REQUIRED:'模型账号待处理',REJECTED_PENDING_RECONCILIATION:'已终止，待成本核对',PRODUCTION_PAUSED:'待继续生成',SHOTS_OK:'母片已完成',BGM_OK:'声音已完成',COMPOSED:'成品',READY_TO_PUBLISH:'待发布',PUBLISHED:'已核实发布',PUBLISHED_VERIFIED:'已核实发布',LEGACY_UNVERIFIED:'历史状态待核实',FAILED:'失败',REJECTED:'已人工终止',BLOCKED:'已暂停'})[s]||s;
const scriptDialogue=script=>(script?.beats||[]).map(b=>b.dialogue).filter(Boolean).join(' ');
function briefSummary(brief){if(!brief)return'';const angle=(brief.angles||[])[brief.recommended_index||0]||{};return [brief.audience,angle.title,angle.thesis,angle.hook].filter(Boolean).join(' · ')}
const activeProductionStates=new Set(['PENDING','PRODUCTION_QUEUED','PRODUCTION_RUNNING','ANCHOR_REVIEW_REQUIRED','PRODUCTION_PAUSED','CHARACTER_OK','SHOTS_OK','VOICE_OK','BGM_OK']);
const autoPollProductionStates=new Set(['PENDING','PRODUCTION_QUEUED','PRODUCTION_RUNNING','CHARACTER_OK','SHOTS_OK','VOICE_OK','BGM_OK']);
const productionPollingPages=new Set(['overview','videos']);
const exceptionalVideoStates=new Set(['FAILED','BLOCKED','REJECTED']);
const manualReviewEligibleStates=new Set(['FAILED','PRODUCTION_REVIEW_REQUIRED']);
const jobNeedsManualReview=job=>job.recovery?job.recovery.action_ids.includes('review_master_candidates'):manualReviewEligibleStates.has(job.state)&&(job.state==='PRODUCTION_REVIEW_REQUIRED'||job.shots?.some(shot=>shot.manual_review_required));
const videoMatchesFilter=(job,filter)=>filter==='all'||(filter==='in_progress'?activeProductionStates.has(job.display_state||job.state):filter==='REVIEW_REQUIRED'?jobNeedsManualReview(job):filter==='FAILED'?(exceptionalVideoStates.has(job.state)&&!jobNeedsManualReview(job)):job.state===filter);
function renderProductionHealth(health){const target=$('#production-health');if(!target)return;if(!health){target.innerHTML='<span>生产队列状态暂不可用</span>';return}const running=(health.running||[]).length;const queued=(health.queued||[]).length;const capacity=health.master_capacity||{};const recovery=health.provider_recovery||{};target.classList.toggle('offline',!health.supervisor_alive);target.innerHTML=`<div><span>监督器</span><strong>${health.supervisor_alive?'运行中':'已停止'}</strong></div><div><span>生成中</span><strong>${running}</strong></div><div><span>排队中</span><strong>${queued}</strong></div><div><span>母片并发</span><strong>${capacity.effective_limit??'—'} / ${capacity.configured_limit??'—'}</strong><small>${capacity.last_reason==='definite_rate_limit'?'限流后自动降档':capacity.last_reason==='clean_wave_recovery'?'稳定运行后恢复一档':'按账号实测容量'}</small></div><div><span>远端核对</span><strong>${recovery.completed||0} 完成 · ${recovery.pending||0} 进行中</strong><small>${recovery.last_sweep_at?`最近核对 ${esc(recovery.last_sweep_at)}`:'等待首次自动核对'} · 已检查 ${recovery.checked||0}</small><button type="button" class="text-action" id="run-provider-reconcile">立即核对</button></div>${health.last_error?`<p>${esc(summarizeError(health.last_error))}</p>`:''}`;const reconcileButton=$('#run-provider-reconcile',target);if(reconcileButton)reconcileButton.onclick=async()=>{reconcileButton.disabled=true;try{await api('/api/production/reconcile',{method:'POST',body:JSON.stringify({limit:10})});toast('远端任务核对完成');await loadPage('videos')}catch(error){reconcileButton.disabled=false;toast(error.message,true)}}}
const visibleVideoJobs=(jobs=state.videos?.jobs||[])=>{const query=state.videoSearch.trim().toLowerCase();return jobs.filter(job=>videoMatchesFilter(job,state.videoFilter)&&(!query||`${job.job_id} ${job.title}`.toLowerCase().includes(query)))};
const videoNextActionLabel=job=>job.next_action?.label||'查看任务详情';
const videoRowContents=job=>`<span class="video-job-id" title="${esc(job.job_id)}">${esc(job.job_id)}</span><strong title="${esc(job.title)}">${esc(job.title)}</strong><span><i class="tag ${esc(job.display_state||job.state)}">${esc(job.display_state_label||videoStateLabel(job.display_state||job.state))}</i></span><span>${money(job.cost_cents)}</span><span class="next-action">${esc(videoNextActionLabel(job))} <i>›</i></span>`;
const videoRowMarkup=job=>`<button class="table-row interactive" data-video-id="${esc(job.job_id)}">${videoRowContents(job)}</button>`;
const bindVideoRow=row=>{row.onclick=()=>{const job=state.videos.jobs.find(item=>item.job_id===row.dataset.videoId);const stage=job&&(job.final_exists||['COMPOSED','READY_TO_PUBLISH','PUBLISHED','PUBLISHED_VERIFIED'].includes(job.state))?'review':'video';openEntityInProject(row.dataset.videoId,'master_review',stage,{videoView:'sessions'},()=>openVideoDrawer(job))}};
function renderVideoTotals(){const total=(state.videos?.jobs||[]).reduce((sum,job)=>sum+(Number(job.cost_cents)||0),0);if(state.overview?.videos)state.overview.videos.cost_cents=total;$('#total-cost').textContent=money(total);if($('#video-inspector-cost'))$('#video-inspector-cost').textContent=money(total)}
function renderVideos(){$$('#video-filters button').forEach(button=>button.classList.toggle('active',button.dataset.state===state.videoFilter));const jobs=visibleVideoJobs();renderVideoTotals();renderProductionHealth(state.productionHealth);$('#video-table').innerHTML=`<div class="table-head"><span>任务编号</span><span>视频主题</span><span>状态</span><span>成本</span><span>下一步</span></div>`+(jobs.length?jobs.map(videoRowMarkup).join(''):empty(state.videoSearch.trim()?'没有匹配的视频任务':'当前筛选下没有任务'));$$('[data-video-id]').forEach(bindVideoRow)}
function batchPickerRows(items,idKey,titleKey,meta){return `<div class="batch-picker">${items.map(item=>`<label><input type="checkbox" data-batch-value="${esc(item[idKey])}"><span><b>${esc(item[titleKey]||item[idKey])}</b><small>${esc(meta(item))}</small></span></label>`).join('')}</div>`}
function selectedBatchValues(){return $$('[data-batch-value]:checked').map(input=>input.dataset.batchValue)}
function openBatchCandidateDelete(){const items=state.sourceCandidates?.candidates||[];if(!items.length)return toast('没有可删除的自动选题候选',true);openModal('BATCH DECISION','批量删除候选',`${batchPickerRows(items,'candidate_id','title',item=>`${sourceKindLabel(item.source_kind)} · ${item.feed_name}`)}<div class="impact-callout"><strong>原子操作</strong><span>只删除候选池记录；任一条已晋级或不存在时，本次全部不执行</span></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="batch-delete-candidates">确认批量删除</button></div>`);$('#batch-delete-candidates').onclick=async()=>{const candidate_ids=selectedBatchValues();if(!candidate_ids.length)return toast('请至少选择一条候选',true);try{const result=await api('/api/source-candidates/batch-delete',{method:'POST',body:JSON.stringify({candidate_ids})});closeModal();toast(`已删除 ${result.deleted} 条候选`);loadPage('scripts')}catch(error){toast(error.message,true)}}}
function openBatchScriptReject(){const items=(state.scripts?.drafts||[]).filter(item=>item.status==='candidate');if(!items.length)return toast('没有可标记为不采用的候选剧本',true);openModal('BATCH DECISION','批量标记不采用',`${batchPickerRows(items,'draft_id','draft_id',item=>item.script?.title||`版本 ${Number(item.variant_index)+1}`)}<div class="impact-callout"><strong>不影响已提交版本</strong><span>仅处理待审核候选；任一条状态变化时，本次全部不执行</span></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="batch-reject-scripts">确认标记不采用</button></div>`);$('#batch-reject-scripts').onclick=async()=>{const draft_ids=selectedBatchValues();if(!draft_ids.length)return toast('请至少选择一个候选剧本',true);try{const result=await api('/api/scripts/batch-reject',{method:'POST',body:JSON.stringify({draft_ids})});closeModal();toast(`已将 ${result.rejected} 个剧本标记为不采用`);loadPage('scripts')}catch(error){toast(error.message,true)}}}
function openBatchProductionPreflight(){const items=(state.videos?.jobs||[]).filter(item=>item.state==='STORYBOARDED');if(!items.length)return toast('没有可执行生产预检的待生成任务',true);openModal('ZERO-COST CHECK','批量生产预检',`${batchPickerRows(items,'job_id','title',item=>item.job_id)}<div class="review-callout"><strong>零付费检查</strong><p>只计算母片数量、预计成本与预算上限，不启动 worker，不调用 Seedance</p></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="batch-run-preflight">运行零付费预检</button></div>`);$('#batch-run-preflight').onclick=async()=>{const job_ids=selectedBatchValues();if(!job_ids.length)return toast('请至少选择一个视频任务',true);try{const result=await api('/api/videos/batch-preflight',{method:'POST',body:JSON.stringify({job_ids})});openModal('PREFLIGHT RESULT','批量预检结果',`<div class="preflight-grid">${detailRow('任务数',result.job_count)}${detailRow('预计付费调用',`${result.estimated_paid_calls} 次`)}${detailRow('预计追加成本',money(result.estimated_cost_cents))}${detailRow('预算检查',result.all_budget_ok?'全部在预算上限内':'存在超过预算上限的任务')}${detailRow('实际付费调用','0 次')}</div><div class="review-callout"><strong>预检完成，未开始生成</strong><p>可分别打开任务核对人物、导演方案和预算；超预算任务需先提高预算上限</p></div><div class="modal-actions"><button class="primary-action" onclick="document.querySelector('#modal-close').click()">完成</button></div>`)}catch(error){toast(error.message,true)}}}
function appendBatchAction(root,label,handler){if(!root||root.querySelector('[data-batch-action]'))return;const button=document.createElement('button');button.type='button';button.className='mini-action batch-action';button.dataset.batchAction='';button.textContent=label;button.onclick=handler;root.append(button)}
const renderSourceCandidatesWithoutBatch=renderSourceCandidates;
renderSourceCandidates=function(){renderSourceCandidatesWithoutBatch();if((state.sourceCandidates?.candidates||[]).length)appendBatchAction($('[data-stage-board="candidates"] .panel-head'),'批量删除',openBatchCandidateDelete)};
const renderScriptsWithoutBatch=renderScripts;
renderScripts=function(data){renderScriptsWithoutBatch(data);if((data.drafts||[]).some(item=>item.status==='candidate'))appendBatchAction($('[data-stage-board="drafts"] .panel-head'),'批量不采用',openBatchScriptReject)};
const renderVideosWithoutBatch=renderVideos;
renderVideos=function(){renderVideosWithoutBatch();if((state.videos?.jobs||[]).some(item=>item.state==='STORYBOARDED'))appendBatchAction($('#page-videos .module-canvas-head'),'批量预检',openBatchProductionPreflight)};
$$('#video-filters button').forEach(b=>b.onclick=()=>{$$('#video-filters button').forEach(x=>x.classList.toggle('active',x===b));state.videoFilter=b.dataset.state;renderVideos()});
$('#video-search').addEventListener('input',e=>{state.videoSearch=e.target.value;renderVideos()});
function applyScriptStage(){$$('[data-stage-board]').forEach(board=>board.hidden=state.scriptStage!=='all'&&board.dataset.stageBoard!==state.scriptStage);$$('#script-stage-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.scriptStage===state.scriptStage))}
$$('#script-stage-tabs button').forEach(button=>button.onclick=()=>{state.scriptStage=button.dataset.scriptStage;applyScriptStage()});
const platformName=id=>({douyin:'抖音',weixin_channels:'微信视频号'})[id]||id;
const WEIXIN_ASSISTANT_URL='https://channels.weixin.qq.com/login';
const configLabels={ARK_API_KEY:'火山方舟 API Key',ARK_REGION:'方舟区域',ARK_VIDEO_PRIMARY_MODEL:'主视频模型',ARK_VIDEO_MODEL:'备用视频模型',ARK_VIDEO_RESOLUTION:'视频分辨率',CHARACTER_ANCHOR_GENERATOR:'人物锚点默认生成器',SCENE_ANCHOR_GENERATOR:'场景锚点默认生成器',SEEDANCE_JOB_BUDGET_CENTS:'单任务预算（分）',SEEDANCE_PRODUCTION_REFERENCE_VIDEO_BUDGET:'参考视频注意力预算',SEEDANCE_MAX_AUTO_RETRIES:'自动重试次数',SEEDANCE_AUTO_RETRY_BUDGET_CENTS:'重试预算（分）',SEEDANCE25_LONG_SCENE_ENABLED:'长场景模式',VIDEO_QA_ENABLED:'视频质检',VOLC_ASR_API_KEY:'录音文件识别 App Key',VOLC_TTS_APPID:'语音合成 App ID',VOLC_TTS_API_KEY:'语音合成 API Key',VOLC_TTS_PRICE_CENTS_PER_10K_CHARS:'语音合成单价（分/万字）',VOLC_AK:'火山 Access Key',VOLC_SK:'火山 Secret Key',VOLC_ASSETS_ENDPOINT:'真人资产 API 地址',VOLC_ASSETS_REGION:'真人资产区域',VOLC_ASSETS_PROJECT:'真人资产项目',TOS_BUCKET:'TOS Bucket',TOS_REGION:'TOS 区域',TOS_ENDPOINT:'TOS API 地址',TOS_OBJECT_PREFIX:'素材目录前缀',PUBLISH_MODE:'发布模式',BRAND_NAME:'品牌名称',DEFAULT_LLM_PROVIDER:'剧本模型供应商',CLAUDE_MODEL:'Claude 模型',DEFAULT_MUSIC_PROVIDER:'音乐模式',SUNO_API_KEY:'Suno API Key',MAX_CONCURRENT_JOBS:'并行任务数',MAX_CONCURRENT_SHOTS:'并行母片数',MAX_RETRIES:'通用重试次数',DOUYIN_CLIENT_KEY:'抖音 Client Key',DOUYIN_CLIENT_SECRET:'抖音 Client Secret',DOUYIN_REDIRECT_URI:'OAuth HTTPS 回调地址',WEIXIN_CHANNELS_ASSISTANT_URL:'视频号助手地址'};
configLabels.SEEDANCE_JOB_BUDGET_CENTS='单任务预算上限（分）';
configLabels.DIALOGUE_AUDIO_MODE='对白声音方案';
function configField(f){const control=f.options?.length?`<select>${f.options.map(option=>`<option value="${esc(option.value)}" ${option.value===f.value?'selected':''}>${esc(option.label)}</option>`).join('')}</select>`:`<input value="${esc(f.value||'')}" placeholder="请输入配置值">`;return `<div class="field" data-key="${esc(f.key)}" data-secret="${f.secret}"><label>${esc(configLabels[f.key]||f.key)}<small> · ${esc(f.key)}</small></label>${f.secret?`<div class="secret-status ${f.configured?'configured':''}"><span>${f.configured?'已安全配置':'尚未配置'}</span><i></i></div><input type="password" placeholder="${f.configured?'输入新值可替换':'粘贴到这里'}" autocomplete="new-password">`:control}</div>`}
function renderConfig(){
  const r=state.config.readiness;
  const provider=(state.config.providers||[]).find(item=>item.provider==='seedance')||{status:'available'};
  const blocked=provider.status==='action_required';
  $('#provider-runtime-status').innerHTML=`<div class="panel-head"><div><p class="section-no">PROVIDER RUNTIME</p><h3>Seedance 生产状态</h3></div><span class="tag ${blocked?'failed':'active'}">${blocked?'待处理':'可用'}</span></div><div class="impact-callout"><strong>${blocked?'账号问题需要人工处理':'当前允许新的付费提交'}</strong><span>${blocked?esc(provider.reason_message||'Seedance 账号不可用，请先处理账号问题。'):'本地未阻断 Seedance 提交。'} 不会自动检测余额，也不会自动续跑历史任务。</span></div>${blocked?'<div class="modal-actions"><button class="primary-action" id="clear-seedance-block">我已处理账号问题，恢复生产</button></div>':''}`;
  const clearBlock=$('#clear-seedance-block');if(clearBlock)clearBlock.onclick=async()=>{try{await api('/api/providers/seedance/clear-block',{method:'POST',body:'{}'});toast('已解除本地阻断，请返回任务确认续跑');await loadPage('config')}catch(e){toast(e.message,true)}};
  $('#readiness').innerHTML=Object.entries(r).map(([k,v])=>`<div class="readiness-card ${v.ready?'ready':''}"><strong>${({script:'剧本流水线',video:'完整视频流水线',publish:'发布流水线'})[k]}</strong><small>${v.ready?'已就绪':`还需 ${v.missing.length} 项`}</small></div>`).join('');
  const progress=state.config.required_progress||{completed:0,total:3};
  $('#required-progress').innerHTML=progress.completed===progress.total?'<strong>必要配置已完成，完整生产线可以运行</strong>':`已完成 <strong>${progress.completed} / ${progress.total}</strong> 组；只做剧本时，先完成第 1 组即可`;
  $('#required-config').innerHTML=(state.config.required_groups||[]).map((g,index)=>`<details class="required-config-card ${g.ready?'ready':''}" ${g.ready?'':'open'}><summary class="config-task-summary"><span class="setup-step">0${index+1}</span><div><div class="config-title-line"><h4>${esc(g.title)}</h4><span class="priority-badge ${g.priority.toLowerCase()}">${g.priority} · ${g.blocking?'必须':'增强'}</span></div><p>${esc(g.capability)}</p></div><b>${g.ready?'已完成 · 点击修改':`待配置 ${g.missing.length} 项`}</b></summary><div class="config-task-body"><div class="impact-callout"><strong>不配置会怎样</strong><span>${esc(g.impact)}</span></div><div class="required-fields">${g.fields.map(configField).join('')}</div><details class="config-guide"><summary>展开配置步骤</summary><ol>${g.guide.steps.map(step=>`<li>${esc(step)}</li>`).join('')}</ol><a href="${esc(g.guide.url)}" target="_blank" rel="noreferrer">${esc(g.guide.label)} ↗</a></details></div></details>`).join('');
  (state.config.required_groups||[]).forEach((group,index)=>{if(group.applies!==false)return;const card=$$('.required-config-card')[index];if(!card)return;const status=$('summary b',card);if(status)status.textContent='当前声音方案不需要';const badge=$('.priority-badge',card);if(badge)badge.textContent='按需启用';});
  const channelKeys=new Set(['DOUYIN_CLIENT_KEY','DOUYIN_CLIENT_SECRET','DOUYIN_REDIRECT_URI','WEIXIN_CHANNELS_ASSISTANT_URL']);
  const optional=(state.config.optional_fields||[]).filter(f=>configLabels[f.key]&&!channelKeys.has(f.key));
  $('#optional-config').innerHTML=optional.length?optional.map(configField).join(''):empty('当前没有需要调整的可选项');
  $('#publish-platform-config').innerHTML=(state.config.publish_platforms||[]).map(p=>{const douyin=p.id==='douyin';const ready=douyin?p.connected:true;const status=douyin?(p.connected?'账号已绑定':p.configured?'待扫码绑定':'待配置应用'):'官方助手可用';const fields=douyin?[fieldByKey('DOUYIN_CLIENT_KEY'),fieldByKey('DOUYIN_CLIENT_SECRET'),fieldByKey('DOUYIN_REDIRECT_URI')]:[];const assistantUrl=p.entry_url||WEIXIN_ASSISTANT_URL;return `<article class="platform-config-card ${ready?'connected':''}"><span class="platform-logo ${p.id}">${p.name[0]}</span><div><div class="config-title-line"><h4>${esc(p.name)}</h4><span class="tag ${ready?'active':''}">${status}</span></div><p>${esc(p.impact)}</p><small>${douyin?'OAuth 账号直连':'微信官方助手 · 扫码登录'}</small></div><button class="text-action" data-platform-expand="${p.id}">${douyin?'配置':'查看入口'}</button></article><div class="platform-fields" data-platform-fields="${p.id}" hidden>${fields.filter(Boolean).map(configField).join('')}${douyin?`<div class="account-bind-panel"><div><b>${p.connected?'已绑定抖音账号':'保存应用配置后绑定账号'}</b><small>${p.connected?`账号 ${esc(p.account?.open_id_masked||'')} · 令牌仅保存在本地服务端且不回显`:'授权回调必须是抖音开放平台登记的 HTTPS 地址'}</small></div><button class="${p.connected?'mini-action danger':'primary-action'}" id="${p.connected?'disconnect-douyin':'bind-douyin'}">${p.connected?'解除绑定':'绑定抖音账号'}</button></div>`:`<div class="account-bind-panel official-assistant-panel"><div><b>在微信官方页面扫码登录</b><small>登录状态由微信官方页面保管，管理台不读取 Cookie，也不会显示虚假的“已绑定”状态</small></div><a class="primary-action" href="${esc(assistantUrl)}" target="_blank" rel="noreferrer">打开视频号助手</a></div>`}</div>`}).join('');
  $$('[data-platform-expand]').forEach(b=>b.onclick=()=>{const panel=$(`[data-platform-fields="${b.dataset.platformExpand}"]`);panel.hidden=!panel.hidden;b.textContent=panel.hidden?(b.dataset.platformExpand==='weixin_channels'?'查看入口':'配置'):'收起'});
  const bind=$('#bind-douyin');if(bind)bind.onclick=startDouyinBinding;
  const disconnect=$('#disconnect-douyin');if(disconnect)disconnect.onclick=disconnectDouyin;
}
function fieldByKey(key){return state.config.fields.find(f=>f.key===key)}
$('#save-config').onclick=async()=>{const changes={};$$('#page-config .field').forEach(f=>{const input=$('input,select',f);if(input&&(f.dataset.secret!=='true'||input.value))changes[f.dataset.key]=input.value});try{await api('/api/config',{method:'POST',body:JSON.stringify({changes})});const recoveryJobId=state.pendingMusicResumeJobId;if(recoveryJobId){try{const result=await api(`/api/videos/${encodeURIComponent(recoveryJobId)}/resume`,{method:'POST',body:'{}'});state.pendingMusicResumeJobId='';toast(result.state==='COMPOSED'?'音乐配置已生效，本地成片已完成':'音乐配置已生效，任务已恢复');return navigate('videos',{historyMode:'push'})}catch(recoveryError){toast(`配置已保存，但任务尚未恢复：${recoveryError.message}`,true);return loadPage('config')}}toast('配置已保存到本机');loadPage('config')}catch(e){toast(e.message,true)}};
async function startDouyinBinding(){try{const result=await api('/api/integrations/douyin/oauth/start',{method:'POST',body:'{}'});const authWindow=window.open(result.authorization_url,'douyin-oauth','width=720,height=820');if(!authWindow)throw new Error('浏览器阻止了授权窗口，请允许本站打开弹窗');openModal('DOUYIN OAUTH','扫码绑定抖音账号',`<div class="oauth-waiting"><i></i><strong>请在新窗口完成抖音扫码授权</strong><p>授权完成后，此页面会自动识别绑定状态。不要在管理台手工粘贴 Access Token</p><small>授权链接 10 分钟内有效</small></div><div class="modal-actions"><button class="mini-action" id="oauth-check-now">我已完成授权</button></div>`);let attempts=0;const check=async()=>{attempts++;try{const status=await api('/api/integrations/douyin');if(status.connected){clearInterval(timer);closeModal();toast(`抖音账号 ${status.open_id_masked} 已绑定`);await loadPage('config')}}catch{}if(attempts>=120)clearInterval(timer)};const timer=setInterval(check,2000);$('#oauth-check-now').onclick=check}catch(e){toast(e.message,true)}}
async function disconnectDouyin(){openModal('DISCONNECT ACCOUNT','解除抖音账号绑定',`<p class="confirm-copy">解除后不会删除已经登记的抖音作品，但新的成片无法直推，直到重新扫码绑定</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-disconnect-douyin">确认解除</button></div>`);$('#confirm-disconnect-douyin').onclick=async()=>{try{await api('/api/integrations/douyin/disconnect',{method:'POST',body:'{}'});closeModal();toast('抖音账号已解除绑定');loadPage('config')}catch(e){toast(e.message,true)}}}
function openModal(kicker,title,body){$('#modal').classList.remove('candidate-review-open','script-guidance-open','performance-review-open');$('#modal-kicker').textContent=kicker;$('#modal-title').textContent=title;$('#modal-body').innerHTML=body;$('#modal').hidden=false}function closeModal(){$('#modal').hidden=true;$('#modal').classList.remove('candidate-review-open','script-guidance-open','performance-review-open')}$('#modal-close').onclick=closeModal;$('#modal').onclick=e=>{if(e.target.id==='modal')closeModal()};
function openDrawer(kicker,title,body){state.openVideoJobId='';$('#drawer-kicker').textContent=kicker;$('#drawer-title').textContent=title;$('#drawer-body').innerHTML=body;$('#context-drawer').hidden=false;$('#drawer').classList.add('open');$('#drawer').setAttribute('aria-hidden','false')}
function closeDrawer(){state.openVideoJobId='';$('#context-drawer').hidden=true;$('#drawer').classList.remove('open');$('#drawer').setAttribute('aria-hidden','true')}
$('#drawer-close').onclick=closeDrawer;$('#context-drawer').onclick=closeDrawer;
const detailRow=(label,value)=>`<div class="detail-row"><span>${esc(label)}</span><strong>${esc(value||'—')}</strong></div>`;
const jsonBlock=value=>`<pre class="json-content">${esc(JSON.stringify(value||{},null,2))}</pre>`;
function briefAngleCards(brief,selectable=false,selectedIndex=null){const activeIndex=selectedIndex==null?Number(brief.recommended_index||0):Number(selectedIndex);return (brief.angles||[]).map((angle,index)=>`<label class="brief-angle ${index===(brief.recommended_index||0)?'recommended':''} ${selectable?'selectable':''}">${selectable?`<input type="radio" name="brief-angle" value="${index}" ${index===activeIndex?'checked':''}>`:''}<header><b>${index+1}</b><strong>${esc(angle.title||`创意角度 ${index+1}`)}</strong>${index===(brief.recommended_index||0)?'<span>推荐</span>':''}</header><p><em>开场</em>${esc(angle.hook||'—')}</p><p><em>承诺</em>${esc(angle.promise||'—')}</p><p><em>叙事</em>${esc(angle.story_arc||'—')}</p>${angle.evidence?.length?`<ul>${angle.evidence.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>`:''}</label>`).join('')||'<p>暂无选题角度</p>'}
const sourceRoleLabel=role=>({hook:'开场钩子',setup:'背景建立',pain:'痛点',conflict:'冲突',turn:'转折',proof:'证明',catchphrase:'金句',cta:'行动引导',other:'其他'})[role]||role;
function sourcePerformanceReviewState(source){
  const segments=source.metadata?.structure_analysis?.segments||[];
  const required=segments.filter(segment=>segment.speaker_review_required).length;
  const manual=segments.filter(segment=>segment.speaker_resolution_status==='manual').length;
  if(required)return{tone:'required',label:'待人工确认',action:'开始校正',required,manual,total:segments.length,summary:`${required} 段待确认说话人或表演信息`};
  if(manual)return{tone:'completed',label:'已完成校正',action:'查看或继续校正',required,manual,total:segments.length,summary:`已人工校正 ${manual} 段，其余沿用解析结果`};
  return{tone:'suggested',label:'建议复核',action:'复核并校正',required,manual,total:segments.length,summary:'AI 已完成初判，建议在生成简报前快速核对'};
}
function sourceAnalysisMarkup(source){
  if(!['video','audio'].includes(source.source_type))return'';
  const metadata=source.metadata||{};
  const analysis=metadata.structure_analysis;
  if(!analysis)return '<section class="detail-section source-video-script"><div class="section-line"><h3>视频剧本解析</h3><span class="tag pending">待解析</span></div><div class="impact-callout"><strong>当前只有 ASR 原始台词</strong><span>重新解析后会补充场景、人物动作、运镜、画面文字和声音信息，并用于后续选题与剧本生成</span></div></section>';
  const segments=analysis.segments||[];
  const observations=metadata.visual_observations||[];
  const reviewState=sourcePerformanceReviewState(source);
  const reviewCount=reviewState.required;
  const performanceReviewAllowed=source.source_type==='video';
  const status=reviewCount
    ?`<span class="tag failed">${reviewCount} 段说话人待校正</span>`
    :'<span class="tag approved">说话人已确认</span>';
  const performance=segment=>[
    segment.emotion&&`情绪：${segment.emotion}`,
    segment.tone&&`语气：${segment.tone}`,
    segment.pace&&`语速：${segment.pace}`,
    segment.volume&&`音量：${segment.volume}`,
    segment.facial_expression&&`表情：${segment.facial_expression}`,
    segment.gaze&&`视线：${segment.gaze}`,
    segment.gesture&&`手势：${segment.gesture}`,
    segment.body_action&&`动作：${segment.body_action}`,
  ].filter(Boolean).join(' · ');
  const reviewWorkflow=performanceReviewAllowed?`<div class="performance-review-workflow ${reviewState.tone}" data-review-state="${reviewState.tone}"><header><div><small>解析后复核</small><b>人物与表演复核</b></div><span class="performance-review-status ${reviewState.tone}">${reviewState.label}</span></header><div class="performance-review-flow" aria-label="视频来源处理流程"><span class="done"><i>1</i>视频解析</span><span class="${reviewState.tone==='completed'?'done':'current'}"><i>2</i>人物与表演复核</span><span><i>3</i>生成选题简报</span></div><p>${esc(reviewState.summary)}。校正会进入后续选题和剧本生成，但此步骤不强制阻断创作。</p><div class="performance-review-metrics"><span><b>${reviewState.total}</b> 总片段</span><span><b>${reviewState.required}</b> 待确认片段</span><span><b>${reviewState.manual}</b> 已人工校正</span></div><button class="primary-action" id="source-performance-review">${esc(reviewState.action)}</button></div>`:'';
  return `<section class="detail-section source-video-script"><div class="section-line source-analysis-heading"><div><h3>视频剧本解析</h3><small>${segments.length} 个时间段 · ${observations.length} 个画面证据</small></div><div>${status}</div></div>${reviewWorkflow}<div class="source-analysis-summary"><b>结构摘要</b><p>${esc(analysis.summary||'已完成画面与原声联合解析')}</p></div><div class="source-script-timeline">${segments.map((segment,index)=>`<article class="${segment.speaker_review_required?'speaker-review-required':''}"><header><b>${String(index+1).padStart(2,'0')}</b><strong>${esc(sourceRoleLabel(segment.role))}</strong><time>${Number(segment.start_sec||0).toFixed(1)}–${Number(segment.end_sec||0).toFixed(1)}s</time></header><div class="source-script-fields">${segment.scene?`<p><b>场景</b><span>${esc(segment.scene)}</span></p>`:''}<p><b>说话人</b><span>${segment.speaker?esc(segment.speaker):'<em>待确认</em>'}${segment.voice_cluster_id?` · 声纹 ${esc(segment.voice_cluster_id)}`:''}${segment.speaker_confidence!=null?` · 置信度 ${Math.round(Number(segment.speaker_confidence)*100)}%`:''}</span></p>${(segment.characters||[]).length||segment.action?`<p><b>人物与动作</b><span>${esc([...(segment.characters||[]),segment.action].filter(Boolean).join(' · '))}</span></p>`:''}${performance(segment)?`<p><b>情绪与语气</b><span>${esc(performance(segment))}</span></p>`:''}${segment.camera?`<p><b>镜头</b><span>${esc(segment.camera)}</span></p>`:''}${segment.on_screen_text?`<p><b>画面文字</b><span>${esc(segment.on_screen_text)}</span></p>`:''}${segment.dialogue||segment.audio?`<p><b>原声与台词</b><span>${esc([segment.dialogue,segment.audio].filter(Boolean).join(' · '))}</span></p>`:''}</div>${segment.evidence?`<small>依据：${esc(segment.evidence)}</small>`:''}</article>`).join('')||'<p class="empty-inline">没有识别到可拆分的时间段</p>'}</div></section>`;
}
function sourceSegmentEvidence(source,segment){
  const metadata=source.metadata||{};
  const observations=metadata.visual_observations||[];
  const frames=metadata.frames||[];
  const preferred=(segment.evidence_frame_indices||[]).map(Number).filter(Number.isFinite);
  const midpoint=(Number(segment.start_sec||0)+Number(segment.end_sec||0))/2;
  let observation=preferred.map(index=>observations.find(item=>Number(item.frame_index)===index)).find(Boolean);
  if(!observation&&observations.length)observation=observations.reduce((best,item)=>Math.abs(Number(item.timestamp_sec||0)-midpoint)<Math.abs(Number(best.timestamp_sec||0)-midpoint)?item:best,observations[0]);
  let frameIndex=Number(observation?.frame_index||preferred[0]||0);
  if(!frameIndex&&frames.length){const frame=frames.reduce((best,item)=>Math.abs(Number(item.timestamp_sec||0)-midpoint)<Math.abs(Number(best.timestamp_sec||0)-midpoint)?item:best,frames[0]);frameIndex=Number(frame.index||0)}
  return {observation,frameIndex};
}
function sourcePerformanceEvidenceMarkup(source,segment,index,mediaUrl){
  const {observation,frameIndex}=sourceSegmentEvidence(source,segment);
  const people=observation?.people||[];
  const confidence=segment.speaker_confidence==null?'未提供':`${Math.round(Number(segment.speaker_confidence)*100)}%`;
  const parsedPeople=people.map((person,personIndex)=>`<li><b>${esc(person.track_id==null||person.track_id===''?`画面人物 #${personIndex+1}`:`画面人物 #${person.track_id}`)}</b><span>${esc([person.appearance,person.position].filter(Boolean).join(' · ')||'未识别外观与站位')}</span><small>嘴部状态：${esc(person.mouth_activity||'不确定')} · ${esc([person.facial_expression,person.gaze,person.gesture,person.body_action].filter(Boolean).join(' · ')||'未识别表演动作')}</small></li>`).join('');
  const poster=source.source_type==='video'&&frameIndex?`/api/sources/${encodeURIComponent(source.source_id)}/frames/${frameIndex}`:'';
  const media=source.source_type==='video'?`<video class="performance-segment-video" data-performance-media="${index}" data-start="${Number(segment.start_sec||0)}" data-end="${Number(segment.end_sec||0)}" controls playsinline preload="metadata" src="${mediaUrl}"${poster?` poster="${poster}"`:''}></video>`:'<div class="performance-evidence-empty">该来源没有可用视频参考</div>';
  return `<aside class="performance-segment-evidence"><div class="performance-evidence-frame">${media}<button type="button" class="mini-action" data-performance-play="${index}" data-start="${Number(segment.start_sec||0)}" data-end="${Number(segment.end_sec||0)}">播放此片段</button></div><div class="performance-evidence-copy"><b>解析依据</b><p>${esc(observation?.visual||segment.evidence||'该段暂无视觉描述，可播放原视频人工判断')}</p><dl><div><dt>原识别说话人 / 声轨</dt><dd>${esc(segment.speaker||'说话人待确认')}${segment.speaker_track_id?` · ${esc(segment.speaker_track_id)}`:''}${segment.voice_cluster_id?` · ${esc(segment.voice_cluster_id)}`:''}</dd></div><div><dt>识别置信度</dt><dd>${esc(confidence)}</dd></div><div><dt>场景与镜头</dt><dd>${esc([segment.scene,segment.camera].filter(Boolean).join(' · ')||'未识别')}</dd></div><div><dt>人物与动作</dt><dd>${esc([...(segment.characters||[]),segment.action].filter(Boolean).join(' · ')||'未识别')}</dd></div></dl>${parsedPeople?`<ul>${parsedPeople}</ul>`:''}</div></aside>`;
}
function openSourcePerformanceReview(source){
  const analysis=source.metadata?.structure_analysis||{};
  const segments=analysis.segments||[];
  const names=[...new Set([
    ...(analysis.character_tracks||[]).flatMap(track=>[track.label,track.id]),
    ...segments.flatMap(segment=>segment.characters||[]),
  ].filter(Boolean))];
  const mediaUrl=`/api/sources/${encodeURIComponent(source.source_id)}/media`;
  const performanceFields=[['emotion','情绪'],['tone','语气'],['pace','语速'],['volume','音量'],['facial_expression','表情'],['gaze','视线'],['gesture','手势'],['body_action','身体动作']];
  const reviewState=sourcePerformanceReviewState(source);
  const fields=segments.map((segment,index)=>{const resolution=segment.speaker_review_required?['required','待确认']:segment.speaker_resolution_status==='manual'?['completed','已人工校正']:['suggested','AI 初判'];const speechMode=segment.speech_mode||(/旁白/.test(segment.speaker||'')?'narration':/背景台词|画外音|画外台词/.test(segment.speaker||'')?'off_screen':'on_screen');return`<article class="performance-review-row ${segment.speaker_review_required?'required':''}"><header><b>${String(index+1).padStart(2,'0')} · ${esc(sourceRoleLabel(segment.role))}</b><span><em class="performance-review-status ${resolution[0]}">${resolution[1]}</em><small>${Number(segment.start_sec||0).toFixed(1)}–${Number(segment.end_sec||0).toFixed(1)}s${segment.voice_cluster_id?` · ${esc(segment.voice_cluster_id)}`:''}</small></span></header><div class="performance-review-row-layout">${sourcePerformanceEvidenceMarkup(source,segment,index,mediaUrl)}<section class="performance-correction-fields"><div class="performance-dialogue"><b>ASR 台词</b><p>${esc(segment.dialogue||'该段无可识别台词')}</p></div><div class="performance-review-grid"><label>实际说话人<input data-performance-speaker="${index}" list="source-character-options" value="${esc(segment.speaker||'')}" placeholder="输入或选择实际说话人"></label><label>声音与画面关系<select data-performance-speech-mode="${index}"><option value="on_screen" ${speechMode==='on_screen'?'selected':''}>画内说话（人物入镜）</option><option value="off_screen" ${speechMode==='off_screen'?'selected':''}>画外台词（人物不入镜）</option><option value="narration" ${speechMode==='narration'?'selected':''}>旁白（无对应入镜人物）</option></select></label></div><div class="performance-review-grid">${performanceFields.map(([key,label])=>`<label>${label}<input data-performance-field="${key}" data-segment-index="${index}" value="${esc(segment[key]||'')}" placeholder="可选，保留原视频表演特征"></label>`).join('')}</div></section></div></article>`}).join('');
  openModal('PERFORMANCE REVIEW','校正说话人与表演',`<div class="performance-review-guide"><div><small>操作顺序</small><b>播放片段 → 核对说话人 → 修正表演 → 保存校正</b><p>逐段对照原视频，不重新调用 AI；保存后返回来源详情，可继续生成选题简报。</p></div><span class="performance-review-status ${reviewState.tone}">${esc(reviewState.label)}</span></div><div class="performance-review-column-labels"><span>原视频与解析证据</span><span>人工校正</span></div><datalist id="source-character-options">${names.map(name=>`<option value="${esc(name)}"></option>`).join('')}</datalist><div class="performance-review-list">${fields}</div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">稍后处理</button><button class="primary-action" id="save-source-performance">保存校正并返回来源</button></div>`);
  $('#modal').classList.add('performance-review-open');
  const performancePlayers=$$('[data-performance-media]');
  const pausePerformancePlayers=active=>performancePlayers.forEach(player=>{if(player!==active)player.pause()});
  performancePlayers.forEach(player=>{
    const start=Number(player.dataset.start||0),end=Number(player.dataset.end||0);
    player.onplay=()=>{pausePerformancePlayers(player);if(player.currentTime<start-.05||(end&&player.currentTime>=end))player.currentTime=start;player.dataset.stopAt=String(end)};
    player.ontimeupdate=()=>{const stopAt=Number(player.dataset.stopAt||end);if(stopAt&&player.currentTime>=stopAt){player.pause();player.currentTime=stopAt;delete player.dataset.stopAt}};
  });
  $$('[data-performance-play]').forEach(button=>button.onclick=()=>{
    const media=$(`[data-performance-media="${button.dataset.performancePlay}"]`);if(!media)return;
    pausePerformancePlayers(media);
    const start=Number(button.dataset.start||media.dataset.start||0),end=Number(button.dataset.end||media.dataset.end||0);
    const playSegment=()=>{media.currentTime=start;media.dataset.stopAt=String(end);media.play().catch(()=>toast('浏览器阻止了自动播放，请点击左侧播放器开始',true))};
    if(media.readyState>=1)playSegment();else{media.load();media.addEventListener('loadedmetadata',playSegment,{once:true})}
  });
  $('#save-source-performance').onclick=async()=>{
    const assignments=$$('[data-performance-speaker]').map(input=>{
      const segment_index=Number(input.dataset.performanceSpeaker);
      const speechMode=$(`[data-performance-speech-mode="${segment_index}"]`);
      const item={segment_index,speaker:input.value.trim(),speech_mode:speechMode?speechMode.value:'on_screen'};
      $$(`[data-performance-field][data-segment-index="${segment_index}"]`).forEach(field=>{item[field.dataset.performanceField]=field.value.trim()});
      return item;
    }).filter(item=>item.speaker);
    if(!assignments.length)return toast('请至少确认一个说话人',true);
    const button=$('#save-source-performance');button.disabled=true;
    try{
      await api(`/api/sources/${encodeURIComponent(source.source_id)}/performance-review`,{method:'POST',body:JSON.stringify({assignments})});
      closeModal();closeDrawer();toast('校正已保存，可继续生成选题简报');
      if(!await loadPage('scripts'))return;
      openSourceDrawer(state.scripts?.sources.find(item=>item.source_id===source.source_id));
    }catch(error){button.disabled=false;toast(error.message,true)}
  };
}
function sourceStudioContent(source){
  if(!source)return'暂无正文';
  const analysis=source.metadata?.structure_analysis;
  if(!analysis)return source.content||'暂无正文';
  const timeline=(analysis.segments||[]).map(segment=>{
    const visual=[segment.scene,segment.action,segment.camera,segment.on_screen_text].filter(Boolean).join('；');
    const speech=[segment.dialogue,segment.audio].filter(Boolean).join('；');
    const performance=[segment.emotion,segment.tone,segment.facial_expression,segment.gesture,segment.body_action].filter(Boolean).join('；');
    const speaker=segment.speaker||segment.voice_cluster_id||'说话人待确认';
    return `[${Number(segment.start_sec||0).toFixed(1)}–${Number(segment.end_sec||0).toFixed(1)}s｜${sourceRoleLabel(segment.role)}｜${speaker}]\n${visual}${performance?`\n表演：${performance}`:''}${speech?`\n原声：${speech}`:''}`;
  }).join('\n\n');
  return `${analysis.summary||'视频剧本解析'}${timeline?`\n\n${timeline}`:''}\n\nASR 原始台词\n${source.content||'暂无台词'}`;
}
async function reanalyzeSource(source){
  const button=$('#drawer-analyze-source');if(button){button.disabled=true;button.textContent='正在解析画面与原声…'}
  try{
    await api(`/api/sources/${encodeURIComponent(source.source_id)}/analyze`,{method:'POST',body:JSON.stringify({confirmed:true})});
    toast('视频剧本解析已更新');
    if(!await loadPage('scripts'))return;
    openSourceDrawer(state.scripts?.sources.find(item=>item.source_id===source.source_id));
  }catch(e){if(button){button.disabled=false;button.textContent='重新解析视频'}toast(e.message,true)}
}
function linkedBriefForSource(sourceId){const project=(state.projects?.projects||[]).find(item=>item.project_id===sourceId);const briefId=project?.brief?.brief_id;return briefId?(state.scripts?.briefs||[]).find(item=>item.brief_id===briefId)||null:null}
function openSourceDrawer(source){if(!source)return;const media=['video','audio'].includes(source.source_type);const reprocessable=source.source_type==='video';const existingBrief=linkedBriefForSource(source.source_id);openDrawer('SCRIPT SOURCE',source.title||'未命名来源',`<div class="drawer-summary"><span class="tag">${esc(sourceTypeLabel(source.source_type))}</span><p>${existingBrief?'该来源已生成选题简报，可直接查看并继续选择创意角度':source.source_type==='audio'?'历史音频来源仅保留查看，不再支持新增或重新解析':media?'已联合分析原声、画面与叙事结构；请复核后生成选题简报':'核对来源正文后，可直接生成选题简报'}</p></div><section class="detail-section"><h3>来源信息</h3>${detailRow('来源 ID',source.source_id)}${detailRow('来源地址',source.source_display||source.source_uri)}${detailRow('创建时间',source.created_at)}</section>${sourceAnalysisMarkup(source)}<section class="detail-section source-content"><h3>${media?'ASR 原始台词':'来源具体内容'}</h3><div class="reading-content">${esc(source.content||'暂无内容')}</div></section><div class="drawer-footer"><button class="mini-action danger" id="drawer-delete-source">删除来源</button>${reprocessable?'<button class="mini-action" id="drawer-analyze-source">重新解析视频</button>':''}<button class="primary-action" id="drawer-generate-brief">${existingBrief?'查看已生成简报':'AI 生成选题简报'}</button></div>`);$('#drawer-delete-source').onclick=()=>{closeDrawer();confirmDeleteSource(source.source_id)};const analyze=$('#drawer-analyze-source');if(analyze)analyze.onclick=()=>reanalyzeSource(source);const performanceReview=$('#source-performance-review');if(performanceReview)performanceReview.onclick=()=>openSourcePerformanceReview(source);$('#drawer-generate-brief').onclick=()=>{closeDrawer();if(existingBrief)return openBriefDrawer(existingBrief);confirmGenerateBrief(source.source_id)}}
function briefFactLedger(brief){
  const digest=brief.source_digest||{};
  const facts=digest.facts||[];
  if(!facts.length)return '<div class="impact-callout"><strong>旧版简报暂无来源事实底稿</strong><span>原始来源仍然保留，重新生成简报后会建立逐条事实与覆盖关系</span></div>';
  const missing=new Set(digest.coverage?.missing_fact_ids||[]);
  return `<section class="detail-section fact-ledger"><div class="section-line"><h3>来源事实底稿</h3><small>${digest.coverage?.covered||0} / ${digest.coverage?.must_keep_total||0} 条必保留事实已覆盖</small></div>${missing.size?`<div class="error-callout"><strong>仍有关键事实未进入任何选题角度</strong><p>${esc([...missing].join('、'))}</p></div>`:''}<div class="fact-ledger-list">${facts.map(fact=>`<article class="${missing.has(fact.fact_id)?'missing':''}"><b>${esc(fact.fact_id)}</b><span><strong>${esc(fact.statement)}</strong><small>${esc(fact.source_id)} · “${esc(fact.quote)}”</small></span><em>${fact.importance==='must_keep'?'必须保留':'辅助事实'}</em></article>`).join('')}</div></section>`;
}
function editBrief(brief){
  const current=brief.brief||{};
  const downstream=(state.scripts?.drafts||[]).some(item=>item.brief_id===brief.brief_id);
  const angleFields=(current.angles||[]).map((angle,index)=>`<section class="editor-section" data-edit-brief-angle="${index}"><header><b>角度 ${index+1}</b><label><input type="radio" name="edit-brief-recommended" value="${index}" ${index===Number(current.recommended_index||0)?'checked':''}> 设为推荐</label></header><label>选题标题<input data-name="title" value="${esc(angle.title||'')}"></label><div class="editor-two-col"><label>前三秒钩子<textarea data-name="hook" rows="2">${esc(angle.hook||'')}</textarea></label><label>观众收益<textarea data-name="promise" rows="2">${esc(angle.promise||'')}</textarea></label></div><label>故事弧<textarea data-name="story_arc" rows="2">${esc(angle.story_arc||'')}</textarea></label><div class="editor-two-col"><label>来源证据（每行一条）<textarea data-name="evidence" rows="3">${esc((angle.evidence||[]).join('\n'))}</textarea></label><label>风险提示（每行一条）<textarea data-name="risk_notes" rows="3">${esc((angle.risk_notes||[]).join('\n'))}</textarea></label></div><label>制作建议<textarea data-name="production_notes" rows="2">${esc(angle.production_notes||'')}</textarea></label><label>关联事实编号（逗号分隔）<input data-name="fact_ids" value="${esc((angle.fact_ids||[]).join(', '))}"></label></section>`).join('');
  openModal('EDIT TOPIC BRIEF','编辑选题简报',`<div class="content-editor brief-editor"><div class="editor-lock-note"><strong>保留来源证据，修改创作决策</strong><span>${downstream?'保存后将废弃现有候选剧本和未启动的视频任务，再按新简报重新生成':'保存形成新修订，不修改原始来源'}</span></div><label>目标受众<input id="edit-brief-audience" value="${esc(current.audience||'')}"></label>${angleFields}<label>推荐理由<textarea id="edit-brief-reason" rows="2">${esc(current.recommendation_reason||'')}</textarea></label><label>本次修改说明<input id="edit-brief-note" placeholder="例如：补充关键事实，强化第二个角度"></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="save-brief-edit">保存为修订 ${Number(brief.revision||1)+1}</button></div>`);
  $('#save-brief-edit').onclick=async()=>{const next=JSON.parse(JSON.stringify(current));next.audience=$('#edit-brief-audience').value.trim();next.recommended_index=Number($('input[name="edit-brief-recommended"]:checked')?.value||0);next.recommendation_reason=$('#edit-brief-reason').value.trim();next.angles=$$('[data-edit-brief-angle]').map(section=>{const original=current.angles?.[Number(section.dataset.editBriefAngle)]||{};const value=name=>$(`[data-name="${name}"]`,section).value.trim();const lines=name=>value(name).split(/\r?\n/).map(x=>x.trim()).filter(Boolean);return {...original,title:value('title'),hook:value('hook'),promise:value('promise'),story_arc:value('story_arc'),evidence:lines('evidence'),risk_notes:lines('risk_notes'),production_notes:value('production_notes'),fact_ids:value('fact_ids').split(/[,，]/).map(x=>x.trim()).filter(Boolean)}});const button=$('#save-brief-edit');button.disabled=true;try{const result=await api(`/api/briefs/${encodeURIComponent(brief.brief_id)}/edit`,{method:'POST',body:JSON.stringify({brief:next,change_note:$('#edit-brief-note').value.trim()})});closeModal();closeDrawer();toast(result.downstream_invalidated?'简报已保存，旧候选剧本已失效':'简报修订已保存');if(!await loadPage('scripts'))return;openBriefDrawer(state.scripts.briefs.find(item=>item.brief_id===brief.brief_id))}catch(e){button.disabled=false;toast(e.message,true)}};
}
function openBriefDrawer(brief,options={}){
  if(!brief)return;
  const b=brief.brief||{};
  const related=(state.scripts?.drafts||[]).filter(x=>x.brief_id===brief.brief_id);
  const candidate=brief.status!=='approved'&&!brief.approved_job_id;
  const hasDrafts=related.length>0;
  const locked=related.some(item=>item.status==='approved'||item.approved_job_id);
  const generationFailed=!candidate&&!hasDrafts;
  const canGenerate=candidate||generationFailed;
  const requestedAngle=Number(options.angleIndex);
  const initialAngleIndex=Number.isInteger(requestedAngle)&&requestedAngle>=0&&requestedAngle<(b.angles||[]).length?requestedAngle:Number(b.recommended_index||0);
  const statusLabel=generationFailed?'生成失败，可重试':candidate?'待确认':'已生成剧本';
  openDrawer('TOPIC BRIEF',brief.recommended_title||'选题简报',`<div class="drawer-summary"><span class="tag ${generationFailed?'failed':esc(brief.status)}">${statusLabel}</span><p>${generationFailed?'上次模型调用未产出候选剧本，可直接重新生成，不会重复创建任务':esc(briefSummary(b)||'查看完整选题内容和推荐角度')}</p></div><section class="detail-section"><h3>选题信息</h3>${detailRow('简报 ID',brief.brief_id)}${detailRow('修订版本',`R${brief.revision||1}`)}${detailRow('状态',generationFailed?'生成失败，等待重试':candidate?'待确认创意角度':'已确认')}${detailRow('目标受众',b.audience)}${detailRow('创建时间',brief.created_at)}</section>${briefFactLedger(b)}<section class="detail-section brief-content"><div class="section-line"><h3>创意角度</h3>${candidate?'<small>选择一个方向继续</small>':''}</div><div class="brief-angles">${briefAngleCards(b,candidate,initialAngleIndex)}</div></section>${b.recommendation_reason?`<div class="review-callout"><strong>推荐理由</strong><p>${esc(b.recommendation_reason)}</p></div>`:''}${locked?'<div class="impact-callout"><strong>简报已锁定</strong><span>剧本已经进入视频制作；如需调整，请从来源复制创建新的内容版本</span></div>':''}<div class="drawer-footer">${candidate?'<button class="mini-action danger" id="drawer-delete-brief">删除简报</button>':''}${!locked?'<button class="mini-action" id="drawer-edit-brief">编辑简报</button>':''}${canGenerate?`<button class="primary-action" id="drawer-generate-scripts">${candidate?'确认角度并生成三版剧本':'重新生成三版剧本'}</button>`:''}</div>`);
  const edit=$('#drawer-edit-brief');if(edit)edit.onclick=()=>{closeDrawer();editBrief(brief)};
  const del=$('#drawer-delete-brief');if(del)del.onclick=()=>{closeDrawer();confirmDeleteBrief(brief.brief_id)};
  const generate=$('#drawer-generate-scripts');if(generate)generate.onclick=()=>{const selected=$('input[name="brief-angle"]:checked');const index=candidate?(selected?Number(selected.value):Number(b.recommended_index||0)):Number(brief.approved_angle_index??b.recommended_index??0);closeDrawer();confirmGenerateScripts(brief.brief_id,index)};
}
function confirmNetworkRetry(job){
  const indexes=job.network_retry_indexes||[];
  const anchors=job.network_retry_anchor_ids||[];
  if(!indexes.length&&!anchors.length)return toast('当前没有可重试的连接失败生成项，请刷新任务',true);
  const candidates=(job.shots||[]).filter(shot=>shot.candidate_preview_url).map(shot=>`#${shot.idx}`);
  const targets=[...(anchors.length?[`${anchors.length} 个未提交锚点`]:[]),...indexes.map(idx=>`母片 #${idx}`)].join('、');
  openModal('RETRY GENERATION','重试未提交的生成项',`<div class="review-callout"><strong>重新排队 ${targets}</strong><p>这些生成项在提交前连接失败。重试成功后会产生生成费用，继续遵守本任务原预算和重试限制。</p><p>累计已发生 ${money(job.cost_cents)}；${candidates.length?`${candidates.join('、')} 的候选视频保留，仍需人工复核。`:'已有锚点和完成母片保留。'}锚点完成后仍需人工审核。</p></div><div class="modal-actions"><button class="mini-action" id="cancel-network-retry">取消</button><button class="primary-action" id="confirm-network-retry">确认重试并继续</button></div>`);
  $('#cancel-network-retry').onclick=closeModal;
  $('#confirm-network-retry').onclick=async()=>{const button=$('#confirm-network-retry');button.disabled=true;try{const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/retry-network-failures`,{method:'POST',body:JSON.stringify({confirmed:true})});closeModal();closeDrawer();toast(result.state==='PRODUCTION_RUNNING'?'失败母片已恢复生成':'失败母片已进入生成队列');await loadPage('videos')}catch(error){button.disabled=false;toast(error.message,true)}};
}
function editScript(draft){
  const current=draft.script||{};
  const beatNames={hook:'钩子',conflict:'冲突',evidence:'证据',reversal:'反转',solution:'解决',cta:'行动'};
  const autoDuration=current.duration_mode!=='manual';
  const beats=(current.beats||[]).map((beat,index)=>`<section class="editor-section" data-edit-script-beat="${index}"><header><b>${String(index+1).padStart(2,'0')} · ${esc(beatNames[beat.beat]||beat.beat)}</b><small>${esc((beat.source_ids||[]).join('、'))}</small></header><div class="editor-two-col"><label>人物 ID<input data-name="speaker" value="${esc(scriptSpeakerIds(beat).join(',')||'presenter')}"></label><label>时长（秒）<input data-name="duration_sec" data-duration-input type="number" min="1.5" step="0.5" value="${Number(beat.duration_sec||4)}" ${autoDuration?'readonly':''}></label></div><label>台词<textarea data-name="dialogue" rows="3">${esc(beat.dialogue||'')}</textarea></label><label>画面、动作与运镜<textarea data-name="visual_intent" rows="3">${esc(beat.visual_intent||'')}</textarea></label><div class="editor-two-col"><label>情绪<input data-name="emotion" value="${esc(beat.emotion||'')}"></label><label>语气<input data-name="tone" value="${esc(beat.tone||'')}"></label></div><div class="editor-two-col"><label>语速<input data-name="pace" value="${esc(beat.pace||'')}"></label><label>音量<input data-name="volume" value="${esc(beat.volume||'')}"></label></div><div class="editor-two-col"><label>表情<input data-name="facial_expression" value="${esc(beat.facial_expression||'')}"></label><label>视线<input data-name="gaze" value="${esc(beat.gaze||'')}"></label></div><div class="editor-two-col"><label>手势<input data-name="gesture" value="${esc(beat.gesture||'')}"></label><label>身体动作<input data-name="body_action" value="${esc(beat.body_action||'')}"></label></div><label>B-roll 建议<input data-name="broll_hint" value="${esc(beat.broll_hint||'')}"></label></section>`).join('');
  openModal('EDIT SCRIPT','编辑候选剧本',`<div class="content-editor script-editor"><div class="editor-lock-note"><strong>这是生产指令，不只是文案</strong><span>修改人物 ID 会影响角色映射；台词变化会影响可安全朗读的分镜时长</span></div><section class="duration-editor-control"><label><input id="edit-script-auto-duration" type="checkbox" ${autoDuration?'checked':''}><span><strong>自动重估分镜时长</strong><small>按台词长度、自然语速和标点停顿计算；可关闭自动调整后手工填写</small></span></label><b id="edit-script-duration-summary">预计总时长 —</b></section><label>剧本标题<input id="edit-script-title" value="${esc(current.title||'')}"></label><label>核心命题<textarea id="edit-script-thesis" rows="2">${esc(current.creative_thesis||'')}</textarea></label>${beats}<label>本次修改说明<input id="edit-script-note" placeholder="例如：缩短开场，增加人物动作"></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="save-script-edit">保存为修订 ${Number(draft.revision||1)+1}</button></div>`);
  const autoToggle=$('#edit-script-auto-duration');
  const syncDurationEditor=(reestimate=false)=>{const sections=$$('[data-edit-script-beat]');sections.forEach(section=>{const duration=$('[data-duration-input]',section);duration.readOnly=autoToggle.checked;section.classList.toggle('auto-duration',autoToggle.checked);if(autoToggle.checked&&reestimate)duration.value=estimateDialogueDuration($('[data-name="dialogue"]',section).value)});const total=sections.reduce((sum,section)=>sum+Number($('[data-duration-input]',section).value||0),0);$('#edit-script-duration-summary').textContent=`预计总时长 ${Number(total.toFixed(1))} 秒 · ${autoToggle.checked?'自动重估':'手工设定'}`};
  $$('[data-name="dialogue"]',$('#modal-body')).forEach(input=>input.addEventListener('input',()=>syncDurationEditor(true)));
  autoToggle.onchange=()=>syncDurationEditor(autoToggle.checked);
  syncDurationEditor(autoToggle.checked);
  $('#save-script-edit').onclick=async()=>{const next=JSON.parse(JSON.stringify(current));next.title=$('#edit-script-title').value.trim();next.creative_thesis=$('#edit-script-thesis').value.trim();next.duration_mode=autoToggle.checked?'auto':'manual';next.beats=$$('[data-edit-script-beat]').map(section=>{const original=current.beats?.[Number(section.dataset.editScriptBeat)]||{};const value=name=>$(`[data-name="${name}"]`,section).value.trim();return {...original,speaker:value('speaker'),speakers:scriptSpeakerIds({speaker:value('speaker')}),dialogue:value('dialogue'),duration_sec:Number(value('duration_sec')),visual_intent:value('visual_intent'),emotion:value('emotion'),tone:value('tone'),pace:value('pace'),volume:value('volume'),facial_expression:value('facial_expression'),gaze:value('gaze'),gesture:value('gesture'),body_action:value('body_action'),broll_hint:value('broll_hint')}});next.total_duration_sec=Number(next.beats.reduce((sum,beat)=>sum+Number(beat.duration_sec||0),0).toFixed(2));next.target_duration_sec=next.total_duration_sec;const button=$('#save-script-edit');button.disabled=true;try{const result=await api(`/api/scripts/${encodeURIComponent(draft.draft_id)}/edit`,{method:'POST',body:JSON.stringify({script:next,change_note:$('#edit-script-note').value.trim(),auto_adjust_durations:autoToggle.checked})});closeModal();closeDrawer();toast(result.duration_adjusted?'剧本修订已保存，分镜时长已重新估算':'剧本修订已保存，已保留手工时长');if(!await loadPage('scripts'))return;openDraftDrawer(state.scripts.drafts.find(item=>item.draft_id===draft.draft_id))}catch(e){button.disabled=false;toast(e.message,true)}};
}
function beatPerformanceSummary(beat){return [beat.emotion&&`情绪 ${beat.emotion}`,beat.tone&&`语气 ${beat.tone}`,beat.pace&&`语速 ${beat.pace}`,beat.volume&&`音量 ${beat.volume}`,beat.facial_expression&&`表情 ${beat.facial_expression}`,beat.gaze&&`视线 ${beat.gaze}`,beat.gesture&&`手势 ${beat.gesture}`,beat.body_action&&`动作 ${beat.body_action}`].filter(Boolean).join(' · ')}
function openDraftDrawer(draft){
  if(!draft)return;
  const script=draft.script||{};
  openDrawer('SCRIPT REVIEW',script.title||`剧本版本 ${Number(draft.variant_index)+1}`,`<div class="drawer-summary"><span class="tag ${esc(draft.status)}">${esc(scriptStatus(draft.status))}</span><p>${esc(script.creative_thesis||'请审核完整台词、时长与画面意图')}</p></div><section class="detail-section"><h3>版本信息</h3>${detailRow('剧本 ID',draft.draft_id)}${detailRow('修订版本',`R${draft.revision||1}`)}${detailRow('目标时长',script.target_duration_sec?`${script.target_duration_sec} 秒`:'—')}${detailRow('创建时间',draft.created_at)}</section><section class="detail-section script-content"><h3>完整剧本</h3><div class="script-beats">${(script.beats||[]).map((beat,index)=>`<article><header><b>${index+1}</b><strong>${esc(beat.beat)}</strong><span>${esc(beat.duration_sec)} 秒</span></header><div class="script-speaker">剧本人物 · ${esc(scriptSpeakerLabel(beat,script)||'presenter')}</div><p>${esc(beat.dialogue)}</p><small>画面：${esc(beat.visual_intent||'—')}</small>${beatPerformanceSummary(beat)?`<small>表演：${esc(beatPerformanceSummary(beat))}</small>`:''}</article>`).join('')||'<p>暂无剧本正文</p>'}</div></section>${draft.status==='candidate'?`<div class="review-callout"><strong>审核通过后的结果</strong><p>系统会按剧本人物拆分对白母片，并默认根据每位人物设定自动生成形象；需要固定形象时可在视频制作设置中选择角色资产</p></div><div class="drawer-footer"><button class="mini-action danger" id="drawer-delete-script">删除剧本</button><button class="mini-action" id="drawer-edit-script">编辑剧本</button><button class="primary-action" id="drawer-approve">审核通过并提交视频</button></div>`:'<div class="impact-callout"><strong>剧本已锁定</strong><span>已进入视频制作的剧本不能覆盖；需要调整时请从来源复制并生成新版本</span></div>'}`);
  const edit=$('#drawer-edit-script');if(edit)edit.onclick=()=>{closeDrawer();editScript(draft)};
  const approve=$('#drawer-approve');if(approve)approve.onclick=async()=>{closeDrawer();await approveScript(draft.draft_id)};
  const del=$('#drawer-delete-script');if(del)del.onclick=()=>{closeDrawer();confirmDeleteScript(draft.draft_id)};
}
function openVideoDrawer(job){if(!job)return;const phase=job.state==='STORYBOARDED'?1:['PENDING','PRODUCTION_QUEUED','PRODUCTION_RUNNING','SHOTS_OK','BGM_OK'].includes(job.state)?2:['COMPOSED','READY_TO_PUBLISH','PUBLISHED'].includes(job.state)?3:0;const deletable=!['PENDING','PRODUCTION_QUEUED','PRODUCTION_RUNNING','SHOTS_OK','VOICE_OK','BGM_OK','READY_TO_PUBLISH','PUBLISHED'].includes(job.state);const review=job.latest_review;const approved=review?.decision==='approved';const media=job.final_exists?`<section class="detail-section"><h3>成品预览</h3><video class="video-preview" controls preload="metadata" src="${videoFileUrl(job)}"></video><div class="media-actions"><a class="mini-action" href="${videoFileUrl(job,true)}">下载 MP4</a></div></section>`:'';const shots=job.shots?.length?`<section class="detail-section"><div class="section-line"><h3>母片与质检</h3><small>${job.master_count} 条 · ${job.total_master_duration_sec} 秒</small></div><div class="master-list">${job.shots.map(s=>`<article data-video-master="${s.idx}" class="master-row ${s.manual_review_required?'review':s.error?'failed':''}"><b>#${s.idx}</b><span>${esc(s.provider||'待生成')} · ${s.duration_sec||0} 秒</span><em>${s.manual_review_required?'待人工复核':s.error?esc(summarizeError(s.error)):esc(s.state)}</em></article>`).join('')}</div></section>`:'';const reviewBlock=job.state==='COMPOSED'?`<section class="detail-section"><h3>人工成片验收</h3><div class="review-checklist"><span>身份一致</span><span>口型与语音</span><span>字幕同步</span><span>手部与画面</span><span>无乱码水印</span><span>剪辑节奏</span></div>${review?`<div class="review-callout"><strong>最近结论：${esc(reviewDecisionLabel(review.decision))}</strong><p>${esc(review.notes||'未填写备注')} · 人工 ${review.manual_minutes||0} 分钟</p></div>`:''}</section>`:'';const settings=job.production_settings||{};const showJobError=job.error&&job.state!=='PRODUCTION_REVIEW_REQUIRED';const statusMessage=job.status_reason?.message||'查看任务阶段、成品和下一步操作';openDrawer('VIDEO JOB',job.title,`<div class="drawer-summary"><span class="tag ${esc(job.display_state||job.state)}">${esc(job.display_state_label||videoStateLabel(job.display_state||job.state))}</span><p>${esc(statusMessage)}</p></div><div class="job-progress"><div class="${phase>=1?'done':''}"><i>1</i><span>制作设置</span></div><b></b><div class="${phase>=2?'done':''}"><i>2</i><span>角色定妆与母片生成</span></div><b></b><div class="${phase>=3?'done':''}"><i>3</i><span>成片验收</span></div></div>${media}${characterMakeupCards(job)}${shots}<section class="detail-section"><h3>生产信息</h3>${detailRow('任务编号',job.job_id)}${detailRow('生产档位',settings.production_profile||job.production_profile)}${detailRow('导演方案',directorPlanLabel(job.director_plan))}${detailRow('出镜角色',(job.characters||[]).map(c=>c.name||c.id).join('、')||'未指定')}${detailRow('画幅',settings.orientation||'9:16')}${detailRow('字幕样式',settings.subtitle_style||'clean')}${costObservationRows(job)}${detailRow('更新时间',job.updated_at)}${showJobError?`<div class="error-callout"><strong>失败原因</strong><p>${esc(summarizeError(job.error))}</p><details><summary>查看技术详情</summary><pre class="technical-error">${esc(job.error)}</pre></details></div>`:''}</section>${reviewBlock}<div class="drawer-footer">${deletable?'<button class="mini-action danger" id="drawer-delete-video">删除记录</button>':''}${job.state==='COMPOSED'&&!approved?'<button class="mini-action" id="drawer-review-recut">退回本地修剪</button>':''}${job.next_action?`<button class="primary-action" id="drawer-next-action">${esc(job.next_action.label)}</button>`:''}</div>`);const next=$('#drawer-next-action');if(next)next.onclick=()=>runVideoNextAction(job);const recut=$('#drawer-review-recut');if(recut)recut.onclick=()=>openVideoReview(job,'local_recut');const del=$('#drawer-delete-video');if(del)del.onclick=()=>{closeDrawer();confirmDeleteVideo(job.job_id)}}
function runVideoNextAction(job){const action=job?.next_action?.id;if(!action)return toast('当前任务没有待执行动作');if(action==='retry_network_failures')return confirmNetworkRetry(job);if(action==='confirm_budget'){closeDrawer();return openProductionSettings(job)}if(action==='increase_job_budget')return confirmVideoBudget(job);if(action==='configure_music_provider'){state.pendingMusicResumeJobId=job.job_id;closeDrawer();toast('请修正音乐配置；保存后系统只恢复本地后期，不会重做母片');return navigate('config',{historyMode:'push'})}if(['resume_failed_video','retry_identity_anchor','resume_local_postproduction'].includes(action)){closeDrawer();return resumeFailedVideo(job)}if(action==='review_continuity_anchors')return approveContinuityAnchors(job);if(['review_master_candidates','review_quality_failure'].includes(action)){const candidate=(job.shots||[]).find(shot=>shot.manual_review_required&&shot.candidate_preview_url);return candidate?openMasterCandidateReview(job,candidate):openTaskDoctor(job)}if(['view_provider_account_guide','reconcile_provider_task','reconcile_rejected_provider_cost'].includes(action))return openProviderAccountGuide(job);if(action==='review_final_video')return openVideoReview(job,'approved');if(action==='submit_publish')return submitForPublish(job.job_id);if(action==='publish_video'){closeDrawer();return navigate('publishing',{historyMode:'push'})}if(action==='repair_execution_contract')return repairExecutionContract(job);if(['recompile_production_spec','retry_failed_job','revise_output_prompt','replace_private_reference','replace_copyright_reference'].includes(action))return confirmFailedDirectorPlanRebuild(job);if(action==='preserve_and_diagnose')return openTaskDoctor(job);if(action==='retry_automatically'){closeDrawer();toast('系统正在按重试策略自动恢复，已刷新任务状态');return loadPage('videos')}if(action==='view_progress')return toast('状态已是最新，系统会在关键阶段自动更新');toast(job.next_action?.label||'请按任务指引继续处理')}
async function repairExecutionContract(job){const button=$('#drawer-next-action');if(button)button.disabled=true;try{const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/execution-contract/repair`,{method:'POST',body:'{}'});closeDrawer();toast(result.dispatched_jobs?.length?'生产状态已同步，任务已恢复执行':'生产状态已同步，任务已重新进入队列');await loadPage('videos')}catch(error){if(button)button.disabled=false;toast(error.message,true)}}
function confirmVideoBudget(job){const details=job.budget_confirmation||{};const minimum=Math.max(Number(details.minimum_budget_without_retry_cents||0),Number(details.current_budget_cents||0));openModal('BUDGET CONFIRMATION','确认任务预算',`<div class="review-callout"><strong>系统已在扣费前暂停</strong><p>已发生 ${money(details.current_cost_cents||job.cost_cents)}；关闭自动重试后，完成当前待生成母片至少需要总预算 ${money(details.minimum_budget_without_retry_cents||minimum)}</p></div><div class="form-stack"><label>本任务预算上限（元）<input id="budget-confirm-limit" type="number" min="0.01" step="0.01" value="${(minimum/100).toFixed(2)}"><small>仅调整当前任务，不修改全局默认配置</small></label><label>自动重试<select id="budget-confirm-retries"><option value="0" selected>0 次（推荐，控制成本）</option><option value="1">1 次（需要更高预算）</option></select></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-video-budget">确认预算并继续</button></div>`);$('#confirm-video-budget').onclick=async()=>{const button=$('#confirm-video-budget');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/budget-confirmation`,{method:'POST',body:JSON.stringify({confirmed:true,approved_budget_cents:Math.round(Number($('#budget-confirm-limit').value)*100),max_auto_retries:Number($('#budget-confirm-retries').value)})});closeModal();closeDrawer();toast('预算已确认，继续生成未完成母片');if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);if(fresh)openVideoDrawer(fresh)}catch(error){button.disabled=false;toast(error.message,true)}}}
function characterMappingSummary(job){const characters=job.characters||[];const mapping=characters.map(character=>{const speaker=character.script_character_name||character.id||'未命名人物';const target=character.generation_mode==='ai_generated'?'AI 自动生成':(character.name||character.asset_character_id||'AI 自动生成');return `${speaker} → ${target}`}).join('；');return mapping||'AI 按剧本人物自动生成'}
function directorPlanLabel(plan={}){const continuity=plan.master_grouping==='content_continuity'?'连续场景母片':'对白母片';if(plan.strategy==='dialogue_plus_broll')return `${plan.dialogue_master_count||0} 条${continuity} + ${plan.broll_master_count||0} 条证据 B-roll`;if(plan.strategy==='cinematic_dialogue_only')return `${plan.dialogue_master_count||0} 条${continuity}`;return '按内容连续性组织母片'}
function confirmDirectorPlanUpgrade(job){openModal('DIRECTOR PLAN UPGRADE','升级新版导演方案',`<div class="review-callout"><strong>只重建分镜计划，不调用付费模型</strong><p>系统将从已审核剧本重新规划对白母片、场景 B-roll 和本地双轨剪辑，同时保留人物绑定、画幅、字幕和成本提醒设置</p></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-director-upgrade">确认升级</button></div>`);$('#confirm-director-upgrade').onclick=async()=>{const button=$('#confirm-director-upgrade');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/upgrade-director`,{method:'POST',body:'{}'});closeModal();closeDrawer();toast('导演方案已升级');await loadPage('videos')}catch(e){button.disabled=false;toast(e.message,true)}}}
function confirmFailedDirectorPlanRebuild(job){openModal('SAFE PLAN REBUILD','重建可执行导演计划',`<div class="impact-callout"><strong>先修正计划，再允许下一次付费调用</strong><span>系统会从已审核剧本重新计算每句台词的最低可说时长，重建母片与连续性约束；旧母片计划将失效，历史成本保留，不会在本步骤调用模型</span></div><div class="review-callout"><strong>为什么不能直接继续</strong><p>当前失败可能来自台词时长、人物服装或画面文字等结构性冲突，直接重试会重复同一请求</p></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-director-rebuild">确认重建并重新预检</button></div>`);$('#confirm-director-rebuild').onclick=async()=>{const button=$('#confirm-director-rebuild');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/rebuild-director`,{method:'POST',body:'{}'});closeModal();closeDrawer();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);toast('导演方案已安全重建，历史成本保留');confirmProduction(fresh)}catch(e){button.disabled=false;toast(e.message,true)}}}
const masterTypeLabel=value=>value==='broll'?'场景 B-roll':'对白母片';
const qualityReasonLabel=value=>({missing_industry_subject:'缺少行业主体或设备',missing_environment_context:'缺少环境建立镜头',missing_meaningful_action:'缺少有效业务动作',meaningless_hand_closeup:'无意义手部特写占比过高',unexpected_readable_text:'画面出现可读文字',visual_mismatch:'画面与导演要求不匹配',unexpected_person:'出现不应出镜的人物',unexpected_speech:'出现非预期人声',transcript_mismatch:'台词与语音不一致',lip_sync_mismatch:'口型与语音不一致',repair_candidate_ready:'局部口型修复候选已生成',qa_error:'母片已生成，自动质检未完成，需人工复核',broll_semantic_evidence_missing:'缺少 B-roll 场景质检证据'})[value]||value;
const qualityDecisionLabel=decision=>({accept_candidate:'人工已接受候选',regenerate_master:'人工已要求单条重做',reject_job:'人工已终止任务',keep_prior:'人工已保留旧版'}[decision]||'人工复核已完成');
const masterStateLabel=state=>({PENDING:'等待生成',GENERATING:'生成中',OK:'生成完成',REVIEW_REQUIRED:'待人工复核',FAILED:'生成失败'})[state]||state||'等待生成';
const remainingPendingMasterCount=(job,currentShot)=>(job?.shots||[]).filter(shot=>String(shot.idx)!==String(currentShot?.idx)&&(shot.state!=='OK'||shot.manual_review_required||shot.quality_status==='candidate_accepted_pending')).length;
const masterQualityText=(shot,job={})=>{
  if(shot.state==='FAILED'&&/下载结果未知/.test(shot.error||''))return '母片已生成，下载待恢复；请核对原任务';
  if(shot.quality_status==='candidate_accepted_pending'){
    const remaining=remainingPendingMasterCount(job,shot);
    return remaining?`已接受候选，等待其余母片（${remaining} 条）`:'已接受候选，正在生成成片';
  }
  return shot.review_resolved?qualityDecisionLabel(shot.human_decision):shot.quality_reasons?.length?shot.quality_reasons.map(qualityReasonLabel).join('；'):shot.error?summarizeError(shot.error):shot.manual_review_required?'待人工复核':masterStateLabel(shot.state);
};

async function resolveMasterCandidate(job,shot,decision){
  const buttons=$$('#modal [data-candidate-decision]');
  buttons.forEach(button=>button.disabled=true);
  const accepting=decision==='accept_candidate';
  const active=buttons.find(button=>button.dataset.candidateDecision===decision);
  if(active)active.textContent=accepting?'正在接受候选…':decision==='regenerate_master'?'正在退回单条重做…':'正在终止任务…';
  try{
    const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/masters/${encodeURIComponent(shot.idx)}/candidate-review`,{method:'POST',body:JSON.stringify({decision})});
    closeModal();closeDrawer();
    if(!await loadPage('videos'))return;
    const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);
    if(decision==='reject_job'){toast('任务已人工终止；已付费候选与母片均已保留')}
    else if(result.state==='PROVIDER_ACTION_REQUIRED'){toast('复核决定已保存；存在待核对远端任务，未启动配乐或重合成');openVideoDrawer(fresh)}
    else if(result.remaining_review_count>0){toast(`本条复核已保存，还有 ${result.remaining_review_count} 条；请继续复核其余母片`);openVideoDrawer(fresh)}
    else if(result.remaining_generation_count>0){toast(`复核已保存，仍有 ${result.remaining_generation_count} 条母片需要继续生成`);openVideoDrawer(fresh)}
    else if(result.state==='COMPOSED')toast(accepting?'候选母片已接受，成片已在本地重新合成':'当前母片已退回单条重做');
    else if(accepting&&autoPollProductionStates.has(result.state)){toast('候选母片已接受，系统正在自动生成成片');openVideoDrawer(fresh)}
    else {toast('复核决定已保存，请按任务下一步继续处理');openVideoDrawer(fresh)}
  }catch(e){buttons.forEach(button=>button.disabled=false);if(active)active.textContent=accepting?'接受候选':decision==='regenerate_master'?'单条重做':'终止任务';toast(e.message,true)}
}

function repairComparisonMarkup(shot){
  const comparison=shot.repair_comparison;
  if(!comparison?.available)return'';
  const start=Number(comparison.repair_start_sec||0);
  const end=Number(comparison.repair_end_sec||0);
  return `<section class="repair-comparison"><header><strong>局部修复前后对比</strong><span>修复区间 ${start}–${end} 秒 · 增量成本 ${money(comparison.additional_cost_cents)}</span></header><div class="repair-comparison-grid"><article><b>原始母片</b><video controls preload="metadata" src="${esc(comparison.original_preview_url)}?v=${Date.now()}"></video></article><article><b>局部修复候选</b><video controls preload="metadata" src="${esc(comparison.candidate_preview_url)}?v=${Date.now()}"></video></article></div><small>两段视频使用同一修复区间；人工确认后才替换原母片，不触发整条视频重做</small></section>`;
}

function candidateReviewContext(job,shot){
  const makeup=job.character_makeup_plan||[];
  const makeupMarkup=makeup.length?`<div class="candidate-context-list">${makeup.map(item=>`<article>${item.preview_url?`<img src="${esc(item.preview_url)}" alt="${esc(item.display_name||'人物')} 定妆">`:'<span class="candidate-context-missing">未提供</span>'}<b>${esc(item.display_name||item.identity_key||'人物')}</b></article>`).join('')}</div>`:'<p class="candidate-context-empty">人物定妆：未提供</p>';
  const scene=(job.scene_anchor_plan||[]).find(item=>(item.master_indexes||[]).map(Number).includes(Number(shot.idx)));
  const sceneMarkup=scene?.preview_url?`<img class="candidate-context-wide" src="${esc(scene.preview_url)}" alt="场景锚点">`:'<p class="candidate-context-empty">场景锚点：未提供</p>';
  const previousMarkup=shot.previous_master_preview_url?`<video class="candidate-context-wide" controls preload="metadata" src="${esc(shot.previous_master_preview_url)}"></video>`:'<p class="candidate-context-empty">上一条连续母片：未提供</p>';
  return `<section class="candidate-review-context"><h3>连续性参照</h3><div class="candidate-context-block"><b>人物定妆</b>${makeupMarkup}</div><div class="candidate-context-block"><b>场景锚点</b>${sceneMarkup}</div><div class="candidate-context-block"><b>上一条连续母片</b>${previousMarkup}</div></section>`;
}

function openMasterCandidateReview(job,shot){
  if(job.recovery&&!job.recovery.action_ids.includes('review_master_candidates'))return openTaskDoctor(job);
  const reasons=(shot.quality_reasons||[]).map(reason=>`<li>${esc(qualityReasonLabel(reason))}</li>`).join('')||'<li>自动质检需要人工确认</li>';
  const technical=shot.error?`<details class="candidate-technical"><summary>查看技术详情</summary><pre class="technical-error">${esc(shot.error)}</pre></details>`:'';
  const readableTextWarning=shot.quality_reasons?.includes('unexpected_readable_text');
  const acceptance='<button class="primary-action" data-candidate-decision="accept_candidate">接受候选</button>';
  const lipSyncRepair=shot.lip_sync_repair_eligible?'<button class="mini-action" data-candidate-lip-sync-repair>局部口型修复</button>':'';
  openModal('MASTER REVIEW',`复核母片 #${shot.idx}`,`<div class="candidate-review-layout"><section class="candidate-review-media"><video class="video-preview" controls preload="metadata" src="${esc(shot.candidate_preview_url)}?v=${Date.now()}"></video><small>此视频是被自动质检拦截的候选版本，尚未进入最终成片</small>${candidateReviewContext(job,shot)}</section><section class="candidate-review-reasons"><p class="section-no">QUALITY FINDINGS</p><h3>候选与要求的差异</h3><ul>${reasons}</ul>${lipSyncEvidence(shot)}<div class="impact-callout"><strong>${readableTextWarning?'检测到悬浮文案，建议优先单条重做，仍可人工接受':'人工决定不会重复购买已接受母片'}</strong><span>${readableTextWarning?'人工接受会保留该质检风险记录并直接进入本地合成，请确认文字不影响交付。':shot.lip_sync_repair_eligible?'无需先接受口型不合格候选；可直接进入局部编辑，原候选保留。':'接受候选只进入本地包装；单条重做只重生成当前非 OK 母片；终止任务不会删除已付费产物。'}</span></div>${technical}</section></div><div class="modal-actions candidate-review-actions"><button class="mini-action danger" data-candidate-decision="reject_job">终止任务</button><button class="mini-action" data-candidate-decision="regenerate_master">单条重做</button>${lipSyncRepair}${acceptance}</div>`);
  $('#modal').classList.add('candidate-review-open');
  if(job.recovery)$$('[data-candidate-decision]',$('#modal')).forEach(button=>{if(!job.recovery.candidate_decisions?.includes(button.dataset.candidateDecision))button.remove()});
  const comparison=repairComparisonMarkup(shot);
  if(comparison){
    const media=$('#modal .candidate-review-media');
    const directVideo=[...media.children].find(item=>item.classList?.contains('video-preview'));
    const directNote=directVideo?.nextElementSibling;
    directVideo?.remove();
    if(directNote?.tagName==='SMALL')directNote.remove();
    media.insertAdjacentHTML('afterbegin',comparison);
  }
  $$('[data-candidate-decision]',$('#modal')).forEach(button=>button.onclick=()=>resolveMasterCandidate(job,shot,button.dataset.candidateDecision));
  const repair=$('[data-candidate-lip-sync-repair]',$('#modal'));if(repair)repair.onclick=()=>confirmLipSyncRepair(job,shot);
}
const beatDisplayLabel=value=>({hook:'开场钩子',conflict:'冲突',evidence:'证据',reversal:'反转',solution:'解决方案',cta:'行动号召'})[value]||value||'场景';
function transitionDisplay(item,index){const type=item.transition_type||'none';const relation=item.continuity_relation||'';if(type==='dip_to_black')return`场景切换 · ${Number(item.transition_duration_sec||0.12)} 秒短暗场`;if(type==='match_cut')return'同场景动作匹配切';if(relation==='audio_bridge')return'声音桥接硬切';if(relation==='same_master_continuation')return'同母片连续推进';if(relation==='opening'||index===0)return'首镜直接进入';return'沿用原剪辑策略'}
function directorTimelineMarkup(job){const items=job.director_timeline||[];if(!items.length)return'';return `<section class="detail-section director-timeline"><div class="section-line"><h3>导演时间线</h3><small>画面、声音与镜头衔接分轨执行</small></div><div class="director-timeline-list">${items.map((item,index)=>`<article class="director-cue ${item.uses_broll?'broll':''}"><header><b>${String(index+1).padStart(2,'0')}</b><strong>${esc(beatDisplayLabel(item.beat))}</strong><i>${Number(item.duration_sec||0)}s</i></header><div><span><small>画面轨</small><em>${esc(item.video_source_label)}</em></span><span><small>声音轨</small><em>${esc(item.audio_source_label)}</em></span></div><div class="director-transition"><small>镜头衔接</small><em>${esc(transitionDisplay(item,index))}</em>${item.transition_reason?`<span>${esc(item.transition_reason)}</span>`:''}</div><p>${esc(item.visual_intent||item.broll_hint||'按剧本节拍执行')}</p></article>`).join('')}</div></section>`}
function confirmMasterRegeneration(job,shot){const estimate=shot.regeneration_uses_library?'本地素材复用，预计不新增模型成本':`预计追加 ${money(shot.regeneration_estimated_cost_cents)}`;openModal('MASTER REGENERATION',`返修 ${masterTypeLabel(shot.master_type)} #${shot.idx}`,`<div class="impact-callout"><strong>只重新生成当前母片</strong><span>其余 ${Math.max(0,(job.completed_master_count||0)-1)} 条成功母片和历史成本都会保留；新成片必须重新人工验收</span></div><div class="preflight-grid">${detailRow('目标母片',`${masterTypeLabel(shot.master_type)} #${shot.idx}`)}${detailRow('母片时长',`${Number(shot.duration_sec||0)} 秒`)}${detailRow('追加成本',estimate)}${detailRow('历史累计',money(job.cost_cents))}</div><label>返修原因<textarea id="master-regeneration-reason" rows="4" placeholder="例如：行业场景不足，需要出现设备、台账和调度动作"></textarea></label><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-master-regeneration">进入单母片返修预检</button></div>`);$('#confirm-master-regeneration').onclick=async()=>{const reason=$('#master-regeneration-reason').value.trim();if(!reason)return toast('请填写该母片需要返修的原因',true);const button=$('#confirm-master-regeneration');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/masters/${shot.idx}/regenerate`,{method:'POST',body:JSON.stringify({reason})});closeModal();closeDrawer();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);toast(`已保留 ${fresh.completed_master_count} 条成功母片`);confirmProduction(fresh)}catch(e){button.disabled=false;toast(e.message,true)}}}
function lipSyncEvidence(shot){const evidence=shot.lip_sync_evidence||{};const segments=evidence.segments||[];if(!segments.length)return'';return`<div class="impact-callout subtle"><strong>口型定位证据</strong><span>整体对齐度 ${Math.round(Number(evidence.overall_alignment||0)*100)}% · ${segments.map(item=>`${item.start_sec}–${item.end_sec} 秒 / 偏移 ${item.offset_ms}ms / 置信度 ${Math.round(Number(item.confidence||0)*100)}%`).join('；')}</span></div>`}
function confirmLipSyncRepair(job,shot){const duration=Math.max(.1,Number(shot.duration_sec||0));const suggestion=shot.lip_sync_repair_suggestion||{start_sec:0,end_sec:duration,dialogue:'',reason:'口型与原生语音不同步'};const suggestedStart=Math.max(0,Math.min(duration,Number(suggestion.start_sec)||0));const suggestedEnd=Math.max(suggestedStart+.1,Math.min(duration,Number(suggestion.end_sec)||duration));openModal('LIP SYNC EDIT',`局部口型修复 #${shot.idx}`,`<div class="impact-callout"><strong>保留原生语音、人物表演和镜头运动</strong><span>Seedance 2.5 只编辑指定时间段的嘴唇、牙齿、舌位与下颌运动；原母片在修复成功并重新验收前始终保留</span></div>${lipSyncEvidence(shot)}${shot.lip_sync_repair_suggestion?`<div class="impact-callout subtle"><strong>AI 建议区间：${suggestedStart}–${suggestedEnd} 秒</strong><span>${esc(suggestion.dialogue||'已根据字幕时间轴定位')}；区间可人工调整</span></div>`:''}<div class="preflight-grid">${detailRow('目标母片',`对白母片 #${shot.idx}`)}${detailRow('母片时长',`${duration} 秒`)}${detailRow('声音策略','沿用原母片原生语音')}${detailRow('画面策略','仅修正口型，不改表演与运镜')}</div><div class="form-grid two"><label>修复开始（秒）<input id="lip-sync-start" type="number" min="0" max="${duration}" step="0.1" value="${suggestion.start_sec??suggestedStart}"></label><label>修复结束（秒）<input id="lip-sync-end" type="number" min="0.1" max="${duration}" step="0.1" value="${suggestion.end_sec??suggestedEnd}"></label></div><label>问题说明<textarea id="lip-sync-reason" rows="3" placeholder="例如：林夏在 3-15 秒嘴型比原生语音慢">${esc(suggestion.reason||'口型与原生语音不同步')}</textarea></label><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-lip-sync-repair">进入局部修复预检</button></div>`);$('#confirm-lip-sync-repair').onclick=async()=>{const repair_start_sec=Number($('#lip-sync-start').value);const repair_end_sec=Number($('#lip-sync-end').value);const reason=$('#lip-sync-reason').value.trim();if(!Number.isFinite(repair_start_sec)||!Number.isFinite(repair_end_sec)||repair_start_sec<0||repair_end_sec<=repair_start_sec||repair_end_sec>duration)return toast('请填写母片时长范围内的有效修复区间',true);const button=$('#confirm-lip-sync-repair');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/masters/${shot.idx}/lip-sync-repair`,{method:'POST',body:JSON.stringify({repair_start_sec,repair_end_sec,reason})});closeModal();closeDrawer();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);toast('已保留原母片，等待确认局部修复预算');confirmProduction(fresh)}catch(e){button.disabled=false;toast(e.message,true)}}}
function confirmMasterExtension(job,shot){openModal('VIDEO EXTEND',`延长母片 #${shot.idx}`,`<div class="impact-callout"><strong>延长 4–30 秒，原母片保持不变</strong><span>Seedance 2.5 使用原片末帧、人物身份、站位、动作惯性、光线与镜头轴线继续生成；结果先进入人工候选复核</span></div><div class="form-grid two"><label>延长秒数<input id="extend-master-seconds" type="number" min="4" max="30" step="1" value="8"></label><label>原母片时长<input value="${Number(shot.duration_sec||0)} 秒" disabled></label></div><label>延长方向<textarea id="extend-master-direction" rows="4" placeholder="例如：镜头继续跟随工程师走向控制台，保持同一场景和站位，并自然收束"></textarea></label><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-master-extension">进入延长预检</button></div>`);$('#confirm-master-extension').onclick=async()=>{const extension_sec=Number($('#extend-master-seconds').value);const direction=$('#extend-master-direction').value.trim();if(!Number.isFinite(extension_sec)||extension_sec<4||extension_sec>30)return toast('延长秒数必须在 4 到 30 秒之间',true);if(!direction)return toast('请填写延长后的动作、运镜和结束状态',true);const button=$('#confirm-master-extension');button.disabled=true;try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/masters/${shot.idx}/extend`,{method:'POST',body:JSON.stringify({extension_sec,direction})});closeModal();closeDrawer();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);toast('已保留原母片，等待确认延长候选预算');confirmProduction(fresh)}catch(e){button.disabled=false;toast(e.message,true)}}}
function masterRepairActions(shot){const review=shot.candidate_preview_url&&shot.candidate_review_eligible!==false?`<button class="mini-action" data-review-master="${shot.idx}">人工复核候选</button>`:'';return`${review}${shot.lip_sync_repair_eligible?`<button class="mini-action" data-lip-sync-repair="${shot.idx}">局部口型修复</button>`:''}${shot.candidate_preview_url?'':`${shot.extension_eligible?`<button class="mini-action" data-extend-master="${shot.idx}">延长母片</button>`:''}${shot.regeneration_eligible?`<button class="mini-action" data-regenerate-master="${shot.idx}">单独返修</button>`:''}`}`}
function enhanceVideoDirectorDrawer(job){const timeline=directorTimelineMarkup(job);const sections=$$('#drawer .detail-section');const masterSection=sections.find(section=>section.querySelector('h3')?.textContent==='母片与质检');if(timeline&&masterSection)masterSection.insertAdjacentHTML('beforebegin',timeline);const list=masterSection?.querySelector('.master-list');if(list&&job.shots?.length){list.innerHTML=job.shots.map(shot=>`<article data-video-master="${shot.idx}" class="master-row master-row-rich ${shot.manual_review_required?'review':shot.error?'failed':''}"><b>#${shot.idx}</b><span><strong>${masterTypeLabel(shot.master_type)}</strong><small>${esc(shot.speaker||shot.provider||'待生成')} · ${Number(shot.duration_sec||0)} 秒 · 累计 ${money(shot.cost_cents)}${shot.error&&job.final_exists?' · 旧成片已保留':''}${shot.review_resolved&&shot.reviewed_at?` · 复核 ${esc(short(shot.reviewed_at))}`:''}</small></span><em>${esc(masterQualityText(shot,job))}</em>${masterRepairActions(shot)}</article>`).join('');$$('[data-review-master]',list).forEach(button=>button.onclick=()=>openMasterCandidateReview(job,job.shots.find(shot=>String(shot.idx)===button.dataset.reviewMaster)));$$('[data-lip-sync-repair]',list).forEach(button=>button.onclick=()=>confirmLipSyncRepair(job,job.shots.find(shot=>String(shot.idx)===button.dataset.lipSyncRepair)));$$('[data-extend-master]',list).forEach(button=>button.onclick=()=>confirmMasterExtension(job,job.shots.find(shot=>String(shot.idx)===button.dataset.extendMaster)));$$('[data-regenerate-master]',list).forEach(button=>button.onclick=()=>confirmMasterRegeneration(job,job.shots.find(shot=>String(shot.idx)===button.dataset.regenerateMaster)))}}
const enhanceVideoDirectorDrawerStateBase=enhanceVideoDirectorDrawer;
enhanceVideoDirectorDrawer=function(job){enhanceVideoDirectorDrawerStateBase(job);const masterSection=$$('#drawer .detail-section').find(section=>section.querySelector('h3')?.textContent==='母片与质检');if(!masterSection)return;const summary=masterSection.querySelector('.section-line small');if(summary)summary.textContent=`${job.completed_master_count||0} 完成 · ${job.generating_master_count||0} 生成中 · ${job.waiting_master_count||0} 等待 · ${job.failed_master_count||0} 异常`;$$('.master-row',masterSection).forEach((row,index)=>{const shot=job.shots?.[index];if(shot)row.classList.add(String(shot.state||'pending').toLowerCase())})};
const openVideoDrawerBase=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerBase(job);if(!job)return;state.openVideoJobId=job.job_id;enhanceVideoDirectorDrawer(job);const footer=$('#drawer .drawer-footer');if(job.director_plan_upgradeable&&footer){footer.insertAdjacentHTML('afterbegin','<button class="mini-action" id="drawer-upgrade-director">升级新版导演方案</button>');$('#drawer-upgrade-director').onclick=()=>confirmDirectorPlanUpgrade(job)}if(['FAILED','BLOCKED'].includes(job.state)&&footer){footer.insertAdjacentHTML('afterbegin','<button class="mini-action" id="drawer-rebuild-director">重建可执行导演计划</button>');$('#drawer-rebuild-director').onclick=()=>confirmFailedDirectorPlanRebuild(job)}};
function customCharacterSeedanceNotice(characters=[]){const names=characters.filter(character=>character.identity_type==='custom').map(character=>character.script_character_name||character.name||character.id);if(!names.length)return'';return `<div class="impact-callout"><strong>自定义角色先生成独立身份母片</strong><span>${esc(names.join('、'))} 的本地图片仅用于角色资产管理和人物设定，不会作为普通人脸参考图提交 Seedance；这是火山隐私规则要求。系统会先生成合规的身份母片与定妆帧，再把它作为所有剧情镜头的一致性引用</span><code>seedance_reference_mode: description_only</code></div>`}
function makeupStatusLabel(status){return({ready:'定妆就绪',pending:'待自动定妆',generating:'正在定妆',failed:'定妆失败'})[String(status||'').toLowerCase()]||'待自动定妆'}
const anchorGeneratorLabel=value=>({seedream_5_0:'Seedream 5.0 · 静态锚点图',seedance_2_5:'Seedance 2.5 · 动态母片抽帧',volc_asset:'火山角色资产'})[value]||value||'待确定';
const anchorGeneratorOptions=selected=>`<option value="seedream_5_0" ${selected==='seedream_5_0'?'selected':''}>Seedream 5.0 · 静态锚点图</option><option value="seedance_2_5" ${selected==='seedance_2_5'?'selected':''}>Seedance 2.5 · 动态母片抽帧</option>`;
function characterMakeupCards(job){const plan=job?.character_makeup_plan||job?.preflight?.character_makeup_plan||[];if(!plan.length)return'';const sourceLabel=item=>item.source==='volc_asset'?'火山角色资产':item.source==='seedance_identity_fallback'?'Seedance 2.5 · 隐私回退母片':anchorGeneratorLabel(item.generator);return `<section class="detail-section makeup-section"><div class="section-line"><h3>角色定妆与一致性锚点</h3><small>${plan.length} 个唯一人物</small></div><div class="makeup-grid">${plan.map(item=>`<article class="makeup-card ${esc(item.status||'pending')}">${item.preview_url?`<img src="${esc(item.preview_url)}" alt="${esc(item.display_name)} 定妆照">`:`<div class="makeup-placeholder">${esc((item.display_name||'?').slice(0,1))}</div>`}<div><strong>${esc(item.display_name)}</strong><span>${sourceLabel(item)}</span><small>${makeupStatusLabel(item.status)}${item.requires_paid_call&&item.estimated_cost_cents?` · ${money(item.estimated_cost_cents)}`:''}</small>${item.model?`<small>${esc(item.model)}</small>`:''}${item.error?`<em>${esc(item.error)}</em>`:''}</div></article>`).join('')}</div><p class="section-note">AI 人物按本任务选择的锚点生成器完成一次定妆并贯穿全部母片；固定火山角色直接复用已授权资产。重复核心场景按需生成，不做全量预生成</p></section>`}
const productionStageStateLabel=value=>({SUCCEEDED:'已完成',RUNNING:'进行中',READY:'已就绪',PENDING:'等待前序',REVIEW_REQUIRED:'待人工确认',ACTION_REQUIRED:'需人工处理',BUDGET_REQUIRED:'预算待确认',FAILED:'执行失败',SKIPPED:'无需执行'})[value]||value;
function productionStageCards(job){const stages=job?.production_stages||[];if(!stages.length)return'';const nodes=job?.production_graph_nodes||[];return `<section class="detail-section production-stage-section"><div class="section-line"><h3>生产进度</h3><small>按依赖自动推进</small></div><div class="production-stage-grid">${stages.map(stage=>`<article class="production-stage-card ${String(stage.state||'PENDING').toLowerCase()}"><i></i><div><strong>${esc(stage.label)}</strong><span>${esc(productionStageStateLabel(stage.state))}</span></div><small>${Number(stage.completed||0)}/${Number(stage.total||0)}</small></article>`).join('')}</div><details class="production-technical"><summary>技术详情</summary><div class="production-node-list">${nodes.map(node=>`<article><b>${esc(node.node_id)}</b><span>${esc(node.state)}</span>${node.provider_task_id?`<small>Provider ${esc(node.provider_task_id)}</small>`:''}${node.error_code?`<em>${esc(node.error_code)}</em>`:''}</article>`).join('')}</div></details></section>`}
function fixedVoiceCards(job){const plan=job?.preflight?.voice_plan||{};const roles=plan.roles||[];if(!roles.length)return'';return `<section class="detail-section fixed-voice-plan"><div class="section-line"><h3>角色固定声线</h3><small>${plan.bound_role_count||0} 已绑定 · ${plan.auto_match_role_count||0} 待自动匹配</small></div><div class="fixed-voice-grid">${roles.map(role=>`<article class="fixed-voice-role ${role.status==='bound'?'ready':'pending'}"><span>${esc((role.display_name||'?').slice(0,1))}</span><div><strong>${esc(role.display_name||role.character_id)}</strong><small>${role.status==='bound'?`${esc(role.voice_name||role.voice_type)} · ${esc(role.voice_type)}`:'将在付费视频调用前按角色设定匹配火山声线'}</small></div></article>`).join('')}</div><p class="section-note">每个角色整条任务固定同一 voice_type；火山 TTS 生成精确音轨和时间戳，Seedance 不再随机合成声线</p></section>`}
function anchorApprovalLabel(item){return String(item?.approval_state||'').toLowerCase()==='approved'?'已确认':'待确认'}
const characterMakeupCardsWithApproval=characterMakeupCards;
characterMakeupCards=function(job){const html=characterMakeupCardsWithApproval(job);const pending=(job?.character_makeup_plan||[]).filter(item=>item.source!=='volc_asset'&&String(item.approval_state||'').toLowerCase()!=='approved');if(!pending.length)return html;return html.replace('</section>',`<div class="anchor-approval-summary"><strong>人物锚点待确认</strong><span>${esc(pending.map(item=>`${item.display_name}·${anchorApprovalLabel(item)}`).join(' / '))}</span></div></section>`)}
function sceneAnchorCards(job){const plan=job?.scene_anchor_plan||job?.preflight?.paid_scene_anchors||[];if(!plan.length)return'';const statusLabel=status=>({ready:'锚点就绪',pending:'待生成',generating:'正在生成',failed:'生成失败'})[String(status||'pending').toLowerCase()]||'待生成';const siteCount=plan.filter(item=>item.anchor_scope==='site').length;const sceneCount=plan.length-siteCount;const scopeSummary=[sceneCount?`${sceneCount} 个同一物理场景`:'',siteCount?`${siteCount} 个项目现场`:''].filter(Boolean).join(' · ');return `<section class="detail-section scene-anchor-section"><div class="section-line"><h3>场景连续性锚点</h3><small>${scopeSummary}</small></div><div class="scene-anchor-grid">${plan.map(item=>{const isSite=item.anchor_scope==='site';const scopeLabel=isSite?'项目现场连续性锚点':'同一物理场景锚点';return `<article class="scene-anchor-card ${esc(item.status||'pending')}">${item.preview_url?`<a href="${esc(item.preview_url)}" target="_blank" rel="noreferrer"><img src="${esc(item.preview_url)}" alt="${esc(item.anchor_key)} ${scopeLabel}"></a>`:`<div class="scene-anchor-placeholder">景</div>`}<div><b>${scopeLabel}</b><strong>${esc(item.description||item.anchor_key)}</strong><span>${esc(item.anchor_key)} · ${esc(anchorGeneratorLabel(item.generator))}</span><small>关联母片 ${(item.master_indexes||[]).map(index=>`#${index}`).join('、')||'待识别'} · ${statusLabel(item.status)}${Number(item.cost_cents||item.estimated_cost_cents)?` · ${money(item.cost_cents||item.estimated_cost_cents)}`:''}</small>${item.model?`<small>${esc(item.model)}</small>`:''}${item.error?`<em>${esc(item.error)}</em>`:''}</div></article>`}).join('')}</div><p class="section-note">同一物理场景锚点锁定空间身份和轴线，人物按自由调度、连续调度或精确锁定执行；项目现场锚点不锁定不同子空间构图。一次性场景不预生成</p></section>`}
const sceneAnchorCardsWithPlan=sceneAnchorCards;
const scenePositionLabels={'screen-left':'左侧','screen-center':'中间','screen-right':'右侧'};
function normalizedScenePositions(item){
  const positions=item?.blocking_anchor?.positions||{};
  if(Array.isArray(positions))return positions.map(position=>({
    display_name:position.display_name||position.identity_key||'人物',
    screen_position:position.screen_position||'screen-center'
  }));
  return Object.entries(positions).map(([name,position])=>({
    display_name:name,
    screen_position:typeof position==='string'?position:(position?.screen_position||'screen-center')
  }));
}
function normalizedSceneState(item,key,fallback=[]){
  const stateValue=item?.blocking_anchor?.[key];
  if(!stateValue)return fallback;
  const positions=stateValue?.positions??stateValue;
  if(Array.isArray(positions))return positions.map(position=>({
    display_name:position.display_name||position.identity_key||'人物',
    screen_position:position.screen_position||'screen-center'
  }));
  if(positions&&typeof positions==='object')return Object.entries(positions).map(([name,position])=>({
    display_name:name,
    screen_position:typeof position==='string'?position:(position?.screen_position||'screen-center')
  }));
  return fallback;
}
function sceneStateChips(positions){
  return positions.map(position=>`<span title="${esc(position.display_name)} · ${esc(scenePositionLabels[position.screen_position]||'画面内')}"><i>${esc(position.display_name.slice(0,1))}</i><b>${esc(position.display_name)}</b><small>${esc(scenePositionLabels[position.screen_position]||'画面内')}</small></span>`).join('')||'<em>按剧情入画</em>';
}
function sceneBlockingStage(item){
  const positions=normalizedScenePositions(item);
  if(!positions.length)return'';
  const mode=String(item?.blocking_anchor?.mode||'locked').toLowerCase();
  const modeLabel={free:'自由调度',continuity:'连续调度',locked:'精确锁定'}[mode]||'精确锁定';
  const baseline=positions.map(position=>`${position.display_name}在${scenePositionLabels[position.screen_position]||'中间'}`).join('，');
  let stage='';
  let summary='';
  if(mode==='free'){
    stage=`<div class="scene-free-zone"><header><small>人物活动区</small><b>不锁定左 / 中 / 右</b></header><div class="scene-free-cast">${sceneStateChips(positions)}</div><div class="scene-free-path"><span>进入画面</span><i>→</i><span>按剧情走位与表演</span><i>→</i><span>换位或离画</span></div><div class="scene-axis"><i></i><small>场景与 180° 轴线保持稳定</small></div></div>`;
    summary=`构图起点仅作参考：${baseline}；人物可按剧情进入、移动、换位或离开画面`;
  }else if(mode==='continuity'){
    const incoming=normalizedSceneState(item,'blocking_in',positions);
    const outgoing=normalizedSceneState(item,'blocking_out',incoming);
    const movement=item?.blocking_anchor?.movement||'承接动作惯性并按剧情推进';
    stage=`<div class="scene-motion-flow"><div class="scene-state-frame incoming"><small>起始状态</small><div>${sceneStateChips(incoming)}</div></div><div class="scene-motion-arrow"><small>镜头运动</small><i>→</i><b>${esc(movement)}</b></div><div class="scene-state-frame outgoing"><small>结束状态</small><div>${sceneStateChips(outgoing)}</div></div><div class="scene-axis"><i></i><small>结束状态交给下一母片继续</small></div></div>`;
    summary='连续调度锁定的是状态交接，而不是全程固定构图；下一母片继承本镜结束时的位置、动作趋势和轴线';
  }else{
    const slots=['screen-left','screen-center','screen-right'];
    const slotMarkup=slots.map(slot=>{
      const people=positions.filter(position=>position.screen_position===slot);
      const peopleMarkup=people.length
        ?people.map(person=>`<span title="${esc(person.display_name)}"><i>${esc(person.display_name.slice(0,1))}</i><b>${esc(person.display_name)}</b></span>`).join('')
        :'<em>留空</em>';
      return `<div class="scene-position ${slot.replace('screen-','')}"><small>${scenePositionLabels[slot]}</small>${peopleMarkup}</div>`;
    }).join('');
    stage=`<div class="scene-locked-stage"><header><small>固定构图</small><b>人物位置保持不变</b></header><div class="scene-blocking-stage">${slotMarkup}<div class="scene-axis"><i></i><small>保持 180° 轴线</small></div></div></div>`;
    summary=`精确锁定：${baseline}；仅用于固定机位、证据镜头或明确指定的构图`;
  }
  return `<article class="scene-blocking-card ${esc(mode)}"><header><strong>空间连续性 · ${modeLabel}</strong><span>${esc(item.description||item.anchor_key||'当前场景')}</span></header>${stage}<p>${esc(summary)}</p></article>`;
}
sceneAnchorCards=function(job){
  const plan=job?.scene_anchor_plan||job?.preflight?.paid_scene_anchors||[];
  if(plan.length){
    const cards=sceneAnchorCardsWithPlan(job);
    const blocking=plan.map(sceneBlockingStage).filter(Boolean);
    const pending=plan.filter(item=>String(item.approval_state||'').toLowerCase()!=='approved');
    const extras=`${blocking.length?`<div class="scene-blocking-summary">${blocking.join('')}</div>`:''}${pending.length?`<div class="anchor-approval-summary"><strong>场景锚点待确认</strong><span>${esc(pending.map(item=>item.description||item.anchor_key).join(' / '))}</span></div>`:''}`;
    return extras?cards.replace('</section>',`${extras}</section>`):cards;
  }
  return `<section class="detail-section scene-anchor-section"><div class="section-line"><h3>场景连续性锚点</h3><small>0 个同一物理场景</small></div><div class="scene-anchor-empty"><strong>本任务没有跨母片重复的同一物理场景</strong><span>人物一致性仍由角色定妆锚点保障；一次性场景由视频模型直接生成</span></div></section>`;
};
function mountSceneAnchorCards(job,container){const html=sceneAnchorCards(job);if(!html||!container||container.querySelector('.scene-anchor-section'))return;const host=document.createElement('div');host.innerHTML=html;const makeup=container.querySelector('.makeup-section');const summary=container.querySelector('.drawer-summary,.preflight-grid');(makeup||summary)?.insertAdjacentElement('afterend',host.firstElementChild)}
const openVideoDrawerWithSceneAnchors=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithSceneAnchors(job);mountSceneAnchorCards(job,$('#drawer-body'))}
async function approveContinuityAnchors(job){const note=$('#anchor-review-note')?.value||'';const button=$('#approve-continuity-anchors');if(button)button.disabled=true;try{const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/anchors/approve`,{method:'POST',body:JSON.stringify({note})});closeDrawer();toast(result.dispatched_jobs?.includes(job.job_id)?'锚点已确认，母片生成已继续':'锚点已确认，任务已重新排队');loadPage('videos')}catch(e){if(button)button.disabled=false;toast(e.message,true)}}
function continuityAnchorProgress(job){const characters=job?.character_makeup_plan||job?.preflight?.character_makeup_plan||[];const scenes=job?.scene_anchor_plan||job?.preflight?.paid_scene_anchors||[];const ready=item=>String(item?.status||'').toLowerCase()==='ready';const characterReady=characters.filter(ready).length;const sceneReady=scenes.filter(ready).length;const waiting=[];if(characterReady<characters.length)waiting.push('人物');if(sceneReady<scenes.length)waiting.push('场景');return {characters,scenes,characterReady,sceneReady,complete:characters.length+scenes.length>0&&!waiting.length,waiting}}
const openVideoDrawerWithAnchorReview=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithAnchorReview(job);const footer=$('#drawer .drawer-footer');if(!footer)return;const progress=continuityAnchorProgress(job);if(job?.state==='ANCHOR_REVIEW_REQUIRED'){footer.insertAdjacentHTML('beforebegin',`<section class="detail-section anchor-review-panel" id="anchor-generation-status"><h3>人物与场景锚点确认</h3><p>人物 ${progress.characterReady}/${progress.characters.length} · 场景 ${progress.sceneReady}/${progress.scenes.length}</p><p>请逐项预览定妆照、场景图和空间连续性模式。确认后系统才会继续产生剧情母片费用。</p><label>审核备注<textarea id="anchor-review-note" rows="3" placeholder="可选：记录造型、场景或人物调度确认意见"></textarea></label></section>`);return}if(['PENDING','PRODUCTION_QUEUED','PRODUCTION_RUNNING'].includes(job?.state)&&progress.characters.length+progress.scenes.length>0&&!progress.complete){footer.insertAdjacentHTML('beforebegin',`<section class="detail-section anchor-review-panel anchor-generation-waiting" id="anchor-generation-status"><h3>锚点正在生成</h3><p>人物 ${progress.characterReady}/${progress.characters.length} · 场景 ${progress.sceneReady}/${progress.scenes.length}</p><small>系统完成全部锚点后会切换为人工确认，不会提前产生剧情母片费用</small></section>`)}}
async function reconcileProviderTask(job,taskId){const button=$(`[data-provider-task="${CSS.escape(taskId)}"]`);if(button)button.disabled=true;try{const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/provider-task/reconcile`,{method:'POST',body:JSON.stringify({task_id:taskId})});closeModal();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);toast(result.state==='REJECTED'?'已终止任务的成本已核对':result.state==='REJECTED_PENDING_RECONCILIATION'?'已核对该远端任务；业务仍保持终止，继续核对剩余成本':result.state==='PROVIDER_ACTION_REQUIRED'?'远端任务仍未结束，任务继续保持暂停':`远端任务已核对，已恢复到 ${videoStateLabel(result.state)}`);if(fresh)openVideoDrawer(fresh)}catch(e){if(button)button.disabled=false;toast(e.message,true)}}
function openProviderAccountGuide(job){const tasks=job?.unresolved_provider_tasks||[];const taskRows=tasks.map(task=>task.reconcilable===false?`<div class="record-row provider-task-manual"><span><strong>${esc(task.provider)} · ${esc(task.intent||'远端任务')}</strong><small>${esc(task.task_id)}</small><small>${esc(task.action_message||'缺少远端任务 ID，请人工处置')}</small></span><i>人工处置</i></div>`:`<button class="record-row interactive" data-provider-task="${esc(task.task_id)}"><span><strong>${esc(task.provider)} · ${esc(task.intent||'远端任务')}</strong><small>${esc(task.task_id)}</small></span><i>核对 ›</i></button>`).join('')||'<p class="candidate-context-empty">未提供可核对的远端 task_id，请查看技术详情</p>';openModal('PROVIDER ACTION','模型账号与远端任务待处理',`<div class="impact-callout"><strong>系统已暂停所有新的付费提交</strong><span>先处理模型账号问题，再逐个核对原 task_id。系统只查询原任务，不会重新 POST。</span></div><section class="detail-section"><h3>待核对远端任务</h3>${taskRows}</section><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">稍后处理</button></div>`);$$('[data-provider-task]',$('#modal')).forEach(button=>button.onclick=()=>reconcileProviderTask(job,button.dataset.providerTask))}
const openVideoDrawerWithBusinessPauseAction=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithBusinessPauseAction(job);if(!['PRODUCTION_REVIEW_REQUIRED','PROVIDER_ACTION_REQUIRED','REJECTED_PENDING_RECONCILIATION'].includes(job?.state))return;const recoveryReady=job.state==='PROVIDER_ACTION_REQUIRED'&&job.resumable&&job.next_action?.id==='resume_failed_video';const summary=$('#drawer .drawer-summary p');if(summary)summary.textContent=job.state==='PRODUCTION_REVIEW_REQUIRED'?'已有候选母片等待人工判断；预览后选择接受或重做。':job.state==='REJECTED_PENDING_RECONCILIATION'?'业务已终止；仅保留远端成本核对，不会恢复生产或重新提交。':recoveryReady?'模型账号阻断已解除；可继续生成剩余母片。':'模型账号需要人工处理；系统已暂停且不会自动重试。'}
const openVideoDrawerWithReconciliationDeleteGuard=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithReconciliationDeleteGuard(job);if(['PROVIDER_ACTION_REQUIRED','REJECTED_PENDING_RECONCILIATION'].includes(job?.state))$('#drawer-delete-video')?.remove()}
const openVideoDrawerWithProductionStages=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithProductionStages(job);const markup=productionStageCards(job);if(!markup)return;const progress=$('#drawer .job-progress');if(progress)progress.insertAdjacentHTML('afterend',markup)}
const doctorStatusLabel=status=>({healthy:'运行正常',repairable:'可安全修复',action_required:'需要人工确认',external_blocked:'外部条件阻塞'})[status]||status;
const doctorSeverityLabel=severity=>({error:'阻塞',warning:'需处理',info:'提示'})[severity]||severity;
const doctorCategoryLabel=category=>({production_graph:'生产图',worker:'执行器',execution_contract:'执行契约',state:'任务状态',provider:'模型服务',media:'本地文件',unknown:'未分类'})[category]||category;
function taskDoctorFindingMarkup(report){
  if(!report.findings?.length)return'<div class="task-doctor-empty"><i>✓</i><div><strong>未发现阻塞问题</strong><span>任务状态、执行节点和本地产物未发现可自动修复异常</span></div></div>';
  return `<div class="task-doctor-findings">${report.findings.map(item=>`<article class="${esc(item.severity)}"><header><span>${esc(doctorCategoryLabel(item.category))}</span><em>${esc(doctorSeverityLabel(item.severity))}</em></header><strong>${esc(item.title)}</strong><p>${esc(item.detail)}</p><small>${item.repair_kind==='automatic'?'可执行零费用自动修复':item.repair_kind==='external'?'需处理外部账号或核对原任务':'需人工确认，不会自动执行'}</small></article>`).join('')}</div>`;
}
function taskDoctorActionMarkup(title,items,tone='neutral'){
  if(!items?.length)return'';
  return `<section class="task-doctor-actions ${tone}"><h3>${esc(title)}</h3>${items.map(item=>`<article><div><strong>${esc(item.label)}</strong><span>${esc(item.detail)}</span></div><em>${item.paid?'可能付费':'不新增模型费用'}</em></article>`).join('')}</section>`;
}
function renderTaskDoctor(job,report,repairResult=null){
  const safeIds=(report.safe_actions||[]).map(item=>item.id);
  const actionLabels=Object.fromEntries([...(repairResult?.before?.safe_actions||[]),...(report.safe_actions||[])].map(item=>[item.id,item.label]));
  const history=report.audit_history||[];
  openModal('TASK DOCTOR','任务诊断与修复',`<div class="task-doctor-summary ${esc(report.status)}"><span>${esc(doctorStatusLabel(report.status))}</span><div><strong>${esc(report.headline)}</strong><small>${esc(job.title||report.title)} · ${esc(job.job_id||report.entity_id)}</small></div></div>${repairResult?`<div class="task-doctor-result"><strong>${repairResult.applied_actions?.length?'安全修复已完成并复检':'本次没有可执行的安全修复'}</strong><span>${repairResult.applied_actions?.length?`已执行 ${repairResult.applied_actions.map(item=>actionLabels[item.id]||item.id).join('、')}`:'任务现场未被修改'}</span></div>`:''}${taskDoctorFindingMarkup(report)}${taskDoctorActionMarkup('可自动执行',report.safe_actions,'safe')}${taskDoctorActionMarkup('需要人工确认',report.confirmation_actions,'confirm')}${taskDoctorActionMarkup('需要外部处理',report.external_actions,'external')}${history.length?`<details class="task-doctor-history"><summary>最近诊断记录（${history.length}）</summary>${history.map(item=>`<div><span>${esc(item.created_at)}</span><b>${item.status==='repaired'?'已修复':'仅诊断'}</b></div>`).join('')}</details>`:''}<div class="impact-callout"><strong>安全边界</strong><span>自动修复不会启动生产 worker、不会创建模型任务、不会提高预算、不会接受候选或删除产物。涉及这些动作时只给出建议，由你确认后再执行。</span></div><div class="modal-actions"><button class="mini-action" id="task-doctor-rediagnose">重新诊断</button>${safeIds.length?'<button class="primary-action" id="task-doctor-repair">执行安全修复</button>':'<button class="primary-action" onclick="document.querySelector(\'#modal-close\').click()">完成</button>'}</div>`);
  $('#task-doctor-rediagnose').onclick=()=>openTaskDoctor(job);
  const repair=$('#task-doctor-repair');if(repair)repair.onclick=()=>repairTaskDoctor(job);
}
async function openTaskDoctor(job){
  openModal('TASK DOCTOR','任务诊断与修复','<div class="task-doctor-loading"><i></i><strong>正在检查任务现场</strong><span>核对状态、执行节点、远端任务、本地文件和恢复条件</span></div>');
  try{const report=await api(`/api/videos/${encodeURIComponent(job.job_id)}/doctor`);renderTaskDoctor(job,report)}catch(error){toast(error.message,true);closeModal()}
}
async function repairTaskDoctor(job){
  const button=$('#task-doctor-repair');if(button){button.disabled=true;button.textContent='正在安全修复并复检…'}
  try{
    const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/doctor/repair`,{method:'POST',body:'{}'});
    await loadPage('videos');
    const fresh=state.videos?.jobs?.find(item=>item.job_id===job.job_id)||job;
    renderTaskDoctor(fresh,result.after,result);
  }catch(error){if(button){button.disabled=false;button.textContent='执行安全修复'}toast(error.message,true)}
}
const openVideoDrawerWithTaskDoctor=openVideoDrawer;
openVideoDrawer=function(job){openVideoDrawerWithTaskDoctor(job);if(!job)return;const footer=$('#drawer .drawer-footer');if(!footer||$('#drawer-task-doctor'))return;footer.insertAdjacentHTML('afterbegin',`<button class="mini-action task-doctor-trigger" id="drawer-task-doctor">诊断修复</button>`);$('#drawer-task-doctor').onclick=()=>openTaskDoctor(job)}
function openVideoDrawerAt(job,masterIndex){openVideoDrawer(job);const row=$(`[data-video-master="${CSS.escape(String(masterIndex))}"]`,$('#drawer'));if(!row)return;row.classList.add('focused');row.setAttribute('aria-current','true');row.scrollIntoView({block:'center'})}
function openPublishReadyDrawer(item){openPublishFlow(item)}
function openPublishFlow(item,selectedPlatform=''){
  if(!item)return;
  const done=new Set(item.published_platforms||[]);
  const pending=new Set(item.pending_platforms||[]);
  const douyin=(state.config?.publish_platforms||[]).find(p=>p.id==='douyin')||{configured:false,connected:false};
  const weixin=(state.config?.publish_platforms||[]).find(p=>p.id==='weixin_channels')||{entry_url:WEIXIN_ASSISTANT_URL};
  const weixinUrl=weixin.entry_url||WEIXIN_ASSISTANT_URL;
  openDrawer('CHANNEL PUBLISH',item.title,`<div class="drawer-summary"><span class="tag ${esc(item.state)}">待发布</span><p>抖音账号已绑定时可直接投稿；微信视频号继续采用下载成片后登记结果</p></div><section class="detail-section"><h3>待发布成片</h3><video class="video-preview" controls preload="metadata" src="/api/videos/${encodeURIComponent(item.job_id)}/file"></video>${detailRow('任务编号',item.job_id)}</section><section class="detail-section"><div class="section-line"><h3>发布渠道</h3><small>每个渠道独立处理，避免误发布</small></div><div class="publish-methods"><article data-publish-method="douyin" class="publish-method-card ${done.has('douyin')?'completed':pending.has('douyin')?'pending':''}"><header><span class="platform-logo douyin">抖</span><div><strong>抖音直连发布</strong><small>${done.has('douyin')?'已发布并核实':pending.has('douyin')?'已提交，等待平台审核':douyin.connected?`已绑定 ${esc(douyin.account?.open_id_masked||'抖音账号')}`:'尚未绑定账号'}</small></div></header>${done.has('douyin')?'<p class="publish-complete">该渠道已有可核实作品记录</p>':pending.has('douyin')?'<p class="publish-pending">投稿已送达抖音并进入平台审核，请勿重复提交</p>':douyin.connected?`<label>标题与话题<textarea id="douyin-publish-text" rows="3" maxlength="1000">${esc(item.title)}</textarea></label><label>封面时间点（秒）<input id="douyin-cover-tsp" type="number" min="0" step="0.1" value="1"></label><label class="check-line publish-confirm"><input id="douyin-publish-confirm" type="checkbox"> 我已确认目标账号、标题和成片，将提交抖音审核</label><button class="primary-action" id="publish-douyin-direct">确认并推送抖音</button>`:`<div class="channel-empty"><p>${douyin.configured?'应用已配置，请先扫码绑定账号':'请先在配置中心填写 Client Key、Secret 和 HTTPS 回调地址'}</p><button class="primary-action" id="go-bind-douyin">${douyin.configured?'绑定抖音账号':'去配置'}</button></div>`}</article><article data-publish-method="weixin_channels" class="publish-method-card ${done.has('weixin_channels')?'completed':''}"><header><span class="platform-logo weixin_channels">视</span><div><strong>微信视频号辅助发布</strong><small>${done.has('weixin_channels')?'已登记作品':'下载成片后在视频号助手发布'}</small></div></header>${done.has('weixin_channels')?'<p class="publish-complete">该渠道已有可核实作品记录</p>':`<a class="mini-action publish-download" href="/api/videos/${encodeURIComponent(item.job_id)}/file?download=1">下载成片</a><label>发布后的作品 ID<input id="weixin-item-id" placeholder="必填"></label><label>作品链接<input id="weixin-item-url" placeholder="选填"></label><button class="mini-action" id="save-weixin-result">保存视频号发布结果</button>`}</article></div></section>`);
  const selectedCard=selectedPlatform?$('[data-publish-method="'+CSS.escape(selectedPlatform)+'"]',$('#drawer')):null;if(selectedCard){selectedCard.classList.add('selected');selectedCard.setAttribute('aria-current','true');selectedCard.scrollIntoView({block:'center'})}
  const weixinDownload=$('.publish-method-card .publish-download');
  if(weixinDownload){
    weixinDownload.insertAdjacentHTML('beforebegin',`<p class="publish-assistant-note">登录状态由微信官方页面保管，管理台不读取 Cookie</p><div class="publish-assistant-actions"><a class="primary-action" href="${esc(weixinUrl)}" target="_blank" rel="noreferrer">打开视频号助手</a></div>`);
  }
  const bind=$('#go-bind-douyin');if(bind)bind.onclick=async()=>{closeDrawer();if(!await navigate('config'))return;const panel=$('[data-platform-fields="douyin"]');if(!panel)return toast('抖音配置区加载失败，请刷新后重试',true);panel.hidden=false;const trigger=$('[data-platform-expand="douyin"]');if(trigger)trigger.textContent='收起';panel.scrollIntoView({behavior:'smooth',block:'center'})};
  const direct=$('#publish-douyin-direct');if(direct)direct.onclick=async()=>{if(!$('#douyin-publish-confirm').checked)return toast('请勾选发布确认',true);direct.disabled=true;direct.textContent='正在上传并提交审核…';try{const result=await api(`/api/videos/${encodeURIComponent(item.job_id)}/publish/douyin`,{method:'POST',body:JSON.stringify({confirmed:true,text:$('#douyin-publish-text').value.trim(),cover_tsp:$('#douyin-cover-tsp').value})});closeDrawer();toast(result.status==='submitted_for_review'?'已提交抖音审核':'抖音发布请求已完成');loadPage('publishing')}catch(e){direct.disabled=false;direct.textContent='确认并推送抖音';toast(e.message,true)}};
  const saveWeixin=$('#save-weixin-result');if(saveWeixin)saveWeixin.onclick=async()=>{const itemId=$('#weixin-item-id').value.trim();if(!itemId)return toast('请填写视频号作品 ID',true);try{await api('/api/publications/batch',{method:'POST',body:JSON.stringify({job_id:item.job_id,channels:[{platform:'weixin_channels',item_id:itemId,item_url:$('#weixin-item-url').value.trim(),title:item.title}]})});closeDrawer();toast('视频号发布结果已保存');loadPage('publishing')}catch(e){toast(e.message,true)}};
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDrawer();closeModal()}});
async function uploadMediaSource(file,title,onUploaded){const csrf=await ensureServerCsrf();return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',`/api/sources/media?filename=${encodeURIComponent(file.name)}&title=${encodeURIComponent(title)}`);xhr.setRequestHeader('Content-Type',file.type||'application/octet-stream');if(csrf)xhr.setRequestHeader('X-CSRF-Token',csrf);xhr.upload.onload=()=>onUploaded?.();xhr.onload=()=>{let payload={};try{payload=JSON.parse(xhr.responseText||'{}')}catch(e){}if(xhr.status>=200&&xhr.status<300)resolve(payload);else reject(new Error(payload.error||`上传失败 (${xhr.status})`))};xhr.onerror=()=>reject(new Error('上传失败，请检查本地服务'));xhr.send(file)})}
async function ensureAsrReadyForMedia(){const config=await api('/api/config');const group=(config.required_groups||[]).find(item=>item.id==='asr');if(group?.ready)return true;openModal('ASR REQUIRED','需要先配置语音识别',`<div class="impact-callout"><strong>本地音视频转写需要先配置火山语音识别</strong><span>请在配置中心填写新版“录音文件识别 App Key”。配置完成后即可重新上传，当前文件尚未发送</span></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="go-asr-config">去配置中心</button></div>`);$('#go-asr-config').onclick=()=>{closeModal();navigate('config')};return false}
$('#new-source').onclick=()=>{openModal('SCRIPT INPUT','新增内容来源',`<div class="form-stack"><label>内容项目名称<input id="source-title" placeholder="例如：行业访谈摘录"></label><label>来源类型<select id="source-type"><option value="manual">人工文本</option><option value="url">公开网页</option><option value="video">本地视频</option></select></label><label id="source-content-field">内容或链接<textarea id="source-content" rows="7" placeholder="粘贴文本或公开网页链接"></textarea></label><label id="source-file-field" hidden>选择文件<input id="source-file" type="file"><small id="source-file-hint">支持本地视频文件，最大 512 MB</small></label><div class="source-upload-status" id="source-upload-status" hidden><i></i><span>准备上传</span></div></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="source-submit">归档并查看下一步</button></div>`);const type=$('#source-type'),contentField=$('#source-content-field'),fileField=$('#source-file-field'),file=$('#source-file'),status=$('#source-upload-status'),submit=$('#source-submit');const syncType=()=>{const media=type.value==='video';contentField.hidden=media;fileField.hidden=!media;file.accept='.mp4,.mov,.webm,.mkv,.avi';submit.textContent=media?'上传并解析':'归档并查看下一步'};type.onchange=syncType;syncType();submit.onclick=async()=>{const media=type.value==='video';if(media&&!file.files[0]){toast('请选择需要上传的视频文件',true);return}if(media&&!await ensureAsrReadyForMedia())return;submit.disabled=true;status.hidden=!media;if(media)status.querySelector('span').textContent='正在上传…';try{const result=media?await uploadMediaSource(file.files[0],$('#source-title').value,()=>{status.querySelector('span').textContent='正在转写并解析画面…'}):await api('/api/sources',{method:'POST',body:JSON.stringify({source_type:type.value,title:$('#source-title').value,content:$('#source-content').value})});closeModal();state.studioProjectId=result.source_id;state.studioStage='source';toast(media?'视频已重新转写、解析并独立归档':'来源已归档，可继续生成简报');if(!await loadPage('scripts'))return;openSourceDrawer(state.scripts.sources.find(x=>x.source_id===result.source_id))}catch(e){submit.disabled=false;if(media)status.hidden=true;toast(e.message,true)}}};
const projectNew=$('#project-new-source');if(projectNew)projectNew.onclick=()=>{$('#new-source').click()};
function confirmGenerateBrief(id,riskConfirmed=false){openModal('AI GENERATION','生成选题简报',`<p class="confirm-copy">系统将读取该来源正文并调用大模型生成选题分析与 1–3 个高质量创意角度。此操作可能产生少量模型费用</p>${riskConfirmed?'<div class="error-callout"><strong>低质量输入风险</strong><p>来源信息不足，生成内容可能缺少依据。仅在你已核对并接受风险时继续</p></div>':''}<div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-generate-brief">${riskConfirmed?'确认风险并生成':'确认并生成'}</button></div>`);$('#confirm-generate-brief').onclick=async()=>{const button=$('#confirm-generate-brief');button.disabled=true;button.textContent='正在生成…';try{const result=await api(`/api/sources/${encodeURIComponent(id)}/generate-brief`,{method:'POST',body:JSON.stringify({confirmed:true,risk_confirmed:riskConfirmed})});closeModal();state.scriptStage='briefs';if(!await loadPage('scripts'))return;toast(result.cached?'已打开现有选题简报':'选题简报已生成');openBriefDrawer(state.scripts.briefs.find(x=>x.brief_id===result.brief_id))}catch(e){if(!riskConfirmed&&e.message.includes('内容不足')){closeModal();confirmGenerateBrief(id,true);return}button.disabled=false;button.textContent='确认并生成';toast(e.message,true)}}}
const generateBriefFromSource=id=>confirmGenerateBrief(id);
function confirmGenerateScripts(id,angleIndex){
  openModal('AI GENERATION','确认角度并生成剧本',`<p class="confirm-copy">系统将锁定第 ${angleIndex+1} 个创意角度，并调用大模型生成候选剧本。以下均为可选建议，留空由 AI 决定，不会作为成片硬性拦截条件</p><div class="script-guidance-grid"><label><span>建议总时长 <small>可选</small></span><div class="input-suffix"><input id="script-suggest-duration" type="number" min="1" step="1" placeholder="AI 决定"><i>秒</i></div><em>仅影响台词密度与节奏，不限制最终总时长</em></label><label><span>建议分镜数 <small>可选</small></span><div class="input-suffix"><input id="script-suggest-shots" type="number" min="1" step="1" placeholder="AI 决定"><i>个</i></div><em>仅提供导演拆解方向，最终按内容决定</em></label><label><span>建议角色数 <small>可选</small></span><div class="input-suffix"><input id="script-suggest-characters" type="number" min="1" step="1" placeholder="AI 决定"><i>人</i></div><em>不遗漏关键人物，也不会为凑人数添加角色</em></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="confirm-generate-scripts">确认并生成候选剧本</button></div>`);
  $('#modal').classList.add('script-guidance-open');
  $('#confirm-generate-scripts').onclick=async()=>{
    const button=$('#confirm-generate-scripts');
    const optionalInteger=id=>{const value=$(id).value.trim();return value===''?null:Number(value)};
    const payload={confirmed:true,angle_index:angleIndex};
    const guidance={suggested_duration_sec:optionalInteger('#script-suggest-duration'),suggested_shot_count:optionalInteger('#script-suggest-shots'),suggested_character_count:optionalInteger('#script-suggest-characters')};
    const rules={suggested_duration_sec:'建议总时长必须是正整数',suggested_shot_count:'建议分镜数必须是正整数',suggested_character_count:'建议角色数必须是正整数'};
    for(const [key,value] of Object.entries(guidance)){const message=rules[key];if(value!==null&&(!Number.isInteger(value)||value<=0))return toast(message,true)}
    Object.entries(guidance).forEach(([key,value])=>{if(value!==null)payload[key]=value});
    button.disabled=true;button.textContent='正在生成…';
    try{const result=await api(`/api/briefs/${encodeURIComponent(id)}/generate-scripts`,{method:'POST',body:JSON.stringify(payload)});closeModal();state.scriptStage='drafts';if(!await loadPage('scripts'))return;toast(result.cached?'已加载相同建议下的候选剧本':`已生成 ${result.draft_count} 版候选剧本`)}catch(e){button.disabled=false;button.textContent='确认并生成三版剧本';toast(e.message,true)}
  };
}
const confirmBriefAndGenerateScripts=(id,index)=>confirmGenerateScripts(id,index);
function confirmDeleteSource(id){openModal('DELETE SOURCE','删除内容来源',`<p class="confirm-copy">删除后不可恢复；该来源衍生但尚未确认的简报和剧本候选也会一并清理。已经进入视频流水线的来源会被系统保护</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-source">确认删除</button></div>`);$('#confirm-delete-source').onclick=async()=>{try{const result=await api(`/api/sources/${encodeURIComponent(id)}`,{method:'DELETE'});closeModal();toast(result.deleted_briefs?`来源及 ${result.deleted_briefs} 份衍生简报已删除`:'来源已删除');loadPage('scripts')}catch(e){toast(e.message,true)}}}
const deleteSource=id=>confirmDeleteSource(id);
function confirmDeleteBrief(id){openModal('DELETE BRIEF','删除选题简报',`<p class="confirm-copy">删除后不可恢复；该简报下尚未提交的视频剧本候选也会一并删除，原始内容来源会保留</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-brief">确认删除</button></div>`);$('#confirm-delete-brief').onclick=async()=>{try{await api(`/api/briefs/${encodeURIComponent(id)}`,{method:'DELETE'});closeModal();toast('选题简报及候选剧本已删除');loadPage('scripts')}catch(e){toast(e.message,true)}}}
const deleteBrief=id=>confirmDeleteBrief(id);
async function approveScript(id){try{await api(`/api/scripts/${encodeURIComponent(id)}/approve`,{method:'POST',body:'{}'});toast('剧本已确认，视频任务已就绪');loadPage('scripts')}catch(e){toast(e.message,true)}}
function confirmDeleteScript(id){openModal('DELETE SCRIPT','删除待审剧本',`<p class="confirm-copy">删除后不可恢复。仅删除当前候选版本，不影响来源和选题简报</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-script">确认删除</button></div>`);$('#confirm-delete-script').onclick=async()=>{try{await api(`/api/scripts/${encodeURIComponent(id)}`,{method:'DELETE'});closeModal();toast('剧本已删除');loadPage('scripts')}catch(e){toast(e.message,true)}}}
const deleteScript=id=>confirmDeleteScript(id);
function confirmDeleteVideo(id){openModal('DELETE VIDEO','删除视频记录',`<p class="confirm-copy">将删除任务记录及其分镜数据，不会删除已生成的本地成品文件。生成中或已进入发布流水线的记录不可删除</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-video">确认删除</button></div>`);$('#confirm-delete-video').onclick=async()=>{try{const result=await api(`/api/videos/${encodeURIComponent(id)}`,{method:'DELETE'});closeModal();toast(result.dispatched_jobs?.length?`视频记录已删除，已自动开始 ${result.dispatched_jobs.length} 个排队任务`:'视频记录已删除');loadPage('videos')}catch(e){toast(e.message,true)}}}
const deleteVideo=id=>confirmDeleteVideo(id);
const reviewDecisionLabel=value=>({approved:'验收通过',local_recut:'退回本地修剪',paid_regen:'需要付费重生',rejected:'不采用'})[value]||value;
function openVideoReview(job,decision){closeDrawer();const items=[['identity','身份一致'],['lip_sync','口型与语音'],['subtitle_sync','字幕同步'],['visual_integrity','手部与画面'],['clean_frame','无乱码水印'],['editing_rhythm','剪辑节奏']];openModal('HUMAN REVIEW',decision==='approved'?'逐项确认成片质量':'退回本地修剪',`<div class="form-stack">${decision==='approved'?`<div class="review-check-grid">${items.map(x=>`<label><input type="checkbox" data-review-check="${x[0]}"><span>${x[1]}</span></label>`).join('')}</div><button type="button" class="mini-action" id="review-check-all">一键勾选全部六项</button><div class="impact-callout"><strong>发布门禁</strong><span>六项全部确认后，成片才会解锁发布</span></div>`:''}<label>人工处理耗时（分钟）<input id="review-minutes" type="number" min="0" step="0.5" value="0"></label><label>验收备注<textarea id="review-notes" rows="4" placeholder="${decision==='approved'?'记录通过依据（选填）':'必须填写需要调整的问题'}"></textarea></label>${decision==='approved'?'<label class="check-line"><input id="review-first-pass" type="checkbox"> 本次为首轮直接通过</label>':''}</div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="review-submit">保存验收结论</button></div>`);const checkAll=$('#review-check-all');if(checkAll)checkAll.onclick=()=>{$$('[data-review-check]').forEach(item=>item.checked=true);checkAll.textContent='六项已全部勾选'};$('#review-submit').onclick=async()=>{const checks={};$$('[data-review-check]').forEach(c=>checks[c.dataset.reviewCheck]=c.checked);if(decision==='approved'&&Object.values(checks).some(v=>!v))return toast('请完成全部六项验收',true);if(decision!=='approved'&&!$('#review-notes').value.trim())return toast('请填写退回原因',true);try{await api(`/api/videos/${encodeURIComponent(job.job_id)}/review`,{method:'POST',body:JSON.stringify({decision,checks,manual_minutes:Number($('#review-minutes').value||0),notes:$('#review-notes').value,first_pass:$('#review-first-pass')?.checked})});closeModal();toast(decision==='approved'?'成片验收通过，可提交发布':'已记录退回原因');if(!await loadPage('videos'))return;openVideoDrawer(state.videos.jobs.find(x=>x.job_id===job.job_id))}catch(e){toast(e.message,true)}}}
async function submitForPublish(id){try{await api(`/api/videos/${encodeURIComponent(id)}/submit-publish`,{method:'POST',body:'{}'});closeDrawer();toast('成品已提交到发布流水线');loadPage('videos')}catch(e){toast(e.message,true)}}
function characterTypeLabel(value){return value==='custom'?'自定义角色':value==='virtual_person'?'虚拟人像':'真人资产'}
function characterStatusLabel(value){return value==='Active'?'已启用':'已停用'}
function characterBackgroundSourceLabel(value){return value==='volc_asset_group'?'火山虚拟人像库自动同步':value==='manual'?'手工维护':'尚未设置'}
function characterQuery(){
  const keyword=$('#character-search')?.value.trim()||'';
  const identityType=$('#character-type-filter')?.value||'';
  const status=$('#character-status-filter')?.value||'';
  return new URLSearchParams({keyword,identity_type:identityType,status});
}
async function fetchCharacters(){return api(`/api/characters?${characterQuery()}`)}
async function loadCharacters(){
  const loadToken=++state.characterLoadSequence;
  const stale=()=>loadToken!==state.characterLoadSequence||state.page!=='characters';
  try{
    const characters=await fetchCharacters();
    if(stale())return false;
    state.characters=characters;
    renderCharacters();
    return true;
  }catch(e){
    if(!stale())toast(e.message,true);
    return false;
  }
}
function renderCharacters(){
  const chars=state.characters?.characters||[];
  const active=chars.filter(c=>c.status==='Active').length;
  const custom=chars.filter(c=>c.identity_type==='custom').length;
  $('#character-metrics').innerHTML=[['当前结果',chars.length,'支持名称、ID 与 assetId 查询'],['已启用',active,'可在视频制作中直接选择'],['自定义角色',custom,'多图定妆 · 描述驱动']].map(item=>`<article><small>${item[0]}</small><strong>${item[1]}</strong><span>${item[2]}</span></article>`).join('');
  $('#character-grid').innerHTML=chars.length?chars.map(c=>`<article class="character-card ${c.status==='Inactive'?'inactive':''}" data-character-card="${esc(c.character_id)}"><button class="character-preview" data-character-preview="${esc(c.character_id)}" aria-label="预览 ${esc(c.display_name)}">${c.preview_url?`<img src="${esc(c.preview_url)}" alt="${esc(c.display_name)} 预览图" loading="lazy">`:`<span>${esc((c.display_name||'?').slice(0,1))}</span><small>暂无资产缩略图</small>`}${c.identity_type==='custom'?`<i class="image-count">${c.image_count||0} 张</i>`:''}</button><div class="character-card-body"><div class="character-card-title"><div><strong>${esc(c.display_name)}</strong><small>${esc(c.character_id)}</small></div><span class="tag ${c.status==='Active'?'active':''}">${characterStatusLabel(c.status)}</span></div><div class="character-card-meta"><span>${characterTypeLabel(c.identity_type)}</span><code>${c.identity_type==='custom'?'本地多图':esc(c.asset_id)}</code></div><p class="character-background">${c.background_description?esc(c.background_description):'尚未设置角色背景描述'}</p><small class="character-usage">${c.usage_count?`已用于 ${c.usage_count} 个视频任务，保留历史引用`:'尚未用于视频任务，可安全删除'}</small><div class="character-card-actions"><button class="mini-action" data-character-preview="${esc(c.character_id)}">预览</button>${!c.preview_url&&c.identity_type!=='custom'?`<button class="mini-action primary" data-character-refresh="${esc(c.character_id)}">同步资产</button>`:''}<button class="mini-action" data-character-toggle="${esc(c.character_id)}" data-status="${c.status==='Active'?'Inactive':'Active'}">${c.status==='Active'?'停用':'启用'}</button><button class="mini-action danger" data-character-delete="${esc(c.character_id)}" ${c.deletable?'':'disabled title="已被视频任务引用，不能删除"'}>删除</button></div></div></article>`).join(''):empty('没有匹配的角色资产；可调整筛选或新增角色');
  $$('[data-character-preview]').forEach(button=>button.onclick=()=>openCharacterDrawer(chars.find(c=>c.character_id===button.dataset.characterPreview)));
  $$('[data-character-toggle]').forEach(button=>button.onclick=()=>toggleCharacter(button.dataset.characterToggle,button.dataset.status));
  $$('[data-character-refresh]').forEach(button=>button.onclick=()=>refreshCharacter(button.dataset.characterRefresh));
  $$('[data-character-delete]').forEach(button=>button.onclick=()=>deleteCharacter(button.dataset.characterDelete));
}
function openCharacterDrawer(character){
  if(!character)return;
  const usage=character.usages||[];
  const voice=character.voice_binding;
  const voiceSection=`<section class="detail-section"><h3>固定声线</h3>${voice?`<div class="voice-binding-card"><div><strong>${esc(voice.voice_name||voice.voice_type)}</strong><small>${esc(voice.voice_type)} · ${voice.source==='manual'?'手工选择':'按角色设定自动匹配'}</small></div>${voice.preview_url?`<audio controls preload="none" src="${esc(voice.preview_url)}"></audio>`:''}</div>`:'<p class="empty-inline">尚未绑定固定声线；生产前会停止，不会让 Seedance 随机选声</p>'}<div class="character-card-actions"><button class="mini-action primary" id="character-voice-auto">按角色设定自动匹配</button><button class="mini-action" id="character-voice-manual">从火山声线库选择</button></div></section>`;
  const gallery=(character.images||[]).length?`<div class="character-gallery">${character.images.map((image,index)=>`<button data-gallery-image="${esc(image.url)}"><img src="${esc(image.url)}" alt="${esc(character.display_name)} 参考图 ${index+1}"><span>${index===0?'主参考图':`参考图 ${index+1}`}</span></button>`).join('')}</div>`:`<div class="character-detail-preview">${character.preview_url?`<img src="${esc(character.preview_url)}" alt="${esc(character.display_name)} 预览图">`:`<div><span>${esc((character.display_name||'?').slice(0,1))}</span><p>本地尚未同步火山缩略图<br>可点击下方按钮重新获取</p><button class="mini-action primary" id="character-detail-refresh">同步资产</button></div>`}</div>`;
  const assetDetails=character.identity_type==='custom'?`${detailRow('角色 ID',character.character_id)}${detailRow('资产类型','自定义角色')}${detailRow('定妆图片',`${character.image_count||0} 张`)}${detailRow('Seedance 方式','首条母片描述驱动')}${detailRow('状态',characterStatusLabel(character.status))}${detailRow('绑定方式','本地图片上传')}<div class="impact-callout"><strong>隐私合规说明</strong><span>本地图片用于定妆预览和角色描述维护；首条母片不会把本地写实人像作为普通参考图提交，后续镜头沿用已生成母片</span></div>`:`${detailRow('角色 ID',character.character_id)}${detailRow('资产类型',characterTypeLabel(character.identity_type))}${detailRow('火山 assetId',character.asset_id)}${detailRow('状态',characterStatusLabel(character.status))}${detailRow('绑定方式',character.source==='api'?'火山 API 自动发现':'手动绑定')}`;
  openDrawer('CHARACTER ASSET',character.display_name,`${gallery}<section class="detail-section"><h3>角色背景</h3><p class="character-background-detail">${character.background_description?esc(character.background_description):'尚未设置人物职业、性格、表达风格与视觉特征。'}</p><div class="background-source"><span>${esc(characterBackgroundSourceLabel(character.background_description_source))}</span><button class="text-action" id="character-background-edit">编辑</button></div></section>${voiceSection}<section class="detail-section"><h3>资产信息</h3>${assetDetails}</section><section class="detail-section"><h3>使用情况</h3>${usage.length?usage.map(item=>`<button class="usage-row" data-character-job="${esc(item.job_id)}"><span><strong>${esc(item.title)}</strong><small>${esc(item.job_id)}</small></span><i class="tag">${esc(videoStateLabel(item.state))}</i></button>`).join(''):'<p class="empty-inline">尚未被视频任务引用</p>'}</section><div class="drawer-footer"><button class="mini-action" id="character-detail-toggle">${character.status==='Active'?'停用角色':'启用角色'}</button><button class="primary-action danger-fill" id="character-detail-delete" ${character.deletable?'':'disabled'}>${character.deletable?'删除角色':'已被引用，不能删除'}</button></div>`);
  $('#character-detail-toggle').onclick=()=>{closeDrawer();toggleCharacter(character.character_id,character.status==='Active'?'Inactive':'Active')};
  $('#character-detail-delete').onclick=()=>{closeDrawer();deleteCharacter(character.character_id)};
  if($('#character-detail-refresh'))$('#character-detail-refresh').onclick=()=>{closeDrawer();refreshCharacter(character.character_id)};
  $$('[data-gallery-image]').forEach(button=>button.onclick=()=>window.open(button.dataset.galleryImage,'_blank','noopener'));
  $('#character-background-edit').onclick=()=>{closeDrawer();editCharacterBackground(character)};
  $('#character-voice-auto').onclick=()=>autoMatchCharacterVoice(character);
  $('#character-voice-manual').onclick=()=>chooseCharacterVoice(character);
  $$('[data-character-job]').forEach(button=>button.onclick=async()=>{const jobId=button.dataset.characterJob;closeDrawer();state.videoFilter='all';if(!await navigate('videos'))return;const job=state.videos?.jobs.find(item=>item.job_id===jobId);if(job)return openVideoDrawer(job);toast('对应视频任务已不存在',true)});
}
function openCharacterCreate(){
  openModal('CHARACTER ASSET','新增角色资产',`<div class="asset-type-picker"><label><input type="radio" name="identity-type" value="real_person" checked><span><b>真人资产</b><small>来自火山真人资产库，需要完成真人授权</small></span></label><label><input type="radio" name="identity-type" value="virtual_person"><span><b>虚拟人像</b><small>来自火山虚拟人像库，无需真人授权</small></span></label><label><input type="radio" name="identity-type" value="custom"><span><b>自定义角色</b><small>不依赖火山资产，上传多张图片保持形象一致</small></span></label></div><div class="form-stack"><label>角色 ID<input id="char-id" placeholder="例如 custom_klein"><small>创建后用于剧本与视频任务引用，建议使用英文与下划线</small></label><label>显示名称<input id="char-name" placeholder="例如 克莱恩"></label><label id="char-gender-field" hidden>人物性别<select id="char-gender"><option value="">请选择</option><option value="male">男性</option><option value="female">女性</option></select><small>用于保证自定义角色的形象与声音设定正确</small></label><label id="char-asset-field">火山 assetId<input id="char-asset" placeholder="asset-..."><small>从火山对应资产库的资产详情中复制 assetId</small></label><label id="char-images-field" hidden>人物参考图片<input id="char-images" type="file" accept="image/jpeg,image/png,image/webp" multiple><small>支持 JPG、PNG、WebP，1–24 张，每张不超过 20 MB；首张作为主参考图，生成时自动选取最多 9 张</small></label><label>角色背景描述<textarea id="char-background" rows="5" placeholder="人物身份、时代背景、外貌、服装、性格、表达方式与禁忌"></textarea><small id="char-background-hint">真人角色建议填写职业、性格、表达风格和专业领域</small></label><div class="upload-progress" id="char-upload-progress" hidden></div></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="char-submit">确认新增</button></div>`);
  const syncType=()=>{const type=document.querySelector('input[name="identity-type"]:checked').value;const custom=type==='custom';$('#char-asset-field').hidden=custom;$('#char-images-field').hidden=!custom;$('#char-gender-field').hidden=!custom;$('#char-background-hint').textContent=custom?'必填；系统会把人物背景与全部参考图用于形象生成':type==='virtual_person'?'可留空；系统将通过 assetId 从火山虚拟人像资产组自动获取':'真人角色建议填写职业、性格、表达风格和专业领域'};
  $$('input[name="identity-type"]').forEach(radio=>radio.onchange=syncType);
  $('#char-submit').onclick=async()=>{const button=$('#char-submit');button.disabled=true;try{const identity_type=document.querySelector('input[name="identity-type"]:checked').value;const character_id=$('#char-id').value;const files=[...($('#char-images')?.files||[])];if(identity_type==='custom'&&!$('#char-gender').value)throw new Error('请选择自定义角色的人物性别');if(identity_type==='custom'&&!files.length)throw new Error('自定义角色至少需要上传一张人物图片');if(files.length>24)throw new Error('每个自定义角色最多上传 24 张图片');await api('/api/characters',{method:'POST',body:JSON.stringify({character_id,display_name:$('#char-name').value,asset_id:$('#char-asset').value,identity_type,gender:$('#char-gender').value,background_description:$('#char-background').value})});if(identity_type==='custom'){for(let index=0;index<files.length;index++){const progress=$('#char-upload-progress');progress.hidden=false;progress.textContent=`正在上传参考图 ${index+1} / ${files.length}`;await uploadCharacterImage(character_id,files[index])}}closeModal();toast(`${characterTypeLabel(identity_type)}已新增`);if(!await loadCharacters())return}catch(err){button.disabled=false;toast(err.message,true)}};
}
async function uploadCharacterImage(characterId,file){const csrf=await ensureServerCsrf();return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',`/api/characters/${encodeURIComponent(characterId)}/images?filename=${encodeURIComponent(file.name)}`);if(csrf)xhr.setRequestHeader('X-CSRF-Token',csrf);xhr.onload=()=>{let data={};try{data=JSON.parse(xhr.responseText||'{}')}catch{}if(xhr.status>=200&&xhr.status<300)resolve(data);else reject(new Error(data.error||'图片上传失败'))};xhr.onerror=()=>reject(new Error('图片上传失败'));xhr.send(file)})}
function editCharacterBackground(character){
  openModal('CHARACTER BACKGROUND','编辑角色背景',`<div class="form-stack"><label>角色背景描述<textarea id="edit-character-background" rows="7" placeholder="补充职业、性格、表达风格和专业领域">${esc(character.background_description||'')}</textarea><small>保存后标记为手工维护，后续同步火山资产时不会覆盖</small></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="save-character-background">保存背景</button></div>`);
  $('#save-character-background').onclick=async()=>{const button=$('#save-character-background');button.disabled=true;try{await api(`/api/characters/${encodeURIComponent(character.character_id)}/background`,{method:'POST',body:JSON.stringify({background_description:$('#edit-character-background').value})});closeModal();toast('角色背景已更新');if(!await loadCharacters())return;openCharacterDrawer((state.characters?.characters||[]).find(c=>c.character_id===character.character_id))}catch(e){button.disabled=false;toast(e.message,true)}};
}
async function toggleCharacter(id,status){try{await api(`/api/characters/${encodeURIComponent(id)}/status`,{method:'POST',body:JSON.stringify({status})});toast(status==='Active'?'角色已启用':'角色已停用');if(!await loadCharacters())return}catch(e){toast(e.message,true)}}
async function refreshCharacter(id){try{toast('正在从火山同步角色资产');const result=await api(`/api/characters/${encodeURIComponent(id)}/refresh`,{method:'POST',body:'{}'});if(!result.preview_url)throw new Error(result.preview_sync==='unavailable'?'火山资产 API 尚未配置':'火山资产未返回可用缩略图');toast(result.background_description_source==='volc_asset_group'?'缩略图与角色背景已同步':'角色资产已同步');if(!await loadCharacters())return}catch(e){toast(e.message,true)}}
function deleteCharacter(id){
  const character=(state.characters?.characters||[]).find(c=>c.character_id===id);
  if(!character)return;
  openModal('DELETE CHARACTER','删除角色资产',`<p class="confirm-copy">将从本地角色资产库删除“${esc(character.display_name)}”${character.identity_type==='custom'?'及其全部本地参考图片':'的绑定记录，不会删除火山账号中的原始资产'}。已被视频任务引用的角色不可删除，请改为停用</p><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action danger-fill" id="confirm-delete-character">确认删除</button></div>`);
  $('#confirm-delete-character').onclick=async()=>{try{await api(`/api/characters/${encodeURIComponent(id)}`,{method:'DELETE'});closeModal();toast('角色资产已删除');if(!await loadCharacters())return}catch(e){toast(e.message,true)}};
}
$('#character-add').onclick=openCharacterCreate;
let characterSearchTimer;
$('#character-search').addEventListener('input',()=>{clearTimeout(characterSearchTimer);characterSearchTimer=setTimeout(loadCharacters,180)});
$('#character-type-filter').addEventListener('change',loadCharacters);
$('#character-status-filter').addEventListener('change',loadCharacters);
function confirmProduction(job){const characterPlan=characterMappingSummary(job);const p=job.preflight||{};const settings=job.production_settings||{};const overBudget=p.budget_ok===false;const additional=Number.isFinite(Number(p.estimated_cost_cents))?money(p.estimated_cost_cents):'待运行时核算';const current=money(p.current_cost_cents??job.cost_cents??0);const projectedCents=Number(p.projected_total_cost_cents||p.estimated_cost_cents||0);const projected=Number.isFinite(projectedCents)?money(projectedCents):additional;const budget=Number.isFinite(Number(p.budget_cents))?money(p.budget_cents):'未设置';const budgetOverride=overBudget?`<label>本任务新预算上限（元）<input id="approved-budget" type="number" min="${Math.ceil(projectedCents/100)}" step="1" value="${Math.ceil(projectedCents/100)}"><small>只提高当前任务预算；运行时每次付费调用仍会校验新上限</small></label>`:'';openModal('PRODUCTION PREFLIGHT','生产预检与成本确认',`<div class="preflight-grid">${detailRow('任务',job.job_id)}${detailRow('生产档位',job.production_profile||'standard')}${detailRow('导演方案',directorPlanLabel(job.director_plan))}${detailRow('人物形象方案',characterPlan)}${detailRow('人物锚点模型',anchorGeneratorLabel(settings.character_anchor_generator))}${detailRow('场景锚点模型',anchorGeneratorLabel(settings.scene_anchor_generator))}${detailRow('预计付费调用',`${p.estimated_paid_calls??job.remaining_master_count??job.master_count??0} 次（含 ${p.paid_identity_anchor_count||0} 次 AI 定妆、${p.paid_scene_anchor_count||0} 次场景锚点、${p.estimated_tts_calls||0} 次固定声线）`)}${detailRow('固定声线成本',money(p.estimated_tts_cost_cents||0))}${detailRow('已发生成本',current)}${detailRow('预计追加成本',additional)}${detailRow('预计累计成本',`${projected} / 预算上限 ${budget}`)}</div>${fixedVoiceCards(job)}${characterMakeupCards(job)}${customCharacterSeedanceNotice(job.characters)}<div class="review-callout ${overBudget?'error-callout':''}"><strong>${overBudget?'需要确认新的任务预算':'成本与重试保护'}</strong><p>${overBudget?'无需返回设置页；确认覆盖本任务预算后即可继续':'系统先锁定每个角色的固定声线与形象锚点，再进入剧情母片；每次付费调用前均校验预算'}</p></div>${budgetOverride}<div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="produce-confirm">${overBudget?'确认新预算并开始生成':'确认成本并开始生成'}</button></div>`);$('#produce-confirm').onclick=async()=>{try{const approved_budget_cents=overBudget?Math.round(Number($('#approved-budget').value)*100):undefined;if(overBudget&&approved_budget_cents<projectedCents)return toast('新预算不能低于预计累计成本',true);await api(`/api/videos/${encodeURIComponent(job.job_id)}/produce`,{method:'POST',body:JSON.stringify({confirmed:true,approved_budget_cents})});closeModal();toast('视频任务已启动：先锁定声线与锚点，再生成剧情母片');loadPage('videos')}catch(e){toast(e.message,true)}}}

const confirmProductionWithSceneAnchors=confirmProduction;
function masterDependencyPlanLabel(job){const graph=job?.preflight?.master_dependency_graph||job?.master_dependency_graph;const waves=graph?.waves||[];if(!waves.length)return'未识别连续性依赖';return `${waves.length} 个并发波次 · ${waves.map((wave,index)=>`第 ${index+1} 波 #${wave.join('、#')}`).join('；')}`}
confirmProduction=function(job){confirmProductionWithSceneAnchors(job);const modal=$('#modal-body');if(!modal)return;modal.dataset.paidSceneAnchorCount=job?.preflight?.paid_scene_anchor_count||0;const mode=job?.preflight?.voice_plan?.mode||job?.production_settings?.audio_mode||'fixed_tts';const fixed=mode==='fixed_tts';const grid=$('.preflight-grid',modal);if(grid){grid.insertAdjacentHTML('beforeend',detailRow('对白声音方案',fixed?'固定火山声线（推荐）':'Seedance 原生声线'));grid.insertAdjacentHTML('beforeend',detailRow('母片依赖调度',masterDependencyPlanLabel(job)))}const notice=document.createElement('div');notice.className='impact-callout audio-mode-summary';notice.innerHTML=fixed?'<strong>固定声线与精确字幕时间轴</strong><span>按角色设定匹配并固化火山声线；TTS 生成音轨和时间戳，Seedance 负责画面，最终回填同一条对白音轨</span>':'<strong>Seedance 原生声线</strong><span>无需配置 TTS，Seedance 直接合成人声；流程更简单，但跨母片音色一致性不保证</span>';if(grid)grid.insertAdjacentElement('afterend',notice);mountSceneAnchorCards(job,modal)}
const confirmProductionWithRisk=confirmProduction;
confirmProduction=function(job){confirmProductionWithRisk(job);const modal=$('#modal-body');if(!modal)return;const p=job?.preflight||{};const risks=(p.paid_masters||[]).filter(item=>item.quality_risk);if(!risks.length)return;const high=(p.requires_master_feasibility_confirmation??p.requires_long_master_confirmation)===true;const riskList=risks.map(item=>`<li><b>母片 #${item.master_idx} · ${item.quality_risk.level==='high'?'高':item.quality_risk.level==='medium'?'中':'低'}风险</b><span>${esc(item.quality_risk.recommendation||'')}</span></li>`).join('');const actions=$('.modal-actions',modal);actions?.insertAdjacentHTML('beforebegin',`<div class="review-callout ${high?'error-callout':''}"><strong>母片可生成性风险</strong><ul class="risk-master-list">${riskList}</ul>${high?'<label class="check-line"><input id="high-risk-master-confirm" type="checkbox"> 我已核对高复杂度母片，接受可能需要单条重做或拆分</label>':''}</div>`);if(!high)return;const button=$('#produce-confirm',modal);if(!button)return;button.onclick=async()=>{try{if(!$('#high-risk-master-confirm',modal)?.checked)return toast('请先确认母片可生成性风险',true);const projectedCents=Number(p.projected_total_cost_cents||p.estimated_cost_cents||0);const overBudget=p.budget_ok===false;const approved_budget_cents=overBudget?Math.round(Number($('#approved-budget',modal)?.value)*100):undefined;if(overBudget&&approved_budget_cents<projectedCents)return toast('新预算不能低于预计累计成本',true);await api(`/api/videos/${encodeURIComponent(job.job_id)}/produce`,{method:'POST',body:JSON.stringify({confirmed:true,approved_budget_cents,high_risk_confirmed:true})});closeModal();toast('视频任务已启动：先锁定声线与锚点，再生成剧情母片');loadPage('videos')}catch(error){toast(error.message,true)}}}
const confirmProductionWithAdaptiveCompiler=confirmProduction;
const productionModeLabel=mode=>({stable:'稳定制作',enhanced:'增强制作',experimental:'实验制作'})[mode]||'自适应制作';
const productionReliabilityLabel=value=>({standard:'标准交付',assisted:'重点复核',best_effort:'实验性质量承诺'})[value]||value;
const adaptationActionLabel=value=>({dialogue_time_allocated:'按完整语义分配对白时长',provider_duration_split:'超过单次能力时透明拆分',continuous_scene_grouping:'同一连续场景合并为原生母片',scene_split:'跨空间使用明确转场',multi_character_staging:'多人同镜采用空间连续性调度'})[value]||value;
confirmProduction=async function(job){
  let compiled=null;
  try{compiled=await api(`/api/videos/${encodeURIComponent(job.job_id)}/compile`,{method:'POST',body:'{}'})}catch(error){toast(`自适应生产方案暂不可用，已保留原预检：${error.message}`,true)}
  confirmProductionWithAdaptiveCompiler(job);
  const modal=$('#modal-body');if(!modal||!compiled)return;
  const grid=$('.preflight-grid',modal);
  const intent=(compiled.required_content||[]).length?(compiled.required_content||[]).map(item=>`<li>${esc(item)}</li>`).join(''):'<li>剧本台词、人物关系与核心动作均按原稿保留</li>';
  const recommendations=(compiled.complexity?.recommendations||[]).map(item=>`<li>${esc(item)}</li>`).join('')||'<li>当前内容可按标准生产路径执行</li>';
  const adaptations=(compiled.adaptation_actions||[]).map(item=>`<li>${esc(adaptationActionLabel(item))}</li>`).join('')||'<li>无需额外拆分，直接生成原生母片</li>';
  const blockers=(compiled.blockers||[]).map(item=>`<li>${esc(item.message)}</li>`).join('');
  const panel=document.createElement('section');panel.className='compiler-preflight';
  panel.innerHTML=compiled.compilable?`<header><div><small>ADAPTIVE PRODUCTION</small><h3>${esc(productionModeLabel(compiled.mode))} · 复杂度 ${Number(compiled.complexity?.score||0)}</h3></div><span>${esc(productionReliabilityLabel(compiled.reliability_commitment))}</span></header><div class="compiler-preflight-grid"><article><b>1. 创作意图</b><strong>不会删减必须保留内容</strong><ul>${intent}</ul></article><article><b>2. 生产模式</b><strong>${esc(productionModeLabel(compiled.mode))}</strong><ul>${recommendations}</ul></article><article><b>3. 系统适配动作</b><strong>${Number(compiled.master_count||0)} 条原生母片</strong><ul>${adaptations}</ul></article><article><b>4. 任务结构</b><strong>${Number(compiled.node_counts?.character_anchor||0)} 人物 · ${Number(compiled.node_counts?.scene_anchor||0)} 场景 · ${Number(compiled.node_counts?.master||0)} 母片</strong><p>依赖满足后自动并发，连续镜头按先后关系串行</p></article></div>`:`<header class="blocked"><div><small>PRODUCTION PREPARATION</small><h3>系统尚未完成生产准备</h3></div><span>${(compiled.blockers||[]).length} 项待处理</span></header><div class="compiler-blockers"><p>这些是缺失资产或非法输入，不能用风险确认绕过</p><ul>${blockers}</ul></div>`;
  (grid||modal.firstElementChild)?.insertAdjacentElement('beforebegin',panel);
  if(!compiled.compilable){const button=$('#produce-confirm',modal);if(button){button.disabled=true;button.textContent='请先修复生产准备'}}
}
async function resumeFailedVideo(job){try{const result=await api(`/api/videos/${encodeURIComponent(job.job_id)}/resume`,{method:'POST',body:'{}'});toast(`已保留 ${result.preserved_masters} 条母片，剩余 ${result.remaining_masters} 条待生成`);if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(item=>item.job_id===job.job_id);confirmProduction(fresh)}catch(e){toast(e.message,true)}}
async function openProductionSettings(job){
  if(job.partial_production){confirmProduction(job);return;}
  const [characterData,configData]=await Promise.all([
    api('/api/characters?status=Active'),api('/api/config')
  ]);
  const active=characterData.characters||[];
  const current=job.production_settings||{};
  const configValue=key=>(configData.fields||[]).find(field=>field.key===key)?.value;
  const characterAnchorGenerator=current.character_anchor_generator||configValue('CHARACTER_ANCHOR_GENERATOR')||'seedance_2_5';
  const sceneAnchorGenerator=current.scene_anchor_generator||configValue('SCENE_ANCHOR_GENERATOR')||'seedance_2_5';
  const audioMode=current.audio_mode||configValue('DIALOGUE_AUDIO_MODE')||'seedance_native';
  const dialogueMasters=(job.masters||job.planned_masters||[]).filter(master=>(master.type||'dialogue')==='dialogue');
  const onScreen=item=>item.speech_mode?item.speech_mode==='on_screen':!/旁白|narrator|narration|voiceover|voice_over|画外音|画外台词|背景台词/i.test(item.speaker||'');
  const speakers=dialogueMasters.flatMap(master=>{
    const beats=master.performance_beats||[];
    return (beats.length?beats:[master]).filter(onScreen).flatMap(item=>[item.speaker,...(item.speakers||[])]);
  });
  // The backend character table already represents visible identities, including
  // silent actors. Only legacy jobs without that table need speaker fallback.
  const effectiveSpeakers=[...new Set(((job.characters||[]).length?(job.characters||[]).map(character=>character.id):speakers).filter(Boolean))];
  const currentMappings=current.character_mappings||Object.fromEntries(effectiveSpeakers.map(id=>[id,current.character_id||'']));
  const options=(selected='')=>`<option value="" ${selected?'':'selected'}>AI 按剧本人物自动生成（推荐）</option>${active.map(c=>`<option value="${esc(c.character_id)}" ${selected===c.character_id?'selected':''}>固定角色：${esc(c.display_name)} · ${characterTypeLabel(c.identity_type)}</option>`).join('')}`;
  const mappingRows=effectiveSpeakers.map(speaker=>{const scriptCharacter=(job.characters||[]).find(c=>c.id===speaker);const name=scriptCharacter?.script_character_name||scriptCharacter?.name||speaker;const look=scriptCharacter?.look||scriptCharacter?.background_description||'按剧本人物描述与场景自动设定形象';return `<label class="speaker-mapping-row"><span><b>剧本人物：${esc(name)}</b><small>${esc(look)}</small></span><select data-speaker-mapping="${esc(speaker)}">${options(currentMappings[speaker]||'')}</select></label>`}).join('');
  openModal('PRODUCTION SETUP','确认视频制作设置',`<div class="form-stack production-settings"><section class="speaker-mapping"><div class="section-line"><h3>人物形象方案</h3><small>${effectiveSpeakers.length} 位剧本人物</small></div><div class="impact-callout"><strong>每位人物先定妆，再进入剧情母片</strong><span>固定角色直接复用火山资产定妆；未绑定角色由 AI 先生成独立身份母片并提取定妆照。所有出镜镜头都会携带同一人物锚点</span></div>${mappingRows||'<p class="empty-inline">剧本没有需要出镜的对白人物</p>'}${customCharacterSeedanceNotice((job.characters||[]).filter(character=>currentMappings[character.id]))}<small>支持混合使用：部分人物绑定固定资产，其余人物由 AI 自动定妆</small></section><section class="anchor-generator-settings"><div class="section-line"><h3>锚点图生成与验收</h3><small>本任务可覆盖全局默认</small></div><label>人物锚点<select id="setting-character-anchor-generator">${anchorGeneratorOptions(characterAnchorGenerator)}</select><small>仅影响 AI 自动定妆；固定火山角色继续使用已授权资产</small></label><label>场景锚点<select id="setting-scene-anchor-generator">${anchorGeneratorOptions(sceneAnchorGenerator)}</select><small>Seedance 2.5 会生成 4 秒静默母片并抽帧，成本高于 Seedream 5.0</small></label><label>锚点验收<select id="setting-anchor-review-mode"><option value="manual">生成后人工确认（推荐）</option><option value="auto">自动验收锚点</option></select><small>人工模式会暂停等待确认，并且只在确认后开始剧情母片付费；自动模式直接使用生成成功的锚点</small></label></section><label>视频画幅<select id="setting-orientation"><option value="9:16">9:16 竖屏短视频</option><option value="16:9">16:9 横屏</option><option value="1:1">1:1 方形</option></select></label><label>生产档位<select id="setting-profile"><option value="standard">标准版 · 原生母片 + 本地导演剪辑</option></select></label><label>字幕样式<select id="setting-subtitle"><option value="clean">简洁重点字幕</option><option value="minimal">极简字幕</option></select></label><label class="check-line"><input id="setting-asr" type="checkbox" ${current.asr_enabled!==false?'checked':''}> 使用 ASR 校准字幕与语音 <small>可选；ASR 未配置时自动使用估算字幕</small></label><label>单任务预算（元）<input id="setting-budget" type="number" min="1" step="1" value="${Number(current.budget_cents||job.preflight?.budget_cents||2500)/100}"></label></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="save-production-settings">保存制作设置并进入预检</button></div>`);
  const audioSetting=document.createElement('section');audioSetting.className='audio-mode-settings';audioSetting.innerHTML=`<div class="section-line"><h3>对白声音方案</h3><small>本任务可覆盖全局默认</small></div><label>声音生成方式<select id="setting-audio-mode"><option value="seedance_native">Seedance 原生声线（默认）</option><option value="fixed_tts">固定火山声线</option></select><small id="setting-audio-mode-help"></small></label>`;const orientationLabel=$('#setting-orientation').closest('label');orientationLabel.parentNode.insertBefore(audioSetting,orientationLabel);$('#setting-audio-mode').value=audioMode;const refreshAudioModeHelp=()=>{$('#setting-audio-mode-help').textContent=$('#setting-audio-mode').value==='fixed_tts'?'按角色自动匹配并固化火山声线；音色、台词和字幕时序更稳定，需要配置火山 TTS':'由 Seedance 直接合成人声；同角色后续母片复用最近已验收音频参考，仍需人工验收音色一致性'};$('#setting-audio-mode').onchange=refreshAudioModeHelp;refreshAudioModeHelp();
  $('#setting-budget').closest('label').childNodes[0].nodeValue='单任务预算上限（元）';
  $('#setting-orientation').value=current.orientation||'9:16';$('#setting-subtitle').value=current.subtitle_style||'clean';$('#setting-anchor-review-mode').value=current.anchor_review_mode||'manual';
  $('#save-production-settings').onclick=async()=>{const button=$('#save-production-settings');button.disabled=true;try{const character_mappings=Object.fromEntries($$('[data-speaker-mapping]').map(select=>[select.dataset.speakerMapping,select.value]));await api(`/api/videos/${encodeURIComponent(job.job_id)}/configure`,{method:'POST',body:JSON.stringify({character_mappings,audio_mode:$('#setting-audio-mode').value,character_anchor_generator:$('#setting-character-anchor-generator').value,scene_anchor_generator:$('#setting-scene-anchor-generator').value,anchor_review_mode:$('#setting-anchor-review-mode').value,orientation:$('#setting-orientation').value,production_profile:$('#setting-profile').value,subtitle_style:$('#setting-subtitle').value,asr_enabled:$('#setting-asr').checked,budget_cents:Math.round(Number($('#setting-budget').value)*100)})});closeModal();if(!await loadPage('videos'))return;const fresh=state.videos.jobs.find(x=>x.job_id===job.job_id);confirmProduction(fresh)}catch(e){button.disabled=false;toast(e.message,true)}};
}
async function autoMatchCharacterVoice(character){
  try{toast('正在按角色背景匹配火山声线');await api(`/api/characters/${encodeURIComponent(character.character_id)}/voice/auto`,{method:'POST',body:'{}'});closeDrawer();toast('固定声线已匹配并固化');if(!await loadCharacters())return;openCharacterDrawer((state.characters?.characters||[]).find(item=>item.character_id===character.character_id))}catch(e){toast(e.message,true)}
}
async function chooseCharacterVoice(character){
  try{
    const data=await api('/api/voices');const voices=data.voices||[];if(!voices.length)throw new Error('火山账号没有可用的 TTS 2.0 声线');
    closeDrawer();openModal('FIXED VOICE','选择固定声线',`<div class="form-stack"><label>火山声线<select id="manual-character-voice">${voices.map((voice,index)=>`<option value="${index}">${esc(voice.voice_name)} · ${esc(voice.gender)} ${esc(voice.age)} · ${esc((voice.labels||[]).slice(0,3).join(' / '))}</option>`).join('')}</select><small>保存后该角色所有母片复用同一 voice_type；不会交给 Seedance 随机生成</small></label><div id="manual-voice-preview"></div></div><div class="modal-actions"><button class="mini-action" onclick="document.querySelector('#modal-close').click()">取消</button><button class="primary-action" id="save-character-voice">保存固定声线</button></div>`);
    const preview=()=>{const voice=voices[Number($('#manual-character-voice').value||0)];$('#manual-voice-preview').innerHTML=voice.preview_url?`<audio controls preload="none" src="${esc(voice.preview_url)}"></audio><p>${esc(voice.description||'')}</p>`:`<p class="empty-inline">该声线未提供试听地址</p>`};$('#manual-character-voice').onchange=preview;preview();
    $('#save-character-voice').onclick=async()=>{const voice=voices[Number($('#manual-character-voice').value||0)];await api(`/api/characters/${encodeURIComponent(character.character_id)}/voice`,{method:'POST',body:JSON.stringify(voice)});closeModal();toast('固定声线已保存');if(!await loadCharacters())return;openCharacterDrawer((state.characters?.characters||[]).find(item=>item.character_id===character.character_id))};
  }catch(e){toast(e.message,true)}
}
function openPublishPicker(){const ready=state.publishing?.ready||[];if(!ready.length)return toast('当前没有待发布成片，请先从视频流水线提交',true);if(ready.length===1)return openPublishFlow(ready[0]);openModal('SELECT VIDEO','选择待发布成片',`<div class="publish-picker">${ready.map(item=>`<button data-pick-publish="${esc(item.job_id)}"><strong>${esc(item.title)}</strong><small>${esc(item.job_id)} · ${esc(item.updated_at)}</small><i>›</i></button>`).join('')}</div>`);$$('[data-pick-publish]').forEach(button=>button.onclick=()=>{const item=ready.find(row=>row.job_id===button.dataset.pickPublish);closeModal();openPublishFlow(item)})}
$('#register-publication').onclick=openPublishPicker;
$('#global-project-search').addEventListener('input',renderGlobalProjects);
const startNewProject=()=>{const trigger=$('#new-source');if(trigger)return trigger.click();toast('内容输入入口加载失败，请刷新后重试',true)};
$('#global-new-project').onclick=startNewProject;
$('#global-new-project-bottom').onclick=startNewProject;
$$('[data-global-stage]').forEach(button=>button.onclick=()=>{const stage=button.dataset.globalStage;const projects=state.projects?.projects||[];const project=projects.find(item=>item.project_id===state.studioProjectId)||projects[0];if(!project)return toast('请先创建或选择一个内容项目',true);openProjectStudio(project,stage)});
$('#manage-source-feeds').onclick=openSourceFeedManager;

// Publishing is hybrid: Douyin uses OAuth + OpenAPI, while WeChat Channels
// stays on the official assistant page and requires verifiable platform IDs.
function renderPublishing(d){
  const platforms=state.config?.publish_platforms||[{id:'douyin',name:'抖音',mode:'oauth',configured:false,connected:false},{id:'weixin_channels',name:'微信视频号',mode:'official_assistant',configured:true,entry_url:WEIXIN_ASSISTANT_URL}];
  $('#channel-status').innerHTML=platforms.map(p=>{const direct=p.mode==='oauth';const assistant=p.mode==='official_assistant';const ready=direct?p.connected:assistant||p.configured;return `<div><span class="platform-logo ${p.id}">${p.name[0]}</span><p><strong>${esc(p.name)}</strong><small>${direct?'OpenAPI 直连':assistant?'官方助手扫码登录':'辅助发布/结果登记'} · ${ready?(direct?'账号已绑定':assistant?'入口可用':'已配置'):(direct&&p.configured?'待绑定账号':'待配置')}</small></p><button class="text-action" data-go-config="${p.id}">${assistant?'查看入口':'配置'}</button></div>`}).join('');
  $$('[data-go-config]').forEach(b=>b.onclick=async()=>{if(!await navigate('config'))return;const panel=$(`[data-platform-fields="${b.dataset.goConfig}"]`);if(!panel)return toast('发布平台配置区加载失败，请刷新后重试',true);panel.hidden=false;const trigger=$(`[data-platform-expand="${b.dataset.goConfig}"]`);if(trigger)trigger.textContent='收起';panel.scrollIntoView({behavior:'smooth',block:'center'})});
  $('#publish-ready').innerHTML=d.ready.length?d.ready.map(x=>`<button class="record-row interactive" data-publish-job="${esc(x.job_id)}"><div class="record-title"><strong>${esc(x.title)}</strong><span class="tag ${esc(x.state)}">待渠道发布</span></div><p>${esc(x.job_id)}</p><small>${x.published_platforms?.length?`已完成 ${x.published_platforms.map(platformName).join('、')} · `:''}${x.pending_platforms?.length?`审核中 ${x.pending_platforms.map(platformName).join('、')} · `:''}${x.remaining_platforms?.length?`待处理 ${x.remaining_platforms.map(platformName).join('、')} · `:''}${esc(x.updated_at)}</small><i class="row-arrow">›</i></button>`).join(''):empty('暂无待发布成片，请先在视频流水线提交成品');
  $('#publication-list').innerHTML=d.publications.length?d.publications.map(x=>`<button class="record-row interactive" data-publication-id="${esc(x.publication_id)}"><div class="record-title"><strong>${esc(x.title||x.job_title)}</strong><span class="tag ${x.verification_status==='pending_review'?'pending':'active'}">${esc(platformName(x.platform))} · ${x.verification_status==='pending_review'?'审核中':'已核实'}</span></div><p>${esc(x.item_id)}</p><small>${x.verification_status==='pending_review'?'已提交平台审核，尚未计入发布成功':x.latest_snapshot?`最近快照 ${esc(x.latest_snapshot.captured_at)} · 播放 ${x.latest_snapshot.views}`:esc(x.published_at||'尚未录入绩效快照')}</small><i class="row-arrow">›</i></button>`).join(''):empty('尚未登记平台投稿或作品');
  const performanceDue=d.performance_due||[];
  const performanceWindowLabel={24:'24 小时',72:'72 小时',168:'168 小时（7 天）'};
  $('#performance-due-count').textContent=performanceDue.length;
  $('#performance-due').innerHTML=performanceDue.length?performanceDue.map(x=>`<button class="record-row interactive performance-due-row" data-performance-publication="${esc(x.publication_id)}"><div class="record-title"><strong>${esc(x.title||x.job_title)}</strong><span class="tag ${x.overdue?'failed':'pending'}">${x.overdue?'已逾期':'待回收'} · ${esc(performanceWindowLabel[x.window_hours]||`${x.window_hours} 小时`)}</span></div><p>${esc(platformName(x.platform))} · ${esc(x.item_id)}</p><small>${x.overdue?'建议尽快补录该时间窗的累计数据':`到期时间 ${esc(x.due_at)}`} · 录入绩效</small><i class="row-arrow">›</i></button>`).join(''):empty('暂无到期绩效待办');
  const unverified=$('#publication-unverified');
  if(unverified)unverified.innerHTML=(d.unverified||[]).length?d.unverified.map(x=>`<div class="record-row"><div class="record-title"><strong>${esc(x.title)}</strong><span class="tag unverified">历史状态待核实</span></div><p>${esc(x.job_id)}</p><small>缺少平台作品映射，不计入真实发布数量</small></div>`).join(''):empty('没有待核实的历史发布状态');
  $$('[data-publish-job]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.publishJob,'publish','publish',{},()=>openPublishFlow(d.ready.find(x=>x.job_id===b.dataset.publishJob))));
  $$('[data-publication-id]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.publicationId,'performance','publish',{},()=>openPublicationDrawer(d.publications.find(x=>x.publication_id===b.dataset.publicationId))));
  $$('[data-performance-publication]').forEach(b=>b.onclick=()=>openEntityInProject(b.dataset.performancePublication,'performance','publish',{},()=>openPublicationDrawer(d.publications.find(x=>x.publication_id===b.dataset.performancePublication))));
}

function performanceSnapshotBlock(snapshot){
  if(!snapshot)return '<p class="empty-inline">尚未录入绩效快照</p>';
  const completion=snapshot.completion_rate==null?'—':`${(Number(snapshot.completion_rate)*100).toFixed(1)}%`;
  return `<div class="performance-grid">${detailRow('采集时间',snapshot.captured_at)}${detailRow('播放 / 完播',`${snapshot.views} / ${snapshot.completed_views??'—'}`)}${detailRow('完播率',completion)}${detailRow('平均观看',snapshot.average_watch_sec==null?'—':`${snapshot.average_watch_sec} 秒`)}${detailRow('互动',`赞 ${snapshot.likes||0} · 评 ${snapshot.comments||0} · 分享 ${snapshot.shares||0} · 收藏 ${snapshot.saves||0}`)}${detailRow('粉丝 / 线索',`${snapshot.follows||0} / ${snapshot.leads||0}`)}</div>`;
}

function openPublicationDrawer(item){
  if(!item)return;
  if(item.verification_status==='pending_review'){
    openDrawer('PLATFORM REVIEW',item.title||item.job_title,`<div class="drawer-summary"><span class="tag pending">${esc(platformName(item.platform))}审核中</span><p>投稿已送达平台，但平台尚未确认发布成功，因此不会计入发布完成或绩效统计</p></div><section class="detail-section"><h3>投稿信息</h3>${detailRow('投稿记录',item.publication_id)}${detailRow('平台作品 ID',item.item_id)}${detailRow('关联任务',item.job_id)}${detailRow('当前状态','等待平台审核')}</section>`);
    return;
  }
  const metric=(key,label,attrs='')=>`<label>${label}<input data-performance="${key}" ${attrs}></label>`;
  openDrawer('VERIFIED PUBLICATION',item.title||item.job_title,`<div class="drawer-summary"><span class="tag active">${esc(platformName(item.platform))}</span><p>该作品已有平台映射证据。绩效数据按平台累计值录入，每次保存形成一个时间快照</p></div><section class="detail-section"><h3>发布信息</h3>${detailRow('发布记录',item.publication_id)}${detailRow('平台作品 ID',item.item_id)}${detailRow('发布时间',item.published_at)}${detailRow('关联任务',item.job_id)}${item.item_url?`<a class="text-action inline-link" href="${esc(item.item_url)}" target="_blank" rel="noreferrer">打开平台作品</a>`:''}</section><section class="detail-section"><h3>最近累计绩效快照</h3>${performanceSnapshotBlock(item.latest_snapshot)}</section><section class="detail-section"><h3>录入累计绩效快照</h3><div class="performance-form">${metric('captured_at','采集时间','type="datetime-local"')}${metric('views','播放量','type="number" min="0" required')}${metric('completed_views','完播量','type="number" min="0"')}${metric('completion_rate','完播率（%）','type="number" min="0" max="100" step="0.1"')}${metric('average_watch_sec','平均观看（秒）','type="number" min="0" step="0.1"')}${metric('likes','点赞','type="number" min="0"')}${metric('comments','评论','type="number" min="0"')}${metric('shares','分享','type="number" min="0"')}${metric('saves','收藏','type="number" min="0"')}${metric('follows','新增粉丝','type="number" min="0"')}${metric('leads','线索','type="number" min="0"')}</div></section><div class="drawer-footer"><button class="primary-action" id="save-performance">保存绩效快照</button></div>`);
  $('#save-performance').onclick=()=>recordPerformanceSnapshot(item);
}

async function recordPerformanceSnapshot(item){
  const payload={};
  $$('[data-performance]').forEach(input=>{if(input.value!=='')payload[input.dataset.performance]=input.type==='number'?Number(input.value):input.value});
  if(payload.views==null)return toast('请填写累计播放量',true);
  if(payload.captured_at)payload.captured_at=new Date(payload.captured_at).toISOString();
  if(payload.completion_rate!=null)payload.completion_rate=payload.completion_rate/100;
  try{
    const result=await api(`/api/publications/${encodeURIComponent(item.publication_id)}/performance`,{method:'POST',body:JSON.stringify(payload)});
    item.latest_snapshot=result.latest_snapshot;
    toast('累计绩效快照已保存');
    openPublicationDrawer(item);
    loadPage('publishing');
  }catch(e){toast(e.message,true)}
}

function applyContinuityVideoPosters(root=document){
  root.querySelectorAll?.('video[src*="/api/videos/"][src*="/file"]').forEach(video=>{
    if(video.dataset.continuityPosterApplied)return;
    const match=video.getAttribute('src')?.match(/\/api\/videos\/([^/?]+)\/file/);
    if(!match)return;
    const jobId=decodeURIComponent(match[1]);
    const job=(state.videos?.jobs||[]).find(item=>item.job_id===jobId)
      ||(state.publishing?.ready||[]).find(item=>item.job_id===jobId)
      ||{job_id:jobId};
    video.poster=videoCoverUrl(job);
    video.dataset.continuityPosterApplied='true';
  });
  const coverTime=root.querySelector?.('#douyin-cover-tsp');
  if(coverTime&&!coverTime.dataset.continuityDefaultApplied){
    coverTime.value='0.04';
    coverTime.step='0.01';
    coverTime.dataset.continuityDefaultApplied='true';
  }
}

const continuityPosterObserver=new MutationObserver(records=>{
  records.forEach(record=>record.addedNodes.forEach(node=>{
    if(node.nodeType===Node.ELEMENT_NODE)applyContinuityVideoPosters(node);
  }));
});
continuityPosterObserver.observe(document.body,{childList:true,subtree:true});
applyContinuityVideoPosters();

const hasActiveProductionJobs=()=>Boolean(state.videos?.jobs?.some(job=>autoPollProductionStates.has(job.display_state||job.state)));
const videoJobFingerprint=job=>JSON.stringify({state:job.state,display_state:job.display_state,display_state_label:job.display_state_label,cost_cents:job.cost_cents,updated_at:job.updated_at,error:job.error,final_exists:job.final_exists,partial_production:job.partial_production,next_action:job.next_action,recovery:job.recovery,allowed_actions:job.allowed_actions,shots:(job.shots||[]).map(shot=>[shot.idx,shot.state,shot.cost_cents,shot.error,shot.manual_review_required,shot.candidate_preview_url,shot.quality_status,shot.review_resolved,shot.human_decision,shot.candidate_review_eligible])});
function patchVideoProductionData(videos,productionHealth){
  const previousJobs=state.videos?.jobs||[];
  const previousById=new Map(previousJobs.map(job=>[job.job_id,job]));
  const previousVisibleIds=visibleVideoJobs(previousJobs).map(job=>job.job_id);
  const nextVisibleIds=visibleVideoJobs(videos.jobs||[]).map(job=>job.job_id);
  const structureChanged=previousVisibleIds.join('\n')!==nextVisibleIds.join('\n');
  state.videos=videos;state.productionHealth=productionHealth;
  if(state.page==='overview')renderStudioTaskDrawer();
  renderVideoTotals();renderProductionHealth(productionHealth);
  if(structureChanged)renderVideos();
  else for(const job of visibleVideoJobs()){
    const previousFingerprint=videoJobFingerprint(previousById.get(job.job_id)||{});
    const nextFingerprint=videoJobFingerprint(job);
    if(previousFingerprint===nextFingerprint)continue;
    const row=$(`[data-video-id="${CSS.escape(job.job_id)}"]`);
    if(!row)continue;
    row.innerHTML=videoRowContents(job);
    bindVideoRow(row);
  }
  const drawer=$('#drawer');
  if(!drawer.classList.contains('open')||!state.openVideoJobId)return;
  const fresh=state.videos.jobs.find(job=>job.job_id===state.openVideoJobId);
  const previous=previousById.get(state.openVideoJobId);
  if(!fresh){closeDrawer();toast('当前视频任务已不存在',true);return}
  if(videoJobFingerprint(previous||{})===videoJobFingerprint(fresh))return;
  const drawerBody=$('#drawer-body');
  const scrollTop=drawerBody.scrollTop;
  openVideoDrawer(fresh);
  drawerBody.scrollTop=scrollTop;
}
async function pollActiveProduction(){
  if(state.polling)return;
  if(!productionPollingPages.has(state.page)||document.visibilityState!=='visible'||!document.querySelector('#modal')?.hidden||!hasActiveProductionJobs())return;
  state.polling=true;
  try{
    const [videos,productionHealth]=await Promise.all([api('/api/videos'),api('/api/production/health')]);
    if(!productionPollingPages.has(state.page)||document.visibilityState!=='visible')return;
    patchVideoProductionData(videos,productionHealth);
  }catch(error){console.warn('生产状态同步失败',error)}
  finally{state.polling=false;scheduleActiveProductionPoll()}
}
function scheduleActiveProductionPoll(){
  clearTimeout(state.productionPollTimer);state.productionPollTimer=null;
  if(!productionPollingPages.has(state.page)||!hasActiveProductionJobs())return;
  if(document.visibilityState!=='visible')return;
  state.productionPollTimer=setTimeout(pollActiveProduction,5000);
}
document.addEventListener('visibilitychange',scheduleActiveProductionPoll);
const initialPage=location.hash.replace(/^#/,"");
if(pageMeta[initialPage])navigate(initialPage);else navigate('overview');
