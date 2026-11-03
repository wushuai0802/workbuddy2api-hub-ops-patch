升级官方v1.10.1并吸收参考补丁r2，保留既有运维保护。

- 固定官方`28527f48cfaea0e456612eb8922cb51b5d804a22`，补丁`v1.10.1-ops.20261102.1`。
- 原生CodeArts第三出口默认关闭；采用面板请求体热配置、绝对429恢复时间、工具过滤、CodeArts会话头及代理认证规整。
- 补齐非法工具名原值/完整匹配、极端时间戳容错、CodeArts一次释放及预检重试所有权、六接口循环/期限/取消/用量保护与同UID记账隔离。
- 22项新增回归；235 Python（52补丁＋183官方）与65 Node。六个模块字节及18个关键AST保持；不增加重试，不调整账号、Key或路由。
- 发布ZIP由通过的CI提交生成。无主动真实模型、CodeArts登录或账号任务验收；合成回归和本地ready不证明云端可用。
- Pin official v1.10.1 and preserve native provider/UI/rate-limit/proxy changes. Add literal tool-name validation, reset overflow tolerance, CodeArts lease ownership and provider-isolated six-adapter safeguards. Inventory: 235 Python and 65 Node suites. Release bytes come from the passing CI commit; no live-model availability claim.
