升级官方v1.7.30，继承签到/保活/调度器、网页会话清理、501、单流ID、静态Key、远程压缩v2及Key策略落盘等修复，保留既有运维保护。

- 补丁：`v1.7.30-ops.20261023.1`；固定官方提交：`84dbc99b1a34182130ee9035f9d80b69a0fc2e8b`。
- 新版压缩仅接受完整正常摘要；截断/过滤/报错/拒答/工具调用时明确失败，不能生成成功compaction替代原历史。接入原生成期限、原始provider用量、实际模型和Key/effort计账，不增加自动重试。
- 账号并发名额在错误、超时及异常退出时归还；刷新后重新校验ready与排除规则，避免重复占槽；并发等待受原期限约束。
- 沿用官方单一网关chunk id、面板body集中排空、目录tier和新调度功能；移除弃用的entitlements依赖。跨出口/积分回退、账号并发和稳定粘滞沿用既有设置及默认关闭。
- 验证清单：205组Python（40补丁＋165官方）、59组Node（2补丁＋57官方）；新增8项压缩/槽位/静态Key回归。
- 下载：`workbuddy2api-hub-v1.7.30-wushuai0802-ops-20261023-r1.zip` 和 `SHA256SUMS.txt`。

Rebased onto upstream v1.7.30. Hardened remote compaction termination, original usage and billed-model/key/effort attribution, and account-slot ownership on every exit. Incomplete or rejected summaries never replace retained history. Existing loop, auth, history/image, SSE and deadline safeguards remain; no extra compaction generation retries are introduced.

Tests use synthetic inputs and loopback upstreams; they do not establish long-session model quality. Python 3.11+ is required. Use the exact clean baseline, back up the image/source/configuration/data and recreate only the intended service. Startup update checks may rewrite their update_check cache. The installer never changes credentials, keys, settings or history. Older releases and tags remain available. MIT with upstream/reference attribution.
