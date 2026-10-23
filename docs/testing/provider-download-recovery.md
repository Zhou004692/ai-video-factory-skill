# 已付费锚点下载与恢复回归

测试34暴露的链路：远端已生成成功，官方素材域名经本机DNS返回Fake-IP，下载器拒绝后节点落入FAILED；账本仍待核对，兄弟锚点被重复付费门禁暂停，而旧恢复入口只允许ACTION_REQUIRED。

## 下载边界

- 普通解析保持原路径。仅官方`.volces.com`域名、HTTPS默认端口、全部系统地址均处于`198.18.0.0/15`时，使用固定公网地址连接Cloudflare JSON DoH；只发送域名，不发送签名URL或供应商凭证。
- DoH保留TLS证书校验、官方Host/SNI、10秒超时，禁用环境代理和自动跳转。应答无效、为空或包含任一非公网地址即停止。
- 媒体实际连接固定到已验证公网IP；每次重定向重新验证。不直接访问Fake-IP、不豁免私网、不关闭TLS、不透传API凭证。
- 已知任务下载校验失败记录`provider_media_download_failed`和原task_id，进入待核对；不重新提交生成。GET核对返回unknown并保留原任务。

## 恢复与继续边界

- ACTION_REQUIRED或FAILED锚点均可按原任务恢复；要求唯一冻结节点、正确供应商与账本归属、未核对任务、无节点租约、任务空闲，外部IO后再次比较完整快照。
- 解码、预览、上传与账本更新完成后只进入PENDING_REVIEW，不批准媒体。原有费用只结算一次。
- 仅解除冻结规格一致、无付费任务/资产/费用/租约、依赖已满足的ProviderTaskGateError兄弟锚点；账本归属含糊时保留阻塞。
- 部分锚点待审核、其他锚点READY时，提供“继续准备剩余人物与场景”，返回预算预检。独立状态事件不会扩大母片返修或批准权限，不自动提交付费生成。

## 验证

运行`tests.test_fake_ip_media_downloads`、`tests.test_anchor_network_recovery`、`tests.test_resolved_provider_gates`，并回归制作失败矩阵、私网重定向、固定目标地址、身份冲突、终止和租约竞争。

用真实业务库的副本以及已下载的现有素材演练，云端调用替换为假回执和假上传，媒体解码使用真实FFmpeg。应当仅恢复一条待审核素材、解锁六个未提交锚点，新增生成调用为0。

参考：[Cloudflare JSON DoH](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)、[Mihomo DNS配置](https://wiki.metacubex.one/config/dns/)。
