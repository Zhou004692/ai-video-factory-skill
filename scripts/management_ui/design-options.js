const concepts = {
  a: {
    name: '方案 A · 效率中枢',
    title: '方案 A · 效率中枢',
    summary: '为每天反复操作生产任务的人设计，用更少的视觉噪音换取更快的判断和执行。',
    principles: [
      ['01', '任务优先', '首页先回答“我现在该做什么”，指标退居辅助'],
      ['02', '稳定操作位', '主要动作始终位于标题区或表格末列，形成肌肉记忆'],
      ['03', '信息可扫描', '紧凑表格、状态色和抽屉详情适合高频批量处理']
    ],
    scores: [['清晰度', 96], ['操作效率', 95], ['内容沉浸', 76], ['扩展能力', 93]],
    total: 92,
    tradeoff: '视觉个性克制，但长期使用成本最低，最适合作为正式生产后台的基础方向。'
  },
  b: {
    name: '方案 B · 内容工作室',
    title: '方案 B · 内容工作室',
    summary: '围绕“一个选题如何成为一条作品”组织界面，让剧本、角色和成片更有创作上下文。',
    principles: [
      ['01', '作品中心', '页面呈现项目叙事，而不是一组相互独立的系统记录'],
      ['02', '编辑感', '暖灰纸张、衬线标题和克制橙色强调内容质感'],
      ['03', '沉浸审阅', '适合长文本、分镜、角色与成片的连续对照和审核']
    ],
    scores: [['清晰度', 88], ['操作效率', 84], ['内容沉浸', 97], ['扩展能力', 86]],
    total: 89,
    tradeoff: '作品感最强，但高密度任务监控不如 A/C；更适合创意团队而非纯运营团队。'
  },
  c: {
    name: '方案 C · 流程驾驶舱',
    title: '方案 C · 流程驾驶舱',
    summary: '为同时管理大量生成任务与发布结果的人设计，突出队列、产能、异常和成本。',
    principles: [
      ['01', '态势优先', '进度、故障、排队和成本占据首屏，快速定位阻塞点'],
      ['02', '异常聚焦', '深色低干扰底色配合少量高辨识语义色'],
      ['03', '实时反馈', '长任务持续展示当前步骤、耗时、重试与下一动作']
    ],
    scores: [['清晰度', 89], ['操作效率', 91], ['内容沉浸', 70], ['扩展能力', 95]],
    total: 88,
    tradeoff: '监控能力最强，但持续阅读剧本和配置表单时较疲劳，不宜把全部页面都做成深色。'
  }
};

const views = {
  workspace: {
    crumb: '生产中心 / 工作台',
    heading: '今天需要推进的内容',
    helper: '优先呈现会阻塞生产或需要人工判断的任务；其余数据按需展开。',
    body: () => `
      <div class="stats">
        ${stat('待审核剧本','7','2 条即将超时')}
        ${stat('生成中视频','3','预计 18 分钟完成')}
        ${stat('待发布成片','5','覆盖 2 个渠道')}
        ${stat('本月模型成本','¥286','预算使用 57%')}
      </div>
      <div class="workspace-grid">
        <section class="surface">
          ${surfaceHead('待我处理','按业务影响排序','查看全部')}
          <div class="task-list">
            ${task('审','神秘数字背后的设备租赁玄机','剧本已生成 · 等待审核','审核剧本','')}
            ${task('!','视频任务需要人工复核','J-TOPIC-4ae0 · 质量门未通过','查看原因','error')}
            ${task('发','3 条成片等待发布','抖音、微信视频号均已配置','去发布','warn')}
          </div>
        </section>
        <section class="surface">
          ${surfaceHead('生产链路','今天的实时流量','')}
          <div class="pipeline">
            ${pipeline('内容来源','12 条进入','82%')}
            ${pipeline('剧本审核','7 条待处理','58%')}
            ${pipeline('视频生成','3 条运行中','43%')}
            ${pipeline('发布反馈','5 条待发布','68%')}
          </div>
        </section>
        <section class="surface">
          ${surfaceHead('最近完成','只展示业务结果','')}
          <div class="timeline">
            ${timeline('成片已完成','设备租赁狗都不做 · 00:33','10:42','')}
            ${timeline('剧本审核通过','高空车租赁的三个误区','10:18','')}
            ${timeline('视频生成失败','未找到匹配的 BGM，已转人工处理','09:56','warn')}
          </div>
        </section>
        <section class="surface">
          ${surfaceHead('系统建议','基于最近 30 条内容','')}
          <div class="task-list">
            ${task('↗','“成本揭秘”题材完播更高','建议下一批选题增加同类结构','查看洞察','')}
            ${task('人','小玲角色表现稳定','最近 5 条角色一致性 94%','查看角色','')}
          </div>
        </section>
      </div>`
  },
  scripts: {
    crumb: '生产中心 / 内容与剧本',
    heading: '内容与剧本',
    helper: '从人工输入或自动抓取进入同一条标准化链路，审核通过后直接提交视频制作。',
    body: () => `
      <div class="button-row page-actions"><button class="button">自动抓取</button><button class="button primary">＋ 新增内容来源</button></div>
      <div class="flow-rail">
        ${flowStep('12','内容来源','今日新增 4 条')}${flowStep('8','选题简报','3 条待生成')}${flowStep('7','剧本候选','需人工审核')}${flowStep('4','已提交制作','流转正常')}
      </div>
      <div class="stage-tabs"><button>内容来源</button><button>选题简报</button><button class="active">剧本候选 <b>7</b></button><button>已审核</button></div>
      <section class="surface">
        <div class="toolbar"><div><input value="搜索标题或来源"><select><option>全部状态</option></select></div><span class="tag orange">7 条待审核</span></div>
        <table class="table"><thead><tr><th>剧本主题</th><th>来源</th><th>版本</th><th>预计时长</th><th>状态</th><th>下一步</th></tr></thead><tbody>
          ${scriptRow('神秘数字背后的设备租赁玄机','人工创建','反转版','33 秒','待审核')}
          ${scriptRow('高空车租赁的三个误区','竞品视频','专业版','42 秒','待审核')}
          ${scriptRow('老板为什么宁愿租也不买','行业热点','情绪版','28 秒','需修改')}
        </tbody></table>
      </section>`
  },
  videos: {
    crumb: '生产中心 / 视频制作',
    heading: '视频制作',
    helper: '清楚区分待生成、生成中、待复核和成品；长任务持续反馈当前步骤与预计时间。',
    body: () => `
      <div class="button-row page-actions"><button class="button">批量操作</button><button class="button primary">＋ 新建视频任务</button></div>
      <div class="stats">
        ${stat('待生成','4','已完成预检')}${stat('生成中','3','1 条即将完成')}${stat('待复核','2','质量门拦截')}${stat('本月成本','¥286','平均 ¥4.82 / 条')}
      </div>
      <div class="stage-tabs"><button>全部 18</button><button>待生成 4</button><button class="active">生成中 3</button><button>成品 9</button><button>异常 2</button></div>
      <section class="surface">
        <div class="toolbar"><div><input value="搜索任务或主题"><select><option>最近创建</option></select></div><span>自动刷新 · 8 秒前</span></div>
        <table class="table"><thead><tr><th>视频主题</th><th>当前步骤</th><th>进度</th><th>角色</th><th>预计成本</th><th>操作</th></tr></thead><tbody>
          ${videoRow('设备租赁的神秘数字','生成母片 2/3','72%','AI 自动生成','¥4.80')}
          ${videoRow('老板宁愿租也不买','本地导演剪辑','88%','小玲','¥3.62')}
          ${videoRow('高空车租赁避坑指南','等待生成资源','18%','老大','¥5.10')}
        </tbody></table>
      </section>`
  },
  publish: {
    crumb: '生产中心 / 发布与数据',
    heading: '发布与数据',
    helper: '以成片为对象选择渠道，发布后登记平台作品 ID，并持续回收播放、互动与转化反馈。',
    body: () => `
      <div class="button-row page-actions"><button class="button">渠道配置</button><button class="button primary">登记发布结果</button></div>
      <div class="stats">
        ${stat('待发布','5','3 条今日计划')}${stat('发布中','2','辅助发布')}${stat('已发布','26','本月累计')}${stat('平均完播率','31.8%','较上周 +4.2%')}
      </div>
      <div class="workspace-grid">
        <section class="surface">
          ${surfaceHead('待发布成片','选择后可批量发布','全选')}
          <div class="task-list">
            ${publishTask('设备租赁狗都不做','00:33 · 9:16','抖音  视频号')}
            ${publishTask('神秘数字背后的租赁玄机','00:40 · 9:16','抖音  视频号')}
            ${publishTask('高空车租赁的三个误区','00:42 · 9:16','视频号')}
          </div>
        </section>
        <section class="surface">
          ${surfaceHead('渠道状态','2 个渠道已就绪','管理')}
          <div class="task-list">
            ${task('抖','抖音','辅助发布 · 账号已配置','可用','')}
            ${task('视','微信视频号','辅助发布 · 账号已配置','可用','')}
          </div>
        </section>
        <section class="surface" style="grid-column:1/-1">
          ${surfaceHead('近期作品表现','累计绩效快照','查看分析')}
          <table class="table"><thead><tr><th>作品</th><th>渠道</th><th>播放量</th><th>完播率</th><th>互动率</th><th>更新时间</th></tr></thead><tbody>
            <tr><td><b>设备租赁狗都不做</b></td><td>抖音</td><td>18,420</td><td>36.4%</td><td>4.8%</td><td>今天 10:20</td></tr>
            <tr><td><b>老板为什么宁愿租</b></td><td>视频号</td><td>6,218</td><td>29.1%</td><td>3.9%</td><td>昨天 18:00</td></tr>
          </tbody></table>
        </section>
      </div>`
  },
  characters: {
    crumb: '资源 / 角色资产', heading: '角色资产', helper: '统一管理真人与火山虚拟人像；支持预览、背景描述、启停和使用关系查询。',
    body: () => `<div class="button-row page-actions"><button class="button primary">＋ 新增角色</button></div><div class="stats">${stat('全部角色','8','真人 3 · 虚拟 5')}${stat('已启用','6','可用于视频制作')}${stat('本月使用','21','覆盖 14 条视频')}${stat('待完善','1','缺少背景描述')}</div><section class="surface"><div class="toolbar"><div><input value="搜索角色名称"><select><option>全部类型</option></select><select><option>已启用</option></select></div></div><div class="empty-visual">角色卡片网格 · 缩略图、身份、背景与快捷操作</div></section>`
  },
  config: {
    crumb: '系统 / 配置中心', heading: '配置中心', helper: '只把阻塞主流程的项目列为必要配置，其余使用经过验证的默认值并渐进展开。',
    body: () => `<div class="button-row page-actions"><button class="button primary">保存配置</button></div><div class="flow-rail">${flowStep('✓','火山账号','视频生成可用')}${flowStep('✓','语音识别 2.0','本地视频可转写')}${flowStep('2','发布渠道','可按需配置')}${flowStep('✓','默认参数','已采用推荐值')}</div><div class="workspace-grid"><section class="surface">${surfaceHead('必要配置','2 / 2 已完成','')}<div class="task-list">${task('1','火山方舟账号','用于 Seedance 2.5 视频生成','已配置','')}${task('2','录音文件识别 2.0','用于本地音视频转写与字幕校准','已配置','')}</div></section><section class="surface">${surfaceHead('可选配置','不影响首次运行','展开')}<div class="task-list">${task('频','默认画质与并发','当前采用标准推荐值','默认','')}${task('发','抖音与微信视频号','仅在需要发布时配置','2 项','warn')}</div></section></div>`
  }
};

function stat(label, value, note) { return `<article class="stat"><span>${label}<i></i></span><b>${value}</b><small>${note}</small></article>`; }
function surfaceHead(title, sub, link) { return `<header class="surface-head"><div><b>${title}</b><small>${sub}</small></div>${link?`<a>${link} →</a>`:''}</header>`; }
function task(icon,title,sub,action,state) { return `<div class="task-row"><span class="task-icon ${state}">${icon}</span><span class="task-copy"><b>${title}</b><small>${sub}</small></span><button>${action} →</button></div>`; }
function pipeline(title,note,width){return `<div class="pipeline-row"><span><b>${title}</b><small>${note}</small></span><div class="bar"><i style="width:${width}"></i></div><em>${width}</em></div>`;}
function timeline(title,desc,time,state){return `<div class="timeline-row"><i class="${state}"></i><span><b>${title}</b><p>${desc}</p></span><time>${time}</time></div>`;}
function flowStep(value,title,note){return `<div class="flow-step"><b>${value}</b><span>${title}</span><small>${note}</small></div>`;}
function scriptRow(title,source,variant,duration,status){const cls=status==='需修改'?'red':'orange';return `<tr><td><b>${title}</b><small>今天 10:20 更新</small></td><td>${source}</td><td>${variant}</td><td>${duration}</td><td><span class="tag ${cls}">${status}</span></td><td><button class="button">审核 →</button></td></tr>`;}
function videoRow(title,step,progress,role,cost){return `<tr><td><b>${title}</b><small>J-TOPIC-${Math.random().toString(16).slice(2,10)}</small></td><td>${step}</td><td><div class="bar"><i style="width:${progress}"></i></div><small>${progress}</small></td><td>${role}</td><td>${cost}</td><td><button class="button">查看</button></td></tr>`;}
function publishTask(title,meta,channels){return `<div class="task-row"><input type="checkbox" checked><span class="task-copy"><b>${title}</b><small>${meta} · ${channels}</small></span><button>预览 →</button></div>`;}

const content = document.querySelector('#preview-content');
const crumb = document.querySelector('#preview-crumb');
let currentView = 'workspace';

function renderView(view) {
  currentView = view;
  const data = views[view] || views.workspace;
  crumb.textContent = data.crumb;
  content.innerHTML = `<div class="page-heading"><div><p>AI VIDEO FACTORY</p><h2>${data.heading}</h2></div><span>${data.helper}</span></div>${data.body()}`;
  document.querySelectorAll('#preview-nav button,.secondary-nav button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
}

function renderConcept(key) {
  const concept = concepts[key];
  document.body.dataset.concept = key;
  document.querySelectorAll('.concept-card').forEach(card => card.classList.toggle('active', card.dataset.concept === key));
  document.querySelector('#concept-name').textContent = concept.name;
  document.querySelector('#decision-title').textContent = concept.title;
  document.querySelector('#decision-summary').textContent = concept.summary;
  document.querySelector('#decision-principles').innerHTML = concept.principles.map(item => `<div class="principle"><span>${item[0]}</span><div><b>${item[1]}</b><p>${item[2]}</p></div></div>`).join('');
  document.querySelector('#total-score').textContent = `${concept.total} / 100`;
  document.querySelector('#score-list').innerHTML = concept.scores.map(item => `<div class="score-row"><span>${item[0]}</span><div class="score-bar"><i style="width:${item[1]}%"></i></div><em>${item[1]}</em></div>`).join('');
  document.querySelector('#decision-tradeoff').textContent = concept.tradeoff;
}

document.querySelectorAll('.concept-card').forEach(card => card.addEventListener('click', () => renderConcept(card.dataset.concept)));
document.querySelectorAll('#preview-nav button,.secondary-nav button').forEach(button => button.addEventListener('click', () => renderView(button.dataset.view)));
document.querySelectorAll('[data-width]').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('[data-width]').forEach(item => item.classList.remove('active'));
  button.classList.add('active');
  document.querySelector('#product-preview').classList.toggle('compact', button.dataset.width === 'compact');
}));

const previewParams = new URLSearchParams(location.search);
const initialConcept = concepts[previewParams.get('concept')] ? previewParams.get('concept') : 'a';
const initialView = views[previewParams.get('view')] ? previewParams.get('view') : 'workspace';
renderConcept(initialConcept);
renderView(initialView);
