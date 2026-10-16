升级官方 **v1.7.8**，迁移现有防复读与恢复补丁，并补齐缓存用量及断网期限边界。

- 补丁：`v1.7.8-ops.20261016.1`
- 官方提交：`1631d4cf091a50820e8aae8da07e58be24b2563e`
- 下载：`workbuddy2api-hub-v1.7.8-wushuai0802-ops-20261016-r1.zip` 与 `SHA256SUMS.txt`
- 纳入5.8.4国际版会话/追踪头及按账号隔离身份，针对上游11-128渠道校验；离线合同不代表所有线上错误已根除。
- 纳入失败计数时间窗、本机断网不惩罚账号、每日活跃网页可配置重试、未活跃名单补跑、冷却/配额模型回退、GPT最小输出16与413提示、新面板测试及DSH思考档位。
- 完善补丁Chat/Responses输出的缓存命中标准字段；响应独立于原始记账快照，保留provider原值和明细，不为收集用量多读上游。
- 新断网重试等待受原deadline约束；保留原混合/引用守卫、唯一安全恢复、连续失败前缀、阶段/总期限、401、池租约、SSE和匿名观察。
- 80组Python定向回归与41组官方Node面板测试，含新增身份/治理/活跃任务/缓存反例；Linux3.11/3.12与Windows3.11结果以本版Actions为准。

仅适用准确干净官方v1.7.8。旧补丁树请先保留，再在新目录整体安装、构建固定镜像；只切换目标服务。账号、配置、历史、请求体限制及挂载沿既有部署保留。新增每日任务重试与模型回退不扩大普通聊天恢复次数；一键账号测试会发送真实模型请求，安装器不会自动执行。

保留v1.7.6的公开默认：三个模型名会被改写到DeepSeek（主动同名请求也匹配），Messages/count_tokens默认关闭。`WB_BACKGROUND_MODEL_NAMES=""`保持原路由，`WB_ENABLE_ANTHROPIC_MESSAGES=1`保留旧接口；cache_control仍未透传。部署者按客户端需要显式选择兼容变量。

本次没有额外真实模型长会话验收，不保证业务循环全部消失。原Release/tag继续保留。

[说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.8-ops.20261016.1/README.md) · [安装/回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.8-ops.20261016.1/docs/INSTALL.md) · [验证](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.8-ops.20261016.1/docs/VERIFICATION.md)

MIT，保留官方及历史附件来源说明。
