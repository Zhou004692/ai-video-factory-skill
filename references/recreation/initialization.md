# 独立复刻初始化

面向使用者的解释、缺项提示、权重下载说明及服务配置引导见[使用引导](user-guide.md)。先检查本地条件，CG阶段无云Key要求；仅选择可选生成后才核对该账号的模型服务/API凭证，以及本次上传实现是否需要TOS，不能漏报存储依赖或提前阻断本地工作。

Blender属于可选的离线骨架后端评估工具，不是默认初始化依赖，也不是已实现的bootstrap阶段。仅检查媒体、提取姿态或检查骨架契约时不安装它；需要验证原生IK时可单独准备官方portable运行时并记录版本、来源、校验与实际运行结果。无需Windows安装包或NVIDIA。工具可启动不代表实片重定向已就绪，参见[工作流的源/目标分离边界](workflow.md)。

相机几何需要独立诊断时，可单独评估官方PyCOLMAP的CPU稀疏SfM，固定wheel版本/校验，放在复刻运行目录的专用环境，不添加到默认`pose`/`fit`/`render`集合。明确禁GPU、限制线程与资源，先小区间验证；不启用需要另行评估的密集重建、学习特征权重下载或远端服务。当前bootstrap没有`camera-sfm`阶段或完整相机适配器，不将某次机器安装写成已分发能力。

复刻属于本Skill的独立子功能。可以在没有旧管线`.venv`、`.env`、数据库、API密钥或管理服务的机器上开始。普通用户只提供素材和目标，以下命令由Agent处理。

## 最小阶段

| 当前工作 | 入口阶段 | 必要依赖 | 不应顺带安装 |
|---|---|---|---|
| 探测原片、保存SHA、抽帧/裁切、评估路线 | `inspect`（默认） | Python 3.10+标准库、FFmpeg、ffprobe | Python第三方库、姿态/大模型、Node、旧管线SDK |
| 从本地ViTPose WholeBody133模型提取姿态 | `pose` | 独立环境内NumPy、一个headless OpenCV、ONNX Runtime CPU；Skill内官方数值适配代码 | RTMLib、Torch、Transformers、CUDA、生成模型 |
| 已有骨架的可见性约束/固定骨长拟合 | `fit` | 独立环境内NumPy、SciPy | Torch、姿态模型、渲染服务 |
| 本地CG渲染与编码 | `render` | FFmpeg/ffprobe、Node.js 22+；实际渲染再准备独立浏览器、固定版本Three.js与动物rig | 管理台、TOS、ASR、TTS、GPU推理环境 |

阶段是能力检查，不是必须依次执行的全量安装清单。已有姿态数据可直接进fit；只审查视频不安装pose。若选用Python渲染器，可单独说明其依赖，不能为通过Node检查无意义地安装另一套渲染栈。

Seedance生成是可选阶段，不属于上述最小初始化，也不要求默认开通2.5或Mini。使用者可停在本地CG交付；需要生成时才按[模型选择与预算确认](workflow.md#可选seedance生成模型选择与预算确认)核对实际已开通且兼容的型号，询问选择并提示本次预算，确认后仅准备该型号必要凭证/适配器。没有模型服务权限不影响inspect/pose/fit/render就绪；不能将开通或充值门槛混入本地依赖检查。

```text
<system-python> scripts/recreation/bootstrap.py --stage inspect --json
<system-python> scripts/recreation/inspect_media.py <本地原片>
<system-python> scripts/recreation/bootstrap.py --stage pose --check-only --json
<system-python> scripts/recreation/bootstrap.py --stage pose --install --json
<system-python> scripts/recreation/bootstrap.py --stage fit --install --json
<system-python> scripts/recreation/bootstrap.py --stage render --check-only --json
```

默认只检查并记录所选阶段；`--check-only`完全只读；`--install`才创建所选阶段环境并安装Python依赖，绝不下载模型权重或构造会自动下载权重的模型。FFmpeg、Node和浏览器为外部工具，缺失时先定位该项，再按宿主平台补齐，不调用旧`install.py`。

## 状态隔离与更新

- 仅识别`AI_VIDEO_RECREATION_HOME`或`--runtime-root`，不读取`FACTORY_*`或旧`.env`。默认Windows为`LOCALAPPDATA/ai-video-factory-recreation`，macOS为用户Application Support下同名目录，Linux为XDG data home下同名目录。
- 可替换Skill目录只放代码和参考。状态、依赖、模型和项目在外部：`runtime.json`、`venvs/pose/`、`venvs/fit/`、按需建立的`models/`和`projects/<项目>/`。初始化不扫描或复制旧业务数据。
- 每个项目保存自己的原片清单、源帧时间映射、身份映射、观测与裁决、拟合、rig/相机配置、渲染记录、媒体和验收。模型版本、哈希和关节schema绑定产物。不可依赖某台机器的output或临时实验目录。
- 更新Skill后用相同外部根目录重新检查。保留既有项目、模型、阶段环境和历史候选；不删除旧成果或把新版本数据倒写到旧记录。
- 不从旧管线自动继承密钥、费用预算、人物授权或生成任务状态。

## 模型与可选重依赖

`pose --install`只安装NumPy、headless OpenCV和ONNX Runtime CPU。ViTPose预处理/解码由Skill内带来源与许可的官方函数适配，不要求安装旧MMPose/MMCV/timm/RTMLib。原生PyTorch仅在权重转换或交叉验证时单独隔离使用，不进入最小用户运行依赖。已有环境里的旧包不自动删除，也不调用旧姿态模型作回退。

依赖检查同时验证版本和唯一OpenCV发行包，当前只接受正式数字发布版本，不将rc/dev/post版本视为已验证版本。姿态阶段依赖ready后，仍需检查实际选定的本地ONNX权重、输入尺寸、WholeBody133关节顺序、任务头、解码器、来源与SHA，详见[ViTPose](vitpose.md)。已缓存且合适时复用；没有时先说明下载来源、大小和许可，按明确模型文件准备，禁止用构造器静默拉取整套权重。`ready`字段只覆盖阶段依赖，不表示权重或效果就绪。

MotionBERT等三维提升器、HuMoR、GVHMR、CUDA、SMPL/SMPL-X和物理仿真均不属于最小初始化。有明确收益才评估源码、权重、运行条件、许可和成本，单独隔离依赖；不能把单人固定镜头demo直接当双人移动镜头方案。缺少可用三维路线时可做标注清楚的2D/2.5D参考，不把它称为真实世界运动恢复。

## 返回与恢复

| 状态 | Agent动作 |
|---|---|
| `ready` | 所选依赖可用，继续检查本阶段实际输入、模型或rig |
| `dependencies_required` | 只准备所选阶段依赖，或复用已验证的独立环境 |
| `runtime_required` | 只修缺失外部工具或无效外部目录 |
| `dependency_install_failed` | 保留项目，说明失败项；修复包源/平台兼容性后有界重试 |

初始化与本地检查应报告`paid_provider_calls=0`、`source_uploads=0`和`model_downloads=0`。安装依赖可能访问包源，与付费视频生成是不同动作。真实GPU/第三方付费服务/素材上传/Seedance提交分别授权；“继续免费分析”不包含这些许可。
