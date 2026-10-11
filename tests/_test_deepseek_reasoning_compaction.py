"""Regression tests for DeepSeek V4.1 long-history reasoning compaction."""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(os.path.dirname(__file__), "_acc"))
os.environ.setdefault("USAGE_DIR", os.path.join(os.path.dirname(__file__), "_usage"))

import wb_proxy as proxy


def check(label, condition, detail=None):
    if not condition:
        raise AssertionError("%s: %r" % (label, detail))
    print("ok - %s" % label)


long = "r" * 70000
messages = [
    {"role": "user", "content": "keep this"},
    {"role": "assistant", "content": "old", "reasoning_content": long,
     "reasoning": long, "tool_calls": [{"id": "c1"}]},
    {"role": "tool", "tool_call_id": "c1", "content": "result"},
    {"role": "assistant", "content": "new", "reasoning_content": "n" * 2000,
     "reasoning": "n" * 2000},
]
before = copy.deepcopy(messages)
fixed = proxy.compact_deepseek_reasoning_history(
    messages, "deepseek-v4.1-flash"
)

check("input is not mutated", messages == before)
check("old reasoning is compacted", fixed[1]["reasoning_content"] == " ")
check("old legacy reasoning is compacted", fixed[1]["reasoning"] == " ")
check("new reasoning is preserved", fixed[3]["reasoning_content"] == "n" * 2000)
check("visible content is preserved", fixed[1]["content"] == "old")
check("tool call is preserved", fixed[1]["tool_calls"] == [{"id": "c1"}])
check("tool result is preserved", fixed[2] == messages[2])

short = [{"role": "assistant", "content": "x", "reasoning_content": "small"}]
check(
    "short history is unchanged",
    proxy.compact_deepseek_reasoning_history(short, "deepseek-v4.1-flash") == short,
)
other = [{"role": "assistant", "content": "x", "reasoning_content": long}]
check(
    "other models are unchanged",
    proxy.compact_deepseek_reasoning_history(other, "gpt-6-astra") == other,
)

for label, primary in (("empty", ""), ("placeholder", " "), ("short", "short")):
    divergent = [{"role": "assistant", "content": "answer", "reasoning_content": primary,
                  "reasoning": long, "tool_calls": [{"id": "c1"}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "result"}]
    original = copy.deepcopy(divergent)
    compacted = proxy.compact_deepseek_reasoning_history(divergent, "deepseek-v4.1-flash")
    check(label + " primary cannot hide oversized alias",
          compacted[0]["reasoning"] == " " and compacted[0]["reasoning_content"] == " ")
    check(label + " divergence leaves source untouched", divergent == original)
    check(label + " divergence preserves visible content and pairs",
          compacted[0]["content"] == "answer" and
          compacted[0]["tool_calls"] == original[0]["tool_calls"] and compacted[1] == original[1])

alias_only = [{"role": "assistant", "content": "answer", "reasoning": long}]
check("alias-only traces are bounded",
      proxy.compact_deepseek_reasoning_history(alias_only, "deepseek-v4.1-flash")[0]["reasoning"] == " ")
duplicates = [{"role": "assistant", "reasoning_content": "r" * 60000, "reasoning": "r" * 60000}]
check("equal aliases consume one logical trace budget",
      proxy.compact_deepseek_reasoning_history(duplicates, "deepseek-v4.1-flash") == duplicates)
divergent = [{"role": "assistant", "reasoning_content": "a" * 40000, "reasoning": "b" * 40000}]
check("different aliases consume separate trace budget",
      proxy.compact_deepseek_reasoning_history(divergent, "deepseek-v4.1-flash")[0]["reasoning"] == " ")
forwarded = proxy.build_upstream_body({"model": "deepseek-v4.1-flash", "messages": [
    {"role": "user", "content": "test"},
    {"role": "assistant", "content": "answer", "reasoning_content": " ", "reasoning": long},
    {"role": "user", "content": "continue"},
]})
assistant = next(m for m in forwarded["messages"] if m.get("role") == "assistant")
check("full outgoing build bounds the legacy alias", assistant["reasoning"] == " ")

print("deepseek reasoning compaction checks passed")
