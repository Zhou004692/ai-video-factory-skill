# AI Video Factory Interaction and Visual Optimization Implementation Plan

> **For agentic workers:** Use `executing-plans` to implement this plan task-by-task in the current session. Steps use checkbox syntax. This document does not authorize paid generation, real task review, channel posting, or overwriting runtime data.

**Goal:** Make project status, next actions, content creation, production settings, media review, recovery and publishing consistent and usable, while preserving direct Skill distribution.

**Architecture:** Keep the existing production graph, ProductionService, recovery_plan and immutable review/publication versions. Add a pure presentation projection, split short database transactions from external IO, and converge the vanilla JavaScript UI into reusable workspaces and components. No React migration is required for these batches.

**Tech Stack:** Python HTTP server / SQLite / existing production services; static HTML, JavaScript and CSS; unittest, Playwright and existing zero-cost/update release gates.

**实施交付入口：** 3.2.0.8 候选，见 [交互与视觉验收](../../testing/interaction-visual-acceptance.md) 和 [稳定交付记录](../../testing/stable-delivery-progress.md)。以下保留原始实施清单；最终测试与包身份以该版本发布证据为准。

---

## 0. 基线、设计输入与执行方式

基线 main `cc094d7` / 3.2.0.7。设计输入为本机 `D:/WORK/github/my-claude-ai/output/ai-video-factory/2026-10-13-optimization-plan/01-整体优化方案.md` 和 `2026-10-13-ant-design-review/01-整体交互与视觉复盘.md`。本计划只建立任务，不代表功能已经实现。

下一次实施从最新 main 创建隔离 worktree；先比较已知基线与新 HEAD，吸收他人变更，不回滚用户已有编辑。正式服务、数据库、角色资产、费用和供应商任务均不作测试对象。使用隔离临时运行目录和端口，凭据缺省为空，付费/外联由 guard 截获。

当前 `management_server.py` 全 GET 与网络 sweeper 共用锁，媒体传输阻塞健康检查已经在隔离环境复现。正式服务首次超时的具体触发源仍未知，不把推断写成事实。

### 批次及依赖

```text
M0: T01→T02/T03→T04/T05
                 ↓
M1: T06→T07
                 ↓
M2: T08→T09
                 ↓
M3: T10→T11→T12
                 ↓
M4: T13→T14
```

T02 与 T03 属独立问题，但默认在本会话顺序执行。每批单独提交和验证，不因下一批未完成阻止已验证批次交付；不可将未通过项移成绿色。

## 1. 文件职责与改动边界

| 文件 | 责任 | 批次 |
| --- | --- | --- |
| `scripts/management_server.py` | 路由、鉴权、health/ready、媒体传输锁边界与请求证据 | M0 |
| `scripts/factory/management_api.py` | 将现有业务查询/命令接入统一展示；盘点并迁出读路径副作用 | M0–M3 |
| `scripts/factory/services/production_read_model.py` | 读取已有图/节点/台账事实，不做生产变更 | M0 |
| 新 `scripts/factory/services/production_presentation.py` | 状态、活动、门禁/阻塞、错误证据与合法动作投影；不引入持久状态机 | M0 |
| `scripts/factory/services/production_service.py`、`recovery_plan.py`、`provider_tasks.py` | 仅针对命令身份/现有恢复动作缺口补齐；不大范围重写 | M0 |
| 新 `scripts/management_ui/api-client.js` | 查询等待期限、取消/过期响应、提交待核对错误 | M0 |
| 新 `scripts/management_ui/presentation.js` | 消费统一 DTO，业务名称与投影渲染；不推断生产状态 | M0 |
| 新 `scripts/management_ui/ui-tokens.css`、`ui-components.js/css`、`workbench.js/css` | 主题、弹层/焦点/固定操作区、布局与通用对象工作区 | M1/M4 |
| 新 `scripts/management_ui/production-settings.js` | 分组设置、预检结果、费用确认与草稿保护 | M2 |
| 新 `scripts/management_ui/review-workspace.js/css` | 播放器同屏、候选/内容/成片审核与路线选择 | M3 |
| `scripts/management_ui/app.js/index.html/styles.css/studio-preview-parity.css` | 渐进替换接线；确认无人使用后移除旧覆盖，不一次性拆全文件 | M0–M4 |
| `scripts/factory/content_review.py/visual_repair.py/native_visual_repair.py` | 已有审核与本地/原生修复合同复用；只有验证发现缺口时改动 | M3 |
| `scripts/factory/performance_feedback.py` | 已有不可变发布与反馈绑定复用 | M3 |
| `scripts/distribute_skill.py`、`tests/test_skill_distribution.py` | 新静态资源进入实际ZIP，运行数据/凭据不进入 | M4 |
| `tests/test_management_console.py/test_management_workflows.py/test_creative_delivery_journey.py` | 扩展现有业务边界回归 | 各批 |
| 新 `tests/test_management_http_concurrency.py/test_production_presentation.py` | 并发与真实展示合同 | M0 |
| 新 `tests/interaction_visual_browser_check.py` | 隔离浏览器交互、响应式、焦点与错误旅程 | M1–M4 |
| `README.md/docs/testing/stable-delivery-progress.md`、新 `docs/testing/interaction-visual-acceptance.md` | 版本、操作与证据边界，不追溯修改旧证明范围 | M4 |

新增前端脚本采用本地静态文件和明确加载次序：tokens/components/API/presentation/workbench 专用模块在 app 初始化前接入。优先使用显式命名空间，避免继续全局覆盖同名函数。引入任何新文件都更新白名单打包与真实ZIP测试。

## 2. M0：访问、状态与合法动作

### T01：固定审计问题的隔离回归与事实合同

**Files:** 新 `tests/test_management_http_concurrency.py`、`tests/test_production_presentation.py`；复用 `tests/fixtures/management_factory.py` 和现有临时数据库工具。

- [ ] 创建无凭据临时 runtime、随机回环端口、可控 provider；真实端口8765不能作为默认测试目标。
- [ ] 将审计慢媒体场景转为 HTTP 回归：临时文件至少32MiB，客户端收头后停止消费；健康检查和普通只读查询并发发起。
- [ ] 为锁外后台核对建立 Event 控制的慢响应；等待期间只读查询可响应；旧版本回写被拒绝。
- [ ] 固定展示场景：STORYBOARDED+PENDING、锚点待审+PENDING、预算暂停、未知提交、有效排队/租约、父任务暂停但其他远端节点已受理、无图旧状态。
- [ ] 运行新增测试，保留当前失败证据；不要为了 RED 修改生产业务数据。

验收数据合同（每种输入都实际调用被测投影后比对，不只检查期望值本身；尚未实施）：

```json
{
  "planned_pending": {"queued": 0, "running": 0},
  "anchor_review_pending": {"queued": 0, "running": 0},
  "submission_unknown": {"may_resubmit": false},
  "legacy_without_execution_evidence": {"activity_known": false},
  "plan_complete_no_media": {"can_advance": false}
}
```

Run: `python -X utf8 -m unittest tests.test_management_http_concurrency tests.test_production_presentation -v`。当前应在所记录问题处失败；T02/T03后通过，所有测试均无付费/外联。

### T02：拆分 HTTP 与后台外部 IO 的锁范围

**Files:** `scripts/management_server.py`、`scripts/factory/management_api.py`、`scripts/factory/services/production_read_model.py`；Test：T01 与 `tests/test_server_security.py`。

- [ ] 列出 GET 方法的 DB读取、恢复写入、网络请求与媒体传输；为带副作用的 GET 明确迁移目标，不能仅删除锁。
- [ ] 将静态/health 路由与业务锁分离；ready 查询使用短且有等待边界的本地读，不测试外部模型。
- [ ] 媒体路径/权限在短读边界解析成不可变元数据，释放业务锁后打开受控文件并流式响应；保留 Range、Server会话与路径校验。
- [ ] 后台核对与来源抓取改为「短锁取快照→锁外 IO→短锁校验并回写」；保护现有连接、事务和租约，拒绝过期任务/规格结果。
- [ ] 将诊断耗时记录为非敏感事件，至少含步骤、持续时间和关联内部对象；不得记录API key或签名URL全文。
- [ ] 运行并发与安全回归；单个慢媒体持续30秒期间多次查询，关闭连接后资源可释放。

处理顺序合同：

```text
authorize_request
  → short_read(path, version, allowed_root)
  → release_database_lock
  → validate_and_open_media
  → stream_range_without_business_lock

short_read(provider_task_id, spec_version, lease)
  → inspect_remote_outside_lock
  → short_transaction(recheck_same_identity, apply_result_if_current)
```

Run: `python -X utf8 -m unittest tests.test_management_http_concurrency tests.test_server_security tests.test_management_console -v`。

完成条件：health<1秒、小规模只读列表<2秒是本机隔离验收目标；慢媒体/核对期间无全局等待，业务事务不交错损坏，未授权媒体依旧拒绝。定位根因并更新证据后提交 `fix: isolate management reads from media and external IO`。

### T03：统一展示读模型、阶段门禁与待办统计

**Files:** 新 `scripts/factory/services/production_presentation.py`；修改 read_model/management_api；新 `presentation.js` 接入 app。Test：`tests/test_production_presentation.py`、现有管理工作流。

- [ ] 冻结 presentation schema_version=1，分别给出活动、计划、制作完成、审核、真实 gate/blockers、observed_at；保留现有 next_action/allowed_actions。
- [ ] 从真实队列/租约/供应商受理生成活动；只规划或暂停的PENDING不计入活动，未知旧状态标未知；父暂停下仍运行的已受理节点保持真实事实。
- [ ] stage gate调用已有后端规则，不用前端锚点数量或score放行；展示标签「计划3/3、母片0/2、待确认人物」。
- [ ] todos返回稳定item_id和item_count/project_count；角标、待办列表和范围标题消费同一查询口径。
- [ ] 所有入口渲染此合同；旧字段或旧记录缺失时保守显示历史待核实，不重新生成数据。
- [ ] 覆盖审计14条假活动与52/8计数问题，再覆盖有效活动和去重；验证查询不改变业务表。

输入/输出合同：

```json
{
  "facts": {"plan_passed": 3, "plan_total": 3, "master_done": 0, "master_total": 2, "queue_record": null, "lease_valid": false, "remote_task": null, "anchor_review_required": true},
  "expected": {"activity": {"queued": 0, "running": 0, "remote_accepted": 0}, "can_advance": false, "label": "等待人物锚点确认"}
}
```

Run: `python -X utf8 -m unittest tests.test_production_presentation tests.test_management_workflows tests.test_management_console -v`。

完成条件：五种入口同对象状态/主动作一致；纯读不变更数据；既有恢复与费用规则不弱化。提交 `feat: unify production presentation and todo counts`。

### T04：请求期限、过期响应与不确定提交

**Files:** 新 `api-client.js`、修改 `app.js/index.html`；Test：新浏览器旅程的请求场景、现有生产命令测试。

- [ ] JSON查询默认15秒等待上限、2秒等待说明；保留上一成功内容与时间，提供局部重读。
- [ ] 使用AbortController和对象请求序号；切换对象取消旧读，旧响应不能覆盖新对象。
- [ ] 区分查询失败、业务命令明确失败、业务命令结果未知。后者保留命令身份并进入核对。
- [ ] 盘点start/resume/修复的命令身份与供应商task关联；已有身份复用，缺口补后端参数摘要/幂等校验后才能开放重提。
- [ ] 通过受控网络注入慢响应、断连、JSON错误、服务器400/409和已受理超时；验证表单保留和重复按钮抑制。

浏览器合同：

```text
query A delayed → select B → response B applied → response A ignored
paid command accepted → browser wait times out → show reconcile action
reconcile finds same provider task → resume existing task → submit_count stays 1
explicit validation rejection → preserve form → show field/error summary
```

完成条件：请求等待有边界；未知提交不能被普通retry重发；读取重试不触发生产。提交 `feat: add recoverable request feedback and submission reconciliation`。

### T05：异常摘要与恢复动作一致性

**Files:** presentation/management_api/app、`recovery_plan.py`；Test：presentation、management_workflows。

- [ ] 读取事实错误来源：节点、供应商、下载、本地处理和审核；摘要保留具体失败步骤及来源。
- [ ] 将内容策略、账号、网络未知提交、预算、下载、候选审核、内容缺失映射至已有合法恢复动作；冲突证据显示待核实。
- [ ] 返回主操作/目标/禁用理由、是否付费与范围；普通用户看到业务名称，内部ID折叠可复制。
- [ ] 待前序操作直达相应人物/场景/母片；没有可执行动作时显示诊断结果和明确人工处理项，不能空转。
- [ ] 每种原因走一条隔离成功与失败分支；失败保留当前素材、成本、表单与定位。

验收案例：版权拒绝不能显示处理账号为唯一动作；已成功远端输出的下载失败优先恢复下载；不明提交不能重新提交；重剪能解决的问题不默认全量生成。

Run: `python -X utf8 -m unittest tests.test_production_presentation tests.test_management_workflows tests.test_creative_delivery_journey -v`。

M0完成后保存本批证据、接口合同和兼容检查，独立交付。没有同时实施M1也可以修复访问和状态问题。

## 3. M1：工作区框架与交互组件

### T06：基础 tokens 与响应式工作区

**Files:** 新 ui-tokens.css/workbench.js/css；修改 index/app/styles/parity。Test：新 `tests/interaction_visual_browser_check.py`。

- [ ] 先定义主色356BFF、侧栏203251、正文14px/辅助12px、4/8间距、组件尺寸和语义状态tokens；不改变业务状态。
- [ ] 明确导航职责：内容工作区、统一待办为主，专业工具明确菜单；六阶段只表示项目位置。
- [ ] 1440/1920保留多栏；1280/1366默认检查栏折叠；1024项目栏抽屉化；<1024显示明确桌面操作提示。
- [ ] 选中项目/对象/阶段与返回位置可恢复；不通过点击阶段按钮隐式回退/提交。
- [ ] 浏览器测各视口正文、主按钮、媒体和右侧边界；覆盖长标题与多角色内容，保存截图与实际DOM尺寸。

合同：

```json
{"viewports":[[1920,1080],[1440,900],[1366,768],[1280,800],[1024,768],[549,970]],"required":["main_action_reachable","no_clipped_critical_content","readable_status_text","narrow_screen_message"],"critical_font_min_px":14,"metadata_font_min_px":12}
```

完成条件：不出现原549空白或1024右栏裁切，提升字号与重新分栏同时通过。提交 `feat: make project workspaces readable across desktop sizes`。

### T07：焦点、草稿与固定操作区

**Files:** 新 ui-components.js/css；修改openModal/openDrawer/任务浮层；浏览器检查。

- [ ] 为弹层实现初始焦点、Tab/Shift+Tab圈定、背景inert、关闭后焦点恢复、dialog命名与可见焦点。
- [ ] 不删除现有Esc支持；对长表单建立统一dirty状态，Esc/遮罩/换项目触发保留/放弃/继续编辑选择。
- [ ] 提交失败保持字段值和媒体位置；提交中禁双击，危险动作附对象和范围。
- [ ] 任务浮层在弹层打开时隐藏或移至无冲突位置；固定底部预留内容间距。
- [ ] 覆盖Tab循环、焦点恢复、输入后Esc、失败后输入保留及抽屉底部碰撞。

合同：`modal_open → activeElement inside dialog`；连续Tab始终位于弹层；关闭回触发入口；dirty关闭不得静默丢失；任何任务浮层与有效底部按钮的矩形交集为0。

M1可独立验收发布；此时旧设置和审核功能仍可运行，只替换容器行为。

## 4. M2：内容创作与制作设置

### T08：内容/导演版本与修改影响

**Files:** workbench/app、现有management_api的来源/简报/剧本能力；Test：management_workflows、creative_ir、creative_delivery_journey。

- [ ] 来源、角度、剧本默认选中单一对象；版本比较独立展开，避免四区长期平均分割。
- [ ] 场次显示台词、画面意图、时长、入镜人物、声音说话人及关键证据；用可读名称显示角色。
- [ ] 编辑保存产生新草稿；显示受影响人物/锚点/母片/时间线/预算/审核，批准绑定具体意图版本。
- [ ] 覆盖三人同框、同一业务员复用、旁白、无人物、场景移动；复用已有角色规范，不新造显示层身份。
- [ ] 版本冲突时保留编辑并提供差异，不覆盖后端新版本或已冻结生产规格。

Run: `python -X utf8 -m unittest tests.test_management_workflows tests.test_creative_ir tests.test_creative_delivery_journey -v`。

完成条件：来源事实、导演意图和实际制作规格可追踪；修改影响和下一步清楚，旧任务不被隐式重建。

### T09：设置抽屉、预检与单次费用确认

**Files:** 新 production-settings.js；components/workbench/app；production_config和预检仅有缺口才改。Test：management_console/management_compiler_api。

- [ ] 角色、场景、声音、输出、预算五组，复杂设置使用880–960px自适应大抽屉；底部费用和主操作固定。
- [ ] 场景/人物名称和所影响场次可读，生成方式差异不截断；无人物任务不强制角色字段。
- [ ] 声音/锚点方案切换依赖与估算；能力来自冻结模型，保留既有测试26参考上限修复。
- [ ] 保存并预检与付费确认分离；明确离线预检、显式连接诊断和付费生成各自含义。
- [ ] 二次提交校验规格版本与预算，未知提交进入核对；成功素材默认复用，不全选重做。
- [ ] 浏览器走设置不完整→定位修正→预检→返回修改→预检失效→确认指定范围；统计实际paid_calls=0的拦截证据。

Run: `python -X utf8 -m unittest tests.test_management_console tests.test_management_compiler_api tests.test_provider_runtime_integration -v`。

M2完成条件：费用摘要始终可见；保存设置不自动付费；各模型/声音/人物组合规则与后端一致。

## 5. M3：审核、修复与发布回流

### T10：同屏素材与关键内容审核

**Files:** 新 review-workspace.js/css；app接线；复用content_review。Test：content_review、creative_delivery_journey。

- [ ] 三类审核使用统一播放器容器：锚点/母片、关键内容、成片；版本与摘要保持可见。
- [ ] 要求点击可seek区间；自动观察、人工状态、实际描述与豁免原因分别显示。
- [ ] 暂存草稿与正式结论分离；素材/要求摘要变化后保留旧依据并明确失效，不直接复用旧通过。
- [ ] 保存内容结论后单独接受候选；未接受前不替换当前文件。
- [ ] 去除正式验收一键全部通过，关键内容和六项质量各自审核，媒体失败不能确认可播放。
- [ ] 覆盖选择旧/当前/候选、保存失败、版本冲突、区间非法、证据不足与人工豁免。

Run: `python -X utf8 -m unittest tests.test_content_review tests.test_creative_delivery_journey -v`。

完成条件：播放器与检查清单同屏；当前/候选版本身份不混淆；保存结论与接受/成片放行各有明确结束状态。

### T11：修复路线与本地重剪恢复

**Files:** review-workspace/app；复用 `visual_repair.py` 中的本地修复与 `native_visual_repair.py`。Test：相应unit与creative旅程。

- [ ] 展示后端推荐与全部可用/不可用路线，解释范围、原因和费用；选路不触发模型调用。
- [ ] 本地重剪生成可播放预览，再明确选择是否采用；关键证据被剪掉时继续阻止成片放行。
- [ ] 原生编辑/延长按单次预算确认，失败保留原文件与候选，不全量重跑。
- [ ] 接受新结果重新审核内容和合成保留；返回审核保留所选要求/区间/位置。
- [ ] 覆盖已有素材足够、缺真实画面、路线不可用、调用已受理但客户端失联、修复完成却不符合要求。

Run: `python -X utf8 -m unittest tests.test_local_visual_repair tests.test_native_visual_repair tests.test_creative_delivery_journey -v`。

完成条件：完整走到本地合成或明确等待人工处理，零费用旅程不替代真实修复效果声明。

### T12：成片、渠道结果与效果版本闭环

**Files:** app/workbench；management_api、performance_feedback现有发布/快照合同复用。Test：management_workflows/creative_delivery_journey。

- [ ] 成片审核绑定当前文件；通过才能选择该版本发布。
- [ ] 付费生成与外部投稿分开的确认；复核渠道、账号、标题、封面和版本。
- [ ] 投稿/平台审核/已发布/拒绝独立展示，辅助渠道下载后登记作品依据；历史未核实不计成功。
- [ ] 指标录入明确观察时间/来源，unknown=null，保留历史快照与实际成片意图。
- [ ] 隔离模拟两个渠道、一个失败和一个成功，验证不互相覆盖；修剪后不能沿用旧成片审核发布。

Run: `python -X utf8 -m unittest tests.test_management_workflows tests.test_creative_delivery_journey -v`。

M3完成条件：审核→接受→本地合成→成片验收→投稿/結果→效果，在同一项目上下文完整可达；不真实投稿、不以合成平台返回证明平台实际成功。

## 6. M4：视觉收敛、完整验收与Skill交付

### T13：八类页面统一组件与覆盖清理

**Files:** tokens/components/workbench/app/styles/parity；资产与配置现有页面；browser check。

- [ ] 将Button/Form/Table/List/Tag/Alert/Empty/Loading统一到tokens和行为规范；只保留当前业务需要的组件。
- [ ] 统一状态文字/图标/颜色，去除技术级别作为业务主标题；实际配置、最近检测与任务预检分离。
- [ ] 角色资产占位说明类型，操作位置一致，禁删有原因；长内部编号次级/复制。
- [ ] 对八类页面和全部关键弹层检查14/12px、焦点、对比度、危险操作、失败状态与固定底部。
- [ ] 逐条证明确认旧CSS/函数未使用再删除；不以一次大规模改文件掩盖行为差异。
- [ ] 输出截图基线、DOM尺寸与问题清单；没有实际观察的页面不能标通过。

完成条件：同类组件一致、旧CSS依赖已验证；回归覆盖与真实素材质量声明分开。

### T14：实际包与旧状态升级旅程

**Files:** distribute_skill/index资源版本；test_skill_distribution/video_upgrade/skill_update_release_gate；README与验收记录。

- [ ] 更新打包允许资源清单，检查所有新JS/CSS进入实际ZIP；依赖顺序与离线静态访问通过。
- [ ] 新包首次启动与真实旧包替换使用隔离 runtime，SQLite在线一致快照；保留费用、供应商task、媒体、候选、审核、发布与反馈。
- [ ] 当前管理台返回的服务版本与实际JS/CSS摘要匹配；仅改版本文字不算更新成功。
- [ ] 使用同一最终HEAD和实际ZIP跑相关单元、完整用户交互、零费用和更新门禁；一次全量作为最终回归，之后只有变更/失败才重跑相关范围。
- [ ] 记录测试总数、失败/跳过、运行边界、完整日志与SHA；原有FlowGuard partial不能因本轮UI通过升级为Full。
- [ ] 整理独立付费小样本候选及预算（若需要），不在此离线计划自动执行；真实效果与平台投稿另验。
- [ ] 备份并在活动任务条件允许时进行正式更新；推送/部署按当时授权执行，不能从本次方案请求推定自动发布。

已存在门禁命令：

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py"
python -X utf8 tests/zero_cost_release_gate.py --level all
python -X utf8 tests/skill_update_release_gate.py
```

预期：所需测试无失败；zero-cost输出 `paid_provider_calls=0 external_requests=0`；更新门禁通过且明确实际ZIP身份。这些是未来实施验收命令，当前编写计划没有运行或宣称通过。

### 浏览器验收命令合同

新 `tests/interaction_visual_browser_check.py` 在 T01/T06 建立 CLI，并由脚本启动自己的临时服务：

```powershell
python -X utf8 tests/interaction_visual_browser_check.py --work-root .verification/interaction-visual/m0 --batch m0
python -X utf8 tests/interaction_visual_browser_check.py --work-root .verification/interaction-visual/m1 --batch m1
python -X utf8 tests/interaction_visual_browser_check.py --work-root .verification/interaction-visual/m2 --batch m2
python -X utf8 tests/interaction_visual_browser_check.py --work-root .verification/interaction-visual/m3 --batch m3
python -X utf8 tests/interaction_visual_browser_check.py --work-root .verification/interaction-visual/final --batch all
```

参数与脚本属于待实施计划，现阶段不可直接执行。输出合同：summary.json含batch、source_head、isolated_runtime、paid_provider_calls、external_requests、business_invariant_changes、scenario_results；场景分别passed/failed/not_run，截图guard阻止采集时注明blocked，不冒充截图检查通过。关闭自建浏览器/服务，保留用户原有进程和页面。

## 7. 批次完成与回退

| 批次 | 独立验收包 | 可回退范围 |
| --- | --- | --- |
| M0 | 并发复现前后、纯读摘要、状态与命令身份合同、错误路线、单元/浏览器日志 | 回退代码时不清空提交台账；活动任务先核对，保持现有任务身份 |
| M1 | 六视口截图、键盘与关闭保护、固定区域碰撞、导航返回旅程 | 可回退布局资源；数据状态不回退，局部草稿明确保留/导出 |
| M2 | 内容版本影响、无人物/多人、设置预检、预算与重提保护 | 保留新生成的草稿/冻结规格；旧代码不兼容时禁止直接继续生产，使用前进修复 |
| M3 | 真实素材复核+隔离完整旅程、候选/内容/成片/发布版本摘要 | 不回滚已发生审核/受理/平台结果，不用旧DB覆盖新账本 |
| M4 | 最终代码/包SHA、首次启动/替换、全量/离线门禁和证明边界 | 保留更新备份，先查schema兼容；无法向后兼容时前进修复或只读维护 |

每次里程碑报告包含完成内容、实际验证、失败/未验证、当前代码与包身份以及下一批入口。不得只报“代码已写完”或累计历史通过项。

## 8. 自复核清单

- [ ] UI-01→T01/T02/T04，UI-02/03/05→T03，UI-04→T05，UI-06/11→T06，UI-07→T09，UI-08→T10，UI-09/10→T07，视觉→T13，更新→T14，均有交付与验收。
- [ ] 核对现有文件/命令，新文件/参数/DTO明确为待实施，不能说已存在或已通过。
- [ ] 正常、空、加载、失败、禁用、保存失败、中断、恢复和更新均覆盖；所有付费与外部发布动作显式确认。
- [ ] 本地快照与合成provider证据不会被当作真实生成效果/平台成功；首次超时根因仍保留未知范围。
- [ ] 不引入Windows安装包；使用方不增加Node构建要求；敏感运行数据不进入Skill或公开报告。
