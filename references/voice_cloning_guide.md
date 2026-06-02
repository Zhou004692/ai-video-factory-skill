# 声音复刻操作指南(SeedTTS ICL 2.0)

> 把"老周"声音从默认 _uranus_bigtts 升级成**你自己/同事录音克隆出的专属声音**。
> 配合角色花名册(roster.yaml),形成"老周脸 + 老周声 + 老周梗"完整品牌 IP。

## 流程总览

```
1. 火山控制台创建 speaker_id (S_xxx)  ─┐
2. 录 30s+ 单人清晰音频              ─┤
3. py scripts/clone_voice.py 上传训练 ─┤
4. 等待几秒-几分钟 (ICL 2.0 秒级克隆)  ─┘
5. 把 S_xxx 填到 roster.yaml voice_id
6. 下次 job 自动用专属音色 (透明)
```

## Step 1:火山控制台创建 speaker

1. 打开 <https://console.volcengine.com/speech/service/9>
2. 进入"语音技术 → 大模型语音合成 → 声音复刻"
3. 点"创建音色",填名称(如"老周-Z01"),选 **ICL 2.0 模型**(秒级克隆,相似度 97.5%)
4. 控制台分配一个 `speaker_id`,格式 **`S_xxx`**(例:`S_a1b2c3d4`)—— **记下来**

## Step 2:录音规范

| 维度 | 要求 |
|---|---|
| 时长 | **30 秒-5 分钟**(30s 是底线,1-3min 最佳) |
| 单人 | **不能有第二个人说话/笑声/背景人声** |
| 环境 | 安静室内,无 echo,无空调风噪 |
| 内容 | 自然口语,**覆盖多种语气**(陈述/疑问/感叹/惊讶各几句) |
| 节奏 | 正常语速,**不要刻意慢/快** |
| 设备 | 手机直接录就行(iPhone/华为/小米),不需要专业麦 |
| 格式 | mp3 / wav / m4a / aac 都行 |
| 多段 | 同 speaker 可上传多段(最多 10 段),**特征取平均更稳** |

**示例录音脚本**(读这段 1 分钟左右)——
> "哎我跟你讲,租设备这行水深得很。我去年租一台挖机,合同上写月租 200,听着挺便宜对吧?结果用了 8 天想退,人家给我算账——3520 块违约金!你猜咋的?合同第 8 页附件第 3 条小字写'按 30 天起算'。我盯着那行字看了半天,字号 6 号,跟蚂蚁似的。哎呀!这就是签合同不看附件的下场。所以我跟你说,以后租设备啊,别光看销售嘴皮子,合同附件那一摞,一页一页给我翻!记住喽——便宜的不是月租,是销售嘴皮子。"

**为什么这段?** 覆盖"陈述/疑问/感叹/记住" + 真实工程老板用词("水深""签合同""挖机""违约金") + 1 分钟时长。

## Step 3:跑一键脚本

```powershell
# 单段
py scripts\clone_voice.py S_a1b2c3d4 path\to\laozhou_30s.mp3

# 多段(推荐,特征更稳)
py scripts\clone_voice.py S_a1b2c3d4 clip1.mp3 clip2.mp3 clip3.mp3
```

输出会显示:
```
[clone] 上传 N 段音频到 S_a1b2c3d4...
[clone] 上传响应: {...}
[clone] 等待训练完成 (ICL 2.0 秒级,服务端可能排队几分钟)...
[clone] S_a1b2c3d4 训练中 (status=1)...
[clone] S_a1b2c3d4 ready (status=2)
✅ 复刻完成!
```

## Step 4:填回 roster

编辑 `references/characters/roster.yaml`,找到对应角色:

```yaml
boss_laozhou:
  name: 老周
  gender: male
  voice_id: zh_male_dayi_uranus_bigtts  # 改这一行
  # 改成:
  voice_id: S_a1b2c3d4
  voice_note: "老周专属复刻音色(ICL 2.0 + 30s 录音)"
```

## Step 5:无需任何代码改动

[seedtts_v2.synth](../scripts/factory/clients/seedtts_v2.py) 检测到 `S_` 前缀**自动**:
- 切换 resource_id → `seed-icl-2.0`
- 跳过 expressive model / context_texts(复刻路径不支持)
- emotion / emotion_scale 也不传(strict 2.0 模式下本就不传)

下次跑 job 时,**老周用专属声音说话**,跨片人格完整。

## 常见问题

**Q: 训练失败 (status=3)?**
A: 检查音频时长 ≥30s + 单人 + 无杂音。重新录后重传。

**Q: 一个角色能克隆多个 speaker_id 备用吗?**
A: 可以。控制台多创建几个,roster.yaml 用主用的那个,备份的写在 `voice_note` 注释里。

**Q: 复刻音色支持 emotion 字段吗?**
A: ICL 2.0 路径**不消费 emotion/emotion_scale**(跟 strict 2.0 一样)。情绪靠 voice_directive 自然语言指令传给 _uranus 路径,**复刻路径目前不支持自然语言情绪指令**——但复刻本身就模拟原录音的情绪范围,通常够用。

**Q: 多少钱?**
A: 复刻训练本身免费(单 appid 免费额度内);合成调用按字符计费,跟 2.0 同价位 ¥0.0005/字符。

**Q: 复刻音色质量比 _uranus 好吗?**
A: 单论"自然度+真实感"明显更好(克隆真人,不是模型合成);情绪表达粒度可能略弱于 _uranus + voice_directive 组合。**适合做品牌 IP 角色(老周/王工)**,辅助角色继续用 _uranus 即可。
