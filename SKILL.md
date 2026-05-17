---
name: ai-video-factory
description: AI 自动化视频号/短视频生产管线。把主题列表批量生成 30-60 秒成片(分镜→画面→配音→BGM→剪辑→发布),支持即梦/可灵/Sora。当用户说"做视频号""批量生成短视频""AI 视频矩阵""把这些主题做成视频""生成口播视频"时调用。涉及单镜头试做、提示词调优、成本估算、热点选题、合规检查也用本 skill。
---

# AI Video Factory

把一个主题列表(CSV)端到端跑成可发布的视频号短片,核心解决三件事:**画面连贯性、成本可控、失败可续跑**。

## 何时调用

触发场景:
- 用户想批量产出短视频(视频号/抖音/小红书)
- 单条主题想走完整管线试做一条
- 已有热点榜要筛选/打分/转脚本
- 想看上次跑了多少、花了多少钱
- 想对接新模型(火山方舟/可灵/Sora/Veo)

**不调用**的场景:
- 仅做单张图片或单段音频(用更轻的工具)
- 只想要分镜文案不要画面(直接用 Claude 即可)
- 视频号账号运营策略问题(本 skill 只管生产)

## 第一次使用前

按顺序确认:
1. `scripts/install.ps1` 是否跑过(装依赖、初始化 SQLite)
2. `~/.claude/skills/ai-video-factory/.env` 是否填了 `ANTHROPIC_API_KEY`、`ARK_API_KEY`(火山方舟,跑即梦/Seedance/豆包 TTS)、`KLING_API_KEY`(可选)
3. `ffmpeg -version` 能跑

没跑过就先跑 install,**不要绕过**。

## 标准工作流

### 场景 A:批量跑一个主题表

1. 让用户提供或确认 `topics.csv` 路径
2. 读一下表头校验字段:`topic_id,title,track,style,target_audience,priority`
3. 如果用户没填 `style`,问一次或按赛道默认(知识→D 极简,情感→A 日系,历史→C 国风,职场→B 赛博,故事→E 港片)
4. 跑:`python scripts/cli.py run <csv_path>`(在 skill 根目录运行)
5. 后台跑期间用 `python scripts/cli.py status` 轮询(每 30s 一次)
6. 跑完汇报:成功数 / 失败数 / 总花费 / 成片路径列表

### 场景 B:单条试做(调试用)

1. 让用户口述主题+赛道+风格
2. 临时生成只有一行的 CSV(`templates/single.csv`)
3. 同上跑,出结果给用户预览
4. 如果某个分镜不满意:`python scripts/cli.py retry-shot <job_id> <idx>` 单独重跑

### 场景 C:热点选题打分

用户给一批热搜词条 → 读 `references/prompts/topic_score.md` → 喂 Claude 输出 0-10 分 → 选 ≥7 的入 CSV。

### 场景 D:成本/进度/历史

- 进度:`python scripts/cli.py status`
- 成本:`python scripts/cli.py cost`
- 看某个 job 详情:`python scripts/cli.py inspect <job_id>`
- 看失败原因:查 SQLite `events` 表

## 关键规则

1. **永远不要清空 `data/`**——所有产物都在那里,删了就要重烧钱
2. **失败 job 默认不重跑**,需要用户确认后再 `cli.py retry`
3. **生成出来不要直接发**,先把 final.mp4 路径报给用户人工预审(视频号上 AI 内容**必须**有人把关)
4. **涉政/医疗/金融建议**:跑 s1 后立刻调 `references/prompts/compliance.md` 复检,有问题标 BLOCKED 不进 s2
5. **单次最多并发 4 个 job × 3 个分镜**,不要为了快擅自调高——容易撞 API 限流被封 key
6. **报告时用相对路径**,例如 `data/assets/J-T001/final.mp4`,方便用户在 VSCode 点开

## 模型选择(默认走即梦,理由见 references/providers.md)

| 场景 | 推荐 | 备选 |
|---|---|---|
| 国内合规、矩阵号 | 即梦视频 3.0 Pro(Seedance) | 可灵 2.1 |
| 高质量品牌片 | Sora 2 Pro | Veo 3 |
| 纯开源自部署 | HunyuanVideo | Wan 2.1 |

切换模型:改 `.env` 里的 `DEFAULT_VIDEO_PROVIDER=seedance|kling|sora`。

## References(按需读取,不要一次全读)

- 端到端流程图与状态机:`references/workflow.md`
- 分镜 Prompt 模板:`references/prompts/storyboard.md`
- 合规审查规则:`references/prompts/compliance.md`
- 主题打分规则:`references/prompts/topic_score.md`
- 口播润色规则:`references/prompts/voiceover_polish.md`
- 5 种风格底色:`references/styles/presets.yaml`
- 模型选型与价格表:`references/providers.md`
- 常见崩坏修复:`references/troubleshooting.md`

## 汇报格式

跑完一批后,**必须**按这个格式汇报:

```
✅ 完成 N 条 / 失败 M 条 / 总花费 ¥X.XX

成功:
- J-T001 《为什么劝你别做完美主义者》 → data/assets/J-T001/final.mp4 (¥1.23)
- J-T002 ...

失败:
- J-T003 卡在 s3_shots:第 4 个分镜超时
  修复建议:python scripts/cli.py retry J-T003 或换 provider 重跑

下一步建议:
- 哪几条值得优先发(根据钩子文案强度)
- 哪个赛道 ROI 最高(按花费/预估完播)
```
