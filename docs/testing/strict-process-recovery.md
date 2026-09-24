# 已完成母片的跨进程素材恢复

本切片已在 `77df18d`（P1 传输诊断）基线上重新集成，验证一条旁白母片从下载损坏到管理台恢复、正式 Worker 重跑、可播放成片和最终审核的链路。它不是全产品严格端到端验收，也不证明识别准确率或内容质量。

## 生产行为

- `resume_failed_video` 仅对同一任务、冻结规格及母片绑定的已完成 Seedance 任务开放下载恢复。节点、镜头和队列状态在写事务内更新；冻结内容不变。
- 新 Worker 读取持久化的 `resume_provider_task_id`，执行原任务的 GET 与下载，继续正式 QA、BigASR 和本地合成；不执行新的 Seedance 生成 POST，也不在恢复中自动重做质量失败的母片。
- 原生成费用通过既有供应商任务账本保持幂等。回放账本为 7 分，实际外部调用和供应商费用为 0；这不是实际供应商收费评估。
- 混合已付费与未生成的待处理母片、任务绑定不匹配、完成证据缺失或损坏时，恢复原子拒绝，不回落到旧生成路径。用户需要先处理明确提示的缺口。
- Worker 在远端响应返回后、完成记账前突然退出时，持久化任务仍未确认完成。停止任务只要存在未核对远端任务，就先进入 P0 的 Provider 核对入口；`resume_failed_video` 不再承担状态迁移。政策失败及无任务证据也不会因此获得重新生成权限。此用例仅证明正确暂停，不证明未知远端结果已恢复。
- 管理台对已排队恢复直接展示进度，不再打开付费生成确认框。

## 执行与证据

测试使用临时文件 SQLite、合成媒体及固定传输契约。管理台 HTTP handler、服务方法、队列派发、`cli.py produce-graph --dispatched`、Graph 节点领取和业务 handler 均执行正式代码。

| 阶段 | 实际执行 | 断言 |
|---|---|---|
| 第一个 Worker | 一次生成提交、两次轮询、损坏媒体下载，共 4 步 | 母片失败；供应商任务及 7 分账本保留 |
| 管理台恢复 | 浏览器点击 → 正式 `/resume` API → 正式队列派发 | 已排队；重复恢复被拒绝；刷新不出现付费确认 |
| 第二个 Worker | GET 原任务、下载、TOS、QA、ASR，共 9 步；本地 FFmpeg 合成 | Worker 执行 PID 不同；母片与合成节点成功；无第二次生成 POST |
| 最终审核 | 浏览器播放预览并提交六项脚本审核 | 媒体 HTTP 200/206、可读视频尺寸；状态到 `READY_TO_PUBLISH` |

Worker 通过测试专用临时 `sitecustomize` 安装传输回放，启动参数严格限定为指定 CLI 任务。Windows venv 可能先启动重定向进程，因此分别记录启动器 PID、实际 Worker PID、父 PID 和 Supervisor PID，不要求启动器 PID 等于执行 PID。

固定 ASR UUID 只替换 BigASR 模块的引用，不再改写 Python 全局 UUID，以免跨进程节点领取事件重复。Windows proactor 的私有唤醒通道在传输护栏安装前创建，业务网络连接仍受限制。

回放进程通过环境白名单启动，测试期间关闭 `.env` 自动加载，并将服务的配置路径指向临时目录，避免安装目录中的配置进入回放。Windows 平台信息在护栏安装前缓存，避免 TOS 初始化触发额外的系统探测命令。浏览器回放超时后，仅清理本次启动的进程树；清理失败保留诊断，超时始终按失败计。

## 边界

- Python Worker 拒绝未知 socket、DNS、传输请求及未登记的媒体命令；Supervisor 限定数据库和 Worker 启动入口。浏览器 HTTP 只允许本次临时服务，禁用 Service Worker。
- 这些约束不是操作系统沙箱，未验证浏览器或原生子进程所有可能的网络入口，`process_tree_verified=false`。不得把回放计数为零解释为操作系统层的完整外连证明。
- 本场景没有人物/场景锚点、来源解析、简报或剧本生成；保留原有单进程旅程作为独立证据。
- 六项审核由测试脚本勾选。播放成功证明媒体传输与可解码性，不证明口型、身份、表演或剪辑效果。
- Flow Guard Full 的 `strict_master_recovery` 单独记录本切片及源码指纹；失败或证据缺失会影响 Full 结果。整体 `strict_transport_replay`、`recognition_accuracy`、`actual_content_quality` 继续为 `not_run`。

## 验证入口

```powershell
python -m unittest tests.test_master_download_recovery tests.test_strict_process_recovery -v
python tests/strict_process_browser_check.py --work-root <临时证据目录>
python -m unittest tests.test_flow_guard_strict_recovery -v
python scripts/flow_guard.py run --suite full --report <本机报告路径>
```

阶段回归结果记录于 [稳定交付实施记录](stable-delivery-progress.md)。

当前 `3.2.0.4` 集成在 P1 基线上完成定向回归和内置执行器复验。内置执行器绑定的源码指纹为 `8ba6261ae6547fa032a7b60716889b771fbdcf58e8a5d38cc296396a6d96ec97`，结果为 `READY_TO_PUBLISH`、真实付费调用 0、外部请求 0。该指纹只绑定 `scripts` 与 `tests` 中参与回放的源码，不替代 Git 提交身份。
