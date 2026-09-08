# 测试26：多人共同台词误建角色

日期：2026-09-08。代码提交：`533f0f3`，已合入本地 main；未推送远端。稳定交付下一阶段仍暂存。

## 现象与根因

测试26选定剧本 `SCRIPT-e0a4a19549cfefd3`，视频任务 `J-TOPIC-24f362fa1c315927`。人物表有王工、林姐、张主管三人，但第 1、5、9 段共同台词使用 `speaker="wang_gong,lin_jie,zhang_zhuguan"`。旧逻辑将整个字符串当作一个人物 ID，自动补出第四个角色，并进入分镜、Creative IR、冻结生产图和人物锚点。

同框描述本身不是人物。修复将多人说话人保存为 `speakers` 数组，`speaker` 保留首人以兼容既有单值接口；画面人物仍由 `characters/cast` 表达。同框未说话者不被提升为说话人。共同台词只保留一份，导演提示词明确列出所有共同说话人。未知多人引用会在分镜生成前报错。生成提示约定、管理台人数与姓名展示、编辑保存同时修复。

## 真实任务修复

在正式库只读检查、SQLite 副本演练和执行契约校验通过后，运行专用脚本 `scripts/repair_test26_group_speakers.py`。脚本默认只预览，应用时必须保存 SQLite 备份；仅接受此任务的旧 spec hash 和锚点待审核状态。持写锁后再次比较完整受影响行，拒绝已提交母片、活动 Worker、状态变化或备份后的并发修改。

- 正式备份：`data/backups/test26-group-20260908T011848473520Z/factory.db`；同目录保存原行快照 `before.json`、修复后契约 `corrected-spec.json`、浏览器截图 `test26-fixed-script.png`。
- 正式状态仍为 `ANCHOR_REVIEW_REQUIRED`。现有 10 条母片未提交，时长、台词、场景和镜头顺序保留。
- 活动人物/定妆卡由 4 个恢复为 3 个。移除虚假角色的活动图节点，原节点及其事件归档至 `production_interventions`，原付费任务、锚点素材及账本不变。
- 三份有效人物锚点和七份场景锚点仍待人工审核；不自动批准图像，也不重做素材。
- 原 11 次 Provider 调用与总费用 2992 分均保留，其中误生成第四份人物锚点 272 分。这是已发生费用，未退款。
- 旧 hash：`9d6d621f961e97318da2a4b5bf46d6e0c117133b28fa55aeee55aa7c9d59bd08`。
- 新 hash：`b8edab829efc686468f2c6c8ba4fe180b5ae0994a6eb393bae09e2d70803cebc`。

通用付费生产图禁止替换的约束未放宽。修复是本事故、母片未开始前的审计迁移；重复执行会因旧契约不匹配而拒绝。其他历史剧本和任务未批量修改。需要回退时从备份选择性恢复本任务，不能在其他任务有新进展后整库覆盖。

## 验证与边界

相关 220 项测试通过：`tests.test_group_speaker_references`、`tests.test_script_drafts`、`tests.test_creative_ir`、`tests.test_production_adapters`、`tests.test_production_compiler`、`tests.test_scene_voiceover`、`tests.test_source_performance_ir`、`tests.test_character_voices`。

另 4 项 `tests.test_test26_group_repair` 通过，覆盖成功迁移与历史审计、活动任务拒绝、已提交母片拒绝、备份后数据变化拒绝。`node --check` 和 `git diff --check` 通过。

正式库 `production_invariants.audit` 无问题。管理台重载后，HTTP 查询确认 3 人、3 张人物定妆卡、10 条母片、2992 分费用和锚点审核下一步。Playwright 使用本机 Edge 打开真实项目“测试26”的剧本页，只允许 GET，断言显示“3 位角色”和“王工、林姐、张主管”，截图已人工视觉核对。

本次没有新增付费生成，没有重新执行完整 Flow Guard Full，也没有对现有锚点视觉质量、实际多人同步说话效果或最终视频质量作验收结论。用户可刷新管理台检查修复后的剧本与锚点，再决定是否继续生成。
