# 常见崩坏与修复

## 一、画面类

### 主角脸在第 3-4 镜后漂移
- **原因**:角色参考图权重不够,模型逐步"自由发挥"
- **修复**:
  - s3 prompt 里强制加 "严格保持参考图主角五官、发型、瞳色,不允许变化"
  - 把 character.png 分辨率提到 1024 以上,人脸占比 > 40%
  - 换用即梦"多镜头模式"——它对主体一致性优化过

### 第一镜画面太暗
- **原因**:该分镜自己的 `lighting` / `scene` 写了"黄昏""暮色"等低照度词，模型将其理解成夜晚。旧版任务也可能在 storyboard 中残留全片 `style_prefix`，但当前管线会忽略该字段
- **修复**:只修改该镜的光线描述，例如"阴天自然散射光"或"日间柔光"；不要再给全片套统一风格前缀

### 文字/招牌全是乱码
- **原因**:目前所有视频模型都画不准中文/英文
- **修复**:分镜 prompt 不要写"招牌写着 XX""书上写着 XX",改成"特写书页(不展示文字)"

### 多人同框五官全崩
- **原因**:模型同时画两张脸会塌
- **修复**:改用过肩镜头/背影/单人特写交替,避免双人正面同框

### 手部动作扭曲
- **原因**:手是 AI 视频死穴
- **修复**:prompt 不要描述"双手做 XX",改成"上半身动作""单手轻举"

## 二、合成类

### ffmpeg 报 "Non-monotonous DTS"
- **原因**:分镜间帧率/编码不一致
- **修复**:s3 生成时统一指定 fps=30;或 compose 前用 ffmpeg 先转码:
  ```
  ffmpeg -i in.mp4 -c:v libx264 -r 30 -c:a aac out.mp4
  ```

### 拼出来的画面有黑边/闪屏
- **原因**:分辨率不一致被自动缩放
- **修复**:强制每段都用 1080x1920,或合成时加 `scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920`

### 口播和画面对不上
- **原因**:单镜台词过长、提示词中同时要求第二个人声，或 2.5 原生口型偶发失配
- **修复**:把单镜控制在 4—12 秒并缩短 `voiceover`；确保提示词要求“完整说出且只能说出”；查看 `video_qa` 中的 BigASR 转写，不通过时用 `retry-shot` 重生该镜

### BGM 比口播还响
- **原因**:`bgm_db` 默认 -14dB 不够低
- **修复**:在 `clients/ffmpeg.py:compose_final` 把 bgm_db 改成 -18 或 -20

## 三、API 类

### 429 限流
- **修复**:
  - `.env` 调低 `MAX_CONCURRENT_SHOTS=1`、`MAX_CONCURRENT_JOBS=2`
  - 保持同一 Seedance 2.5 provider，等待限流窗口恢复或在控制台申请提额；不要临时切换未验收模型
  - 火山方舟单 key QPS 默认很低,要去后台申请提额

### Seedance 任务一直 running 不结束
- **原因**:复杂 prompt 卡队列
- **修复**:`cli.py retry-shot <job_id> <idx>` 重跑;或简化 prompt(< 50 字)

### Claude 输出 JSON 解析失败
- **原因**:模型偶尔加了 markdown 围栏或解释文字
- **修复**:`clients/claude.py:_extract_json` 已经做了 3 重容错,如果还失败,把 system prompt 加强成 "禁止任何 markdown、禁止解释、只输出 JSON,任何额外字符都将导致系统崩溃"

## 四、发布类(视频号 RPA)

### 视频号上传后审核不通过
- **原因 90%**:AI 标识检测 + 敏感词
- **修复**:
  - 跑 `references/prompts/compliance.md` 复检脚本
  - 标题不要写"AI 生成"反向触发,但视频内可以加"由 AI 创作"水印满足合规
  - 涉敏词换说法:"股票" → "投资""赚钱" → "增长"

### RPA 上传失败
- 持久化 profile:`channels.weixin.qq.com` 扫码登录一次后保存 cookies
- 文件大小:控制 final.mp4 < 500MB
- 节奏:每条间隔 ≥ 30 秒,模拟真人

### 视频号 RPA 发布(参考实现路径)
1. 用 `playwright codegen https://channels.weixin.qq.com/platform` 录制扫码登录
2. 保存 storage_state.json
3. 写 publish 脚本:加载 state → 进入发布页 → 上传 mp4 → 等转码 → 填标题/话题/合集 → 点击发表
4. 加 #AI生成 标签满足平台要求
5. 多账号:每个号一个独立 profile + 独立 IP(住宅代理)

## 五、成本类

### 一条视频花了远超预期
- 看 `cli.py inspect <job_id>` 里 shots 的 `cost_cents`,定位哪一镜烧钱
- 通常是 s3 反复重试导致——把 `MAX_RETRIES` 调低到 1,失败立即 fail 不重烧

### 想知道哪个主题 ROI 最好
- 先用 `publication-map` 绑定准确平台作品 ID，再导入 24/72/168 小时绩效快照
- 通过 `learning-report` 和预注册实验报告对比成本、完播率与线索；样本不完整时不得输出胜出结论
