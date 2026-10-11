# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Installation and rollback](docs/INSTALL.md) · [Patch details](docs/PATCHES.md) · [Verification scope](docs/VERIFICATION.md)

An unofficial, inspectable and reversible source patch for [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), focused on DeepSeek reasoning repetition, tool-result image errors, expired authentication and streaming failures.

DeepSeek v4.1 Flash can repeat “Let me go.” and “Hmm.” many times among otherwise different reasoning text. The new bounded mixed-stall detector handles high-density instances that the existing repetition detector may miss. An occasional phrase during useful work is allowed.

## Release and compatibility

- Release: **v1.6.20-ops.20261011.1**, dated 2026-10-11.
- [Download the ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.20-ops.20261011.1).
- Archive: `workbuddy2api-hub-v1.6.20-ops-20261011-r1.zip`.
- Exact upstream baseline: **v1.6.20**, commit **`cb6a844588705ea01388be2b9df1f0cbbbcde357`**.
- Python **3.11+** is required. Docker users must build an image from the patched exact upstream source.

This is a patch bundle, not a complete gateway, account bundle, prebuilt image or arbitrary-version overlay. The installer rejects unknown patches and mismatched or mixed source trees.

For upstream v1.6.19, use the [historical r2 release](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2) and the instructions at its tag. The bundles target different baselines and cannot be used interchangeably.

## Included protections

| Group | Behavior |
| --- | --- |
| Loop and deadline guards | Detect high-confidence reasoning/content repetition within one generation; use a shared 300-second generation deadline by default. |
| Mixed-stall guard | Detect dense repeated short action sentences and Hmm in DeepSeek v4.1 Flash reasoning using bounded windows. |
| One safe recovery | Share at most one recovery between eligible empty turns and pure-reasoning loops. Never automatically replay after content or a tool trace. |
| Reasoning history | Apply a default 65,536-character outgoing old-reasoning budget, exact failed-prefix quarantine and confirmed repeated-Hmm cleanup. |
| Tool-result image compatibility | Replace DeepSeek v4.1 Flash tool-result images with explanatory text to handle the associated `11133` error. |
| HTTP 401 refresh | Attempt a bounded credential refresh and retry after upstream authentication rejection. |
| Answer and stream repairs | Trusted-boundary answer recovery, DSML cleanup, first-frame validation, explicit truncation errors and bounded reasoning buffering. |
| Prompt, web and diagnostics | Keep passthrough instructions after 403 by default; bound web decompression, provide configurable search fallback and bounded diagnostics. |

See [patch details](docs/PATCHES.md) for scope and configuration. Sign-in scheduling, account/cache affinity, upstream 429 handling and empty-function placeholders are upstream capabilities, not claims of original work here.

## Quick start

Run in your own working directory:

~~~sh
git clone --depth 1 --branch v1.6.20-ops.20261011.1 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-v1620
git clone --depth 1 --branch v1.6.20 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.6.20
git -C workbuddy2api-hub-v1.6.20 rev-parse HEAD
python3 -B ops-patch-v1620/apply.py --verify-package
python3 -B ops-patch-v1620/apply.py --target ./workbuddy2api-hub-v1.6.20 --dry-run
~~~

Check the full upstream commit above. A successful dry run changes nothing. Follow [INSTALL.md](docs/INSTALL.md) to back up your own configuration, data and previous image, stop the target service, apply the patch and rebuild only your gateway service.

Only `wb_proxy.py`, `wb_webtools.py` and `wb_accounts.py` are replaced; `wb_ops.py` and `wb_stall.py` are added. The installer does not modify account data, settings, usage, keys, ports or routing, and does not deploy anything. Do not force it past a refusal or copy one module into a live container. For unknown existing patches, use a separate clean baseline and retain the old deployment.

## Upstream v1.6.20 and migration

The rebase retains v1.6.20's panel sorting, daily activity and sign-in history, update checks, remaining-usage estimates, persistent account limits, connection reuse and polling/statistics improvements. Its panel 401 polling fix is separate from this patch's model-request credential refresh. Core reasoning-loop, safe-empty-recovery, tool-image and reasoning-history protections remain part of this patch. See the [official v1.6.20 changelog](https://github.com/ardeyouxipianyi/workbuddy2api-hub/blob/v1.6.20/docs/CHANGELOG.md).

- The upstream request-body default is now **16 MiB**, down from 50 MiB. This patch retains it. For larger JSON histories, explicitly set `WB_MAX_PAYLOAD_BYTES=52428800` to restore 50 MiB after considering concurrent-request memory use.
- The mainland streak manager can perform automatic makeup sign-ins, redemption and lottery draws. Makeup sign-ins and draws consume applicable cards/available attempts; review your enabled scheduled tasks before upgrading.
- Remaining-usage priority scheduling is off by default. Enabling it sends more requests to accounts near exhaustion and may reach limits sooner.
- Client one-click configuration is unavailable in Docker/OpenWrt server deployments; it writes client configuration on the gateway's own machine.
- To upgrade from the v1.6.19 patch, prepare a fresh exact v1.6.20 source tree and apply this bundle. Retain the previous image, configuration and data.

## Verification and limits

The earlier v1.6.19 component evaluation covered 132 real reasoning blocks, published only as anonymous aggregate results, and detected 3 anomalous blocks including the two longest repetition blocks. Ten negative-case categories, 100 independent short tool rounds and 12 focused regression groups passed. These historical results do not establish v1.6.20 migration compatibility; see [VERIFICATION.md](docs/VERIFICATION.md) for this release and connection-pool coverage. No original conversations, accounts or credentials are included.

No paid model requests or online long-session replay were made for this release, and we do not claim the entire upstream test suite is green. See [VERIFICATION.md](docs/VERIFICATION.md).

This does not guarantee that all loops or phrases disappear. It guards one upstream generation at a time; repeated successful tool rounds are not automatically treated as a loop. Outgoing-history cleanup does not alter the client's stored conversation or prevent credit exhaustion. Health checks and offline tests do not prove your deployment's actual conversation behavior.

## Attribution and license

Upstream: [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT. This repository is also distributed under [MIT](LICENSE), retaining upstream attribution.

After an upstream upgrade, the patch must be rebased and verified against that exact version. Switching to an official image removes these local code changes. When reporting issues, include versions, protocol, error code and redacted reproduction steps; exclude account files, API keys, complete configuration, private histories and credential-bearing images.
