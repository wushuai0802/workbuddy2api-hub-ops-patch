基于官方v1.8.0完善风控状态、每日活跃停手机制和早期错误诊断；本轮没有新的官方基线版本。

- 补丁：`v1.8.0-ops.20261026.1`；官方提交：`4f7ae6f2efcae5e6ae85aefa732035206044514b`。
- 区域暂停写入有界、原子的`usage/restriction-state.json`，只存账号哈希/时间/区域。重启沿用原到期，不延长暂停；损坏和写失败可见，内存保护保持。
- 每日活跃遇明确11140也会记原账号冷却、通知运行中的区域状态并停止后续网页轮次；受限期间不再发出新任务请求，不标记当日完成。普通配额、内容拒绝和数字前缀保持区分。
- 连接及上游拒绝加入观察记录，新增entry/response阶段和可用HTTP状态，风控/暂停/限流有独立分类；成功路径不重复记账。空账号池统一503并与账本一致。
- 原防循环、首帧看门狗、取消、租约、计费用量、历史/图片、401及白名单保护保留，模型预算和重试次数不变。未从历史报错追补账号限制，配置、路由及两个原生兼容选项不由安装器修改。
- 验证清单215 Python（48补丁＋167官方）和61 Node；新增21项专项覆盖新进程、并发、坏文件/数值/符号链接、三协议、任务批次和误判负例。
- 下载`workbuddy2api-hub-v1.8.0-wushuai0802-ops-20261026-r1.zip`与`SHA256SUMS.txt`。

没有主动发起真实模型、签到或活跃上报探测。上游账号风控仍需官方侧恢复，外部供应商网关/连接故障也不由本补丁消除。安装和回退时保留现用accounts/usage；旧版不识别新增的区域状态文件。

Upstream remains pinned to v1.8.0. Realm restriction windows persist hashed account markers and original expiry across restarts. Daily activity now records verified account restrictions, shares the live gateway pause and stops later web attempts without marking success. Early connection/rejection errors become observable with entry/response stages and HTTP status; empty-pool responses and ledgers agree on 503. No additional retries, model changes or historical restriction backfill are introduced.

The inventory is 215 Python and 61 Node suites, including 21 new persistence, entry and task cases. No active live-generation or check-in probe was sent. Cloud restrictions and other providers' transport faults remain external; a formal release is not proof of upstream availability. Preserve current account/usage data on rollback. Older releases/tags and MIT attribution remain available.
