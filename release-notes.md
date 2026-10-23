升级官方v1.7.32，保留现有运维修复，并完善15秒SSE心跳与流保护、期限、恢复、记账的衔接。

210组Python、61组Node及隔离验证已通过；真实上游成功生成验收仍待完成，正式发布不表示生产调用链已全面验收。

- 补丁：`v1.7.32-ops.20261023.1`；准确官方提交：`477ba36ceea78ec4252c47a68e94a5a1612bab0b`。
- 继承官方模型别名、CN专属判定、OpenCode上下文/输出上限、面板主题对比度和HTTP入口提示；后台改道仅对元数据明确的后台任务生效，显式选模型按所选模型路由。
- 三条流接口与缓冲写出共享心跳锁；首帧前也能发送合法注释，心跳不计输出/TTFT/用量或延长期限。进入首帧探测前发送流式头，之后读取失败发送SSE错误；建连/认证错误与非流式保持原状态处理。
- 心跳写失败会中断所属上游并阻止恢复，终态停止线程，原地重开前先关闭旧线程。Messages上游断流保留错误帧及原用量，不误记客户端取消；首帧失败保留实际计费模型。
- 结合实际连接超时现象补齐3项期限回归：准备耗尽后不发包，迟到响应不记成功，预算耗尽不再惩罚账号或重试。保留既有超时/恢复额度，外部供应商的代理/配额/上下文问题仍由相应线路处理。
- 验证清单：210组Python（43补丁＋167官方）、61组Node；新增7项心跳衔接回归，含三协议与真实loopback HTTP。现有压缩、防循环、401、名额、历史/图片及白名单保护继续保留。
- 下载：`workbuddy2api-hub-v1.7.32-wushuai0802-ops-20261023-r1.zip` 和 `SHA256SUMS.txt`。

Rebased onto upstream v1.7.32. Native model aliases, metadata-only background routing, realm/context fixes and dashboard improvements are retained. The 15-second heartbeat shares the buffered writer across three streaming protocols, including initial silence. Heartbeats never count as model output or reset deadlines. Stream headers precede first-frame probing, so later failures are SSE errors; connection/authentication errors and nonstream statuses retain their original handling. Downstream heartbeat failures interrupt the owned upstream and prevent replay; every exit stops its thread, including before reopen. Messages read errors retain error terminals and original billing instead of being labeled client cancellation.

Offline/synthetic checks passed, but successful live upstream validation remains pending. Release status does not imply complete production-path acceptance.

Seven heartbeat cases cover initial/body silence, real HTTP, disconnects, deadlines, upstream resets, reopen and background-write failure. Three added connection-budget cases prevent sends after preparation exhausts the budget, close late responses without success and avoid account penalties/retries after expiry. Inventory: 210 Python and 61 Node suites. Synthetic tests do not prove all long-session failures are resolved. The exact clean v1.7.32 baseline and Python 3.11+ are required. Back up image/source/config/data and recreate only the intended service. Existing credentials/settings and compaction/loop/auth/history/image/slot/allowlist protections remain; no extra generation retries are introduced. Older releases/tags remain available. MIT with upstream/reference attribution.
