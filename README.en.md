# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Install and rollback](docs/INSTALL.md) · [Patch scope](docs/PATCHES.md) · [Verification](docs/VERIFICATION.md)

Release **v1.7.24-ops.20261021.2**, targeting upstream **v1.7.24** at `205d8b622d626f4b09da7b57efb3259f91403f4e`. Python 3.11+ is required.

[Download ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.24-ops.20261021.2): `workbuddy2api-hub-v1.7.24-wushuai0802-ops-20261021-r2.zip`.

This version migrates our retained safeguards onto the exact v1.7.24 source and retains our mixed-stall guard, reasoning-alias budget, tool-image compatibility, refusals, bounded refresh, and pooled connection ownership. It preserves upstream additional-tools/custom namespace support, Anthropic thinking, DSML fallback, block I/O and shared TLS contexts.

The installer replaces four upstream Python modules and the dashboard, and adds `wb_ops.py`, `wb_stall.py` and `wb_observe.py`, checking the unchanged core dependencies. It preserves accounts, keys, configuration and usage and does not deploy. The running gateway writes a separate bounded observation log, disabled with `WB_OPS_OBSERVE=0`.

The header phase respects panel settings (120 seconds by default); its 45-second first-frame phase starts after headers. Initial connection, recovery and web-tool follow-ups share the original generation deadline. The sole safe pure-reasoning recovery now adds a short continuation hint by default; set `WB_OPS_LOOP_RECOVERY_HINT=0` to disable the hint. Consecutive exact failed prefixes are cleaned within 16-segment/65536-character bounds. Guard thresholds remain unchanged; anonymous bounded rule/window metrics and retry-stop reasons are persisted.

The supplied v1.7.5-r2 reference informed quote-span masking and detached validated cumulative usage snapshots. Quoted examples remain protected while short quoted tool names no longer exempt a surrounding repeated plan. Original SSE bytes and tool arguments are preserved.

## v1.7.24 changes

Absorbs official v1.7.19–v1.7.24: account `tasksEnabled` separated from API `enabled`, printed tool calls released as soon as their closing tag arrives (#549), a mid-stream failure now emits a real error frame instead of a fake `[DONE]` (#549), hourly web-activity windows with concurrency/dedupe/linear backoff, per-key account binding, models-only-available filtering, reasoning-effort mapping, global prompt text, site content versions, dashboard heatmap refresh, Windows portable one-click update, and an external update executor. Official features come from the v1.7.24 baseline; this patch only keeps our own safeguards.

Three stream-boundary fixes are carried by this patch, each with regression coverage. A closing tag split across deltas (`</invoke>` then `</calls>`) is swallowed instead of leaking a literal `DSML` fragment as prose. Printed calls are released only after a normal terminal, so a truncated or failed transport never synthesizes a side-effecting tool. The content loop detector now reads a DSML-scrubbed view, so a repeated value inside a tool parameter is not treated as repeated prose.

r2 hardens that work in three further ways. Closing tags are now removed only when they belong to a parsed call, so an unrelated `</calls>` or `</invoke>` in prose or a code block survives verbatim. A turn that fails or disconnects returns the withheld original text, in order, instead of dropping it or executing it. A `content_filter` or other non-normal terminal keeps printed markup as text and never turns it into an executable call. Six regressions cover these boundaries.

## v1.7.17 changes

Absorbs official v1.7.17 stream reconnect before the first client byte, egress accounting at read completion, model-scoped 5xx isolation, immediate daily-limit disable, inline Codex bearer setup, X-Private-Data settings, credit-expiry display, OpenRouter pricing calibration, manual growth-task claims, free-tier entitlement badges, deploy modes, per-task schedule hours, completion-token throughput, and reasoning-only failure handling. Existing DeepSeek loop safeguards, one safe recovery, bounded 401 refresh, usage snapshots and connection leases remain.

Includes native empty-chart hover, late-response isolation, English phrases and 15-second polling cache fixes. Comparison windows now derive their calendar from the supplied request boundary, including midnight, Monday and month transitions. Manual range changes retain exact time. The optional `WB_CN_CHAT_UPSTREAM` leaves existing defaults and account domains unchanged; the installer does not configure an exit.

Includes v1.7.13–v1.7.16 account-isolated connections, native identity headers, tool schema/pairing/image fixes, live credit deductions, configurable schedule hours and cooldown rows. Web logs work on every platform without spawning a server-side window. Managed deployments still reject process shutdown; desktop shutdown requires a strict acknowledgement and bounded health probes. Rejections, timeouts and uncertain HTTP status leave polling active.

Successful bounded 401 refresh retries the original eligible account without adding the background refresh loop. Local deadline/size failures and HTML proxy 401s do not mark credentials expired; error-body inspection shares the original deadline and a 4096-byte bound. Pinned routes retain their restriction across worker-thread recovery and cannot silently rotate or switch to a paid model. Existing loop, usage, history and connection safeguards remain.

## Retained v1.7.10 behavior

Adds native panel switches for Messages and paid-model fallback, per-key concurrency (0/unset is unlimited; excess requests return429), and DSH vision metadata. Paid fallback is off by default and can spend credits when enabled. Settings and environment enablement are ORed; clear a legacy true environment override when moving control to the panel.

All six adapters now fail an upstream `finish_reason=error`, retaining consumed provider usage and visible output while preventing extra recovery/tool replay. Billing keeps the actual model and requested alias distinct across probing and recovery. Optional paid fallback inherits the original deadline and exclusions. Existing loop, history, auth, framing and lease protections remain.

## Retained v1.7.8 changes

Includes upstream 5.8.4 identity/conversation headers, account-scoped identifiers, a 600-second failure window, host-network failure classification, configurable web activity retries, pending-account activity actions, cooldown/cap-aware daily-model fallback, GPT minimum output 16, 413 hints, panel testing and DSH reasoning options. Daily-model fallback applies to activity jobs; normal chat routing remains a separate policy. Panel account tests send real requests and are never triggered by the installer.

The retained patch now normalizes positive cache-hit aliases at Chat/Responses output boundaries while keeping detached responses and original provider accounting snapshots. Host-network retry waits remain bounded by the original phase deadline. The single safe chat-recovery allowance is unchanged.

## Retained v1.7.6 compatibility

Upstream now redirects three named models (`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex`) to DeepSeek by default. Matching uses the model name alone, so explicit foreground requests also match. Set `WB_BACKGROUND_MODEL_NAMES=""` to retain original routing, or configure the list/target explicitly.

Anthropic Messages and count_tokens endpoints now return400 by default, after authentication and before payload forwarding. The bridge still ignores `cache_control`; cache parity is not fixed. Set `WB_ENABLE_ANTHROPIC_MESSAGES=1` only when retaining this protocol is intended. This public bundle preserves the upstream defaults; existing protocol regression suites opt in explicitly.

## Install

Prepare separate clean pinned sources, keep the previous deployment and backups, and verify first:

~~~sh
git clone --depth 1 --branch v1.7.24-ops.20261021.2 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v1724
git clone --depth 1 --branch v1.7.24 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.24
git -C workbuddy2api-hub-v1.7.24 rev-parse HEAD
python3 -B ops-patch-v1724/apply.py --verify-package
python3 -B ops-patch-v1724/apply.py --target ./workbuddy2api-hub-v1.7.24 --dry-run
~~~

Use [INSTALL.md](docs/INSTALL.md) to stop the one intended service, apply, rebuild and recreate it with its original parameters. Earlier patched trees are not overlays; do not delete state or checksum metadata to bypass a refusal. The [previous v1.6.20 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1) remains available.

Requests explicitly support integer `n=1`. Eligible empty/pure-reasoning recovery shares one attempt and a 300-second default deadline; visible content, refusal and tool traces forbid replay. Equal reasoning aliases count once, divergent aliases both count against the 65536-character budget. Leading-thinking mixed detection stays separate from ordinary prose and quoted/code examples.

A response whose wire body was read in one block retains its connection lease until buffered frames are checked. Valid completion returns it to the pool; loops/timeouts discard it. Already received usage is preserved on failure. Authentication refresh uses a separate non-pooled connection, one HTTP attempt, a 1MiB cap and a shared deadline capped at30seconds. OS DNS cannot be forcibly cancelled, but a late lookup cannot send expired credentials.

## Verification and limits

The runner covers 175 Python groups (36 patch and all 139 official Python groups), plus 50 official and 2 patch Node groups. CI checks Linux3.11/3.12 and Windows3.11; see [VERIFICATION.md](docs/VERIFICATION.md). Tests use synthetic data and loopback HTTP with external Python sockets denied; they do not establish online task completion.

r2 protects quoted repetition examples, preserves already-consumed provider usage across guard failure, transport errors and cancellation, and bounds SSE lines/events (4/8MiB defaults). A request-local history observer reports repeated tool results without blocking or replaying tools. It does not combine sessions or infer generic shell-command progress. Durable metrics distinguish requests from guarded attempts and legal-output recovery from business completion, with explicit time/version/protocol grouping and retention limits.

Upstream v1.7.4/v1.7.5 adds optional SQLite logs (off by default), retention limits, checkpoint fixes, manual credential-key import, a new dashboard, daily/manual catalog refresh, provenance/raw archives and actual credit multipliers. Model listing and displayed prices follow the updated native catalog. Persist `/app/catalog` or set `WB_PROXY_CATALOG_DIR=/app/usage/catalog` to reuse an existing usage mount. Include upstream `wb_logstore.py` even when the optional log database is disabled. Earlier1.7 updates include account failure/backoff handling, weights/notes, web engines and cache improvements. Request-body default remains16MiB; use explicit configuration for larger histories. Upgrades can rebuild checkpoint caches while preserving append-only history.

`/health/retry` reports source and a payload fingerprint frozen at import, comparable with `BASELINE.json`. Original authentication remains in force. Report versions, protocol, codes and minimal synthetic examples; exclude credentials, complete configuration and private conversations.

Derived from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT, retaining attribution in [LICENSE](LICENSE), [NOTICE.md](NOTICE.md) and [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).

The patched dashboard is installed, rolled back and fingerprinted with the Python payload. Quota scans use an empty model list. Native window probing defaults on and can send model requests at quota reset; explicitly set `settings.window_probe=false` before deploying to retain the old passive behavior. The installer does not change configuration or schedule settings.
