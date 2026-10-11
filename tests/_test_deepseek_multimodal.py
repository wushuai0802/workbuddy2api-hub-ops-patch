"""Regression tests for DeepSeek v4.1 tool-output image sanitization."""

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(os.path.dirname(__file__), "_acc"))
os.environ.setdefault("USAGE_DIR", os.path.join(os.path.dirname(__file__), "_usage"))

import wb_proxy as proxy


IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def check(label, condition, detail=None):
    if not condition:
        raise AssertionError("%s: %r" % (label, detail))
    print("ok - %s" % label)


user_image = copy.deepcopy(IMAGE)
tool_image = copy.deepcopy(IMAGE)
payload = {
    "model": "deepseek-v4.1-flash",
    "reasoning_effort": "high",
    "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, user_image]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {
                "name": "inspect", "arguments": "{}"
            }}
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": [
            {"type": "text", "text": "tool text"}, tool_image
        ]},
    ],
}
before = copy.deepcopy(payload)
body = proxy.build_upstream_body(payload)
tool = body["messages"][3]
user = body["messages"][1]

check("input is not mutated", payload == before)
check("user image is preserved", user["content"][1] == IMAGE)
check("tool image is removed", not any(p.get("type") == "image_url" for p in tool["content"]))
check("tool placeholder is text", any(
    p.get("type") == "text" and "images only in user messages" in p.get("text", "")
    for p in tool["content"]
))
check("tool text is preserved", tool["content"][0] == {"type": "text", "text": "tool text"})
check("tool pairing is preserved", tool["tool_call_id"] == "call-1")
check("assistant tool call is preserved", body["messages"][2]["tool_calls"][0]["id"] == "call-1")

other = proxy.build_upstream_body({
    "model": "gpt-6-astra",
    "messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {
                "name": "inspect", "arguments": "{}"
            }}
        ]},
        {"role": "tool", "tool_call_id": "x", "content": [IMAGE]},
    ],
})
check("other model keeps tool image", other["messages"][2]["content"][0] == IMAGE)

print("deepseek multimodal checks passed")
