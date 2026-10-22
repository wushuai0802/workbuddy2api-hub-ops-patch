升级官方v1.7.28，修复旧r2遗漏的断流原文丢失和过滤结果工具化，并完善跨出口回退的账号策略和期限边界。

- 补丁：`v1.7.28-ops.20261022.1`；固定官方提交：`c22109def2793234236e0c3ab1d2c11f02a05c39`。
- 官方变化：账号ID不再被脱敏改写、会员档位读取计费摘要、账号排序保持冷却提示归属、身份句兼容、更新备份与版本回退；跨出口回退默认关闭。
- 补丁修复：Chat/Responses/Messages读取异常保留完整已收原文；六路径过滤结果不转为可执行工具；恢复白名单冻结，实际出口/期限/定址/风控策略贯穿回退。防复读阈值与恢复次数不变。
- 验证：194组Python（38补丁＋156官方）、55组Node（2补丁＋53官方）；新增7项专项回归，并把旧断流测试强化为完整文本等值检查。
- 下载：`workbuddy2api-hub-v1.7.28-wushuai0802-ops-20261022-r1.zip` 和 `SHA256SUMS.txt`。

Rebased onto upstream v1.7.28. Fixes the r2 read-error suffix loss and filtered printed calls across all six adapters. Realm fallback retains account restrictions, actual realm and the original deadline; pinned/explicit/restricted/expired requests cannot cross realms. The native fallback switch remains off by default.

Validation uses synthetic data and loopback upstreams; it does not establish long-session model quality. Python 3.11+ is required. Install onto the exact clean baseline, rebuild only the intended service, and keep image/source/configuration/data backups. The installer never changes accounts, keys, settings or history. Older tags and releases remain available. MIT; upstream and reference attribution retained.
