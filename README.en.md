# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Install and rollback](docs/INSTALL.md) · [Patch scope](docs/PATCHES.md) · [Verification](docs/VERIFICATION.md)

Release **v1.7.3-ops.20261014.1**, targeting upstream **v1.7.3** at `c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`. Python 3.11+ is required.

[Download ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.7.3-ops.20261014.1): `workbuddy2api-hub-v1.7.3-wushuai0802-ops-20261014-r1.zip`.

This version rebases the supplied v1.7.2 bundle onto the exact v1.7.3 source and retains our mixed-stall guard, reasoning-alias budget, tool-image compatibility, refusals, bounded refresh, and pooled connection ownership. It preserves upstream additional-tools/custom namespace support, Anthropic thinking, DSML fallback, block I/O and shared TLS contexts.

The installer replaces `wb_proxy.py`, `wb_accounts.py`, `wb_webtools.py` and `wb_upstream_pool.py`, adds `wb_ops.py` and `wb_stall.py`, and checks all unchanged dependencies. It does not modify account data, API keys, configuration or usage, and does not deploy.

## Install

Prepare separate clean pinned sources, keep the previous deployment and backups, and verify first:

~~~sh
git clone --depth 1 --branch v1.7.3-ops.20261014.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v173
git clone --depth 1 --branch v1.7.3 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.7.3
git -C workbuddy2api-hub-v1.7.3 rev-parse HEAD
python3 -B ops-patch-v173/apply.py --verify-package
python3 -B ops-patch-v173/apply.py --target ./workbuddy2api-hub-v1.7.3 --dry-run
~~~

Use [INSTALL.md](docs/INSTALL.md) to stop the one intended service, apply, rebuild and recreate it with its original parameters. Earlier patched trees are not overlays; do not delete state or checksum metadata to bypass a refusal. The [previous v1.6.20 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261012.1) remains available.

Requests explicitly support integer `n=1`. Eligible empty/pure-reasoning recovery shares one attempt and a 300-second default deadline; visible content, refusal and tool traces forbid replay. Equal reasoning aliases count once, divergent aliases both count against the 65536-character budget. Leading-thinking mixed detection stays separate from ordinary prose and quoted/code examples.

A response whose wire body was read in one block retains its connection lease until buffered frames are checked. Valid completion returns it to the pool; loops/timeouts discard it. Already received usage is preserved on failure. Authentication refresh uses a separate non-pooled connection, one HTTP attempt, a 1MiB cap and a shared deadline capped at30seconds. OS DNS cannot be forcibly cancelled, but a late lookup cannot send expired credentials.

## Verification and limits

56 focused groups passed locally on Windows/Python3.14.7. CI checks Linux3.11/3.12 and Windows3.11; use actual workflow results linked by [VERIFICATION.md](docs/VERIFICATION.md). Tests use synthetic data and loopback HTTP, with external Python sockets denied. They do not establish online recovery rate or guarantee business progress over independent tool rounds.

Upstream v1.7.3 improves streaming I/O, custom namespace tools, panel layout, timeout settings, international activity and recent-usage visibility. Earlier1.7 updates include account failure/backoff handling, weights/notes, web engines and cache improvements. Request-body default remains16MiB; use explicit configuration for larger histories. Upgrades can rebuild checkpoint caches while preserving append-only history.

`/health/retry` reports source and a payload fingerprint frozen at import, comparable with `BASELINE.json`. Original authentication remains in force. Report versions, protocol, codes and minimal synthetic examples; exclude credentials, complete configuration and private conversations.

Derived from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT, retaining attribution in [LICENSE](LICENSE), [NOTICE.md](NOTICE.md) and [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).
