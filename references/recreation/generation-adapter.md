# 独立可选视频生成适配器

入口为 `scripts/recreation/generation.py`。生成状态与 HTTPS 传输只依赖标准库、本目录 `project_store.py`、FFprobe；下载后的本地包装另用 FFmpeg 和 `generation_package.py`。仅选择 TOS 上传时额外依赖 `tos`，SDK 与配置检查在创建 PUT journal 之前完成，缺 SDK 不消耗一次 PUT 机会。不导入管理管线、业务数据库、旧配置读取器，不加载 `.env`、环境变量中的 Key 或历史授权。项目与配置文件必须放在可替换 Skill 以外。

本适配器已经离线验证状态和传输边界，**没有使用真实供应商账号进行上传、提交、权限探测或生成质量验证**。选择模型前由使用方核实当前账号已开通的型号；不自动开通、充值、切换模型、重试购买或返修。

## Agent 执行流程

1. 读取项目当前 `cg_silent`，确认 `status=accepted`、当前 acceptance 与实际 SHA 一致；`project_store.get_artifact` 同时复验原片、角色绑定与媒体。CG 需无音轨；可选身份图使用已接受的 `identity_*` artifact，人工确认其为合成动物图。原片与原音只供本地项目核验，不进入请求。
2. 根据使用方已开通型号、兼容项、当前价格和此次预算，准备独立账号选择与价格依据 JSON。`prepare` 只做本地工作，可先完成，无需上传。输出 `run_id`、`plan_path`、`plan_sha256`。
3. 读取并向用户展示具体冻结方案：型号、账号/区域、CG 与图片哈希、prompt、参数、价格依据、保守估计、预算、**仅一次付费生成且零自动重试**，并明确原片/原音不上传。2.0 `duration=-1` 会由模型自主选长度，不能承诺输入片长。用户此前接受 CG、提供 Key、旧项目预算或旧确认都不构成本次购买授权。
4. 只有用户本轮明确同意这个方案及上传合成输入后，才调用 `approve`，`--user-confirmation` 记录用户实际表述，不由 Agent 生成“用户已同意”。确认绑定此次 run ID 和完整方案哈希；修改媒体、角色、型号、价格、prompt 或预算需重新准备、重新确认。
5. 执行 `upload`。已有 HTTPS CG 可只读下载校验完整 SHA，或通过 TOS 执行一次私有 PUT，随后通过模型将使用的 HTTPS URL 读回完整字节校验。URL 文件名、对象 HEAD/ETag、大小相同都不能替代 SHA 验证。图片在提交时以内联 base64 发送，仅发送批准的图片字节。
6. 执行一次 `submit`，持久 journal 在 POST 前排他创建并 fsync。拿到任务 ID 后只能 `status` GET 查询和 `download` GET 下载。无 ID、超时、5xx、响应解析失败、进程中断或明确拒绝后都不再 POST。
7. 下载保留供应商原始字节和真实 FFprobe 流、逐帧 PTS、帧数、帧率、时长信息。`model-original.bin` 是原生媒体，不修改编码；扩展名不影响 FFprobe。随后自动在版本化 `delivery/` 中生成 `model-original-audio.mp4`、`comparison.mp4`、`review.html` 和 `report.json`：原音版复制全部原生视频帧，仅在本地对齐原音；对比左为模型结果、右为原片裁切，只覆盖实际共同时间窗。原音可能早于原生视频结束；无原音则包装仍静音。源时钟和下载视频覆盖分别报告，不循环、冻结或变速补足片长，不把封装音轨时长写成视频覆盖时长。
8. 原片已变化、缺少真实末帧时长或包装失败时，原生下载文件仍保留，报告 `local_packaging_failed_original_preserved`。修复本地条件后可运行 `package`，不需要凭证或云请求；不以包装失败重新购买。每次包装另存版本，不覆盖旧审阅结果。

## 已编译的兼容子集

以下是本适配器实现的保守子集，非供应商完整能力表。`--model` 无默认值，不会选择 Mini 或 2.5。没有列出的模型、接入点别名、区域和参数直接拒绝。

| Model ID | CG 输入时长 | 本适配器输出分辨率 | 输出时长参数 | 输出容器参数 | 2.5 任务类型字段 |
|---|---|---|---|---|---|
| `doubao-seedance-2-0-260128` | 2–15 秒 | 480p / 720p | 整数 4–15 或 -1 | 仅 MP4，省略 `output_format` | 不发送 |
| `doubao-seedance-2-0-fast-260128` | 2–15 秒 | 480p / 720p | 整数 4–15 或 -1 | 仅 MP4，省略 `output_format` | 不发送 |
| `doubao-seedance-2-0-mini-260615` | 2–15 秒 | 480p / 720p | 整数 4–15 或 -1 | 仅 MP4，省略 `output_format` | 不发送 |
| `doubao-seedance-2-5-260628` | 4–30 秒 | 480p / 720p | 必须 -1 | MP4 / MOV，显式 `output_format` | `omni_reference_task_type=edit` |

全部 profile 固定 `ratio=adaptive`、`generate_audio=false`，只支持一个静音 CG 视频和最多九张合成身份图。输入视频需 MP4/MOV、H.264/H.265、24–60fps、宽高各 300–6000px、像素数 407696–8295044、宽高比 0.4–2.5、≤200MB。身份图限制为真实 PNG/JPEG 单帧、宽高各 300–4096px、宽高比 0.4–2.5、≤10MB。未知/不兼容规格会拒绝，不自动转码或切片。

虽然普通 2.0 官方支持更高分辨率，本版仅实现上述子集。Fast、Mini 不能继承普通 2.0 的 1080p/4K；2.0 系列不能发送 MOV 或 2.5 的 `omni_reference_task_type` 字段。2.5 编辑提示需明确表达编辑意图，输出可能稍短于输入；最终时间以下载媒体 probe 为准。

2026-10-20 核对的一手来源：

- [Seedance 2.0 官方能力表](https://docs.volcengine.com/docs/ark/seedance-2-0?lang=zh)：三个独立型号 ID、分辨率、MP4、参考视频与静音输出支持。
- [Seedance 2.5 官方文档](https://docs.volcengine.com/docs/ark/seedance-2-5?lang=zh)：编辑模式、adaptive/-1、4–30秒及 MOV。
- [创建视频生成任务 API](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)：视频输入约束、各型号参数支持范围。
- [模型开通与计费规则](https://docs.volcengine.com/docs/ark/seedance-model-activation-usage-and-refund?lang=zh)：开通条件与付费口径。供应商可能更新文档；执行时需重新核对使用方实际开通与报价。

## 外部配置

以下仅为字段示例，值必须由使用方提供。`account.json` 不放 Key：

```json
{
  "account_id": "selected-account-id",
  "region": "cn-beijing",
  "enabled_models": ["explicit-model-id-from-table"],
  "entitlement_evidence": "本轮核对所选账号控制台的开通状态及时间"
}
```

`pricing.json` 必须包含全部字段；不得照搬示例金额、时长、日期：

```json
{
  "model": "explicit-model-id-from-table",
  "account_id": "selected-account-id",
  "region": "cn-beijing",
  "resolution": "720p",
  "currency": "CNY",
  "upper_cost_cents": 100,
  "basis": "本轮账号实际价格、输出窗口与参考视频费用的保守上界计算，记录公式与所用费率",
  "source_url": "https://docs.volcengine.com/docs/ark/model-pricing",
  "checked_at": "ISO-8601 with timezone, within 24 hours",
  "valid_until": "ISO-8601 with timezone, current quote expiry",
  "covered_output_seconds": 4,
  "includes_reference_video_cost": true
}
```

`upper_cost_cents` 需不超过本轮预算。报价覆盖当前账号、区域、分辨率、参考视频和最长输出窗口：2.0 的 `-1` 至少按 15 秒覆盖；2.5 编辑至少覆盖 CG 实际时长。Agent 必须核对报价计算，不得填一个任意数字绕过门禁。**预算是提交前本地估计上界检查，供应商接口没有由此设置硬账单上限，无法保证最终扣费绝不超出估计。** 无法获得可信保守报价时不执行；不自动付费探测。

`credentials.json` 必须显式传入，严格接受以下字段，不继承环境或旧配置：

```json
{
  "account_id": "selected-account-id",
  "region": "cn-beijing",
  "ARK_API_KEY": "<ARK_API_KEY>"
}
```

TOS 模式再显式加入 `VOLC_AK`、`VOLC_SK`、`TOS_BUCKET`、`TOS_REGION`、`TOS_ENDPOINT`。本版只支持 `cn-beijing` 与对应官方 endpoint；无存储默认值。SDK `max_retry_count=0`、`follow_redirect_times=0`，私有 ACL、禁止覆盖。已有 URL 模式不需要 TOS SDK/AK/SK。

已有 URL 用外部 `reference.json` 提供：`{"url":"https://host/exact-cg?signature=...","expires_at":"ISO-8601-with-timezone"}`。上传/提交前至少剩十分钟，每次提交前再读回 SHA。URL 与完整供应商响应仅存项目内 `*-private.json`，不要发送到聊天、纳入分发包或版本库；应为项目目录设置合适文件权限。日志与 CLI 输出会脱敏 URL 和 Key。TOS Key 与 ARK Key 不写入方案、journal 或私有响应。

## CLI 契约

由 Agent 运行所在 stage 的 Python；不要求用户手动执行。下列 `PROJECT`、`RUN`、配置路径和选项均由本轮方案填入。

```text
python scripts/recreation/generation.py prepare PROJECT --model MODEL --prompt-file PROMPT --account-file EXTERNAL_ACCOUNT_JSON --pricing-file EXTERNAL_PRICING_JSON --budget-cents BUDGET --duration DURATION --resolution RESOLUTION [--output-format mp4|mov] [--image-artifact identity_NAME]
python scripts/recreation/generation.py approve PROJECT RUN --plan-sha256 EXACT_HASH --user-confirmation ACTUAL_CURRENT_USER_STATEMENT --synthetic-inputs-only
python scripts/recreation/generation.py upload PROJECT RUN --credentials-file EXTERNAL_CREDENTIALS_JSON [--reference-config-file EXTERNAL_REFERENCE_JSON]
python scripts/recreation/generation.py submit PROJECT RUN --credentials-file EXTERNAL_CREDENTIALS_JSON
python scripts/recreation/generation.py status PROJECT RUN
python scripts/recreation/generation.py status PROJECT RUN --credentials-file EXTERNAL_CREDENTIALS_JSON
python scripts/recreation/generation.py download PROJECT RUN --credentials-file EXTERNAL_CREDENTIALS_JSON
python scripts/recreation/generation.py package PROJECT RUN
```

`status` 不带凭证只读取本地 journal。带凭证仅 GET 当前已知任务，不因媒体失效或报价过期阻止查询/下载。查询失败不会切模型或创建新任务。上传后如需续签或恢复，在保持同一已批准字节的前提下运行：

```text
python scripts/recreation/generation.py verify-upload PROJECT RUN --credentials-file EXTERNAL_CREDENTIALS_JSON [--reference-config-file EXTERNAL_RENEWED_REFERENCE_JSON]
```

`verify-upload` 仅签名/GET，绝不 PUT。`upload-journal.json` 已存在时 `upload` 永不再次尝试。`submit-journal.json` 已存在时 `submit` 永不再次尝试；即使是 in-flight、空/损坏文件、明确 4xx 或进程重启也一样。不要删除 journal、复制已批准 run 或新建 run 绕过未知结果。未知提交且未取得 ID 时，只能由人工核对供应商控制台；本版未提供把猜测任务 ID 绑定回项目的入口。

Windows 执行前会校验计划、journal 与最长本地 delivery 最终路径均不超过可移植的 259 个 UTF-16 字符；过深时在 prepare/approve/upload/submit 前停止，先选择更短的外部项目目录。原子写入使用短 `.g-<12hex>.tmp` 兄弟文件，不再次拼接完整文件名。此机制不宣称支持任意长路径。delivery 创建前同时解析并校验仍在 run 和 project 内，目录 junction/symlink 不能把媒体写出项目。

对比以下载媒体的实际帧 PTS 为主时钟；源片末帧允许在其真实显示时长内被采样，并在真实共同窗口结束。输出帧数限制为该窗口实际存在的原生采样帧，避免较高源帧率在 native EOF 后复制模型末帧；逐帧时钟不符时拒绝包装，不扩展生成覆盖。

## 离线验证

运行 `python -m unittest tests.test_recreation_generation`。测试使用临时文件与 fake transport，覆盖当前接受与哈希、角色变化、图片变化、静音 CG、型号切换、Mini/MOV 不兼容、预算与报价窗口、未确认门禁、完整远端 SHA、缺 SDK 不消耗 PUT 机会、一次 PUT 与超时恢复、一次 POST、并发、超时/无 ID/5xx/拒绝、GET-only、Key/URL 脱敏及原始文件不补时长。另用实际 FFmpeg fixture 验证 project_store 接受、MP4/MOV、静态 PNG，以及短原生结果的原音回装、不同帧率对比和审阅包装保持原生帧数/覆盖。

未验证项包括真实账号权限、真实 TOS PUT/签名、供应商异步服务、最终计费和生成质量。这些仅能在未来获得该次型号、素材、预算及次数的明确确认后实测。
