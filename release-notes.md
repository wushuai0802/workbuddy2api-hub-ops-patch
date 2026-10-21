升级官方v1.7.18，吸收11-128身份句指纹、积分批量刷新崩溃、成长任务专家登记、数据用途声明文案及面板/缓存修复，并继续保留DeepSeek防复读与一次安全恢复。

- 补丁：`v1.7.18-ops.20261021.1`；官方提交：`c1943745640c78e9d6036ec8d2c3e3396af2d5a3`。
- 下载：`workbuddy2api-hub-v1.7.18-wushuai0802-ops-20261021-r1.zip` 与 `SHA256SUMS.txt`。
- 官方11-128身份句指纹：system/developer 中「自我介绍句式 + 品牌」整句改为中性句，user/assistant/tool 内容保持原样；补丁只重基继承，不重复实现。
- 官方修复全量刷新积分默认间隔下的 `NameError`、成长任务「无畏契约拼豆专家」登记、`X-Private-Data` 文案反置、模型清单消费倍率列截断及聚合缓存测试的 exFAT mtime 假设。
- 保留混合/引用防复读、最多一次安全恢复、原始用量与实际模型、历史/工具图片、有界401刷新、阶段/总期限、定址恢复、SSE及连接所有权保护。原阈值和恢复提示默认不变。
- 保留Web日志与管理型部署停止保护。额度窗口探针原生默认开启；需要延续被动行为的部署应沿用`settings.window_probe=false`，公开安装器不修改设置或业务数据。
- 覆盖36补丁＋139原生共175组Python，以及50原生＋2补丁共52组Node；CI检查Linux3.11/3.12与Windows3.11。

仅支持准确干净的官方v1.7.18，完整Python3.11+。旧补丁需在新基线整体迁移；先备份旧镜像、源码、配置和数据，再定向部署唯一服务。保留账号、历史、挂载、Key和请求体限制。旧Release/tag继续保留。

没有额外真实模型长会话验收，不将健康或离线测试当作所有循环已消失。未构建精简便携运行时。MIT，保留官方及历史来源归属。

[说明](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.18-ops.20261021.1/README.md) · [安装与回滚](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.18-ops.20261021.1/docs/INSTALL.md) · [验证](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/blob/v1.7.18-ops.20261021.1/docs/VERIFICATION.md)
