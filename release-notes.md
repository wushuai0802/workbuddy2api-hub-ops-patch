升级官方v1.7.24，吸收账号任务开关、打印式工具调用即时释放、断流标准错误帧、网页活跃排程与并发、Key账号绑定、仅可用模型、思考档位映射、全局提示词与看板修复，并继续保留DeepSeek防复读与一次安全恢复。

- 补丁：`v1.7.24-ops.20261021.1`；官方提交：`205d8b622d626f4b09da7b57efb3259f91403f4e`。
- 下载：`workbuddy2api-hub-v1.7.24-wushuai0802-ops-20261021-r1.zip` 与 `SHA256SUMS.txt`。
- 官方能力（v1.7.19～v1.7.24）由基线直接提供：`tasksEnabled` 与 `enabled` 分离、打印调用识别闭合标签后即时释放（#549）、流中途失败改发 `upstream_stream_error` 而不再伪造 `[DONE]`（#549）、网页活跃按小时窗口与并发/去重/线性退避、Key绑定账号、仅返回可用模型、思考档位映射、全局提示词正文、站点内容版本、看板热力图刷新、Windows便携版一键更新及外部执行器更新通道。
- 补丁新增三处流边界修复：拆分到达的收尾标签不再把 `DSML` 原文漏给客户端；打印式调用只在正常终止后放行，截断/报错时不合成工具；正文复读检测改用去除DSML的视图，工具参数中的重复字符串不再误判为正文复读。
- 保留混合/引用防复读、最多一次安全恢复、原始用量与实际模型、历史/工具图片、有界401刷新、阶段/总期限、定址恢复、SSE及连接所有权保护。原阈值和恢复提示默认不变。
- 保留Web日志与管理型部署停止保护。额度窗口探针原生默认开启；需要延续被动行为的部署应沿用`settings.window_probe=false`，公开安装器不修改设置或业务数据。
- 覆盖36补丁＋153原生共189组Python，以及52原生＋2补丁共54组Node；CI检查Linux3.11/3.12与Windows3.11。

仅支持准确干净的官方v1.7.24，完整Python3.11+。旧补丁需在新基线整体迁移；先备份旧镜像、源码、配置和数据，再定向部署唯一服务。保留账号、历史、挂载、Key和请求体限制。旧Release/tag继续保留。

没有额外真实模型长会话验收，不将健康或离线测试当作所有循环已消失。未构建精简便携运行时。MIT，保留官方及历史来源归属。

[说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.24-ops.20261021.1/README.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.24-ops.20261021.1/docs/INSTALL.md) · [验证](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.24-ops.20261021.1/docs/VERIFICATION.md)
