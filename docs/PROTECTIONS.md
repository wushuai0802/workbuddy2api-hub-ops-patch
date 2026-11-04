# 累积保护清单与合并热点

核对日期：2026-11-05。固定官方v1.10.9 / `077fb2fcfc273e29bd1266e527afc4a3d548b516`，补丁 `v1.10.9-ops.20261105.1`。六个保护模块字节、29个关键AST与上版一致；保护合同不改变阈值、恢复次数或期限。运行指纹见BASELINE.json。

下表按保护合同归组，而非按发布版本。代码位置均相对 `files/`；套件相对 `tests/`，官方套件在固定官方源码的 `tests/`。已阅读对应实现、差异和测试合同，但不是逐行审计；“部分”表示已有相关原生实现，不能证明与本地增量等价。“未确定”不作为移除理由。

以下真实触发统计沿用2026-11-02只读审计，未重新采集，不代表11-05新增流量。真实证据只统计在线保留的 `usage/ops-observations.jsonl` 及其轮换文件：截至 2026-11-02 21:38（UTC+8）向前 30 天，3940 行，其中 request 1967、attempt 1973，JSON 解析错误 0。保留数据实际覆盖 2026-10-15 至 11-02，不能代表完整 30 天或更早历史。request outcome：completed 1939、client_aborted 26、upstream_aborted 2。只在 request 阶段累计 signals，避免重复计数；signal 次数不等于故障请求数、恢复率或业务完成率。旧记录无细分 guard_details，不能把 8 次循环检测拆分到具体规则。没有对应可识别信号的格子写“无”，表示这份数据无法归因，不表示从未发生。

| 保护内容 | 代码位置 | 对应测试套件 | 官方是否已有同等能力 | 近 30 天真实触发证据 | 后续建议 |
|---|---|---|---|---|---|
| 推理精确重复、短句轮换和快速意图检测；结构化文本、引用和代码围栏豁免 | `wb_ops.py:detect_reasoning_loop`、`ReasoningLoopGuard`、`_QuoteContext` | `test_integration.py`、`test_dated_guards.py`、`test_loop_recovery.py`、`test_v1712.py` | 未见同等有界循环守卫；原生流读取不是等价检测 | `reasoning_loop_detected=8`，不能区分规则 | 保留 |
| 可见正文使用更保守的循环阈值，不检测工具参数 | `wb_ops.py:ReasoningLoopGuard(conservative=True)`；`wb_proxy.py:_GuardedStream._frames` | `test_integration.py`、`test_review_fixes.py`、`test_v1712.py` | 未确定 | 无（`content_loop_detected=0`） | 观察 |
| DS4.1 混合计划停滞检测及 leading-thinking 中相同检测 | `wb_stall.py:MixedStallGuard`；`wb_ops.py:LeakedThinkingGuard`；`wb_proxy.py:_GuardedStream` | `test_wb_stall.py`、`test_mixed_stall_proxy.py`、`test_leaked_mixed_stall.py` | 未见同等实现；仅作用于受支持单候选模型形状 | 无（旧记录不能把循环细分到 mixed-stall） | 保留 |
| 推理停滞与跨工具回合重复结果只观察，不硬阻断 | `wb_ops.py:StallObserver`；`wb_observe.py:progress_signal` | `test_progress_observe.py`、`test_observability.py` | 未见同等有界匿名观察 | `reasoning_stall_observed=5`、`progress_repeated_result=24` | 观察，先校准 |
| 仅推理失败且未见可见输出/工具/拒答时最多一次恢复；共享原期限、同上下文 TTL 电路 | `wb_proxy.py:_try_loop_continuation`、`_start_empty_continuation`；`wb_ops.py:LoopRetryCircuit` | `test_loop_recovery.py`、`test_phase_recovery.py`、`test_reasoning_recovery.py` | 部分：原生有续写路径，本地增加重放安全门与预算 | `retry_started=6`、`retry_recovered=4`、`retry_failed=2` | 保留；不据此宣称完成率 |
| 已失败推理前缀仅存哈希，TTL/容量/清理长度有界；只改出站副本 | `wb_ops.py:FailedReasoningCache`；`wb_proxy.py:_loop_recovery_body` | `test_loop_recovery.py`、`test_v1712.py`、`test_phase_recovery.py` | 未确定 | 无（无专用持久信号） | 保留 |
| DS4.1 历史混合停滞只删确证窗口内 Hmm 中性重复，双推理别名同步 | `wb_stall.py:clean_stalled_reasoning`；`wb_proxy.py:clean_deepseek_stalled_history` | `test_wb_stall.py`、`test_mixed_stall_proxy.py` | 未见同等出站历史清理 | 无 | 保留，未来凭反例复核 |
| DS4.1 tool 图片转文字占位，保留 user 图片/工具配对 | `wb_proxy.py:sanitize_deepseek_tool_images` | `_test_deepseek_multimodal.py`、`test_v172.py` | 未见同等模型定向转换 | 无（观测未记录转换次数） | 保留 |
| DS4.1 历史推理 65536 字符预算，最新优先，双别名不绕过，原历史不写回 | `wb_proxy.py:compact_deepseek_reasoning_history` | `_test_deepseek_reasoning_compaction.py`、`test_v1712.py` | 未见同等模型定向预算 | 无 | 保留 |
| 推理流无损短缓冲，2 秒/8192 字符/64KiB/记录数限制，内容/协议边界释放 | `wb_ops.py:CompactSSEBuffer`；`wb_proxy.py:_GuardedStream._iter_items`、`_reasoning_buffer_boundary` | `test_nullable_buffer.py`、`test_metadata_buffer.py`、`test_compact_buffer.py`、`test_buffer_records.py` | 未确定；不能把原生逐块读写视为同等缓冲合同 | 无（缓冲事件不在持久信号白名单） | 观察 |
| 首帧独立期限、前导大小上限、失败同原预算重开，取消/到期不借额外尝试 | `wb_proxy.py:probe_first_frame`、`_ProbedStream`、`_open_first_stream` | `test_auth_deadline.py`、`test_phase_recovery.py`、`test_v1733.py` | 部分：原生有首帧流程，本地接入 watchdog/取消/阶段预算 | 无 | 保留 |
| 连接准备、HTTP 头、流生成共享期限；真实 socket 到期/取消唤醒；取消不惩罚账号 | `wb_accounts.py:_RefreshDeadline`；`wb_proxy.py:_generation_deadline`、`_interrupt_upstream`、`open_upstream` | `test_auth_deadline.py`、`test_connection_deadline.py`、`test_v1733.py`、`test_v1101.py` | 部分：原生有网络超时，本地加强绝对期限和所有权 | `client_aborted=26`；取消结果计数不能证明具体唤醒分支触发；超时信号无 | 保留 |
| SSE 行/事件大小有界，UTF-8/CRLF/分块无损，预取 provider 用量不丢，真实 DONE 停读 | `wb_proxy.py:_SSELimits`、`iter_sse_lines`、`_GuardedStream._frames` | `test_sse_framing.py`、`test_guarded_sse_batching.py`、`test_v192.py` | 部分：原生有按块读取和可选 DONE 停读，本地受保护流始终终态停读 | 无（`upstream_sse_limit=0`） | 保留 |
| 入站严格只接受整数 n=1；上游多候选不合并、不安全恢复 | `wb_proxy.py:_single_candidate_error`、`_unsupported_stream_choices`、`_GuardedStream._frames` | `test_proxy_boundaries.py`、`test_v172.py`、`test_review_fixes.py` | 未确定；单候选生命周期不能静默接受多候选 | 无 | 保留 |
| EOF 后验证完成前持有连接租约，取消只关闭自己持有的连接 | `wb_upstream_pool.py:_PooledResponse._release`；`wb_proxy.py:_hold_stream_response`、`_interrupt_upstream` | `test_keepalive_guard.py`、`test_v1733.py` | 部分：原生池化存在；本地增加 defer-release 所有权 | 无 | 保留 |
| 心跳与缓冲共用写锁，阻塞读取前 flush；写失败及时取消上游、不恢复 | `wb_proxy.py:SSEHeartbeat`、`_BufferedStreamWriter` | `test_heartbeat_lifecycle.py`、`test_guarded_sse_batching.py`、`test_stream_contract.py` | 部分：原生 15 秒心跳存在；本地接入受保护流取消/锁 | `client_aborted=26`，具体心跳触发无 | 保留 |
| 六路径成功/失败/断连使用统一终态、单次记账、收到的 provider 用量和实际模型/Key/effort | `wb_proxy.py:_attempt_recorder`、`_record_billed_usage`、`_record_billed_error` 及六适配器 | `test_stream_contract.py`、`test_v1710.py`、`test_v1730.py`、`test_v190.py` | 部分：官方有协议和账本，本地统一守卫后归属及失败保全 | request completed 1939 / aborted 28；是否经过某个补充分支未确定 | 保留 |
| DSML/打印工具调用和推理答案恢复：完整合法形状才转换，截断/过滤/拒答不伪造工具 | `wb_ops.py:ReasoningAnswerRecovery`、`ReasoningScrubber`；`wb_proxy.py:LeakedToolCallStreamFilter` | `test_reasoning_recovery.py`、`test_printed_call_boundaries.py`、`test_extra_fields.py` | 部分：原生有 DSML/工具转换，本地增强失败/终态边界 | 无 | 保留 |
| 401 有界正文与严格 Bearer invalid_token 挑战；仅同号一次刷新，原 deadline，静态 Key 不走 OAuth | `wb_proxy.py:_read_auth_error_message`、`open_upstream`；`wb_accounts.py:refresh_after_401`、`_bearer_invalid_token` | `_test_401_refresh.py`、`test_auth_deadline.py`、`test_v1715.py`、`test_v190.py` | 部分：官方有挑战/刷新相关处理；本地严格参数匹配、并发刷新及有界读取 | 无（无认证错误持久原因） | 保留 |
| 账号并发名额绑定响应、close 并发幂等；刷新前归还后再准入；异常/取消不漏还 | `wb_proxy.py:bind_account_slot_to_response`、`open_upstream` | `test_account_slot_lifecycle.py`、`test_v1730.py` | 部分：原生有限额，本地加强生命周期/所有权 | 无 | 保留 |
| 白名单/定址/排除账号贯穿重试与回退；冻结调用方名单，原体与原期限不变 | `wb_proxy.py:_make_retry_call`、`open_upstream`、`open_upstream_with_fallback` | `test_v1715.py`、`test_realm_fallback_boundaries.py`、`test_v1728.py` | 部分：原生有 Key 及出口限制，本地补齐恢复/期限合同 | 无 | 保留 |
| 免费模型与积分回退的冷却豁免/失败归属，不把已用禁用号作为候选 | `wb_accounts.py:blocked_by_model_cooldown`；`wb_proxy.py:open_upstream` | `test_v190.py`、`test_v1101.py`、`test_v1710.py` | 部分：原生支持免费模型/积分回退，本地补齐资格及错误账本 | 无 | 保留 |
| 区域风控阈值状态持久化、UID 哈希去重、原到期不延长；坏文件可见 | `wb_proxy.py:_load_restriction_state_locked`、`_save_restriction_state_locked`、`note_account_restriction` | `test_restriction_persistence.py`、`test_entry_observation.py` | 部分：原生有账号 11140 冷却；未见同等跨重启区域保护 | 无（模型观测无该原因；不把活动历史混入本列） | 保留 |
| 每日活跃网页/桌面尊重账号与区域暂停，11140 落盘并停后续轮次，不记完成 | `wb_accounts.py:_task_restriction_error`、`_capture_task_restriction`、`daily_chat_web`、`daily_chat` | `test_task_restriction.py`、`test_v190.py` | 部分：原生有任务参与/冷却，本地补齐网页与区域门控 | 无（ops-observations 只覆盖模型请求，不覆盖 scheduler） | 保留 |
| 请求入口及六路径匿名观测、轮换/白名单/数值上限；写失败不中断业务 | `wb_observe.py:ObservationStore`、`tracked_entry`、`tracked_response`；`wb_proxy.py:_observation_store` | `test_observability.py`、`test_entry_observation.py`、`test_progress_observe.py` | 未见同等 request/attempt 观测；原生日志是不同合同 | 1967 request / 1973 attempt；解析错误 0 | 观察，保持隐私合同 |
| 工具名按原字符串完整匹配，不 strip 后误放行 | `wb_proxy.py:normalize_tools` | `test_v1101.py` | 部分：原生已有工具名过滤；本地保留原字符串严格匹配 | 无 | 保留 |
| 429 异常 reset_at 类型/超大整数不抛出，原响应可正常返回 | `wb_proxy.py:RateLimited.__init__` | `test_v1101.py`；官方 `_test_rate_limit_reset_at.py` | 部分：原生绝对时刻/过期校验已具备，本地补构造器容错 | 无 | 保留 |
| CodeArts 响应单次释放、预检失败只关 transport；六接口守卫/取消/原用量/同 UID 池隔离 | `wb_codearts.py:CodeArtsStream.close`、`_client_chat`；`wb_proxy.py:_OwnedCodeArtsStream`、`_record_billed_usage`、`record_usage` | `test_v1101.py`、`test_v1100.py` | 部分：原生提供 CodeArts/会话头/代理规整；本地补所有权及六路径合同 | 无（当前观测无 CodeArts 请求） | 保留；真实登录/生成未验收 |
| 内置 Web 工具压缩解码有界、异常引擎不拖垮聚合 | `wb_webtools.py:_decompress`、`_open_url`、`_search_fanout` | `test_v172.py`、`test_v1617.py` | 部分：原生有搜索/抓取，本地限制解压膨胀并隔离异常 | 无 | 观察 |
| 容器/守护部署禁面板停服；能力确认、明确回执与连续探测后才提示状态 | `wb_proxy.py:_console_capabilities`、`Handler._route_shutdown`；`dashboard.html:initConsoleButtons`、`shutdownGateway` | `test_dashboard_controls.js`、`test_v176.py`、`test_v1617.py` | 部分：原生有停止按钮和 health 复核，本地增加部署能力/确认边界 | 无 | 保留 |
| 日/周/月比较区间使用同一个 untilAt，避免跨日负区间 | `dashboard.html:prevRange` | `test_dashboard_ranges.js` | 未见同等：固定基线仍以 new Date() 另取当前日界 | 无（模型观测不覆盖前端） | 保留 |
| 默认禁恢复提示及请求降级，启用须显式选择；运行身份绑定 payload 哈希 | `wb_proxy.py:prompt_degrade_enabled`、`_loop_recovery_body`、`_patch_payload_fingerprint` | `test_recovery_hint.py`、`test_build_identity.py`、`test_v176.py` | 部分：原生有设置/降级能力；本地增加默认策略及字节身份 | 无 | 保留 |

## v1.10.7 的覆盖与精简判断

没有运行时保护被移除。官方新特性或部分同名实现，不构成原保护合同的等价证据。

- 账本追加基础设施由官方v1.10.7 `wb_usage_io.append_line` 覆盖；`_persist_usage`桥接该函数，保留写失败诊断及业务不中断合同。12项新回归包含3线程/2子进程150条大Unicode行和写失败；这不是线上丢账的证据。
- 官方非流式finally关闭与现有守卫重叠，两个新增重复关闭块不叠加；原守卫单次关闭及名额所有权保留，既有六路径/CodeArts关闭与取消套件继续验证。
- 旧429日期测试适配由官方v1.10.7动态样本覆盖，当前原生套件原字节、原断言直接运行，无冻结墙钟。旧固定日期夹具兼容分支仅供维护回归，不参与当前官方日期套件。
- 连接时保存账号及无认证信息的代理出口快照，六路径成功/失败/恢复用实际连接归因，换连接清旧标记，CodeArts不借同UID的WorkBuddy代理或积分。媒体额度拒绝/异常的单次释放为可复现反例驱动修复。
- 五模块字节保持：wb_ops、wb_stall、wb_observe、wb_webtools、wb_upstream_pool。25个关键函数/类AST保持；build_upstream_body仅增加官方ensure_explicit_effort调用。新增12项合同适配本仓库夹具，不执行参考包脚本。

## wb_proxy.py 合并热点

以下位置由本轮候选AST计算。升版不同时抽取模块；入口仍须显式传入持有者、取消、账本、白名单、期限和连接所有权。

| 当前符号 | 行号 | 约束 |
|---|---:|---|
| `_GuardedStream` | 13074 | 统一守卫与连接快照 |
| `open_upstream` | 17261 | 选号、原deadline、白名单、401及名额 |
| `_record_billed_usage` | 20797 | 原provider用量、实际模型及出口 |
| `_aggregate_with_recovery` | 21147 | 非流式关闭所有权 |
| `_stream_chat_events` | 20856 | 六路径终态、恢复、取消 |
| `Handler` | 21934 | 原生鉴权、媒体和协议入口 |
