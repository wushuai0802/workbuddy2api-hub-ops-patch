吸收外部v1.7.28补丁包的跨出口回退边界，补齐隐式期限、风控暂停与候选检查超时三处遗漏，并推送更新。

- 补丁：`v1.7.28-ops.20261022.2`；固定官方提交：`c22109def2793234236e0c3ab1d2c11f02a05c39`。
- 修复：回退helper未显式给期限时，按面板响应头预算为两次连接共享一个deadline；国内出口处于风控暂停时不进入回退；候选可用性检查耗尽剩余期限后不再连接第二个出口。保留r1的读取异常原文回放、六路径过滤不转工具、白名单冻结与期限/定址边界。
- 验证：195组Python（39补丁＋156官方）、55组Node（2补丁＋53官方）；新增8项跨出口专项回归，覆盖六接口实际出口/账号/计费用量归属及首帧同号重开期限。
- 下载：`workbuddy2api-hub-v1.7.28-wushuai0802-ops-20261022-r2.zip` 和 `SHA256SUMS.txt`。

Absorbs the external v1.7.28 package's cross-exit boundaries: one shared panel-configured header deadline when a helper call omits it, no fallback into a domestic exit under a restriction hold, and no second upstream connection when the backup eligibility check exhausts the remaining budget. Retains the r1 read-error text replay, six-adapter filtered-terminal protection and frozen allowlists.

Validation: 195 Python (39 patch + 156 official) and 55 Node (2 patch + 53 official) suites, including eight new cross-exit regressions on the real six adapter paths. Synthetic data and loopback upstreams only; this does not establish long-session model quality. Python 3.11+ is required. Install onto the exact clean baseline, rebuild only the intended service, and keep image/source/configuration/data backups. The installer never changes accounts, keys, settings or history. Older tags and releases remain available. MIT; upstream and reference attribution retained.
