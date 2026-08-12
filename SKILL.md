---
name: ai-video-factory
description: 用自然语言完成可追溯短视频剧本与标准成片。自动处理首次初始化、配置诊断、人工材料或用户提供的公开网页/字幕/音视频归档、Brief、三版剧本、人工审批、Seedance 2.5 原生母片、本地导演剪辑、成本控制、发布映射和绩效回流。用户提到短视频剧本、热点材料、竞品结构分析、固定角色口播、Seedance 成片、批量视频、成本复盘或视频实验时使用；普通用户不需要执行命令或理解内部 ID。
---

# AI Video Factory

## 对话契约

把 Skill 当成托管式产品，而不是命令行教程。

- 不要求普通用户运行安装、诊断或 CLI 命令。
- 不向普通用户暴露 `SRC/BRIEF/SCRIPT/JOB` 等内部 ID，除非排错或用户明确索要。
- 自动完成安全、可逆且无付费的本地操作。
- 只在缺少业务材料、外部凭证、真人授权、剧本选择或付费确认时询问用户。
- 一次只询问当前阶段真正缺少的最少信息。
- 用自然语言报告结果、预算、风险和产物路径，不粘贴后台日志。
- 不自动发布，不自动批准剧本，不自动确认付费视频生成。

用户理想体验：

```text
用户提供主题或素材
→ AI 自动初始化并归档
→ AI 返回三版可选剧本
→ 用户选择一版
→ AI 自动生成 Storyboard 和零调用预算预览
→ 用户确认预算
→ AI 生成并验收成片
→ 用户人工发布后，AI 接收数据并复盘
```

## 首次触发：自动自举

每次开始新工作前，Agent 在后台运行：

```text
<runtime-python> scripts/bootstrap.py --profile <script|video|learning> --json
```

优先使用 Skill 内 `.venv`；不存在时使用系统 Python 执行自举。自举可以创建 `.venv`、安装依赖、创建 `.env` 和 `data/`，但不调用任何付费供应商，结果必须为 `paid_provider_calls=0`。

根据返回结果处理：

| 状态 | Agent 行为 |
|---|---|
| `ready` | 静默继续用户任务 |
| `configuration_required` | 只说明缺少哪类凭证；优先使用产品的安全密钥输入能力，不在回复中复述密钥 |
| `runtime_required` | 自动修复可安全修复的本地依赖；FFmpeg 等系统级依赖无法自动处理时，再给一条针对性说明 |

首次只启用完成当前请求所需的最低档位：

- 只做剧本：`script`，豆包只需 `ARK_API_KEY`；Claude 只需 `ANTHROPIC_API_KEY`。
- 生成成片：先完成并批准剧本，再切到 `video`，检查 Seedance、BigASR、TOS、FFmpeg。
- 数据复盘：`learning`，无需模型凭证。

高级模型名、区域、并发和预算采用 `templates/env.example` 默认值，不主动询问普通用户。读取完整配置语义时查看 `references/providers.md` 和 `references/troubleshooting.md`。

## 剧本工作流

### 1. 理解输入

接受：

- 用户直接描述的主题、受众、目标和素材；
- 用户上传的文本、Markdown、字幕、音频或视频；
- 用户提供的公开网页；
- 用户提供的热点或竞品内容。

当前“自动化”是自动处理用户提供的来源；系统主动发现全网热点和竞品仍属后续能力，不得假装已经上线。

如果用户只说一个主题，先以其原话建立人工来源；只有受众或目标会显著改变结果时才追问。公开 URL 仅抓取公网地址，不访问本机、内网或重定向后的私有地址。音视频必须具备处理权。

### 2. 后台串联

Agent 自动调用现有 CLI 完成以下步骤，并从 JSON/标准输出中保存内部 ID：

```text
source-add 或 media-add
→ 必要时 source-analyze
→ brief-generate
→ brief-approve（必须等用户选择角度）
→ script-generate
→ script-list
```

不要让用户复制 ID 或重新输入命令。一次性返回三版完整剧本，清楚标注：

- 标题与核心创意；
- 开头钩子；
- 完整台词；
- 预计时长；
- 画面与 B-roll 思路；
- 推荐版本及原因；
- 主要事实来源或风险。

每版必须包含 `hook / conflict / evidence / reversal / solution / cta`，每个 beat 保留来源 ID。竞品只能提供叙事结构，不得复制台词、人物身份、品牌口号或受保护表达。

### 3. 用户审批

等待用户用自然语言选择，例如“用第二版”“把第一版结尾改一下”。需要修改时生成修订稿，不把未批准稿送入视频阶段。

批准后，Agent 在后台执行 `script-approve`、`script-export` 和 `plan`，然后只向用户展示：

- 已选剧本；
- 1—3 条母片和本地剪辑方案；
- 预计费用上限；
- 真人授权状态；
- 是否可以开始付费生成。

## 视频工作流

进入 `video` 前必须满足：

1. 剧本已经人工批准且来源可追溯。
2. 视频档位自举结果为 `ready`。
3. 真人角色有本人授权及供应商批准的 `asset://`；普通照片、公开截图或 AI 定妆图不能冒充授权资产。
4. 标准版不超过 3 条付费母片、总时长不超过 36 秒，且预算不超过用户确认值。
5. `PUBLISH_MODE=manual`。

先在后台调用不带确认参数的 `produce-approved` 获取零调用预览。只有用户明确同意展示的预算后，才能调用带付费确认的生产路径。

标准版固定使用：

```text
Seedance 2.5 原生画面、声音和口型
→ BigASR 台词硬验收 + 视频 QA
→ FFmpeg 本地导演剪辑、字幕、B-roll、BGM 和 CTA
```

不恢复 TTS、OmniHuman 或外部口型模型。字幕移除陈述句末句号，保留问号与叹号。失败时只重试最小失败母片；等待超时先查询原任务，禁止直接重复提交付费请求。

生成后主动报告 `final.mp4` 路径、真实累计费用和质检结果。提醒人工验收身份、口型、声音、字幕、手部、乱码和节奏；未经确认不得发布。

## 角色

读取 `references/characters/roster.yaml`。分发版只带虚构示例角色。新增真人角色时：

- 用户在火山控制台自行完成真人资产创建和授权；Skill 不指导创建，也不使用浏览器读取控制台。
- Agent 先在后台运行 `character-resolve --character-id <角色ID> --name <角色名>`，复用 `VOLC_AK` / `VOLC_SK` 调用火山方舟 Assets API，只查询 `LivenessFace + Active` 资产。
- 只有名称精确且唯一时自动绑定，并把 `asset://<assetId>` 保存在本地数据库；后续同一角色自动复用。
- API 未开通、无权限、无结果或结果不唯一时，不猜测、不使用浏览器兜底，只问用户一次：`请把火山真人资产详情里的 assetId 发给我（形如 asset-...；也可以直接粘贴完整资产 URI）`。
- 用户提供后，Agent 在后台运行同一命令并增加 `--asset-id <用户提供值>`，完成格式归一化和本地绑定；普通用户不执行命令。
- 不把私人图片、授权 URI 或组织专属角色提交到公共仓库；
- 未授权时可先使用虚构角色验证管线。

## 学习工作流

用户提供发布平台、作品 ID 或数据文件后，Agent 在后台：

1. 用 `review` 记录人工验收耗时、首轮通过率和失败原因；
2. 用 `publication-map` 绑定唯一平台作品 ID，不按标题模糊匹配；
3. 在 24/72/168 小时导入累计数据，批量导入先 dry-run；
4. 用学习与实验报告复盘，但不自动写回生产 prompt；
5. 实验只改变一个预注册变量，样本不足不宣布胜出。

## 固定安全规则

1. 不删除或清空 `data/`，其中包含任务状态、成片和付费调用证据。
2. 剧本阶段不调用视频供应商。
3. Brief、剧本、真人资产、付费生成和发布都保留人工门槛。
4. 涉政、医疗、金融或法律建议时读取 `references/prompts/compliance.md`；高风险内容转人工审核。
5. 成本累计成功、失败和重试调用，不只计算最终成片。
6. 不在对话、日志或版本库中回显或提交用户密钥。

## 按需读取

- 端到端状态与产物：`references/workflow.md`
- Storyboard 契约：`references/prompts/storyboard.md`
- 合规规则：`references/prompts/compliance.md`
- 模型与成本：`references/providers.md`
- 故障处理：`references/troubleshooting.md`
- 角色配置：`references/characters/roster.yaml`
