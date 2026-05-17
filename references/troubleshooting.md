# 常见崩坏与修复

## 一、画面类

### 主角脸在第 3-4 镜后漂移
- **原因**:角色参考图权重不够,模型逐步"自由发挥"
- **修复**:
  - s3 prompt 里强制加 "严格保持参考图主角五官、发型、瞳色,不允许变化"
  - 把 character.png 分辨率提到 1024 以上,人脸占比 > 40%
  - 换用即梦"多镜头模式"——它对主体一致性优化过

### 第一镜画面太暗
- **原因**:风格底色 prompt 里"黄昏""暮色"被理解成"夜晚"
- **修复**:改成 "傍晚 6 点的金色阳光,逆光",或临时用 A 预设里的 prompt 替换为 "日间柔光"

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
- **原因**:TTS 实际时长 ≠ 预期
- **修复**:s4 前用 ffprobe 量出每镜实际长度,反过来调整口播文本长度;或最后用 `-shortest` 截齐

### BGM 比口播还响
- **原因**:`bgm_db` 默认 -14dB 不够低
- **修复**:在 `clients/ffmpeg.py:compose_final` 把 bgm_db 改成 -18 或 -20

## 三、API 类

### 429 限流
- **修复**:
  - `.env` 调低 `MAX_CONCURRENT_SHOTS=1`、`MAX_CONCURRENT_JOBS=2`
  - 换 provider:`DEFAULT_VIDEO_PROVIDER=kling`
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
- 接入数据回流后(目前未实装),用 `成本/完播率` 排序
- 当前阶段先靠人工判断 + 钩子文案打分
