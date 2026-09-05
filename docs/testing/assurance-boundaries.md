# Flow Guard 测试证据边界

`passed` / `partial` / `attention` / `failed` 保留原有套件状态语义和退出码兼容性，但都不是“整个产品已验证”。`assurance.level=full` 只表示配置检查层未跳过，不表示真实生产 handler 全链路、识别准确率或内容质量验收通过。

## 当前证据分层

| 维度 | 实际使用的来源与替换边界 | 通过时能证明 | 不能证明 |
|---|---|---|---|
| `component_propagation` | 正式图调度 + `ScriptedSeedance`；其底层调用高层业务 handler 夹具 | 已执行场景的状态流转、调度和契约断言 | 全部生产 handler 执行、真实生成结果 |
| `video_performance_propagation` | 合成视频/图片；ASR 文本、说话人、情绪、视觉观察、结构答案预填 | 预填答案的解析、归一化、绑定与重建传播 | 真实媒体识别准确率 |
| `formal_service_with_high_level_fixture` | 正式管理服务、部分 HTTP/UI/Worker 调度；高层生成 handler 夹具，简报/剧本 Replay | 夹具条件下的服务衔接与单独记录的动作事实 | 严格生产 handler E2E、模型输出质量 |
| `media_technical_checks` | FFmpeg 合成运动/音频与人工坏样本 | 冻结、黑帧、帧时间戳断层、音画时长错位的技术检测 | 身份、口型、表演、叙事、节奏和审美 |
| `production_shadow_lookup` | 正式 SQLite 只读快照与管理台动作映射 | 结构问题、当前状态、处理入口映射 | 恢复已经执行、任务已恢复、人工待审核就是故障 |
| `historical_incident_replays` | 各历史事故回归夹具，具体见 `detection_boundary` | 已运行测试的指定护栏 | 未覆盖故障和完整生产链 |
| `structural_mutation_checks` | 人工修改结构夹具 | 指定坏结构是否被检出 | 媒体识别和内容质量 |
| `strict_transport_replay` | **当前 Flow Guard 未接入，`not_run`** | 无 | 保留全部生产 handler、仅替换外部 transport 的完整旅程 |
| `recognition_accuracy` | **未运行，`not_run`** | 无 | 从真实音视频获得台词、说话人、表情、手势等的准确率 |
| `actual_content_quality` | **未运行，`not_run`** | 无 | 真实成片内容质量 |

`full_flow_regression.build_zero_cost_handlers` 替换 `character_anchor`、`scene_anchor`、`master` 和 `bgm` 的业务处理：生成本地合成文件，直接写锚点/母片/音乐状态和路径。它不是只 Mock Seedance，也不是只替换 HTTP transport。`local_compose` 和 `final_review` 节点仍调用正式适配器，不改变上述缺口。

核心服务旅程 `run_source_to_ready_to_publish` 使用 `asr_enabled=False`。最终 `service.review_video` 的身份、口型、字幕同步、画面完整性、干净画面和剪辑节奏检查由脚本提交 `True`。`READY_TO_PUBLISH` 在这里证明工作流接受了该审核输入，不证明这些质量条件经真实识别或人工检查成立。

## 机器报告契约

新增顶层 `evidence_boundaries`，不移除现有 `status`、`assurance`、`coverage` 或套件结果：

```json
{
  "schema_version": 1,
  "status_scope": "technical_suite",
  "metadata_status": "recorded",
  "dimensions": {
    "strict_transport_replay": {
      "level": "strict_transport_replay",
      "status": "not_run",
      "source_kind": "none",
      "replacement_boundary": "当前 Flow Guard 未接入严格 transport 回放旅程。",
      "proves": [],
      "does_not_prove": ["真实生产 handler 的严格端到端执行"],
      "observed_checks": []
    }
  }
}
```

上例仅展示一个维度；实际报告包含上表全部维度。

- `status`：`passed`、`failed`、`partial`、`attention` 保留该检查的实际结果；`not_run` 表示明确未执行；`unknown` 表示缺少可确认的来源或结果。
- `source_kind`：实际已知来源；没有执行不能把计划使用的来源当成已用来源。
- `replacement_boundary`：明确替换了哪一层、输入是否合成/预填、哪些处理未运行。
- `proves`：该类检查**通过时**的有限证明范围，不是失败、未知或未执行时的成功结论。
- `does_not_prove`：即使通过也不能推出的结论。
- `observed_checks`：本轮返回的场景、护栏、变异或审计计数；不自动补出未执行的能力。
- `metadata_status=recorded` 表示有可记录的内置来源结果或明确跳过记录，不表示所有维度通过，也不是防篡改证明。

当前构建器只识别固定的内置 runner 函数对象，并要求对应结果记录。自定义/注入 runner 不沿用内置来源标签；`mode=full`、`provider_mode`、`strict_e2e=true` 或单独 `status=passed` 都不能生成严格证据。新增完整生产旅程前，上表最后三个维度固定为 `not_run`。独立客户端 transport 单元测试的通过也不会自动升级 Flow Guard 整体旅程。

旧报告缺少元数据、版本不支持或单维度字段不完整时，不从旧计数和通过标志反推来源：相关维度按 `unknown` / `not_run` 处理。HTML 对旧 `passed` 顶部显示“范围未知”，原状态仍保留在 JSON 和兼容记录中；写回报告时也会持久化保守默认值。

## 人工等待、故障与恢复

人工确认锚点、候选或成片是业务门；程序故障、契约错误和缺失处理入口是另一类问题。当前 `attention` 只是现有影子审计的待处理汇总，不能单靠它推断故障原因。读取 `recovery_action` 并确认前端有入口，属于动作映射证据，不是点击成功、Worker 完成或结果正确的证据。影子审计不会执行正式恢复。

后续要声明严格端到端通过，需要保留真实业务 handler、隔离整个进程树的外部 I/O、记录实际请求/响应及产物来源，并逐条证明终态与恢复动作执行。识别准确率需要独立真实媒体及非预填答案的识别结果；内容质量需要明确量表、素材来源及真实验收证据。不能仅改开关或把旧 `passed` 换名字。

## 本次验证范围

只运行纯单元测试，验证边界构建、未知默认、状态兼容、HTML 转义和报告落盘：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_flow_guard_evidence tests.test_flow_guard_html_report -v
```

不连接模型、不生成真实媒体、不执行浏览器/Full 旅程、不读取或修改生产数据库。本次单元测试的通过不构成上表任何真实业务旅程的运行记录。
