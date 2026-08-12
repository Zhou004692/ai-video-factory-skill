# 端到端生产流

## 状态机

```text
PENDING → STORYBOARDED → SHOTS_OK → BGM_OK → COMPOSED → PUBLISHED
                                                        ↘ FAILED
                                                        ↘ BLOCKED（合规拦截）
```

每个状态都有落盘产物。中断后从数据库中的当前状态继续，禁止清空 `data/`。

## 各步骤

| 步骤 | 输入 | 产物 | 主要能力 |
|---|---|---|---|
| `s1_storyboard` | 主题、赛道、受众、目标时长 | `storyboard.json`、母片任务入库 | 生成 2—3 条付费母片和 4—8 段本地导演时间线；合规和结构校验 |
| `s3_shots` | 母片、角色 `look`、前序锚点 | `shots/01.mp4...`、更新后的 `storyboard.json` | Seedance 2.5 原生母片画面/语音/口型；BigASR 与视频 QA；按真实逐句时间重建 `subtitle_cues` |
| `s5_bgm` | `storyboard.bgm` | `bgm.mp3` 或静音策略 | 本地音乐库 |
| `s6_compose` | 已验收母片、导演时间线、字幕、BGM | `concat.mp4`、`final.mp4` | 从母片分别取画面/原生音频，本地裁切、B-roll 覆画、字幕、混音和 CTA |
| `s7_publish` | `final.mp4` | 发布记录 | 默认人工预审并手工发布 |

## 真人角色前置解析

真人角色进入付费生产前执行：本地已绑定资产复用 → 火山 Assets API 精确唯一匹配 → 向用户索要 `assetId`。解析只接受可用的 `LivenessFace / Active` 资产，不使用浏览器兜底；无法唯一确认时停止在付费调用前。

## Seedance 镜头路由

标准版默认采用“原生母片生成 + 本地导演剪辑”，不是把 4—8 个成片镜头全部送去生成：

1. 生成 2—3 条母片：钩子真人、主体真人长母片、可选 B-roll 母片；付费母片总时长不超过 36 秒。
2. 成片时间线由 4—8 个本地剪辑段组成，每段分别用 `video_master_idx` 和 `audio_master_idx` 选择画面与音频来源。
3. B-roll 段只替换画面，音频继续按时间顺序取自主体真人母片；对白母片必须从 0 秒开始连续消费到结尾，不允许漏词、重叠或重排。
4. 字幕、hook、CTA 和 BGM 全部后期合成，不要求生成模型产出文字。

母片内部路由仍遵循以下规则：

1. 首个真人母片只用文本中的角色 `look` 和 `voice_directive` 建立身份。
2. 后续真人母片只引用最近一个已验收真人视频，维持人物身份。
3. 独立 B-roll 母片不带人物/声线锚点。
4. 视频引用和音频引用不叠加；前者约增加一倍 token，仅在人物一致性需要时使用。

## 镜头验收

- 对白：BigASR 归一化后比对目标台词；数字、日期、英文缩写做 ITN 处理。
- 画面：检查人物、场景、动作、镜头稳定性和提示词遵循度。
- 场景画外音：额外检查画面无可识别人脸。
- 全部镜头：禁止生成可读文字、数字、商标或水印；字幕由合成阶段生成。
- 台词不匹配、意外人声/人物、可读文字或 QA 异常属于硬缺陷，可在预算内同模型整镜重生一次。
- 只有 `visual_mismatch` 属于软偏差：保存候选并转人工复核，不自动产生第二次费用。
- `SEEDANCE_AUTO_RETRY_BUDGET_CENTS` 默认限制单次重试不超过 ¥8；`SEEDANCE_JOB_BUDGET_CENTS` 默认在下一次调用前把标准版单 job 总支出封顶为 ¥25。
- 生产流允许无新增模型费用的本地导演剪辑，但不回退到 TTS、外部数字人或外部口型模型。

## 长镜头

长镜头默认开启，推荐 13—24 秒。25—30 秒只用于低台词密度、简单动作和稳定构图；无论时长都执行相同 ASR 和视觉验收。

## 成本记录

Seedance 2.5 以任务返回的 `usage.completion_tokens` 计价，当前实测为 `¥0.07 / 1,000 tokens`。每次 provider 调用都写入调用明细并累计到镜头和 job；失败调用及重试不能丢失。

本次 18 秒三镜干净路径实测估算：首镜纯提示词 ¥4.06、场景音频引用 ¥4.06、真人视频引用 ¥8.10，加分镜 LLM 约 ¥0.01，合计约 ¥16.23。验证过程中因被拒绝实验和重试产生的真实累计费用会更高，应与干净路径分开披露。

## 文件布局

```text
data/
├── factory.db
└── assets/
    └── <job_id>/
        ├── storyboard.json
        ├── shots/
        │   ├── 01.mp4
        │   └── ...
        ├── concat.txt
        ├── concat.mp4
        ├── bgm.mp3
        ├── final.mp4
        └── thumbnail.jpg
```

## 并发与失败处理

- `MAX_CONCURRENT_JOBS=4` 控制 job 并发。
- `MAX_CONCURRENT_SHOTS=3` 只适用于互不依赖的 B-roll；连续人物镜头需按顺序生成锚点。
- 步骤异常写入 `events`，并按既有退避策略重试。
- 调度器的 `MAX_RETRIES` 不得绕过 Seedance 镜头预算；达到费用上限后必须人工提高预算或缩短方案。
- job 最终失败后由用户决定是否执行 `python scripts/cli.py retry <job_id>`。
- 单镜可用 `python scripts/cli.py retry-shot <job_id> <idx>` 重跑。
