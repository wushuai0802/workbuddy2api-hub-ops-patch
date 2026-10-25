升级官方v1.8.0，保留现有运维保护，并验证原生日志队列与流式失败收尾的联动。

- 补丁：`v1.8.0-ops.20261025.1`；准确官方提交：`4f7ae6f2efcae5e6ae85aefa732035206044514b`。
- stderr写出采用原生2000行后台队列，堵塞时请求线程继续处理；满队列丢控制台行并显示计数告警。更新获取路径各允许10秒，模型调用期限保持。
- 桌面chat兼容与国际活跃证书失败上报均默认关闭。前者仅调整桌面chat头；后者只在证书类错误时走账号自身区域上报，上报拒绝不记当日完成。TLS校验、请求体、路由和账号风控保护保持。
- 首帧心跳、Windows看门狗、取消、池租约、压缩、防循环、历史/图片、401、白名单和原用量保护继续保留。五个模块字节、16个核心符号AST与上一正式版一致。
- 验证清单：212 Python（45补丁＋167官方）、61 Node；新增日志饱和下三协议失败终态、用量传递、名额释放和不重试的联动检查。
- 下载：`workbuddy2api-hub-v1.8.0-wushuai0802-ops-20261025-r1.zip` 与 `SHA256SUMS.txt`。公开安装器不改账号、Key、业务配置或部署。

没有额外发送真实模型、签到或活跃上报。真实上游成功生成验收仍待完成，官方客户端识别限制不因本补丁解除；正式发布不等于生产调用链已全面验收。

Rebased onto exact upstream v1.8.0. Native bounded stderr logging, 10-second update-fetch paths and opt-in desktop chat/activity-report capabilities are retained. Both new switches default off. TLS verification, request bodies, routing and existing model budgets remain unchanged. The existing Windows first-frame watchdog, cancellation, pooled ownership, usage, compaction, loop, history/image, auth and allowlist safeguards remain.

Inventory: 212 Python and 61 Node suites. New integration coverage saturates the stderr queue and checks terminal errors, received provider usage, account-slot release and no retries across all three streaming protocols. No additional live generation, check-in or activity report was sent. Successful live upstream validation remains pending. Older releases/tags and MIT attribution remain available.
