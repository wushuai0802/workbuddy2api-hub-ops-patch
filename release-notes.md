修复账号并发名额误还、返回前错误漏关连接、401刷新持槽及等待超时误分类，吸收外部v1.7.30-r2的有效边界，保留本地更完整的压缩与防复读保护。

- 补丁：`v1.7.30-ops.20261023.2`；官方仍为v1.7.30 / `84dbc99b1a34182130ee9035f9d80b69a0fc2e8b`。
- 只有实际计数请求才归还名额；关闭限额不会误减其他在途请求。并发close一次性归还，异常/取消关闭未交出的响应，401刷新后重新准入，等待沿用原期限。
- 仅完整有界JSON对象401才作请求刷新过期证据，代理文本/畸形/溢出不误停账号；保留后台重试与验证TLS策略。
- 缓存用量忽略畸形details、布尔/非有限值及数值溢出，保留有效别名与原provider快照。
- 验证清单：206组Python（41补丁＋165官方）、59组Node；新增18项回归，先复现旧缺陷再修复，不放宽原断言。
- 下载：`workbuddy2api-hub-v1.7.30-wushuai0802-ops-20261023-r2.zip` 和 `SHA256SUMS.txt`。

Only counted admissions own account slots. Unlimited requests cannot release older requests; concurrent close releases once, unreturned responses close on failure/cancellation, and OAuth refresh reacquires under the original policy and deadline. Request refresh requires a complete bounded JSON-object 401 to establish expiry and never rereads errors after its watchdog. Malformed/nonfinite cache aliases no longer hide valid hits. Background retry and verified TLS policies remain unchanged.

Eighteen regressions cover the former failures and six adapters, loopback HTTP, cancellation and concurrent close. The inventory is 206 Python and 59 Node suites. Synthetic tests do not prove production long-session completion rates. The exact upstream v1.7.30 baseline and Python 3.11+ remain required; back up the image/source/config/data and recreate only the intended service. Existing compaction, loop, history/image, billing, deadline, allowlist and connection-ownership protections remain. Older releases/tags are retained. MIT with reference attribution.
