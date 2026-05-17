# 新机器部署清单

把这个 skill 跑通需要 ~10 分钟手工配置(主要是抓 API 密钥)。

## 1. 系统依赖

```powershell
# Windows
winget install -e --id Python.Python.3.12 --scope user
winget install -e --id Gyan.FFmpeg --scope user
# 验证(新开 PowerShell)
python --version   # 3.12+
ffmpeg -version
```

## 2. 克隆 skill

```powershell
git clone https://github.com/Zhou004692/ai-video-factory-skill `
  $env:USERPROFILE\.claude\skills\ai-video-factory
```

## 3. 跑安装脚本

```powershell
cd $env:USERPROFILE\.claude\skills\ai-video-factory
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

`install.ps1` 会:创建 `.venv`、装 Python 依赖、初始化 SQLite、复制 `.env` 模板。

## 4. 填 `.env`

```powershell
notepad $env:USERPROFILE\.claude\skills\ai-video-factory\.env
```

### 不敏感配置(直接复制下面,与生产保持一致)

```ini
# Anthropic(可选,默认走豆包,不必填)
CLAUDE_MODEL=claude-opus-4-7

# 火山方舟
ARK_REGION=cn-beijing
ARK_VIDEO_MODEL=doubao-seedance-2-0-fast-260128
ARK_IMAGE_MODEL=doubao-seedream-5-0-260128
ARK_LLM_MODEL=doubao-1-5-pro-32k-250115
ARK_VIDEO_RESOLUTION=720p
OMNIHUMAN_RESOLUTION=720

# 豆包语音(旧版控制台)
VOLC_TTS_PROVIDER=legacy
VOLC_TTS_CLUSTER=volcano_tts
VOLC_TTS_VOICE=BV001_streaming
VOLC_TTS_VOICE_MALE=BV002_streaming
VOLC_TTS_VOICE_FEMALE=BV001_streaming
TTS_VERSION=v2

# TOS
TOS_REGION=cn-beijing
TOS_ENDPOINT=tos-cn-beijing.volces.com
TOS_BUCKET=ai-video-factory
TOS_OBJECT_PREFIX=ai-video-factory

# 默认 provider
DEFAULT_LLM_PROVIDER=doubao
DEFAULT_VIDEO_PROVIDER=seedance
DEFAULT_TTS_PROVIDER=doubao
DEFAULT_MUSIC_PROVIDER=silent

# 调度
MAX_CONCURRENT_JOBS=1
MAX_CONCURRENT_SHOTS=1
MAX_RETRIES=2
VIDEO_DURATION=30
PUBLISH_MODE=manual
```

### 敏感字段(每台机器都要手动抓一次)

| 字段 | 在哪复制 |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com/settings/keys |
| `ARK_API_KEY` | https://console.volcengine.com/ark → 系统管理 → API Key 管理 |
| `VOLC_AK` | https://console.volcengine.com/iam/keymanage → **Access Key ID**(完整字符串) |
| `VOLC_SK` | 同上 → **Secret Access Key** |
| `VOLC_TTS_APPID` | https://console.volcengine.com/speech/app → 应用详情 → **APP ID** |
| `VOLC_TTS_TOKEN` | 同上 → **Access Token** |

> `VOLC_AK` / `VOLC_SK` 一套通吃:TOS 上传 + OmniHuman 视觉智能服务,都用同一对。
> 豆包语音 TTS 用的是另一套(APPID + Token),控制台地址也不一样。

## 5. 火山引擎控制台开通模型(每个账号一次性)

如果是**新账号**或**没开通过**这些,需要先去开:

### 方舟控制台 (https://console.volcengine.com/ark/region:ark+cn-beijing/openManagement)
- `doubao-seedance-2-0-fast-260128`(视频生成)
- `doubao-seedance-1-0-pro-250528`(视频备选)
- `doubao-seedream-5-0-260128`(图像生成)
- `doubao-1-5-pro-32k-250115`(LLM,写分镜)

### 豆包语音控制台 (https://console.volcengine.com/speech/app)
- 创建应用 `ai-video-factory`
- 开通 **「语音合成」V1 普通版**(用于 BV001/BV002 备选)
- 开通 **「豆包语音合成模型 2.0」**(SeedTTS 2.0)
- 在 SeedTTS 2.0 "音色详情" → "可开通服务试用" → **批量开 20+ 音色**(免费试用包)

### 视觉智能服务 (https://console.volcengine.com/speech)
- 开通 **「OmniHuman 1.5」**(对口型)

### TOS 对象存储
- 在 `cn-beijing` 区域创建桶 `ai-video-factory`(公共读私有写)

## 6. 验证

```powershell
cd $env:USERPROFILE\.claude\skills\ai-video-factory
.\.venv\Scripts\python.exe scripts\cli.py status
# 应该输出 "(no jobs)" — 表示 SQLite 通了

# 跑一条测试(成本约 ¥25,30s 视频)
.\.venv\Scripts\python.exe scripts\cli.py run templates\test_g.csv
```

## 故障排查

| 错误 | 原因 | 修复 |
|---|---|---|
| `ARK_API_KEY not set` | .env 没读到 | 文件必须在 skill 根目录,不是 scripts/ 下 |
| `ModelNotOpen` | 方舟模型没开通 | 去 openManagement 点开通 |
| `resource ID is mismatched` | 音色不在 SeedTTS 2.0 资源里 | 已有自动 fallback 到 1.0,无需处理 |
| `Invalid workflow synthesize_with_sami` | V1 TTS 服务没开 | 豆包语音控制台开"语音合成"普通版 |
| `seedream 400 ... size at least 3686400` | Seedream 5.0 要求 ≥3.69M 像素 | s2 已默认 1440×2560,无需调 |

更多见 `references/troubleshooting.md`。
