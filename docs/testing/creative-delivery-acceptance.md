# 创意交付验收（3.2.0.7）

本切片在 `d99ea186` 的 `3.2.0.6` 基线上完成请求/更新预检、导演与关键内容闭环、管理台中断/修改/恢复及发布反馈。交付为直接 Skill 文件夹/ZIP；未合并 main、未推送、未更新正式服务。普通用户无需命令，不自动批准、付费或发布。

## 可验收行为

| 范围 | 验收契约 |
|---|---|
| 请求 | 首尾帧与 omni 互斥；edit 必须有视频；视频/音频各累计最多 30 秒，未知时长先停止/本地探测；默认 3 段为产品注意力预算，批准 4 段仍受时长约束；已有本任务付费 task_id 先 GET-only 恢复 |
| 更新 | 全文件复制、旧状态保留、版本化迁移标记、SQLite/WAL 一致性快照、Windows 长路径签名/复制/清理；非空目标冲突停止；事务阶段恢复，不覆盖/删除原数据 |
| 导演 | 稳定编号、可读描述、关键内容、运镜、保留/调整项及作者画外音，贯穿批准、冻结规格、导演 IR、编译和 prompt；输入是意图，不是观察 |
| 证据 | 实际媒体 SHA、要求哈希与盲检时间窗；关键证据缺失/无效转人工内容复核，模型不能豁免，不自动付费重试；ASR 后裁切、转场、成片顺序保留实际关键时间窗 |
| 本地重剪 | 实际源片、冻结完整计划、ASR 缓存 SHA 绑定/并发校验；完整预览确认后免费合成；保留音轨/BGM/付费图/账本；手动时间线改变需重新预览；重复/重启幂等 |
| 原生修复 | 有界实际源片、请求指纹、尝试与预算；同 SHA 持久归档用于一次 POST/回装；受管 MOV→MP4，完整候选保留原音频和关键内容；失败原始结果不可接受；已提交决定清理后仍幂等，原付费历史不变 |
| 管理台 | 保护当前审核轮次、选定候选、已接受版本、终审与发布引用；真实 HTTP 页面作内容审核、预览确认、中断恢复和最终审核 |
| 反馈 | 全部发布版本与一个当前版本；各版本/时间快照可空指标；冻结实际资产、意图、明确受控标签、来源、批准剧本 SHA；当前 B 修改不重写 A；未知/旧版不追补；实验精确已知冻结标签匹配 |

原生人脸编辑依赖供应商账号、模型和近 30 天素材资格。小数时长容差、内部 24—60 FPS 与 `¥0.07/千 tokens`、`¥0.68/1.53/3.44 每秒` 是项目历史校准/估算，非已核实当前资费。人工作品关联不证明上传或因果效果；没有新增自动平台统计采集器。无新契约旧任务保留兼容入口，不倒填已批准/付费快照。

## 切片证据

- Tasks 1–5 各有独立规格/质量复核，最终实现 `56bce62362ea84a4e7b7be223d95a7844201f250`。5B 规格在 `129e6bb` 有 7 项外部组合/12 项定向，`56bce62` 有 4 项清理回归；质量含 22 项定向（118.740 秒）/9 探针；有重叠，不能相加。
- 同实现 R0 25 项、R1 68 项、R2 全流程 3 项/旧浏览器/新默认临时目录浏览器、R3 12 项通过。新浏览器 204.031 秒、R3 10.032 秒；实际付费/外部请求 0，场景矩阵 10，不变量问题 0。此前 Win32 SQLite 清理失败不计通过，显式关闭连接/浏览器后才通过。
- 真实 Edge 旅程记录 3 次 HTTP 服务、至少 3 次 Supervisor-owned Worker，经过审核、中断重启、重剪、终审、发布/绩效。源/成片红像素约 16.14%/16.16%，合成正弦音频约 4.017125 秒、518/521 Hz，`speech_verified=false`。技术模式与音轨保留不证明真人、业务动作或 ASR。
- 较早 `f45` 候选对真实 `59556cf`（3.2.0.1）和 `d99ea18`（3.2.0.6）各 22 个不同检查；源码 SHA 与旧包不同，旧来源归档/历史审核/发布/反馈、944 分夹具模拟历史费用、重复更新/失败回滚/硬退出恢复保持。这些不能替代最终 3.2.0.7 ZIP，也不代表本轮真实付费。
- 较早宽列表 236 个入口中 235 通过、1 不存在模块加载错误，不能称全通过；纠正后 71 项定向通过。Task6 首次更新门禁 63 项中 2 失败/1 错误：长路径故障注入未命中逻辑路径，辅助模块裸导入失败。改为逻辑 promotion seam 和标准包导入，保留全部回滚断言，重跑 63 项通过（81.507 秒），付费/外部请求均 0。旧 edit 断言先 RED 1 项再 GREEN 1 项。

## 最终候选

运行实现保持 `56bce62`；文档/版本检查点 `52ee57c`，包内供应商说明经规格复核修正于 `2210372`。最终包包含 159 个文件、831016 字节，路径为 `.verification/releases/ai-video-factory-3.2.0.7.zip`，SHA256 为 `3272fdf410d95e6a0a8f376a5d16495da92dbcf347d87619851a5b707457591e`。它是下面最终升级旅程实际使用的 ZIP，不是只根据相同版本号认定的包。

| 验证 | 实际结果与边界 |
|---|---|
| 更新门禁 | 63 项通过，81.507 秒；实际付费/外部请求均 0。首次 2 失败/1 错误的日志保留，测试注入与导入修订不修改产品 |
| 稳定树全量 | 2036 项，1139.461 秒，1 错误、1 跳过、2034 通过，退出 1。唯一错误是固定声线 mux 测试使用不可解码 `b"voice"` WAV，被新时长预检正确拦截 |
| 唯一错误修复 | 独立 RED 1 错误（0.138 秒）；改为真实 1.2 秒/16kHz/单声道/16-bit PCM 静音，独立 GREEN 1 项（0.118 秒），整个声线模块和直接提交邻居 95 项通过（1.144 秒）。测试提交 `e554def`，未放宽产品预检、未伪造时长。没有重跑最终新测试树的 2036 项，不能称其全量通过 |
| 独立 R4 | 原门禁 R4 命令/护栏：28 项单元通过（269.646 秒），Smoke 6 条路径均通过，总 699.063 秒，退出 0；矩阵库存 10，付费/外部请求/不变量问题均 0。Flow Guard 自身为 `partial`，Full 专属维度跳过，不代表 Full |
| 最终真实旧包旅程 | 从 Git `d99ea1861b23a058e5ecdb97ed6b382e3a48fe49` 的真实 3.2.0.6 升级到最终 3.2.0.7 ZIP，同时做干净 Python/首次自举/HTTP/持锁检查；22 个不同检查、21 条命令或服务记录，退出 0，实际付费/外联 0 |
| 包检查 | 脱敏审计无发现；ZIP 完整性、源码逐文件字节绑定、Python AST、管理台 1 个 JavaScript 块的 Node 语法均通过；包含新运行模块与 runtime_filesystem，排除凭证/数据库/业务媒体/private paths/tests/docs/venv/git/worktree；只保留空 data/.gitkeep |

最终旧包旅程验证旧来源归档、历史审核/发布/反馈、944 分模拟历史账本、重复替换、失败升级回滚和更新器硬退出恢复。旧/新 `script_drafts.py` SHA256 分别为 `247bc07b494cd9784af52990b573be344b4bb6c87deccf05378bc6fded4dd3fa` / `ce9ac13f3d261feb186d33350050f549af034de2fc923bcb6cfc25807f7af64c`；不是改 VERSION 冒充旧包。首次在长证据路径运行的真实旧包因旧 pip/MAX_PATH 失败，保留失败报告；短目录重跑才通过。较早修正文档前的包结果独立保留，不替代本包。

运行源码指纹 `8f6d1aba6ee81b0b4bc35e2431f0d277aad64104a6375cad1a33d0c06f4c529f`，算法为排序 scripts 相对路径 + NUL + 实际文件 bytes + NUL，排除 pycache。R4 自身输入指纹为 `5386a7dcd5aba97b4b44e8f6f161222f0fbcd6b14a231206a9069964e77f6b45`，算法/输入范围不同，不能当作同一指纹。测试修订与本节补录不在 ZIP 中。原 `.verification/releases/ai-video-factory-3.2.0.6.zip` 保持 SHA256 `6a9562d569c18d063000c2561b00dc8856d9c3132a2033190c2c08e80694145f`。

## 可复核原始证据

- 最终包旅程：[report.json](D:/WORK/github/my-claude-ai/.codex-tmp/avf-t6d99b/report.json)。修正文档前较早候选位于 `avf-t6d99`，长目录失败报告位于 `factory-implementation-20261012/evidence/task6-skill-final-d99`，不混用。
- 包身份/源码与采用记录：工作树 `.verification/releases/creative-delivery-3.2.0.7-evidence.json` 和 `creative-delivery-archive-adoption.json`；早先 `2f659…` 初包和 `1abcf…` 旧候选分别另名保留为 `pre-journey` / `pre-provider-doc` ZIP。
- [全量原始日志](D:/WORK/github/my-claude-ai/.codex-tmp/factory-implementation-20261012/evidence/task6-discovery-final.log)、[95 项补验](D:/WORK/github/my-claude-ai/.codex-tmp/factory-implementation-20261012/evidence/task6-fixed-voice-green.log)、[R4 原始日志](D:/WORK/github/my-claude-ai/.codex-tmp/factory-implementation-20261012/evidence/task6-r4.log)、[更新门禁](D:/WORK/github/my-claude-ai/.codex-tmp/factory-implementation-20261012/evidence/task6-update-gate-green.log)。
- 首轮加载旧测试对象的全量被有界终止，没有最终结论，单独记录在 `task6-discovery-interrupted.json`；不是成功或失败结果。

全量使用隔离 `run_tests.py --root <worktree> discover`；95 项使用 `tests.test_character_voices tests.test_seedance_submission_contracts tests.test_video_upgrade.SeedancePayloadTests tests.test_video_upgrade.ReferenceAttentionTests tests.test_video_upgrade.MusicFallbackTests`。R4 使用旁路 `run_r4_only.py` 选择原 R4 命令，原 socket/凭证/状态签名护栏不变，不重跑已证实相同产品码 R0–R3。最终旅程入口为 `tests/skill_delivery_journey.py --legacy-ref d99ea1861b23a058e5ecdb97ed6b382e3a48fe49 --legacy-expected-version 3.2.0.6`，使用已有离线 wheelhouse 和短独立证据目录。

## 证明范围

浏览器使用高层合成 handler、预填意图和脚本终审，独立记录服务/UI/Worker 事实，非全正式 handler 严格 transport E2E。Python socket 护栏/凭证清除/本机路由限制不是操作系统进程树沙箱；本机已有 Python/FFmpeg，依赖来自预备离线 wheelhouse。整体 `strict_transport_replay`、`recognition_accuracy`、`actual_content_quality` 保持原边界，不因有限 PASS 升级。49 份既有媒体索引/接触表仅支持代表性视觉观察，不能推断音频、口型或动作成功率。真实试点需独立批准样本与预算。

相关：[Skill 更新](skill-delivery-acceptance.md)、[证据边界](assurance-boundaries.md)、[实施记录](stable-delivery-progress.md)。
