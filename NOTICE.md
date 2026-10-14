# Attribution

This unofficial bundle derives from [WorkBuddy2API-Hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub), distributed under MIT. Original notice is preserved in [LICENSE](LICENSE).

Supported upstream: v1.7.3, commit `c7dad8fe32316d47ce4f0ff260bb7ac7802b8a94`.

The user-supplied `workbuddy2api-hub-v1.7.2-ops-20261012-r1.zip` provided a reference migration and DSML boundary fixes. Its credential-deadline and refusal work credits this repository's earlier `v1.6.20-ops.20261012.1`. We retained relevant reference changes while preserving our additional guard/history/image behavior. See [THIRD-PARTY-zh.md](THIRD-PARTY-zh.md).

The four modified upstream modules retain upstream attribution. `wb_ops.py` holds retained operations safeguards; `wb_stall.py` adds bounded mixed-stall detection. This repository is independently maintained.

Block SSE reading/writing, shared TLS, additional/custom namespace tools, Anthropic thinking, egress/account governance, sign-in scheduling, panel and usage improvements are upstream features. The guarded lease deferral, usage preservation and retained operations protections are documented separately.

Historical releases and their exact-baseline documentation remain available through their tags. Public tests contain synthetic inputs only, with no account records or private conversations.
