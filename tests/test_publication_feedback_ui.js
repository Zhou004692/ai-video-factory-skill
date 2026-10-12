const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('scripts/management_ui/app.js','utf8');
const start = source.indexOf('function performanceSnapshotBlock(');
const end = source.indexOf('function openPublishFlow(',start);
const inputs = [{dataset:{performance:'views'},value:'100',type:'number'},
  {dataset:{performance:'likes'},value:'0',type:'number'},
  {dataset:{performance:'comments'},value:'',type:'number'}];
let request,drawerMarkup;
const elements = new Map();
const works = [{publication_id:'old-pub',job_id:'J',platform:'douyin',item_id:'old-item',is_current:0},
  {publication_id:'new-pub',job_id:'J',platform:'douyin',item_id:'new-item',is_current:1}];
const context = vm.createContext({detailRow:(a,b)=>`${a}:${b}`,esc:String,
  state:{publishing:{publications:works}},
  $$:()=>inputs,$:selector=>{if(!elements.has(selector))elements.set(selector,{});return elements.get(selector)},toast:()=>{},loadPage:()=>{},
  openDrawer:(_kicker,_title,markup)=>{drawerMarkup=markup}, platformName:String, Date,Number,encodeURIComponent,
  api:async (url,options)=>{request={url,payload:JSON.parse(options.body)};return {latest_snapshot:{views:100}};}});
// Extract these adjacent functions only; no browser startup or external socket.
const finish = source.indexOf('\n}',source.indexOf('async function recordPerformanceSnapshot(',start))+2;
vm.runInContext(source.slice(start,finish),context);
const markup = vm.runInContext("performanceSnapshotBlock({views:100,likes:0,comments:null,shares:null,saves:null,follows:null,leads:null})",context);
assert.ok(markup.includes('赞 0'));
assert.ok(markup.includes('评 未提供'),markup);
assert.ok(markup.includes('未提供 / 未提供'),markup);
(async ()=>{
  context.openPublicationDrawer(works[0]);
  assert.ok(drawerMarkup.includes('id="performance-work"'));
  assert.ok(drawerMarkup.includes('old-item'));
  assert.ok(drawerMarkup.includes('new-item'));
  await context.recordPerformanceSnapshot(works[0]);
  assert.equal(request.url,'/api/publications/old-pub/performance');
  assert.equal(request.payload.likes,0);
  assert.ok(!Object.hasOwn(request.payload,'comments'));
  elements.get('#performance-work').onchange({target:{value:'new-pub'}});
  assert.ok(drawerMarkup.includes('value="new-pub" selected'));
  await elements.get('#save-performance').onclick();
  assert.equal(request.url,'/api/publications/new-pub/performance');
  console.log('Publication feedback UI missing/zero and exact work assertions passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
