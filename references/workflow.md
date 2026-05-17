# 端到端流程

## 状态机

```
PENDING → STORYBOARDED → CHARACTER_OK → SHOTS_OK → VOICE_OK → BGM_OK → COMPOSED → PUBLISHED
                                                                                  ↘ FAILED
                                                                                  ↘ BLOCKED (合规拦截)
```

每个状态对应一段产物已落盘,中断重启从当前状态继续——所以**永远不要清空 data/**。

## 各步骤产物

| 步骤 | 输入 | 产物 | 调用 | 成本 |
|---|---|---|---|---|
| s1_storyboard | 主题+赛道+风格 | `storyboard.json` + `shots` 入库 | Claude Opus | ¥0.10-0.30 |
| s2_character | storyboard.character | `character.png` | Seedream / Midjourney | ¥0.05-0.20 |
| s3_shots | storyboard.shots[] + character.png | `shots/01.mp4...10.mp4` | Seedance / Kling | ¥1.00-3.00 |
| s4_voice | storyboard.voiceover 拼接 | `voice.mp3` | 豆包 TTS | ¥0.05-0.15 |
| s5_bgm | storyboard.bgm | `bgm.mp3` | Suno / 海绵 / library | ¥0-0.50 |
| s6_compose | 上述全部 | `final.mp4` | FFmpeg(本地) | ¥0 |
| s7_publish | final.mp4 | publish_url | 手动 / Playwright RPA | ¥0 |

## 单条 60s 视频典型成本

- **即梦路线**:¥1.5-3.5(默认,国内合规)
- **可灵路线**:¥5-8
- **Sora 2 路线**:¥30-60(画质天花板,海外)

## 文件布局

```
data/
├── factory.db                          # SQLite,所有状态
└── assets/
    └── J-T001/                         # 每个 job 一个文件夹
        ├── storyboard.json
        ├── character.png
        ├── shots/
        │   ├── 01.mp4
        │   └── ...10.mp4
        ├── concat.txt                  # FFmpeg concat list
        ├── concat_silent.mp4           # 纯画面拼接(无音轨)
        ├── voice.mp3
        ├── bgm.mp3
        └── final.mp4                   # 最终成片 ← 这个发布
```

## 并发模型

- `MAX_CONCURRENT_JOBS=4`:同时处理 4 个 job
- `MAX_CONCURRENT_SHOTS=3`:单 job 内 3 个分镜并行
- 最大并发视频生成请求 = 12

撞 API 限流被封 key 怎么办:把 `.env` 里两个值改成 `2` 和 `1`,代价是慢一倍但稳。

## 失败处理

- step 内异常:写 `events` 表,job retries+1,**指数退避**重试(2/4/8s)
- 重试 3 次仍失败:job 状态 → FAILED,error 字段记录最后一个异常
- 用户决定:`python cli.py retry <job_id>` 把它放回 PENDING 重新跑
- 单个分镜失败不波及全 job:用 `retry-shot <job_id> <idx>` 单独重跑那一镜
