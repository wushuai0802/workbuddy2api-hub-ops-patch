升级官方v1.9.2，保留既有运维保护。

- 固定官方`736d08eaf067bf2c0487ec2872f48d8f8aaecc54`，补丁`v1.9.2-ops.20261031.1`。
- 继承慢轮询去重、更新卡片提示、密码加密环境错误与启动失败保护。SSE终态和通用错误缓存沿用原生默认关闭；现有受保护流仍在DONE立即停读。
- 保留循环阈值、期限、取消、名额、401、区域暂停、历史及实际用量/模型归属。新增3项专项，清单225 Python＋61 Node；未主动请求真实模型或账号任务。
- Pin official v1.9.2. Adopt its polling, update-action and password-crypto fixes while retaining stronger guarded stream terminals and existing safety/accounting contracts. Native switches remain opt-in. Inventory: 225 Python and 61 Node suites; synthetic checks do not prove upstream availability.

## Previous release: v1.9.0-ops.20261029.1

升级官方v1.9.0并吸收参考补丁中的认证、免费模型冷却和失败模型归属改进。

- 正式补丁`v1.9.0-ops.20261029.1`，固定官方`a6a967ef04b9824e962c4f285ebd589fbb1f1312`。
- 采用官方6004持久化、健康细分、池空/限流归因和界面修复；保留本地区域保护、入口观察和Messages补充。
- 严格区分真正Bearer挑战与HTML/Basic/realm文字；原刷新期限及有界读取保持。免费模型冷却与ready同口径，付费回退建连失败保留实际模型/原模型/账号/Key。
- 网页活跃受限返回失败阻断，防止误报今日已完成。循环阈值和恢复次数不变。
- 验证清单221 Python（49补丁＋172官方）与61 Node。无主动真实模型、签到或活跃探测；不承诺解除账号风控。官方DSH规则会改变匹配的system/developer/assistant文本，见docs/V190.md。
- 下载`workbuddy2api-hub-v1.9.0-wushuai0802-ops-20261029-r1.zip`与`SHA256SUMS.txt`。保留现用数据、准确基线预检与回滚。

Pin official v1.9.0 and adopt native quota/readiness/UI fixes. Require strict Bearer challenge evidence, align free-model cooldown admission, preserve failed paid-fallback identity and report restricted web activity honestly. Inventory: 221 Python and 61 Node suites; offline checks do not prove live-upstream availability. Native DSH normalization changes matching history text. Preserve account/usage data on rollback.

## Previous release: v1.8.0-ops.20261026.1

基于官方v1.8.0完善风控状态、每日活跃停手机制和早期错误诊断；该轮没有新的官方基线版本。

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
