const projects = {
  rental: {title:'设备租赁狗都不做？其实亏的都是用老方法干的人',video:'/api/videos/J-SD25-DIRECTOR-20260811/file',poster:'/design-assets/story-rental.jpg',duration:'00:33'},
  number: {title:'神秘数字背后的设备租赁玄机',video:'/api/videos/J-TOPIC-ef32f171ea4d81cb/file',poster:'/design-assets/story-number.jpg',duration:'00:30'},
  goku: {title:'悟空与佛利沙的激烈对波',video:'/api/videos/J-TOPIC-4ae0d218f7df3c87/file',poster:'/design-assets/story-goku.jpg',duration:'00:40'}
};
const modeCopy={review:['内容项目 / 成片审核','通过并提交发布'],ideate:['内容项目 / 选题创作','生成三版剧本'],produce:['内容项目 / 视频制作','提交成片审核']};
const canvas=document.querySelector('#canvas');
const assetBrowser=document.querySelector('#asset-browser');
const inspector=document.querySelector('#inspector');
const video=document.querySelector('#preview-video');
const viewer=document.querySelector('#viewer');
const playButton=document.querySelector('#play-button');
const reviewAssets=assetBrowser.innerHTML;
const reviewInspector=inspector.innerHTML;

function reviewCanvas(){
  canvas.innerHTML=`<div class="canvas-toolbar"><div><button>−</button><span>72%</span><button>＋</button></div><div><button>适应窗口</button><button>全屏</button></div></div><div class="viewer" id="viewer"><video id="preview-video" src="${video?.src||projects.rental.video}" preload="metadata" poster="${video?.poster||projects.rental.poster}"></video><button class="play" id="play-button" aria-label="播放">▶</button><div class="viewer-caption">“真正的问题不是设备租赁不能做”</div></div><div class="transport"><button id="transport-play">▶</button><time id="time-current">00:00</time><div class="scrubber" id="scrubber"><i id="scrubber-fill"></i><span class="comment-mark one"></span><span class="comment-mark two"></span></div><time id="time-total">00:33</time><button>⌁</button><button>⋮</button></div><div class="filmstrip"><button class="active"><span class="frame rental"></span><small>开场钩子</small></button><button><span class="frame number"></span><small>痛点拆解</small></button><button><span class="frame rental"></span><small>方案反转</small></button><button><span class="frame goku"></span><small>行动号召</small></button></div><div class="ai-dock"><span class="spark">✦</span><div><span>告诉 AI 你想调整什么</span><small>例如：把开场压缩到 3 秒，字幕更克制</small></div><button>⌘ ↵</button></div>`;
  bindPlayer();
}
function ideaCanvas(){
  canvas.innerHTML=`<div class="canvas-toolbar"><div><button>−</button><span>58%</span><button>＋</button></div><div><button>整理画布</button><button>分享</button></div></div><div class="idea-board"><article class="idea-note one"><b>核心冲突</b><p>大家认为“设备租赁狗都不做”，但真正亏钱的是仍在用老方法经营的人。</p></article><article class="idea-note two"><b>目标观众</b><p>设备租赁行业中小老板、个体经营者、有意进入赛道的创业者。</p></article><div class="idea-media one"></div><div class="idea-media two"></div><div class="connector a"></div><div class="connector b"></div><article class="idea-thread"><header><b>AI 选题建议</b><span class="tag">91.8 分</span></header><p>从“老板甩采购单”进入，比直接讲行业痛点更具画面感。建议保留反转结构。</p></article></div><div class="prompt-dock"><textarea placeholder="描述你想探索的创作方向…">围绕“行业不能做，还是方法不对”再生成三个更有冲突的短视频角度</textarea><div class="prompt-controls"><div><button>＋ 参考素材</button><button>Seedance 2.5</button><button>9:16</button></div><button class="generate">✦ 生成方向</button></div></div>`;
}
function produceCanvas(){
  canvas.innerHTML=`<div class="canvas-toolbar"><div><button>画面</button><button>字幕</button><button>音频</button></div><div><button>预览画质 ▾</button><button>全屏</button></div></div><div class="produce-viewer"><video src="${projects.rental.video}" poster="${projects.rental.poster}" controls></video></div><div class="timeline-editor"><div class="timeline-tools"><span>00:00:00</span><span>自动吸附 · 开</span></div><div class="tracks"><div class="track-label">画面</div><div class="track"><span class="clip video one">开场钩子</span><span class="clip video two">小玲讲解母片</span><span class="clip video three">设备 B-roll</span><i class="playhead"></i></div><div class="track-label">字幕</div><div class="track"><span class="clip one">狗都不干？</span><span class="clip two">拼的不是腿，是信息和效率</span><span class="clip three">把时间留给客户</span></div><div class="track-label">音频</div><div class="track"><span class="clip video" style="width:100%">原生人声 · 已完成 ASR 校准</span></div></div></div><div class="ai-dock"><span class="spark">✦</span><div><span>用自然语言调整当前剪辑</span><small>例如：缩短第一个镜头；重新断行字幕；降低 BGM</small></div><button>⌘ ↵</button></div>`;
}
function renderMode(mode){
  document.body.dataset.mode=mode;
  document.querySelectorAll('.review-bar [data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===mode));
  document.querySelector('#context-path').textContent=modeCopy[mode][0];
  document.querySelector('#main-action').textContent=modeCopy[mode][1];
  if(mode==='review') reviewCanvas(); else if(mode==='ideate') ideaCanvas(); else produceCanvas();
  renderSidePanels(mode);
}
function renderSidePanels(mode){
  if(mode==='ideate'){
    assetBrowser.innerHTML=`<header><div><b>创作素材</b><span>8 项</span></div><div><button>＋ 导入</button></div></header><div class="asset-tabs"><button class="active">全部</button><button>灵感</button><button>来源</button></div><div class="asset-grid"><button class="asset-card selected"><span class="asset-thumb rental"><i>▶</i></span><b>竞品开场结构</b><small>视频参考</small></button><button class="asset-card"><span class="asset-thumb source"><i>文</i></span><b>核心业务材料</b><small>人工创建</small></button><button class="asset-card"><span class="asset-thumb number"><i>图</i></span><b>行业痛点截图</b><small>画面参考</small></button><button class="asset-card"><span class="asset-thumb xiaoling"><i>人</i></span><b>小玲</b><small>候选主讲人</small></button></div>`;
    inspector.innerHTML=`<header><div class="inspector-tabs"><button class="active">方向</button><button>洞察</button><button>历史</button></div><button>×</button></header><section class="quality"><header><b>选题质量</b><span>91.8 / 100</span></header><div class="quality-grid"><span><i></i>相关性 95</span><span><i></i>故事潜力 90</span><span><i></i>证据质量 92</span><span><i></i>生产适配 90</span></div></section><section class="comments"><header><b>三个候选方向</b><button>重新生成</button></header>${ideaOption('01','行业不能做，还是方法不对？','推荐 · 冲突最强')}${ideaOption('02','业务员天天跑工地？','一线视角')}${ideaOption('03','不知道赚不赚钱？','数据揭秘')}</section><div class="comment-input"><span>补充创作要求…</span><button>↑</button></div>`;
  }else if(mode==='produce'){
    assetBrowser.innerHTML=`<header><div><b>镜头计划</b><span>3 段</span></div><div><button>＋</button></div></header><div class="asset-tabs"><button class="active">镜头</button><button>素材</button><button>版本</button></div><div class="shot-list"><button class="shot-item active"><span class="shot-cover rental"></span><span><b>01 · 开场钩子</b><small>原生母片 · 已完成</small></span><em>06s</em></button><button class="shot-item"><span class="shot-cover number"></span><span><b>02 · 小玲讲解</b><small>Seedance · 已完成</small></span><em>22s</em></button><button class="shot-item"><span class="shot-cover goku"></span><span><b>03 · 设备 B-roll</b><small>素材库复用</small></span><em>08s</em></button></div>`;
    inspector.innerHTML=`<header><div class="inspector-tabs"><button class="active">属性</button><button>质量</button><button>任务</button></div><button>×</button></header><section class="quality"><header><b>当前镜头</b><span>01 / 03</span></header><div class="quality-grid"><span><i></i>画面完成</span><span><i></i>人声完成</span><span><i></i>口型通过</span><span><i></i>字幕通过</span></div></section><section class="comments"><header><b>导演参数</b><button>重置</button></header>${property('角色','传统销售')}${property('景别','中近景')}${property('时长','6 秒')}${property('字幕','简洁重点字幕')}${property('模型','Seedance 2.5')}</section><div class="approval-card"><div><span class="avatar-stack"><i>¥</i></span><span><b>当前成本 ¥18.88</b><small>3 个镜头均已生成</small></span></div><div><button class="reject">重新生成</button><button class="approve">保存剪辑</button></div></div>`;
  }else{
    assetBrowser.innerHTML=reviewAssets;
    inspector.innerHTML=reviewInspector;
  }
}
function ideaOption(no,title,note){return `<article><span class="comment-avatar">${no}</span><div><header><b>${title}</b></header><p>${note}</p><footer><button>放到画布</button><button>采用</button></footer></div></article>`}
function property(k,v){return `<article><span class="comment-avatar">·</span><div><header><b>${k}</b><time>${v}</time></header></div></article>`}
function bindPlayer(){
  const v=document.querySelector('#preview-video'),vw=document.querySelector('#viewer'),pb=document.querySelector('#play-button'),tp=document.querySelector('#transport-play'),scrub=document.querySelector('#scrubber'),fill=document.querySelector('#scrubber-fill'),cur=document.querySelector('#time-current'),total=document.querySelector('#time-total');
  if(!v)return; const toggle=()=>v.paused?v.play():v.pause(); pb?.addEventListener('click',toggle);tp?.addEventListener('click',toggle);v.addEventListener('play',()=>{vw.classList.add('playing');if(tp)tp.textContent='❚❚'});v.addEventListener('pause',()=>{vw.classList.remove('playing');if(tp)tp.textContent='▶'});v.addEventListener('loadedmetadata',()=>total.textContent=format(v.duration));v.addEventListener('timeupdate',()=>{const ratio=v.duration?v.currentTime/v.duration:0;fill.style.width=`${ratio*100}%`;cur.textContent=format(v.currentTime)});scrub?.addEventListener('click',e=>{const r=scrub.getBoundingClientRect();v.currentTime=((e.clientX-r.left)/r.width)*v.duration});
}
function format(s){if(!Number.isFinite(s))return'00:00';return`${String(Math.floor(s/60)).padStart(2,'0')}:${String(Math.floor(s%60)).padStart(2,'0')}`}
document.querySelectorAll('.review-bar [data-mode]').forEach(b=>b.addEventListener('click',()=>renderMode(b.dataset.mode)));
document.querySelectorAll('[data-project]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-project]').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');const p=projects[b.dataset.project];document.querySelector('#project-title').value=p.title;const v=document.querySelector('#preview-video');if(v){v.src=p.video;v.poster=p.poster;v.load()}document.querySelector('#time-total')&&(document.querySelector('#time-total').textContent=p.duration)}));
bindPlayer();
const requestedMode=new URLSearchParams(location.search).get('mode');
if(['ideate','produce'].includes(requestedMode))renderMode(requestedMode);
