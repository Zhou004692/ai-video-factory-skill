# 独立复刻初始化

普通用户通过对话提供原片、人物范围和目标动物，以下命令由 Agent 在后台执行。独立入口为 `scripts/recreation/recreate.py`，不调用旧 `install.py`、管理服务、业务数据库或旧 `.env`。外部项目格式与恢复规则见[项目格式](project-format.md)，对话说明见[使用引导](user-guide.md)。

## 按阶段准备

| 当前工作 | bootstrap 阶段 | 实际依赖与边界 |
|---|---|---|
| 原片事实、人物选择页、项目状态 | `inspect` | Python 3.10+ 标准库、FFmpeg/FFprobe；无需云 Key、姿态权重或 Node |
| 锚框身份跟踪、WholeBody133 观测 | `pose` | 独立环境 NumPy、唯一 headless OpenCV、ONNX Runtime CPU；观测还需显式准备固定 ONNX 权重 |
| 额外的运动学研究工具 | `fit` | NumPy、SciPy；默认 `local` 路径不会自动执行世界空间拟合 |
| 已分发犬体 CG 与同步审阅 | `render` | FFmpeg/FFprobe、Node.js 22+、本机 Chrome/Edge/Chromium；随 Skill 分发的 Three.js 0.181.0 与犬体资产需通过 manifest SHA 检查 |
| 选择 TOS 上传 CG | `generate-tos` | 可选 `tos>=2.8,<3` 独立环境；仅该上传实现需要，已有 HTTPS 引用不安装 TOS |

这是按当前动作选择的依赖集合，无需安装全部阶段。`bootstrap --check-only` 完全只读，默认检查会记录运行状态，只有 `--install` 安装当前阶段 Python 依赖；安装不下载模型、不读 Key、不调用生成服务。外部 FFmpeg、Node、浏览器缺失时只补该项。包源访问与模型下载分别记录，不把“零视频生成费用”说成零流量或零处理时间。

```text
<python> scripts/recreation/bootstrap.py --runtime-root RUNTIME --stage inspect --check-only --json
<python> scripts/recreation/bootstrap.py --runtime-root RUNTIME --stage pose --install --json
<python> scripts/recreation/bootstrap.py --runtime-root RUNTIME --stage render --check-only --json
<python> scripts/recreation/recreate.py --runtime-root RUNTIME doctor --stage pose
<python> scripts/recreation/recreate.py --runtime-root RUNTIME doctor --stage render
```

`recreate.py` 的全局 `--runtime-root` 必须放在子命令之前。`RUNTIME`、项目、模型与配置均放 Skill 外；默认路径由 `AI_VIDEO_RECREATION_HOME` 或系统用户数据目录选择，不读取 `FACTORY_*`。Windows 默认 `LOCALAPPDATA/ai-video-factory-recreation`，macOS 为用户 Application Support，Linux 为 XDG data home 下同名目录。已有环境、缓存与历史项目不删除。

## 依赖就绪与可执行就绪

`bootstrap` 的 `ready` 只表示所选阶段依赖可用。`recreate.py doctor` 进一步返回：

- `dependencies_ready`：该阶段解释器、包和外部工具符合检查。
- `execution_ready`：除依赖外，该阶段实际模型或浏览器、rig、vendor 资产也已通过检查。
- `resources`：明确列出权重、浏览器或资产缺项；不会因此自动下载或启动浏览器。

即使 `execution_ready=true`，也不证明项目输入、人物身份、CG 质量或生成授权已通过。pose 依赖就绪但无权重时仍不可观测；render 依赖就绪但无浏览器或资产 SHA 错误时仍不可渲染。doctor 只覆盖 `inspect/pose/fit/render`；TOS 依赖使用 `bootstrap --stage generate-tos` 检查，服务权限由生成流程另外核对。

## 显式准备 WholeBody133 模型

当前分发入口只登记已固定的 ViTPose WholeBody **B** ONNX，而非普通 17 点 B 模型。缓存登记也必须核对完整字节与 SHA；不能仅凭文件名匹配。它来自第三方 ONNX 转换，权重不在 Skill 包内。

| 项 | 固定值 |
|---|---|
| 模型 ID | `vitpose-wholebody-b` |
| schema / 解码器 | `coco_wholebody133` / `mmpose-msra-default` |
| 输入尺寸 | 宽 192 × 高 256 |
| 文件大小 | **360242862 字节** |
| SHA256 | `017c08e161bfa2c72a96297b3b6de975ecd83c65c4fd56fb5b0bfc1298709069` |
| 转换修订 | `e83805274e89428969355ec4afffcbc413e79188` |

```text
<python> scripts/recreation/recreate.py --runtime-root RUNTIME model --describe
<python> scripts/recreation/recreate.py --runtime-root RUNTIME model --source EXPLICIT_CACHED_ONNX
<python> scripts/recreation/recreate.py --runtime-root RUNTIME doctor --stage pose
```

`--source` 只登记显式选择的已验证缓存，不复制或搜索旧业务目录。成功后在 `RUNTIME/models/vitpose-wholebody-b/model-manifest.json` 写入权重绝对路径、来源、关节与解码器配置。已登记且通过 SHA 的模型可复用。其他模型不会自动回退到 RTMPose 或 17 点模型。

缓存缺失时先说明实际大小、固定转换来源 `JunkyByte/easy_ViTPose` 及上述修订、上游 Apache-2.0 代码许可与转换权重条款需分别核对，再依据已有授权下载。只有明确允许该次权重下载后，Agent 才执行：

```text
<python> scripts/recreation/recreate.py --runtime-root RUNTIME model --download-approved
```

下载严格校验文件大小与 SHA，失败不登记为 ready；构造模型不会静默联网下载整套资产。`pose --install` 只安装最小 CPU 依赖，不安装 Torch、Transformers、RTMLib、旧 MMPose/MMCV 或 CUDA。

## 最短本地执行路径

```text
<python> scripts/recreation/recreate.py inspect --source SOURCE --output EXTERNAL_SELECTION_DIR
<python> scripts/recreation/recreate.py init --source SOURCE --project PROJECT --roles ROLES_JSON
<python> scripts/recreation/recreate.py --runtime-root RUNTIME local --project PROJECT
<python> scripts/recreation/recreate.py status --project PROJECT
```

Agent 先展示 `selection.html` 和人物位置，确认后生成 `ROLES_JSON`；不要把检测候选编号当作已确认身份。`local` 按缺项继续 track → observe → preview，复用当前有效产物；也可分别调用 `track/observe/preview`。若使用自定义浏览器，给 `preview/local` 传 `--browser-path BROWSER`；观测可显式传 `--model-manifest MANIFEST`。具体字段与纠正入口见[项目格式](project-format.md)。

当前渲染只支持 `husky/golden` 及 `screen-space-2.5d`。该模式没有恢复真实相机、深度、地面或物理接触；低置信/缺失部位会隐藏并报告，不能据此宣称完整动物重定向已就绪。其他动物或世界空间模式会明确拒绝。

## 可选生成与更新

本地 CG 可独立交付，无需开通云模型。只有用户选择生成并完成本轮型号、素材、次数和预算确认后，才进入[独立生成适配器](generation-adapter.md)。它不继承旧 Key 或授权，HTTPS 引用只用标准库；仅 TOS 上传按需准备：

```text
<python> scripts/recreation/bootstrap.py --runtime-root RUNTIME --stage generate-tos --install --json
```

检查工具、账号开通、云上传、付费提交是不同步骤。没有模型权限不影响本地阶段；不得自动充值、开通、换模型或付费探测。云传输与状态流程已做离线测试，真实账号、TOS、计费和生成质量尚未实测。

更新 Skill 后保持相同外部 `RUNTIME` 与 `PROJECT`，先 `doctor`、`status`，保留模型、环境、候选、人工确认和已有远端任务。Blender、PyCOLMAP、MotionBERT、HuMoR、SMPL/SMPL-X、CUDA 或物理仿真仅是单独评估路线，没有被此 bootstrap 或默认 local 流程实现，不因机器上已有安装就宣称完成世界几何或全身重定向。

## 常见阻塞的恢复方向

| 现象 | Agent 处理 |
|---|---|
| 依赖 ready，但 pose execution_ready 为 false | 检查固定权重的实际路径、大小、SHA 与 manifest；只登记/准备缺项，不重装整套环境 |
| CG 中人物或肢体消失，报告含 missing | 定位源帧、身份框和观测证据；必要时补该人物的显式锚点再 `select/local`。不冻结上一姿态或把模型平滑当身份恢复 |
| 改锚框后原 CG 仍在磁盘 | 正常保留历史文件；读取状态确认 stale，从失效上游重算，不重新接受旧 SHA 绕过失效 |
| 本地已有当前 CG，`local` 只返回审阅入口 | 正常恢复行为；先审阅。需要修正时改变明确输入/配置并输出新候选，不因“继续”自动付费 |
| 模型准备进程中断后仍留 `.prepare.lock` | 文件存在不代表锁仍被占用；进程锁退出会释放，可正常再次检查。活动进程占用时等待/诊断，不删除活动锁强行并行 |
| 生成前提示 Windows 输出路径过长 | 在购买前选较短的外部项目目录；不要为绕过限制把项目或凭证放回可替换 Skill |
| 上传/提交超时且远端结果未知 | 读取原尝试日志，先只读核对。已知任务 ID 用 `status`；无 ID 保留未知，不能重 POST 或以新 run 掩盖未决购买 |

逐帧时间戳由真实源片验证；平均帧率差异、VFR 与非零首帧时间均不能通过改 FPS、补帧或重置原音来“修复”。先定位源时钟、解码相对时钟或观测绑定的差异。
