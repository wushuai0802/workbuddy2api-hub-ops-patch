# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Install and rollback](docs/INSTALL.md) · [Patch scope](docs/PATCHES.md) · [Verification](docs/VERIFICATION.md)

Release **v1.7.6-ops.20261015.1**, targeting upstream **v1.7.6** at `7dbe11564c0c74a1d2cdb05791b313e339dddbf5`. Python 3.11+ is required.

[Download ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.6-ops.20261015.1): `workbuddy2api-hub-v1.7.6-wushuai0802-ops-20261015-r1.zip`.

This version migrates our retained safeguards onto the exact v1.7.6 source and retains our mixed-stall guard, reasoning-alias budget, tool-image compatibility, refusals, bounded refresh, and pooled connection ownership. It preserves upstream additional-tools/custom namespace support, Anthropic thinking, DSML fallback, block I/O and shared TLS contexts.

The installer replaces four upstream modules and adds `wb_ops.py`, `wb_stall.py` and `wb_observe.py`, checking all unchanged dependencies. It preserves accounts, keys, configuration and usage and does not deploy. The running gateway writes a separate bounded observation log, disabled with `WB_OPS_OBSERVE=0`.

The header phase respects panel settings (120 seconds by default); its 45-second first-frame phase starts after headers. Initial connection, recovery and web-tool follow-ups share the original generation deadline. The sole safe pure-reasoning recovery now adds a short continuation hint by default; set `WB_OPS_LOOP_RECOVERY_HINT=0` to disable the hint. Consecutive exact failed prefixes are cleaned within 16-segment/65536-character bounds. Guard thresholds remain unchanged; anonymous bounded rule/window metrics and retry-stop reasons are persisted.

The supplied v1.7.5-r2 reference informed quote-span masking and detached validated cumulative usage snapshots. Quoted examples remain protected while short quoted tool names no longer exempt a surrounding repeated plan. Original SSE bytes and tool arguments are preserved.

## v1.7.6 compatibility

Upstream now redirects three named models (`gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.3-codex`) to DeepSeek by default. Matching uses the model name alone, so explicit foreground requests also match. Set `WB_BACKGROUND_MODEL_NAMES=""` to retain original routing, or configure the list/target explicitly.

Anthropic Messages and count_tokens endpoints now return400 by default, after authentication and before payload forwarding. The bridge still ignores `cache_control`; cache parity is not fixed. Set `WB_ENABLE_ANTHROPIC_MESSAGES=1` only when retaining this protocol is intended. This public bundle preserves the upstream defaults; existing protocol regression suites opt in explicitly.

## Install

Prepare separate clean pinned sources, keep the previous deployment and backups, and verify first:

~~~sh
git clone --depth 1 --branch v1.7.6-ops.20261015.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v176
git clone --depth 1 --branch v1.7.6 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.6
git -C workbuddy2api-hub-v1.7.6 rev-parse HEAD
python3 -B ops-patch-v176/apply.py --verify-package
python3 -B ops-patch-v176/apply.py --target ./workbuddy2api-hub-v1.7.6 --dry-run
~~~

Use [INSTALL.md](docs/INSTALL.md) to stop the one intended service, apply, rebuild and recreate it with its original parameters. Earlier patched trees are not overlays; do not delete state or checksum metadata to bypass a refusal. The [previous v1.6.20 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1) remains available.

Requests explicitly support integer `n=1`. Eligible empty/pure-reasoning recovery shares one attempt and a 300-second default deadline; visible content, refusal and tool traces forbid replay. Equal reasoning aliases count once, divergent aliases both count against the 65536-character budget. Leading-thinking mixed detection stays separate from ordinary prose and quoted/code examples.

A response whose wire body was read in one block retains its connection lease until buffered frames are checked. Valid completion returns it to the pool; loops/timeouts discard it. Already received usage is preserved on failure. Authentication refresh uses a separate non-pooled connection, one HTTP attempt, a 1MiB cap and a shared deadline capped at30seconds. OS DNS cannot be forcibly cancelled, but a late lookup cannot send expired credentials.

## Verification and limits

70 focused groups passed locally on Windows/Python3.14.7. CI checks Linux3.11/3.12 and Windows3.11; see [VERIFICATION.md](docs/VERIFICATION.md). Tests use synthetic data and loopback HTTP with external Python sockets denied; they do not establish online task completion.

r2 protects quoted repetition examples, preserves already-consumed provider usage across guard failure, transport errors and cancellation, and bounds SSE lines/events (4/8MiB defaults). A request-local history observer reports repeated tool results without blocking or replaying tools. It does not combine sessions or infer generic shell-command progress. Durable metrics distinguish requests from guarded attempts and legal-output recovery from business completion, with explicit time/version/protocol grouping and retention limits.

Upstream v1.7.4/v1.7.5 adds optional SQLite logs (off by default), retention limits, checkpoint fixes, manual credential-key import, a new dashboard, daily/manual catalog refresh, provenance/raw archives and actual credit multipliers. Model listing and displayed prices follow the updated native catalog. Persist `/app/catalog` or set `WB_PROXY_CATALOG_DIR=/app/usage/catalog` to reuse an existing usage mount. Include upstream `wb_logstore.py` even when the optional log database is disabled. Earlier1.7 updates include account failure/backoff handling, weights/notes, web engines and cache improvements. Request-body default remains16MiB; use explicit configuration for larger histories. Upgrades can rebuild checkpoint caches while preserving append-only history.

`/health/retry` reports source and a payload fingerprint frozen at import, comparable with `BASELINE.json`. Original authentication remains in force. Report versions, protocol, codes and minimal synthetic examples; exclude credentials, complete configuration and private conversations.

Derived from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT, retaining attribution in [LICENSE](LICENSE), [NOTICE.md](NOTICE.md) and [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).
