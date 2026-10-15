# Attribution

This unofficial bundle derives from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), distributed under MIT. Original notice is preserved in [LICENSE](LICENSE).

Supported upstream: v1.7.5, commit `762982531fae2d58aacfaa78c989a873f06206df`.

The user-supplied `workbuddy2api-hub-v1.7.2-ops-20261012-r1.zip` provided a reference migration and DSML boundary fixes. Its credential-deadline and refusal work credits this repository's earlier `v1.6.20-ops.20261012.1`. We retained relevant reference changes while preserving our additional guard/history/image behavior. See [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).

The four modified upstream modules retain upstream attribution. `wb_ops.py` holds retained operations safeguards; `wb_stall.py` adds bounded mixed-stall detection; `wb_observe.py` adds privacy-safe request/attempt and tool-history observations. This repository is independently maintained.

Block SSE reading/writing, shared TLS, additional/custom namespace tools, Anthropic thinking, egress/account governance, sign-in scheduling, optional SQLite logs, catalog refresh/archive and panel/usage improvements are upstream features. The guarded lease deferral, usage preservation, phase deadlines, bounded safe recovery and retained operations protections are documented separately.

Historical releases and their exact-baseline documentation remain available through their tags. Public tests contain synthetic inputs only, with no account records or private conversations.

The supplied v1.7.5-r2 reference (SHA256 `ad92ecc91c16e4cb78841486e5e22c348a60b72df32a4e749df786865bc9de59`) informed bounded quote-span masking and validated detached cumulative provider-usage selection. Attribution and retained differences are recorded in THIRD-PARTY-zh.md.
