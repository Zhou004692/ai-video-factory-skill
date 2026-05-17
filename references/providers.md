# 模型选型与价格表

## 视频生成

| Provider | 模型 | 单段 | 9:16 1080p 单条 60s 成本 | 国内可商用 | 备注 |
|---|---|---|---|---|---|
| **seedance**(即梦底模) | doubao-seedance-1-0-pro | 5-12s | ¥1.5-3.5 | ✅ | 默认,多镜头模式独有 |
| **kling**(可灵) | kling-v2-1-master | 10s | ¥5-8 | ✅ | 运动幅度最好 |
| sora | sora-2-pro | 20s | ¥30-60 | ❌ | 画质天花板,海外 |
| veo | veo-3 | 8s | ¥40-80 | ❌ | 原生人声+音效最强 |
| hunyuan | HunyuanVideo(开源) | 5-8s | 仅算力 | ✅ | 自建 GPU |
| wan | Wan 2.1(开源) | 5-10s | 仅算力 | ✅ | 自建 GPU |

切换:`.env` 改 `DEFAULT_VIDEO_PROVIDER=seedance|kling`。
要接入 sora/veo:写一个 `clients/sora.py` 实现 `text_to_video(...) → bytes` 即可。

## TTS

| Provider | 引擎 | 中文 | 价格 | 备注 |
|---|---|---|---|---|
| **doubao**(默认) | 火山 / 豆包 | ★★★★★ | 约 ¥0.0001/字 | 音色多,情绪丰富 |
| cosyvoice | 阿里开源 | ★★★★ | 自建 | 可克隆音色 |
| elevenlabs | ElevenLabs | ★★★ 中文 | $0.18/1k 字 | 海外,英文神级 |
| azure | Azure Neural | ★★★★ | $4/1M 字 | 稳定但调性偏正式 |

## BGM

| Provider | 来源 | 价格 | 备注 |
|---|---|---|---|
| **library**(默认) | 本地音乐库 | 0 | 需自己放 mp3 到 `references/bgm_library/<style>/` |
| suno | sunoapi(第三方) | $0.05/首 | Suno 官方未公开 API |
| haimian | 海绵音乐 | TBD | API 未公开,占位 |
| musicgen | Meta 开源 | 自建 | GPU 自跑 |

## 实际推荐组合

**矩阵号(成本敏感)**:seedance + doubao + library → 约 ¥1.5/条
**质量优先(品牌片)**:kling + doubao + suno → 约 ¥6/条
**海外/广告级**:sora + elevenlabs + suno → 约 ¥40/条

## 选 provider 的决策树

```
要发到国内平台(视频号/抖音)?
├── 是 → 必须 ≤ ¥10/条 → seedance + doubao + library
│         可承担 ¥10-20 → kling + doubao + suno
│         要做品牌级 → kling Pro Master + 豆包顶级音色 + suno + 真人审校
└── 否 → 走 sora / veo,质量优先
```
