# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Install and rollback](docs/INSTALL.md) · [Patch scope](docs/PATCHES.md) · [Verification](docs/VERIFICATION.md)

Release **v1.9.0-ops.20261029.1**, targeting upstream **v1.9.0** at `a6a967ef04b9824e962c4f285ebd589fbb1f1312`. Python 3.11+ is required. Offline/synthetic checks passed; successful live upstream validation remains pending. Upstream has announced tighter unofficial-client detection and reduced maintenance; this patch cannot remove upstream restrictions.

[Download ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.9.0-ops.20261029.1): `workbuddy2api-hub-v1.9.0-wushuai0802-ops-20261029-r1.zip`.

This version migrates our retained safeguards onto the exact v1.9.0 source and retains our mixed-stall guard, reasoning-alias budget, tool-image compatibility, refusals, bounded refresh, and pooled connection ownership. It preserves upstream additional-tools/custom namespace support, Anthropic thinking, DSML fallback, block I/O and shared TLS contexts.

The installer replaces four upstream Python modules and the dashboard, and adds `wb_ops.py`, `wb_stall.py` and `wb_observe.py`, checking the unchanged core dependencies. It preserves accounts, keys, configuration and usage and does not deploy. The running gateway writes a separate bounded observation log, disabled with `WB_OPS_OBSERVE=0`.

The v1.9.0 migration adopts upstream quota persistence, readiness and pool-error classification. Additional fixes require real Bearer challenge evidence, preserve free-model cooldown classification and failed paid-fallback attribution, and prevent restricted web activity from appearing complete. Native DSH normalization changes matching system/developer/assistant text; user/tool text and tool arguments retain their existing treatment. See [v1.9.0 scope and boundaries](docs/V190.md).

The header phase respects panel settings (120 seconds by default); its 45-second first-frame phase starts after headers. Initial connection, recovery and web-tool follow-ups share the original generation deadline. The sole safe pure-reasoning recovery now adds a short continuation hint by default; set `WB_OPS_LOOP_RECOVERY_HINT=0` to disable the hint. Consecutive exact failed prefixes are cleaned within 16-segment/65536-character bounds. Guard thresholds remain unchanged; anonymous bounded rule/window metrics and retry-stop reasons are persisted.

The supplied v1.7.5-r2 reference informed quote-span masking and detached validated cumulative usage snapshots. Quoted examples remain protected while short quoted tool names no longer exempt a surrounding repeated plan. Original SSE bytes and tool arguments are preserved.

## 2026-10-26 restriction state and early errors

The retained v1.8.0 safeguards persist realm pauses with their original expiry through a bounded, atomic `usage/restriction-state.json` containing hashed account markers and timestamps only. Invalid/symlinked state and write failures are reported; memory enforcement remains when persistence fails. Existing account cooldowns and the explicit off switch remain, with no historical restriction backfill.

Daily activity now records verified 11140 restrictions and consults the live gateway's pause before new work. Web retries stop without marking the day completed; quota wording and code prefixes cannot become false account restrictions. Early model-entry failures join observation records with `stage` and optional HTTP status, without duplicate request/usage records. Empty-pool responses and ledgers agree on 503 across all three protocols.

Twenty-one new cases cover fresh processes, concurrency, malformed/oversized files, entry errors, task batches and unchanged requests. Loop thresholds, model budgets and successful response semantics remain. This does not remove cloud restrictions or repair other providers' transport failures.

## v1.8.0 changes (baseline capabilities)

Native stderr writes now use a bounded 2,000-line background queue. A blocked console channel cannot park request threads; a full queue drops console lines and reports the count through panel warnings. Each update-download path now permits 10 seconds; model connection, first-frame and generation budgets stay unchanged.

Desktop chat compatibility and certificate-only daily-activity reporting fallback are both off by default. The former affects desktop chat headers only; the latter uses the account's own realm and leaves non-certificate failures or rejected reports unsuccessful. TLS verification, request bodies and existing routing remain. These are upstream capabilities, not a claim of restriction removal.

One new integration case fills the stderr queue and checks error terminals, received provider usage, response closure, account-slot release and no retry across three streaming protocols. Five patch modules and the AST of 16 critical guard symbols match the prior stable version.

## v1.7.33 changes (historical safeguards retained)

The first-frame watchdog records expiry before closing its owned socket, so a Windows clock-tick race cannot open extra connections. Client cancellation is handled before egress penalties. Both cases have deterministic regressions; the original single-connection assertion remains.

Native successful-request credential recovery, initial tool-argument deltas, daily-activity budgets/ordered candidates and restored model cooldowns come from upstream. Review the new 600-second daily budget, random slots and default-on startup catch-up before updating. The public installer preserves settings.

The external r3 reference informed Windows timed-read interruption and cancellation during recovery. Only the owned socket handle may close; released pool leases remain protected. Cancellation is checked before recovery, after connection and during probing, closes the new response and cannot become an account error or another retry. Initial-silence heartbeats and original deadlines remain. Eight new cases exercise real sockets across three protocols, cancellation, released leases and fragmented/interleaved native tool arguments.

## v1.7.32 changes (historical safeguards retained)

Includes native model aliases, metadata-only background rewriting, corrected realm gates, actual OpenCode context/output limits, accessible theme colors and direct-file entry guidance. Explicit model choices now reach their named model. Existing account/key/routing settings are retained.

The native 15-second SSEHeartbeat shares the guarded buffered writer across Chat, Responses and Messages, including silence before the first upstream frame. Comments do not count as model output, TTFT or usage and never reset generation/phase deadlines. Stream headers precede first-frame probing, so subsequent probe failures are SSE errors; authentication/connection failures and nonstreaming statuses retain their prior behavior. Downstream heartbeat failure interrupts the owned upstream, forbids replay and is visible to the request thread. Close/error/cancellation stop the heartbeat, including before same-account reopen.

Messages upstream resets now remain upstream failures with original usage instead of being mislabeled as client cancellation. First-frame errors preserve the actual billed model. Seven new cases cover all three protocols and real loopback HTTP. The inventory is 210 Python and 61 Node suites; no extra generation retries are introduced.

Three connection-deadline regressions additionally cover budget exhaustion during preparation, headers returned after the deadline, and expired socket errors. No connection starts after preparation spends the budget; late responses close without success or account penalties, with slots returned. Existing waits, retry counts and model selection are retained. Heartbeats cannot restore unavailable upstreams; proxy refusal, quotas and context limits on other provider routes require route-specific treatment.

## v1.7.30 changes (historical safeguards retained)

r2 adopts selected fixes from the supplied v1.7.30-r2 reference. Only counted admissions own slots: unlimited requests cannot release earlier requests, concurrent close releases once, and all exits close unreturned responses. OAuth refresh releases before renewal and reacquires under the original policy/deadline. Only complete bounded JSON-object 401 bodies establish expiry in request-triggered refresh; no late error-body reads occur after its watchdog. Background retry policy and verified TLS context remain unchanged. Cache accounting skips malformed details, booleans, nonfinite values and numeric overflow without rewriting the provider snapshot.

The v1.7.30 r2 inventory was 206 Python suites (41 patch and 165 native) plus 59 Node suites. Its eighteen added cases cover slot ownership, six adapters, cancellation, loop recovery, real loopback HTTP, 401 evidence and cache aliases; those safeguards remain in the current inventory above. Synthetic tests do not establish production long-session completion rates.

Includes native check-in/keepalive/scheduler fixes, body draining and single chunk IDs, web-conversation cleanup/backoff, static API-key accounts, remote compaction v2, custom dropdowns and key policy persistence. Account concurrency, stable affinity, realm fallback and paid fallback retain existing settings and their off defaults.

The new compaction adapter keeps the original generation deadline, consumed provider usage, actual billed model and key/effort attribution. Truncated, filtered, failed, refused, tool-bearing or empty summaries cannot produce a successful compaction item; no extra generation retry is added. Compaction output follows the native v2 format. Real long-session replay remains unverified.

Concurrency slots are released on hard HTTP errors, timeout and all exceptional exits; refreshed accounts remain subject to readiness/exclusion checks. Waits share the original deadline. The removed entitlements module is retired in favor of upstream catalog tiers. Startup update checks may change only their persisted update_check cache.

## v1.7.28 changes (historical)

Includes native account-ID redaction, billing-tier discovery, grouped cooldown-row sorting, identity compatibility, retained update backups and optional realm fallback. The native realm fallback remains off by default. Removed the obsolete process-table fixture adaptation now fixed upstream.

The fallback shares the original deadline, key allowlist and actual serving realm. Pinned routes, explicit realms, restriction holds and expired budgets cannot switch realms. Recovery snapshots freeze allowlists; adapters without account-policy parameters fail instead of discarding restrictions.

Corrects incomplete r2 coverage: the old disconnect test checked only a prefix, while a real read exception still dropped the held suffix. Chat, Responses and Messages now flush the original received text before propagating the read error. All six paths preserve filtered printed text without synthesizing executable tools. Seven focused cases and a strengthened existing assertion reproduce the former failures.

Native international tier discovery can add one billing-summary read per account per day. Automatic update executors do not preserve this third-party patch; continue using the patched-image deployment workflow.

## v1.7.24 changes (historical)

Absorbs official v1.7.19–v1.7.24: account `tasksEnabled` separated from API `enabled`, printed tool calls released as soon as their closing tag arrives (#549), a mid-stream failure now emits a real error frame instead of a fake `[DONE]` (#549), hourly web-activity windows with concurrency/dedupe/linear backoff, per-key account binding, models-only-available filtering, reasoning-effort mapping, global prompt text, site content versions, dashboard heatmap refresh, Windows portable one-click update, and an external update executor. Official features come from the v1.7.24 baseline; this patch only keeps our own safeguards.

Three stream-boundary fixes are carried by this patch, each with regression coverage. A closing tag split across deltas (`</invoke>` then `</calls>`) is swallowed instead of leaking a literal `DSML` fragment as prose. Printed calls are released only after a normal terminal, so a truncated or failed transport never synthesizes a side-effecting tool. The content loop detector now reads a DSML-scrubbed view, so a repeated value inside a tool parameter is not treated as repeated prose.

r2 introduced closing-tag ownership and six regressions. Its disconnect and filtered-terminal coverage was incomplete; the v1.7.28 section describes the corrected implementation and scope.

r2.2 absorbs three cross-exit boundaries from the external v1.7.28 package: a fallback helper called without an explicit deadline now shares one panel-configured header budget across both exits, a domestic exit under a restriction hold is not selected as a backup, and a slow backup-eligibility check that exhausts the remaining budget no longer opens a second upstream connection. Eight focused regressions cover the six adapters' actual realm/account/billing attribution and the first-frame same-account phase deadline.

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

Upstream v1.7.6 through v1.7.30 redirected three named models (`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex`) by name alone. Since v1.7.31, redirection requires explicit background metadata; foreground model choices are preserved. Set `WB_BACKGROUND_MODEL_NAMES=""` to disable background redirection, or configure the list/target explicitly.

Anthropic Messages and count_tokens endpoints now return400 by default, after authentication and before payload forwarding. The bridge still ignores `cache_control`; cache parity is not fixed. Set `WB_ENABLE_ANTHROPIC_MESSAGES=1` only when retaining this protocol is intended. This public bundle preserves the upstream defaults; existing protocol regression suites opt in explicitly.

## Install

Prepare separate clean pinned sources, keep the previous deployment and backups, and verify first:

~~~sh
git clone --depth 1 --branch v1.9.0-ops.20261029.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v190
git clone --depth 1 --branch v1.9.0 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.9.0
git -C workbuddy2api-hub-v1.9.0 rev-parse HEAD
python3 -B ops-patch-v190/apply.py --verify-package
python3 -B ops-patch-v190/apply.py --target ./workbuddy2api-hub-v1.9.0 --dry-run
~~~

Use [INSTALL.md](docs/INSTALL.md) to stop the one intended service, apply, rebuild and recreate it with its original parameters. Earlier patched trees are not overlays; do not delete state or checksum metadata to bypass a refusal. The [previous v1.6.20 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1) remains available.

Requests explicitly support integer `n=1`. Eligible empty/pure-reasoning recovery shares one attempt and a 300-second default deadline; visible content, refusal and tool traces forbid replay. Equal reasoning aliases count once, divergent aliases both count against the 65536-character budget. Leading-thinking mixed detection stays separate from ordinary prose and quoted/code examples.

A response whose wire body was read in one block retains its connection lease until buffered frames are checked. Valid completion returns it to the pool; loops/timeouts discard it. Already received usage is preserved on failure. Authentication refresh uses a separate non-pooled connection, one HTTP attempt, a 1MiB cap and a shared deadline capped at30seconds. OS DNS cannot be forcibly cancelled, but a late lookup cannot send expired credentials.

## Verification and limits

The runner covers 221 Python groups (49 patch and all 172 official groups), plus 59 official and 2 patch Node groups. CI checks Linux3.11/3.12 and Windows3.11; see [VERIFICATION.md](docs/VERIFICATION.md). Tests use synthetic data and loopback HTTP with external Python sockets denied; they do not establish online task completion.

r2 protects quoted repetition examples, preserves already-consumed provider usage across guard failure, transport errors and cancellation, and bounds SSE lines/events (4/8MiB defaults). A request-local history observer reports repeated tool results without blocking or replaying tools. It does not combine sessions or infer generic shell-command progress. Durable metrics distinguish requests from guarded attempts and legal-output recovery from business completion, with explicit time/version/protocol grouping and retention limits.

Upstream v1.7.4/v1.7.5 adds optional SQLite logs (off by default), retention limits, checkpoint fixes, manual credential-key import, a new dashboard, daily/manual catalog refresh, provenance/raw archives and actual credit multipliers. Model listing and displayed prices follow the updated native catalog. Persist `/app/catalog` or set `WB_PROXY_CATALOG_DIR=/app/usage/catalog` to reuse an existing usage mount. Include upstream `wb_logstore.py` even when the optional log database is disabled. Earlier1.7 updates include account failure/backoff handling, weights/notes, web engines and cache improvements. Request-body default remains16MiB; use explicit configuration for larger histories. Upgrades can rebuild checkpoint caches while preserving append-only history.

`/health/retry` reports source and a payload fingerprint frozen at import, comparable with `BASELINE.json`. Original authentication remains in force. Report versions, protocol, codes and minimal synthetic examples; exclude credentials, complete configuration and private conversations.

Derived from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT, retaining attribution in [LICENSE](LICENSE), [NOTICE.md](NOTICE.md) and [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).

The patched dashboard is installed, rolled back and fingerprinted with the Python payload. Quota scans use an empty model list. Native window probing defaults on and can send model requests at quota reset; explicitly set `settings.window_probe=false` before deploying to retain the old passive behavior. The installer does not change configuration or schedule settings.
