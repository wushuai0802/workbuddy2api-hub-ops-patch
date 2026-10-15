升级官方 **v1.7.6**，保留现有防复读、恢复、历史、协议与连接保护。

- 补丁：`v1.7.6-ops.20261015.1`
- 官方提交：`7dbe11564c0c74a1d2cdb05791b313e339dddbf5`
- 下载：`workbuddy2api-hub-v1.7.6-wushuai0802-ops-20261015-r1.zip` 与 `SHA256SUMS.txt`
- 原生新增三个模型名的默认重定向。匹配不区分前台/后台，主动请求同名模型也会改写；`WB_BACKGROUND_MODEL_NAMES=""`关闭名单，`WB_BACKGROUND_MODEL_TARGET`自定义目标。
- Messages及count_tokens默认在转发前返回400；原鉴权先执行。`WB_ENABLE_ANTHROPIC_MESSAGES=1`显式保留旧协议。转换层仍不透传cache_control，不能把此防护当成缓存修复。
- 保留共享总期限与独立header/首帧预算、唯一安全循环恢复提示、连续精确失败前缀清理、引用片段保护、有效累计用量、独立401刷新、池租约与SSE字节上限、匿名观察。
- 三方迁移四个替换模块及三个新增模块，准确依赖保持；只能安装到干净固定官方v1.7.6，不直接覆盖旧补丁树。
- 70组Python定向回归（新增模型别名7项/入口兼容6项），官方39组Node面板套件；Linux3.11/3.12及Windows3.11结果以本版Actions为准。

公开包保留上游新默认。升级使用已有模型路由或Messages客户端时，显式选择兼容变量并保留原配置/挂载。公共入站默认16MiB，较大限制需显式配置。目录归档仍需持久化，SQLite日志保持原选择；模型、账号、配置、历史由使用者原部署保留。

先备份镜像/源码/配置/数据，再在独立准确基线安装、构建固定镜像，定向切换唯一服务。原Release/tag继续保留。没有额外调用真实模型验证长会话完成率；单流保护不能保证所有跨工具回合停滞消失。

[说明与安装](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.6-ops.20261015.1/README.md) · [验证](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.6-ops.20261015.1/docs/VERIFICATION.md) · [Actions](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/actions/workflows/tests.yml)

MIT，保留官方及历史附件来源说明。
