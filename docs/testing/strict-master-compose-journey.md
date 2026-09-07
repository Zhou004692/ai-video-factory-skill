# 母片到导演合成正式处理器回放

状态：实现完成，独立规格审查与代码质量审查均通过。实现提交 `c825edc`、`ea61ae7`、`7d27ca1`、`9ca049f`。

本批补齐“正式母片生成 → 质量判断 → ASR → 本地导演合成 → 最终审核”的严格回放。它保留生产编译器、冻结图、节点状态机和正式业务 handler，只在最终外部传输边界使用固定响应；不调用在线模型，也不证明真实视频的识别准确率或内容质量。

## 验收对象

真实 Production Spec v2 编译与冻结图 → 临时文件 SQLite → 正式节点领取与 `build_handlers` → Seedance 提交、轮询和下载 → TOS 签名与上传 → 豆包视频 QA → BigASR → 媒体 SHA 绑定的识别派生产物 → 本地 FFmpeg 导演剪辑与无声 BGM → 最终审核 → `READY_TO_PUBLISH`。

| 边界 | 保留内容 | 允许替换 |
|---|---|---|
| 生产图与状态 | 冻结规格、依赖、租约、节点转换、任务投影和审核事务 | 独立临时数据库与资产目录 |
| Seedance / QA / ASR | 正式客户端、请求结构、响应解析、质量决策、台词对齐 | 固定的 HTTP transport 响应与测试轮询等待 |
| 对象存储 | 正式 SDK、签名、HEAD、上传和响应解析 | 固定 Requests transport、假凭证和固定时间 |
| 本地媒体 | 正式导演合成、字幕、音轨和 FFmpeg/ffprobe 命令 | 固定摘要的 64×64、4 秒可解码合成视频与无声 BGM |
| 费用与审核 | 正式预算、7 分模拟账本、候选恢复、最终审核事务 | 不消耗真实供应商余额 |

固定契约校验方法、完整 URL、嵌套 JSON 结构、关键数组长度、选定业务字段和媒体摘要。它不是对整段提示词做逐字相等，也不应用来约束无关文案演进。

## 已验证的正向闭环

1. 正式导演模式 `native_master_local_director` 从冻结图生成单条有声母片，并经 QA、ASR 和本地导演合成进入 `COMPOSED`。
2. 正常路径固定执行 11 个外部传输步骤，只产生一次 Seedance 付费提交和一次 BigASR 提交；模拟费用为 7 分，实际外部请求与费用为 0。
3. ASR 结构化结果按 `job_id + master_idx + media_sha256` 持久化。导演合成只复用与当前媒体字节匹配的结果，不从可变 storyboard 猜测来源。
4. ASR 成功但返回空文本、零 utterance 时仍记录为“已识别”；合成阶段不再次调用 ASR，并清除冻结规格中的估算字幕和字幕 cue。
5. 最终审核在同一写事务中复核节点状态，写入审核事件、节点事件和任务投影。失败或过期节点不能批准；并发相反决策只允许一个提交。
6. 相同冻结图重复进入不新增请求、子进程、费用或审核事件。

## 已验证的失败与恢复

- QA transport 失败不会被当作质量通过；候选和已发生费用保留，进入可人工处理状态。
- ASR transport 失败不会继续本地合成；重复进入不重新购买母片或重复记账。
- 质量候选被人工接受后沿用同一冻结图和同一媒体 SHA 的 ASR 结果，不再次请求识别。
- 新媒体字节生成新的 SHA 派生产物，旧版本不会覆盖新版本，也不能被跨版本误用。
- 损坏识别缓存、无穷或负时间戳、倒置时间区间、非有限置信度均按缓存未命中处理，并与同母片的其他媒体版本隔离。
- 下载损坏或导演合成失败时保留供应商任务与费用证据；重新进入不重复 POST 或记账。
- FFmpeg/ffprobe 只允许预期的本地命令形态；远程输入、未登记程序和额外参数会被拒绝。

## 验证入口

```powershell
.\.venv\Scripts\python.exe -m unittest `
  tests.test_strict_master_compose_journey `
  tests.test_master_transcriptions `
  tests.test_management_compiler_api -q

.\.venv\Scripts\python.exe tests\zero_cost_release_gate.py --level r1
```

在提交 `9ca049f` 上，根代理移除 Provider 凭证后运行上述定向组合 28 项，通过（72.614 秒）；R0 22 项、R1 68 项通过，门禁记录 `paid_provider_calls=0`、`external_requests=0`、`invariant_issues=0`。随后执行全量 1615 项，通过（805.742 秒，跳过 1 项）；内含的零费用 Flow Guard 增量巡检通过，`paid_provider_calls=0`，严格回放仍按边界标记为 `not_run`。

独立质量复审另运行严格导演旅程、空 ASR、候选、异常、新 Schema 和终审事务 13 项，通过（70.435 秒）；`test_video_upgrade` 与严格客户端边界 357 项通过（56.371 秒）；并发相反终审额外连续执行 5 次，均仅一个决策原子提交。

## 证据边界

- 合成样本尺寸和时长只为验证技术链路与命令，不是画面质量金标。
- QA / ASR 固定响应只能证明正式代码在该响应下的行为，不证明真实模型准确率、人物一致性、口型、表演、叙事或剪辑审美。
- 当前是单进程正式 handler 旅程；尚未覆盖浏览器点击、Supervisor/Worker 进程树重启、跨进程恢复和真实 Provider E2E。
- 该旅程尚未注册为 Flow Guard Full 的内置严格 runner，因此整体报告中的 `strict_transport_replay` 仍应保持 `not_run`，不能仅因本测试通过改成 `passed`。
