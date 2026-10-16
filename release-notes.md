v1.7.10 运维补丁 r2：加固首帧缓存读取边界，补齐分块记账与官方回归。

- 补丁：`v1.7.10-ops.20261016.2`
- 官方提交：`7e5c38dd18d78e6582f6f5730bc2f050c601d142`
- 下载：`workbuddy2api-hub-v1.7.10-wushuai0802-ops-20261016-r2.zip` 与 `SHA256SUMS.txt`
- 首帧包装器拒绝未实现的原始读取方法，避免绕过已缓存前缀/剩余块；保留计费模型、期限、usage观察、flush与连接所有权。
- 新增4项回归，包含16组读取边界和三种分块×六路径的18组真实记账字段检查，不增加重放。
- 六条流/非流路径将上游finish_reason=error报告为失败，保留已接收provider用量和可见正文；不增加恢复、不重放工具、不把失败打印调用合成为工具。
- 实际计费模型与原请求别名独立记账，跨首帧包装/安全恢复/错误/取消正确归属；可选积分回退继续使用原deadline与账号排除条件，关闭时保持原403/提示词保护路径。
- 保留原生Messages/积分回退面板开关、每Key并发（0/空值不限，超限429）、DSH视觉配置。环境变量与面板启用值是OR关系，旧环境变量为1时面板关闭不能抵消。
- 默认不启用积分回退；启用可能将全池6004后的DS4.1F切到积分版-sg并消耗积分。cache_control仍未透传，识图配置不等于实际模型读图保证。
- 保留混合/引用守卫、唯一安全恢复、历史/图片兼容、阶段/总期限、401独立刷新、SSE/池租约和匿名观察；新增固定upstream_finish_error原因及信号。
- 离线覆盖34组补丁＋全部115组官方Python，另有41组官方Node；Linux3.11/3.12、Windows3.11以本版Actions为准。保留原生断言，隔离适配打包清单/stdlib和子进程设置目录；未构建或认证精简便携运行时，使用完整Python3.11+。

仅安装到准确干净的官方v1.7.10；旧补丁树需在独立基线整体迁移。先备份镜像/源码/配置/数据，定向切换唯一服务，保留原凭据/模型路由/挂载/历史/请求体限制。公开安装器不修改配置或业务数据。

原来用环境变量开启Messages的部署，若要让面板接管，应在备份停服后保留settings.enable_anthropic_messages=true并清空旧环境变量；积分回退建议继续关闭。关闭配额回退不是防复读策略，原聊天恢复仍最多一次。

没有额外真实模型长会话验收，不保证所有业务循环消失。旧Release/tag保留。MIT，保留官方及历史来源归属。

[说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.10-ops.20261016.2/README.md) · [安装/回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.10-ops.20261016.2/docs/INSTALL.md) · [验证](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.10-ops.20261016.2/docs/VERIFICATION.md)
