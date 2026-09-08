const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname,'../scripts/management_ui/app.js'),'utf8');
const start = source.indexOf('function confirmNetworkRetry(job)');
const end = source.indexOf('\nfunction editScript(',start);
let modal, notified, sent;
const buttons = {};
const context = vm.createContext({
  $: id => buttons[id] ||= {}, money: value => value / 100,
  toast: text => notified = text, closeModal:()=>{},closeDrawer:()=>{},loadPage:async()=>{},
  openModal: (...args) => modal = args,
  api: async(url,options) => { sent = {url,options}; return {state:'PRODUCTION_QUEUED'}; },
});
vm.runInContext(source.slice(start,end),context);
(async()=>{
  context.confirmNetworkRetry({job_id:'J',network_retry_indexes:[],network_retry_anchor_ids:['scene_anchor:x'],cost_cents:2448});
  assert.equal(notified,undefined);
  assert.match(modal[2],/1 个未提交锚点/);
  assert.match(modal[2],/已有锚点和完成母片保留/);
  assert.equal(sent,undefined);
  await buttons['#confirm-network-retry'].onclick();
  assert.equal(sent.url,'/api/videos/J/retry-network-failures');
  assert.equal(JSON.parse(sent.options.body).confirmed,true);
  context.confirmNetworkRetry({network_retry_indexes:[],network_retry_anchor_ids:[]});
  assert.match(notified,/当前没有可重试/);
  console.log('7 anchor retry UI assertions passed');
  const job = {job_id:'J',state:'ANCHOR_REVIEW_REQUIRED',next_action:{id:'review_continuity_anchors',label:'审核人物与场景锚点'}};
  const project = {project_id:'P',next_action:'view_progress',stage:'production'};
  let opened;
  Object.assign(context,{
    state:{page:'overview',studioProjectId:'P',studioStage:'video'},
    studioSelectedMaster:()=>({idx:1}), studioRelated:()=>({video:job}),
    openProjectStudio:async()=>true,
    openVideoDrawer:()=>opened='anchors',openVideoDrawerAt:()=>opened='master',
  });
  const labelStart=source.indexOf('function studioPrimaryActionLabel(');
  vm.runInContext(source.slice(labelStart,source.indexOf('\nfunction renderStudioPrimaryAction(',labelStart)),context);
  assert.equal(context.studioPrimaryActionLabel(project,{video:job}),'审核人物与场景锚点');
  const routeStart=source.indexOf('async function routeProjectAction(');
  vm.runInContext(source.slice(routeStart,source.indexOf('\nfunction ',routeStart)),context);
  await context.routeProjectAction(project);
  assert.equal(opened,'anchors');
  console.log('2 anchor review entry assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1});
