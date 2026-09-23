# P1 请求预检与传输诊断

日期：2026-09-23。基线：`e8b8446`。

## 目标

P0 已解决任务恢复死路，但不能阻止外部网络在新的付费提交前失败，也不能说明 QA、轮询或下载究竟卡在 DNS、TCP、TLS 还是 HTTP。本阶段补齐零费用请求预检和统一的脱敏传输证据，不改变 Provider 的实际可用性承诺。

## 实现

1. 新增 `transport_diagnostics`，依次检查模型 API、TOS 上传端点和 Seedance 成片下载端点。
2. 每个目标记录 DNS、TCP、TLS、无鉴权 `HEAD` 与应用网络路径；401、403、404、405 等 HTTP 响应表示连接已建立，不代表账号、权限或业务请求有效。
3. 正式管理服务在开始付费生成及预算确认继续前自动执行预检。任一目标失败时抛出 `transient_network`，保持 `provider_task_created=false`、`paid_provider_calls=0`，不会进入生产启动处理器。
4. 制作队列检查器提供手动诊断和分阶段结果。最近报告保存到独立运行目录的 `data/runtime/transport-preflight.json`，Skill 更新不会覆盖。
5. Seedance 提交、轮询、下载与远端核对使用同一诊断结构，区分 `not_submitted`、`unknown`、`submitted_known`。错误证据只保留阶段、主机、异常类型、稳定原因码和重试语义。
6. 视频 QA 的连接异常不再只保存 `qa_error: <原异常>`。候选母片继续进入人工复核，同时在 `video_qa` 中保存脱敏传输证据。
7. `scripts/smoke_check.py` 已改为纯只读探针，删除 LLM 小请求、无效视频 POST 和带凭证 TOS 检查。

## 隐私与费用边界

- 不保存 API Key、Authorization、代理用户名/密码、URL 查询参数、签名下载参数或本机路径。
- 报告中的代理信息最多显示主机和端口。
- 预检不调用模型、不创建 Provider Task、不写任务业务状态。
- 预检通过只证明检查时刻的传输路径可建立，不能保证紧随其后的请求必然成功；实际提交仍保留“提交前失败可重试、提交结果未知只核对原任务”的 P0 规则。

## 验收证据

实际运行 `python scripts/smoke_check.py`：

| 目标 | DNS | TCP | TLS | 只读 HTTP | 应用路径 |
|---|---|---|---|---|---|
| 模型 API | 通过 | 通过 | 通过 | 401 | 401 |
| TOS 上传 | 通过 | 通过 | 通过 | 405 | 405 |
| 成片下载 | 通过 | 通过 | 通过 | 403 | 403 |

报告 `transport-1fabeb695679659c` 明确记录 `paid_provider_calls=0`、`provider_task_created=false`。该样本仅代表本次检查时刻，不外推长期成功率。

自动回归：

- `tests.test_transport_diagnostics`：12 项通过。
- `tests.test_production_failure_matrix`：102 项通过。
- `tests.test_management_console`：389 项通过。
- 视频生产、恢复与编译相邻组合将在发布门禁中继续执行。
- 全量发现执行 1736 项，出现 5 个错误：其中 4 个在未合入本分支的 `main` 上以相同命令复现（旧语音夹具缺少 `jobs.state`、Windows 媒体文件锁、既有固定事件 ID 冲突），另 1 个全流程用例隔离复跑通过。该结果不计作本阶段新增回归通过，但没有发现传输诊断改动产生的新错误签名。

## 剩余边界

本阶段没有自动切换网络、修改 DNS、关闭代理或重试未知提交。若预检持续失败，应使用另一条独立网络运行同一只读诊断对照，再依据主机、北京时间、阶段和原因码向网络或服务方定位。
