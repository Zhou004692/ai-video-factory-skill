---
name: ai-video-factory
description: 可追溯的短视频剧本与标准成片工作流。用于把人工材料、网页、字幕或本地音视频整理为来源素材，生成并审批 ScriptBrief 和三版完整剧本，导出 Seedance 2.5 原生母片 + 本地导演剪辑 Storyboard，并在明确授权后执行成片、成本控制、发布映射和绩效回流。用户提到短视频剧本、热点选题、竞品结构模仿、固定角色口播、Seedance 成片、批量视频生产、成本复盘或视频实验时使用。
---

# AI Video Factory

使用两条输入管线、一个剧本引擎：

```text
人工输入 ─┐
          ├→ 来源归档 → Brief → 三版剧本 → 人工审批 → Storyboard
自动发现 ─┘                                        ↓
                              Seedance 2.5 原生母片 → 本地导演剪辑
```

当前可稳定使用人工输入管线；热点自动抓取与竞品结构模仿属于后续扩展。不要把规划中的能力描述成已经上线。

## 选择运行档位

| 档位 | 适用目标 | 外部依赖 | 付费风险 |
|---|---|---|---|
| `script` | 素材归档、Brief、三版剧本、审批、Storyboard | 方舟 LLM | 仅文本模型 |
| `video` | 已批准 Storyboard 生成标准成片 | Seedance 2.5、BigASR、TOS、FFmpeg | 有视频调用 |
| `learning` | 批次成本、发布映射、24/72/168h 数据与实验报告 | 人工导入平台作品 ID/数据 | 不自动发布 |

默认从 `script` 开始。除非用户明确要求成片且视频预检通过，否则不得进入 `video`。

## 首次使用

在 Skill 根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
.\.venv\Scripts\python.exe scripts/doctor.py --profile script
```

macOS/Linux：

```bash
python3 scripts/install.py
./.venv/bin/python scripts/doctor.py --profile script
```

安装器只在 `.env` 不存在时复制 `templates/env.example`，不得覆盖使用者已有凭证。诊断只做本地检查，必须显示 `paid_provider_calls=0`。

准备生成视频时再运行：

```powershell
.\.venv\Scripts\python.exe scripts/doctor.py --profile video
```

下文的 `python` 均指该 Skill 安装生成的 `.venv` 解释器；Windows 可替换为 `.\.venv\Scripts\python.exe`，macOS/Linux 可替换为 `./.venv/bin/python`。

## 剧本工作流

### 1. 归档来源

文本、URL、Markdown 或字幕：

```powershell
python scripts/cli.py source-add --file material.md --topic-id TOPIC-001
python scripts/cli.py source-add --url https://example.com/article --topic-id TOPIC-001
python scripts/cli.py source-add --text "访谈原文" --title "客户访谈" --topic-id TOPIC-001
```

本地音视频：

```powershell
python scripts/cli.py media-add interview.mp4 --topic-id TOPIC-001
python scripts/cli.py source-analyze SRC-...
```

只允许抓取公开 URL；不得访问本机、内网或重定向后的私有地址。音视频必须由使用者拥有或具备处理权。

### 2. 生成并批准 Brief

```powershell
python scripts/cli.py brief-generate SRC-001 SRC-002
python scripts/cli.py brief-approve BRIEF-... --topic-id TOPIC-001 --angle 1
```

Brief 必须保留来源集合、重复选题提示、三个创意角度和人工选择结果。不要跳过审批直接生成视频。

### 3. 生成三版完整剧本

```powershell
python scripts/cli.py script-generate BRIEF-...
python scripts/cli.py script-list BRIEF-...
```

每版固定包含 `hook / conflict / evidence / reversal / solution / cta` 六个 beat。每个 beat 必须有台词、时长、画面意图、来源 ID 和 B-roll 建议；未知来源 ID 必须失败。

### 4. 审批并导出 Storyboard

```powershell
python scripts/cli.py script-approve SCRIPT-...
python scripts/cli.py script-export SCRIPT-... storyboard.json
python scripts/cli.py plan storyboard.json --budget 2500
```

审批输出采用 `production_mode=native_master_local_director`：优先生成 1—3 条原生母片，再本地切成 4—8 个编辑镜头。字幕移除陈述句末句号，保留问号和叹号。

## 视频工作流

进入视频阶段前确认：

1. 剧本已人工批准，来源可追溯。
2. `doctor.py --profile video` 全部通过。
3. 真人角色已获得本人授权，并登记供应商批准的 `asset://`；普通照片或公开截图不得冒充授权资产。
4. 标准版付费母片不超过 3 条、总时长不超过 36 秒，预计成本不超过用户确认预算。
5. 发布模式保持 `manual`。

从人工审核过的剧本进入视频阶段时，只运行批准后的 Job，不要重新导入 CSV：

```powershell
python scripts/cli.py produce-approved JOB-ID
# 检查零调用预览后，再显式确认付费生产
python scripts/cli.py produce-approved JOB-ID --yes
```

该命令从 `STORYBOARDED → s3_shots` 继续，明确不会重新执行 `s1_storyboard`。批量生产全新、尚未生成 Storyboard 的主题时才使用：

```powershell
python scripts/cli.py run topics.csv --batch-id PILOT-001
python scripts/cli.py status
python scripts/cli.py inspect JOB-ID
```

失败时只重试最小失败单元：

```powershell
python scripts/cli.py retry-shot JOB-ID MASTER-INDEX
```

供应商本地等待超时时，先查询原任务；不得直接重新提交付费请求。视频生成后先报告 `final.mp4` 路径，必须人工验收身份、口型、声音、字幕、手部、乱码和剪辑节奏，未经确认不得发布。

## 学习工作流

- 用 `review` 记录人工验收耗时、首轮通过率和失败原因。
- 用 `publication-map` 绑定明确的平台作品 ID；不得使用标题模糊匹配。
- 分别导入 24/72/168 小时数据，先 `--dry-run`。
- 实验只能改变一个预注册变量；样本不完整时不得输出胜出结论。
- 不自动写回生产 prompt，不自动发布。

需要完整命令时运行：

```powershell
python scripts/cli.py --help
```

## 固定安全规则

1. 不删除或清空 `data/`；其中包含任务状态、成片和付费调用证据。
2. 不在剧本阶段调用视频供应商。
3. 不恢复 TTS、OmniHuman 或外部口型模型；标准版使用 Seedance 2.5 原生声音与口型。
4. 涉政、医疗、金融、法律建议在生成前读取 `references/prompts/compliance.md`；高风险内容必须转人工审核。
5. 自动发现不等于自动批准；Brief、剧本、真人资产和发布都保留人工门槛。
6. 竞品只能提供叙事结构，不得复制台词、人物身份、品牌口号或受保护表达。
7. 报告成本时累计成功、失败和重试调用，不只计算最终成片。

## 分发维护者

只有维护者需要执行：

```powershell
python scripts/distribute_skill.py --output dist
```

打包器使用白名单复制并执行隐私审计。分发包不得包含 `.env`、`.git`、`.venv`、数据库、`data/` 产物、测试、内部计划、个人角色资料或真人授权 URI。

## 按需读取

- 端到端状态与产物：`references/workflow.md`
- Storyboard 契约：`references/prompts/storyboard.md`
- 合规规则：`references/prompts/compliance.md`
- 模型与成本：`references/providers.md`
- 故障处理：`references/troubleshooting.md`
- 角色配置：`references/characters/roster.yaml`
