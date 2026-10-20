# 独立复刻项目格式与恢复

`scripts/recreation/project_store.py` 管理外部项目，schema 为 `animal-recreation-project-v1`。Agent 使用 `recreate.py` 读写，不手工编辑带校验的 `project.json`。代码更新可以替换 Skill；项目、模型、阶段环境、凭证与生成任务保留在 Skill 之外。

## 目录与入口

```text
PROJECT/
  project.json
  tracks/<version>.json
  observations/<version>.json
  candidates/<version>/
    cg-silent.mp4
    cg-original-audio.mp4
    comparison.mp4
    review.html
    report.json
  generation/<run-id>/
    plan.json
    approval.json
    upload-journal.json
    submit-journal.json
    reference-private.json
    provider-response-private.json
    model-original.bin
    download.json
    delivery/<version>/
```

目录仅示意已执行阶段会创建的内容；未生成时不会有云端 run。原片按 `source.path` 指向用户提供的原始文件，不复制旧业务数据库。私有配置放 Skill 外并设置适当文件权限，不进入压缩包、聊天或版本库。

## 项目字段

| 字段 | 内容与约束 |
|---|---|
| `schema`、`project_id` | 固定 schema 与本地 UUID |
| `source` | 原片绝对路径、真实 SHA256、尺寸、FPS 元数据、解码帧数、每帧 PTS/时长、裁切及音轨数 |
| `roles` | 当前角色 ID、目标动物及显式身份锚点，不自动设定人数或动物 |
| `binding_sha256` | 当前源清单与角色的共同绑定；角色/锚点纠正会改变 |
| `artifacts` | 当前轨迹、观测、CG 与审阅等产物的版本、实际 SHA、依赖与人工接受 |
| `events` | 初始化、角色纠正、注册、失效与接受等事件；保留旧候选记录 |
| `stage` | `initialized`、`needs_review` 或 `accepted` 等项目摘要；不能代替逐 artifact 状态或购买授权 |
| `state_sha256` | store 原子发布时生成的完整性校验，不是安全签名或人工验收 |

`source.frames` 每项为 `{index, pts_seconds, duration_seconds}`，按真实解码顺序从 0 编号。不能用 `index/fps` 重写 VFR 时钟，也不假设首帧 PTS 为 0。末帧需要实际时长；缺失不能默认补一个 CFR 帧。源片 SHA 或帧清单校验失败时停止复用，保留原文件并建立明确的新项目/版本，不篡改旧接受记录。

## 人物选择与锚框

先运行 `inspect` 生成本地 `selection.html`、首帧及 `source-facts.json`，让用户用自然语言确认人物位置及映射。较晚入画人物使用其真实出现帧；示例中数字仅说明格式，Agent 必须根据本次画面填写：

```json
{
  "roles": [
    {
      "id": "lead",
      "animal": "husky",
      "anchors": [
        {"frame_index": 0, "bbox_xyxy": [10, 10, 200, 400], "provenance": "user_reviewed"}
      ]
    }
  ]
}
```

`roles` 文件也可直接为数组。角色 ID 唯一；锚点帧号唯一且必须落在解码范围。`bbox_xyxy` 是**裁切画面内坐标、包含端点**，须落在裁切边界内，不能混入整幅源图坐标。只有确实由人复核时才标记 `user_reviewed`，估计锚框保留估计来源。当前犬体资产仅支持 `husky` 与 `golden`；store 可以记录其他动物，但 renderer 会拒绝，不以错误犬体代替。

```text
<python> scripts/recreation/recreate.py init --source SOURCE --project PROJECT --roles ROLES_JSON [--crop x,y,width,height]
<python> scripts/recreation/recreate.py select --project PROJECT --roles CORRECTED_ROLES_JSON --confirmation ACTUAL_REVIEW_STATEMENT
```

`init` 不覆盖已有项目。`select` 是明确纠正入口：改变角色绑定、将旧产物标为 `stale`、移除当前接受并保留历史。它不删除旧媒体；随后 `local` 从缺失或失效阶段继续。

## 跟踪与观测

`track` 使用 CPU 稀疏 LK 前后向光流，只做保守的平移框传播，不估计尺寸/旋转变化，不自动重新获取身份。首锚之前、纹理不足、大位移、出界、交叉/重叠或流支持不足时保持 missing，直到新显式锚点。传播结果均需审阅；HOG 只是候选帮助函数，不是稳定身份系统。

`observe` 读取已注册 tracks 和固定 WholeBody133 模型，原始 `xy133/scores133`、身份来源、缺测与不确定性都保留，不把 17 点模型伪装为全身观测。133 点被保存不代表当前默认 CG 已完整重定向手指、脸部、足部、情绪或隐藏动作。姿态与输出资产契约另见 [ViTPose](vitpose.md)。

## Artifact 与人工接受

artifact 主要字段为 `name/path/sha256/status/source_sha256/binding_sha256/group_id/depends_on/recorded_at`。路径必须是项目内部实际文件，禁止目录逃逸、源片冒充输出或失效媒体复用。`depends_on` 保存依赖名称和对应 SHA；读取/接受时递归复验依赖文件。

renderer 的 `cg_silent/cg_audio/comparison/review/render_report` 一次注册为同一 `group_id`，干净 CG 依赖同组审阅产物以及观测。产物默认 `needs_review`，注册不能直接声明 accepted；用户实际审阅后才执行：

```text
<python> scripts/recreation/recreate.py accept --project PROJECT --artifact cg_silent --sha256 EXACT_CURRENT_SHA --confirmation ACTUAL_CG_REVIEW_STATEMENT
```

接受记录为 `acceptance={sha256,source_sha256,binding_sha256,group_id,user_confirmation,at}`。命令前后都检查真实字节。新 tracks、observations 或 CG SHA 注册会使旧下游失效；同组对比、报告被改动也会阻止继续使用 CG。旧记录留在 `history/events`，旧文件不覆盖。

`accepted` 只对应这个具体产物；不表示整个项目所有内容已通过，更不表示上传/付费许可。CG 接受、当前生成计划确认、供应商任务完成和最终观感接受分别处理。

## 时间、原音与两个交付层

本地 CG 沿源帧 PTS 生成，导出按共同源时钟对齐并核验真实帧数/覆盖。首帧时间原点在媒体包装时明确归零；有原音时仅在本地转换并对齐，超出视频覆盖的样本裁剪，初始音频间隙按报告说明。源片无音轨时不合成假原音。`cg_silent` 才能进入纯 CG 云参考，`cg_audio` 只供本地审阅。

云端下载是另一套原生时钟：`model-original.bin` 保留模型原始字节，`download.json` 保存真实 probe。它可能比 CG 短，也可能有不同帧率；API 的整数 `duration` 不代替真实帧 PTS。后续 `delivery/` 的原音版保持原生视频帧，对比只覆盖实际共同窗口，不补长视频。纯本地包装失败可用 `generation package` 重做，不重新购买。详见[生成适配器](generation-adapter.md)。

## 恢复与更新

Agent 先 `status --project PROJECT`，再按缺项运行 `local`；该入口复用当前有效轨迹、观测和 CG，不覆盖历史候选。角色/输入变化必须走显式接口，不直接改校验字段。阶段依赖与模型准备见[初始化](initialization.md)。

云端 run 自己保存冻结计划、当前确认和 PUT/POST 排他 journal，不借用 `project.stage`。有 task ID 时只 GET 恢复；无 ID 或结果未知时人工核对供应商，不能删除 journal 或复用已消耗授权再次 POST。更新包不包含原片、模型、凭证、签名 URL、私人实验路径或项目目录。

Windows 可选生成在准备及变更门禁前检查全部最终输出路径的可移植长度，包括 `generation/<run>/delivery/<version>/`；太深时改用较短的外部项目目录，不能等付费后才搬动 journal。它不提供任意长路径保证。delivery 的 junction/symlink 逃逸会在创建或编码前拒绝。
