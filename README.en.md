# WorkBuddy2API-Hub Ops Patch

[中文](README.md) · [Installation and rollback](docs/INSTALL.md) · [Patch details](docs/PATCHES.md) · [Verification scope](docs/VERIFICATION.md)

An unofficial, inspectable and reversible source patch for [workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), focused on DeepSeek reasoning repetition, tool-result image errors, expired authentication and streaming failures.

DeepSeek v4.1 Flash can repeat “Let me go.” and “Hmm.” many times among otherwise different reasoning text. The new bounded mixed-stall detector handles high-density instances that the existing repetition detector may miss. An occasional phrase during useful work is allowed.

## Release and compatibility

- Release: **v1.6.19-ops.20261011.2**, dated 2026-10-11.
- [Download the ZIP and SHA256SUMS.txt](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch/releases/tag/v1.6.19-ops.20261011.2).
- Archive: `workbuddy2api-hub-v1.6.19-ops-20261011-r2.zip`.
- Exact upstream baseline: **v1.6.19**, commit **`55dbb9ebcac3d55b26442d9bbc8b2e11ece637da`**.
- Python **3.11+** is required. Docker users must build an image from the patched exact upstream source.

This is a patch bundle, not a complete gateway, account bundle, prebuilt image or arbitrary-version overlay. The installer rejects unknown patches and mismatched or mixed source trees.

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
git clone --depth 1 --branch v1.6.19-ops.20261011.2 https://github.com/wushuai0802/workbuddy2api-hub-ops-patch.git ops-patch-20261011
git clone --depth 1 --branch v1.6.19 https://github.com/ardeyouxipianyi/workbuddy2api-hub.git workbuddy2api-hub-v1.6.19
git -C workbuddy2api-hub-v1.6.19 rev-parse HEAD
python3 -B ops-patch-20261011/apply.py --verify-package
python3 -B ops-patch-20261011/apply.py --target ./workbuddy2api-hub-v1.6.19 --dry-run
~~~

Check the full upstream commit above. A successful dry run changes nothing. Follow [INSTALL.md](docs/INSTALL.md) to back up your own configuration, data and previous image, stop the target service, apply the patch and rebuild only your gateway service.

Only `wb_proxy.py`, `wb_webtools.py` and `wb_accounts.py` are replaced; `wb_ops.py` and `wb_stall.py` are added. The installer does not modify account data, settings, usage, keys, ports or routing, and does not deploy anything. Do not force it past a refusal or copy one module into a live container. For unknown existing patches, use a separate clean baseline and retain the old deployment.

## Verification and limits

An offline evaluation of 132 real reasoning blocks, published only as anonymous aggregate results, detected 3 anomalous blocks including the two longest repetition blocks. Ten negative-case categories, 100 independent short tool rounds and 12 focused regression groups passed. No original conversations, accounts or credentials are included.

No paid model requests or online long-session replay were made for this release, and we do not claim the entire upstream test suite is green. See [VERIFICATION.md](docs/VERIFICATION.md).

This does not guarantee that all loops or phrases disappear. It guards one upstream generation at a time; repeated successful tool rounds are not automatically treated as a loop. Outgoing-history cleanup does not alter the client's stored conversation or prevent credit exhaustion. Health checks and offline tests do not prove your deployment's actual conversation behavior.

## Attribution and license

Upstream: [ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), MIT. This repository is also distributed under [MIT](LICENSE), retaining upstream attribution.

After an upstream upgrade, the patch must be rebased and verified against that exact version. Switching to an official image removes these local code changes. When reporting issues, include versions, protocol, error code and redacted reproduction steps; exclude account files, API keys, complete configuration, private histories and credential-bearing images.
