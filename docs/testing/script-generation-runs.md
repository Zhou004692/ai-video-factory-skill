# 候选剧本逐版生成离线验收

本轮修复针对三版完整剧本在6000/12000回答上限被截断，以及整组失败无法保留已完成内容的问题。新管理台创建持久化后台运行，每次只请求一个完整版本；保留旧同步调用方的兼容路径。

## 核查范围

- 单版使用严格JSON解析，人物、对白、场景、表演与导演字段必须完整；来源ID白名单同时覆盖画面要求中的引用。后台追踪字段从创作简报移出，来源正文保持原样。
- 请求发出前保存唯一尝试身份，模型等待时没有数据库写事务或业务锁；成功版本独立提交。
- 长度截断不自动重试；语法/契约修复只处理当前版本，每版最多两次尝试。网络结果未知不自动重发，继续需要用户确认重复费用风险。
- 刷新、输入修订、手动编辑候选、批准候选及服务中断均有对应保护；失效回调不能覆盖已编辑或已批准内容。
- 完整缓存与部分缓存分别显示。旧版一条合法候选保留其历史语义，不强行补成三条；推荐失败不阻止审核，也不将默认第一版伪称为AI推荐。
- 数据库升级采用新增表/列，不移动或重建现有来源、候选、人物、视频及费用记录。

## 自动验证

使用仓库现有Python运行时，`PYTHONPATH`同时加入 `scripts` 与 `tests`；部分历史HTTP测试仍使用裸测试模块导入，需要后一项。

```powershell
python -m unittest tests.test_script_generation tests.test_script_generation_management tests.test_script_generation_ui tests.test_script_drafts tests.test_llm_json tests.test_management_request_client tests.test_management_console tests.test_management_workflows tests.test_management_http_concurrency tests.test_server_security
node --check scripts/management_ui/app.js
git diff --check
```

所有模型返回通过本地夹具提供，无真实供应商调用。测试运行通过不代表真实模型的创意质量、时长或视频效果已获用户认可。

## 本地页面旅程

`tests/browser_script_generation_offline.py --output FRESH_DIRECTORY` 启动独立回环管理台，只创建离线来源、简报与候选。普通使用者无需运行此脚本，由Agent用于回归验收；已有输出目录的数据库不会覆盖。

页面确认生成后：前两版保存，第三版模拟超时进入未知状态；刷新仍应看到“已保存2/3版”。未勾选确认时点击继续不得新增调用。勾选后仅补第三版，再进行一次轻量推荐，最终显示3/3，前两版ID不变，候选全部保持未批准。

夹具调用顺序为：版本1成功、版本2成功、版本3未知、明确确认后版本3成功、比较推荐。实际供应商调用及视频生成均为零。关闭测试时创建输出目录的 `stop` 文件，测试服务正常退出，并关闭Agent自己创建的浏览器标签。

## 发布边界

代码需要通过正常Skill更新并重启管理服务后生效。源代码分支通过离线验证不代表当前运行的旧管理服务已经升级，也不授权Agent生成真实候选、批准剧本或确认视频费用。
