升级官方v1.7.33，吸收外部r3的Windows读取取消和恢复连接收尾改进，保留现有运维修复。

- 补丁：`v1.7.33-ops.20261023.2`；准确官方提交：`47775c5e4aceb9f977bdebf5dd20175b44b5a78b`。
- 原生更新：成功请求解除凭证失效标记、Responses首段工具参数增量、每日活跃总预算/有序候选/随机时段/启动补跑，以及429模型窗口跨重启恢复。由官方基线提供，不重复覆盖。
- Windows心跳断连关闭当前拥有的socket句柄，及时唤醒带超时读取；已释放的池租约仍受保护。恢复前、连接返回后及首帧探测期间检查取消，关闭新响应，不继续读取/重试或误记账号失败。
- 首帧看门狗在中断前记录到期状态，Windows时钟刻度差不能触发额外连接；客户端取消先于出口退避处理，不惩罚出口。两项确定性反例先失败后通过，原首帧只连接一次断言保留。
- 保留三协议首帧前15秒心跳、共享缓冲写出、原连接/生成期限、压缩、防循环、历史/图片、401、白名单、provider用量和名额保护，不增加恢复次数。
- 验证：210组Python（44补丁＋166官方）、61组Node；新增8项专项，覆盖三协议真实HTTPResponse/socket、取消竞态、已释放池租约及原生工具首段参数分块/交错一致性。旧实现复现三协议退出延迟与两项取消合同失败，修复后通过。
- 每日活跃默认600秒预算，留空时两个随机时段，启动补跑默认开启；更新前核对任务策略。公开安装器保留业务设置。
- 下载：`workbuddy2api-hub-v1.7.33-wushuai0802-ops-20261023-r2.zip` 与 `SHA256SUMS.txt`。

官方已公告收紧非官方客户端识别并进入低频维护。真实上游成功生成验收仍待完成；本次没有再对受限账号发生成探测，补丁不能解除云端风控。正式发布不表示生产调用链已全面验收。

Rebased onto exact upstream v1.7.33. Native credential-state recovery, initial tool-argument deltas, daily-activity budgets/ordered candidates and restored model cooldowns are inherited. The external r3 reference informed Windows timed-read interruption and cancellation during recovery. Only an owned socket handle may close; released pool leases remain isolated. Cancellation closes newly returned responses, reaches first-frame probing and cannot start another retry or become an account error. The first-frame watchdog records expiry before interruption, preventing an extra connection during Windows clock-tick races. Client cancellation precedes egress penalties. Initial-silence heartbeats, original deadlines and all existing accounting, compaction, history/image, auth and allowlist safeguards remain.

Validation: 210 Python and 61 Node suites, including eight new synthetic cases with real local HTTPResponse/socket reads across three protocols. Upstream has announced stricter unofficial-client detection and reduced maintenance. No new live generation probe was sent; successful live upstream validation remains pending. Review the native 600-second daily budget, random slots and default-on startup catch-up before deployment. The public installer preserves credentials/settings. Older releases and tags remain available. MIT with upstream/reference attribution.
