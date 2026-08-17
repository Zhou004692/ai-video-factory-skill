# 新机器部署清单

## 普通用户：无需命令

安装 Skill 后，只需要对 Agent 说：**“打开 AI 视频工厂”**。

Agent 会在后台自动完成 Python 检查、隔离环境创建、依赖安装、SQLite 初始化和管理台启动。若必要凭证尚未配置，页面会直接进入“配置中心”；用户只在页面内填写缺失项，不需要复制命令、编辑 `.env` 或理解内部任务 ID。

安装完成后也可以双击根目录中的“打开 AI 视频工厂”启动器。重复启动只做幂等检查，不会覆盖已有配置、数据库、角色资产或业务数据。

以下内容仅供管理员排障和离线部署使用，普通用户不需要执行。

当前生产链只需要：分镜 LLM、Seedance 2.5、BigASR、TOS、FFmpeg 和可选 BGM。

## 1. 系统依赖

```powershell
winget install -e --id Python.Python.3.12 --scope user
winget install -e --id Gyan.FFmpeg --scope user
python --version
ffmpeg -version
```

## 2. 安装

```powershell
git clone https://github.com/Zhou004692/ai-video-factory-skill `
  $env:USERPROFILE\.claude\skills\ai-video-factory
cd $env:USERPROFILE\.claude\skills\ai-video-factory
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

安装脚本会创建 `.venv`、安装依赖、初始化 SQLite，并复制环境变量模板。

## 3. 配置 `.env`

以 [templates/env.example](templates/env.example) 为准，至少填写：

- `ARK_API_KEY`：Seedance 2.5 与视频理解质检。
- `VOLC_ASR_APPID`、`VOLC_ASR_TOKEN`：BigASR 原生台词验收。
- `VOLC_AK`、`VOLC_SK`、`TOS_BUCKET`：上传成片供质检和连续镜头引用。
- 使用 Claude 写分镜时填写 `ANTHROPIC_API_KEY`；默认豆包分镜可不填。

默认模型：

```ini
ARK_VIDEO_PRIMARY_MODEL=doubao-seedance-2-5-260628
ARK_VIDEO_MODEL=doubao-seedance-2-5-260628
ARK_VIDEO_RESOLUTION=720p
VIDEO_QA_ENABLED=true
DEFAULT_LLM_PROVIDER=doubao
DEFAULT_MUSIC_PROVIDER=silent
```

## 4. 控制台开通

1. 在火山方舟开通 `doubao-seedance-2-5-260628` 和用于分镜/视频理解的豆包模型。
2. 在火山语音控制台创建应用并开通 BigASR 极速版，取得 APP ID 与 Access Token。
3. 在 `cn-beijing` 创建 TOS 桶；视频必须能被质检模型访问。

不需要开通独立语音合成、角色定妆图模型或外部数字人服务。

## 5. 验证

```powershell
cd $env:USERPROFILE\.claude\skills\ai-video-factory
.\.venv\Scripts\python.exe scripts\cli.py status
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

首次实跑建议使用一条 8—12 秒、单人、短台词样例，并保持：

```ini
MAX_CONCURRENT_JOBS=1
MAX_CONCURRENT_SHOTS=1
```

## 6. 可选：抖音直连发布

直连发布不是视频生成的必要条件。需要时，在抖音开放平台准备已审核应用并开通
`video.create.bind` 权限，再在管理台「配置中心 → 发布渠道 → 抖音」填写：

- `DOUYIN_CLIENT_KEY`
- `DOUYIN_CLIENT_SECRET`
- `DOUYIN_REDIRECT_URI`：开放平台登记的公网 HTTPS 地址，路径指向
  `/api/integrations/douyin/oauth/callback`

保存后点击“绑定抖音账号”，在抖音授权窗口扫码。普通用户不需要复制 Access Token；
授权令牌只保存在本机数据库。投稿返回作品 ID 后，管理台会先显示“平台审核中”，
不会提前计入发布成功。

## 7. 微信视频号发布

微信视频号无需在本地配置账号。进入管理台「发布与数据」，选择待发布成片后：

1. 下载最终 MP4；
2. 点击“打开视频号助手”，在微信官方页面扫码登录并上传发布；
3. 发布完成后回到管理台填写作品 ID，作品链接可选填。

登录状态由微信官方页面保管，管理台不读取 Cookie，也不会显示虚假的“已绑定”状态。
如需覆盖入口地址，只允许使用 `https://channels.weixin.qq.com` 域名下的官方页面。

## 常见错误

| 错误 | 修复 |
|---|---|
| `ARK_API_KEY not set` | 确认 `.env` 位于 skill 根目录 |
| `ModelNotOpen` | 在方舟模型开通页启用 2.5 |
| `VOLC_ASR_APPID / VOLC_ASR_TOKEN not set` | 填写 BigASR 应用凭证 |
| `BigASR failed` | 确认应用已开通 `volc.bigasr.auc_turbo` |
| `TOS_BUCKET not set` | 补齐 TOS 桶与 AK/SK |
| 台词验收失败 | 缩短单镜台词后运行 `retry-shot` |

更多见 [references/troubleshooting.md](references/troubleshooting.md)。
