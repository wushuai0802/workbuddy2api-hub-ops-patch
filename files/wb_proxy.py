#!/usr/bin/env python3
"""WorkBuddy (workbuddy.ai) -> OpenAI-compatible reverse proxy.
Reuses the credentials the WorkBuddy desktop app already stored on this machine
(%%LOCALAPPDATA%%\\CodeBuddyExtension\\Data\\Public\\auth\\*.info), so no separate
login is needed. Exposes:
    GET  /v1/models
    POST /v1/chat/completions     (stream=true and stream=false)
    GET  /health
Only the Python standard library is required.
    python3 wb_proxy.py                    # bind 127.0.0.1:8788
    python3 wb_proxy.py --port 9000
    python3 wb_proxy.py --api-key sk-local # require a bearer token
Launchers: start-wb-proxy.bat / start-wb-proxy-lan.bat on Windows,
start-wb-proxy.command (or ./start-wb-proxy.sh) on macOS/Linux.
"""
import argparse
import copy
import hashlib
from collections import Counter, deque
import re
import itertools
import json
import os
MAX_PAYLOAD_BYTES = int(os.environ.get("WB_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))  # 50MB limit
# Upstream chat calls may hold a handler thread for up to 600s, and every
# request gets its own thread, so an unbounded pool lets a handful of slow
# clients pin hundreds of threads and the memory behind them. Bound the number
# of chat/responses requests in flight; dashboard and management calls are not
# affected. Excess callers wait briefly, then get a 503 instead of queueing
# forever.
#
# The ceiling is a fixed 32 by default, which has nothing to do with how many
# accounts the pool holds: a 200-account gateway and a 5-account gateway share
# it. Set WB_MAX_CONCURRENT_CHAT=auto to size it from the pool instead - one
# slot per ready account, never below 32. See resize_chat_slots().
_CHAT_SLOTS_ENV = os.environ.get("WB_MAX_CONCURRENT_CHAT", "32").strip().lower()
CHAT_SLOTS_AUTO = _CHAT_SLOTS_ENV in ("auto", "pool", "dynamic")
CHAT_SLOTS_FLOOR = 32
try:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR if CHAT_SLOTS_AUTO else int(_CHAT_SLOTS_ENV)
except ValueError:
    MAX_CONCURRENT_CHAT = CHAT_SLOTS_FLOOR
CHAT_SLOT_WAIT_SECONDS = float(os.environ.get("WB_CHAT_SLOT_WAIT", 30))
import socket
import sys
import threading
import time
import math
import urllib.error
import urllib.request
import uuid
import wb_accounts
import wb_activity
import wb_atrest
import wb_catalog
import wb_ipintel
import wb_pricing
import wb_settings
import wb_webtools
from wb_stall import MixedStallGuard, clean_stalled_reasoning
from wb_ops import (
    CONTINUATION_MAX_ATTEMPTS, FIRST_FRAME_DEADLINE, FIRST_FRAME_MAX_ATTEMPTS,
    REASON_DSML_MAX_HOLD, REASON_LOOP_TAIL, UPSTREAM_IDLE_TIMEOUT,
    FailedReasoningCache, LeakedThinkingGuard, LoopRetryCircuit, ReasoningAnswerRecovery, ReasoningLoopGuard, ReasoningScrubber, _scrub_dsml_reasoning_messages, detect_reasoning_loop,
    dsml_safe_prefix, finish_reason_of, quarantine_dsml_text,
    reasoning_only_turn, strip_dsml_from_text, StallObserver, CompactSSEBuffer,
)
import wb_identity
import wb_prompt
import wb_modelsdev
import wb_probes
import wb_updates
import wb_agents
IS_WINDOWS = os.name == "nt"
def launcher_hint(port):
    """Platform-appropriate launcher command for starting on another port."""
    if IS_WINDOWS:
        return "start-wb-proxy.bat %d" % port
    return "./start-wb-proxy.sh %d" % port
def port_owner_hint(port):
    """Command that lists the process holding a local TCP port."""
    if IS_WINDOWS:
        return "netstat -ano | findstr :%d" % port
    return "lsof -nP -iTCP:%d -sTCP:LISTEN" % port
CURRENT_REALM = os.environ.get("WB_PROXY_DEFAULT_REALM", "intl")
def detect_model_realm(model_id):
    if not model_id:
        return CURRENT_REALM
    m = str(model_id).lower()
    intl_only = {
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
        "grok-4.7"
    }
    if m in intl_only or any(m.startswith(p) for p in ("gpt-", "gemini-")):
        return "intl"
    cn_only = {
        "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "glm-5.1", "glm-5.0-turbo", "glm-4.6v",
        "kimi-k3-1", "kimi-k2.7", "kimi-k2-thinking",
        "hy3-x", "hy4-preview-dev", "hy4-preview-x"
    }
    if m in cn_only or any(m.startswith(p) for p in ("minimax-", "deepseek-v4-pro")):
        return "cn"
    return CURRENT_REALM
# glm-5.3-flash was listed as cn-only, but the international exit serves it:
# an official intl account posting to www.workbuddy.ai gets HTTP 200, and the
# intl desktop client ships it in its own model list. Only deepseek-v4-pro
# still answers "service info not found" there.
# Models that exist on one side only. Everything else (deepseek-v4.1-flash,
# hy3, glm-5.3 ...) is served by both exits, so it must not be treated as a
# conflict.
INTL_EXCLUSIVE_PREFIXES = ("gpt-", "gemini-")
CN_EXCLUSIVE_PREFIXES = ("minimax-", "deepseek-v4-pro")
INTL_EXCLUSIVE = {
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.4", "gpt-5.3-codex", "gemini-3.5-flash",
    "grok-4.7",
}
CN_EXCLUSIVE = {
    "deepseek-v4-pro", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.7", "minimax-m3",
    "hy3-x", "hy4-preview-dev", "hy4-preview-x",
}
def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else ""."""
    if not model_id:
        return ""
    m = str(model_id).lower()
    if m in INTL_EXCLUSIVE or m.startswith(INTL_EXCLUSIVE_PREFIXES):
        return "intl"
    if m in CN_EXCLUSIVE or m.startswith(CN_EXCLUSIVE_PREFIXES):
        return "cn"
    return ""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
def install_console_close_handler():
    """Release the port when the console window is closed by the user.
    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running".
    Closing the window raises CTRL_CLOSE_EVENT in every process attached to
    that console, which is exactly the signal we want. Registering a handler
    for it is event-driven, so unlike polling a parent pid there is no
    chance of a false positive. Harmless when started without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6
        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False
        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None
UPSTREAM = "https://www.workbuddy.ai"
CHAT_PATH = "/v2/chat/completions"
MODELS_PATH = "/v2/enterprises/personal/models"
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
# The WorkBuddy AI desktop app caches its account product config here on every
# launch. That file carries the real model catalog the app shows in its picker
# (21 models, incl. deepseek-v4.1-flash / gpt-6-astra) - the CLI-facing
# /v2/enterprises/personal/models endpoint returns a narrower list, so prefer
# the cache and fall back to the endpoint.
PRODUCT_CONFIG_CACHE = os.path.join(os.path.expanduser("~"), ".workbuddy-ai", "cache", "acc-product-config-v3.json")
NOISE_KEYS = ("extra_fields", "refusal", "reasoning_content")
class BodyTooLarge(Exception):
    """Raised when a request body exceeds the configured cap."""
    def __init__(self, length):
        super(BodyTooLarge, self).__init__(length)
        self.length = length
class BadJSON(Exception):
    """Raised when a request body is present but not a JSON object."""
# CORS is only needed by browser-based chat clients that call the OpenAI-style
# API from another origin. Management routes (accounts, settings, usage,
# scheduler, panel) serve the dashboard, which is same-origin, so they get no
# ACAO header - that keeps a stray page on the LAN from reading their replies.
CORS_PATH_PREFIXES = ("/v1", "/chat", "/completions", "/models", "/responses", "/health/retry")
# Management paths that happen to live under /v1 must not be treated as API:
# /v1/usage reports account-level spend and is gated by the panel session.
MANAGEMENT_PATH_PREFIXES = ("/v1/usage", "/usage", "/accounts", "/settings",
                            "/tasks", "/scheduler", "/panel", "/logs")
def cors_origin_allowed(path):
    """True when the OpenAI-style API path should advertise CORS."""
    path = (path or "").split("?")[0]
    if path.startswith(MANAGEMENT_PATH_PREFIXES):
        return False
    return path.startswith(CORS_PATH_PREFIXES)
_lock = threading.Lock()
_login_lock = threading.Lock()
_chat_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHAT)
def resize_chat_slots(ready_accounts):
    """Size the chat concurrency ceiling from the pool, when asked to.

    With WB_MAX_CONCURRENT_CHAT=auto the ceiling becomes one slot per ready
    account (never below CHAT_SLOTS_FLOOR), so the gateway scales with the pool
    it actually has instead of a constant that fits neither a 5-account nor a
    200-account deployment. A fixed numeric WB_MAX_CONCURRENT_CHAT keeps the
    1.6.x behaviour and makes this a no-op.

    Only ever grows the ceiling. Requests already holding a slot keep theirs -
    replacing the semaphore cannot revoke a permit - so a shrink would let the
    in-flight count exceed the new ceiling and the surplus releases would raise
    ValueError from BoundedSemaphore. Growing is the direction that matters:
    the pool is loaded once at startup, so in practice this runs before the
    listener accepts anything.
    """
    global _chat_slots, MAX_CONCURRENT_CHAT
    if not CHAT_SLOTS_AUTO:
        return MAX_CONCURRENT_CHAT
    try:
        wanted = max(CHAT_SLOTS_FLOOR, int(ready_accounts or 0))
    except (TypeError, ValueError):
        return MAX_CONCURRENT_CHAT
    if wanted <= MAX_CONCURRENT_CHAT:
        return MAX_CONCURRENT_CHAT
    MAX_CONCURRENT_CHAT = wanted
    _chat_slots = threading.BoundedSemaphore(wanted)
    log("chat slots : %d (one per ready account; WB_MAX_CONCURRENT_CHAT=auto)"
        % wanted)
    return wanted
_login_attempts = {}  # ip -> list of timestamp
def _prune_login_attempts(now=None, window=60):
    """Drop stale per-IP entries so the dict cannot grow without bound.
    Caller must hold _login_lock.
    """
    now = now or time.time()
    for ip in list(_login_attempts.keys()):
        recent = [t for t in _login_attempts[ip] if now - t < window]
        if recent:
            _login_attempts[ip] = recent
        else:
            del _login_attempts[ip]
_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}
# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request. Defaults to a folder next to
# this script; override with --usage-dir or WB_PROXY_USAGE_DIR.
USAGE_DIR = os.environ.get("WB_PROXY_USAGE_DIR") \
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
DASHBOARD_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit")
# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions live in memory only, so a
# restart forces browsers to log in again.
PANEL = wb_settings.PanelSessions()
API_KEY_FILE_SET = False
def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return wb_settings.api_keys(ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []
def auth_required():
    """Whether /v1 calls must present a key at all."""
    if wb_settings.auth_disabled(ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(API_KEY)
def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.
    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (API_KEY,)
    return wb_settings.match_api_key(ACCOUNTS_DIR, supplied, extra_keys=extra)
def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0, "cost_cny": 0.0, "cost_missing": {},
            "started": time.time(), "by_model": {},
            # Same aggregation keyed by (model, realm), so the metrics table
            # can show one row per exit for a model that ran through both.
            "by_model_realm": {},
            # And again keyed by (model, realm, account), so a model served
            # by two accounts on the same exit can be split per account.
            "by_model_acct": {},
            # latency accumulators (averages; percentiles come from the JSONL)
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}
_usage = _empty_stats()
def normalize_usage_cache_aliases(usage):
    """Write the best cache-hit value into every alias.

    Some responses carry the real hit in prompt_tokens_details.cached_tokens
    while also emitting cache_read_input_tokens: 0 / cached_tokens: 0
    compatibility aliases; strict downstream parsers may prefer the zero
    aliases and lose the hit. Mutates and returns the usage dict.
    """
    if not isinstance(usage, dict):
        return usage
    best = _best_cached_tokens(usage)
    if best <= 0:
        return usage
    usage["cache_read_input_tokens"] = best
    usage["cached_tokens"] = best
    usage["prompt_cache_hit_tokens"] = best
    prompt_details = dict(usage.get("prompt_tokens_details") or {})
    prompt_details["cached_tokens"] = best
    usage["prompt_tokens_details"] = prompt_details
    if isinstance(usage.get("input_tokens_details"), dict):
        input_details = dict(usage["input_tokens_details"])
        input_details["cached_tokens"] = best
        usage["input_tokens_details"] = input_details
    return usage


def _extract_usage(usage):
    """Normalize the upstream usage block into the fields we track."""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": _best_cached_tokens(usage),
        "total_tokens": usage.get("total_tokens") or 0,
        "credit": usage.get("credit") or 0,
    }
def row_realm(row):
    """The realm a log row belongs to.

    Rows written since the field was added carry it directly. Older rows are
    attributed by their account, then by the model's home realm - the same
    order row_matches_realm used, so a filter and a per-realm breakdown can
    never disagree about the same row.
    """
    r = row.get("realm")
    if r:
        return r
    acct_uid = row.get("account")
    if acct_uid and POOL:
        acc = POOL.get(acct_uid)
        if acc:
            return acc.realm
    model = row.get("model")
    if model:
        return detect_model_realm(model)
    return "intl"


def row_matches_realm(row, realm):
    # None means every realm. "all" is accepted here as well so that a caller
    # that forwards the literal cannot silently match nothing: the previous
    # behaviour compared every row's realm against the string "all".
    if not realm or realm == "all": return True
    return row_realm(row) == realm
def realm_scope(realm, fallback=None):
    """Map a caller-supplied realm onto a log filter.

    "all" means every realm, so it becomes None and disables filtering
    entirely: passing the literal through would make row_matches_realm
    compare every row against "all" and match nothing at all. An empty
    or missing value falls back to the second argument: CURRENT_REALM for
    the endpoints whose clients expect the global switch, None (everything)
    for the analytics payload, which has always reported both realms
    combined.
    """
    if realm == "all":
        return None
    return realm or fallback


def _local_midnight(ts=None, days_back=0):
    """Local midnight `days_back` days before `ts` (default: now).

    mktime normalises an out-of-range day, so stepping back past the 1st of a
    month still lands on a real local midnight instead of raising.
    """
    lt = time.localtime(ts if ts is not None else time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, 0, 0, 0, 0, 0, -1))


def _epoch_or_none(value):
    """A panel-supplied epoch second, or None when it cannot be trusted.

    A negative or unparseable bound is dropped rather than clamped. The panel
    rejects those before they are ever sent, so one arriving here means a
    hand-written URL, and "no bound on this side" is a much smaller surprise
    than silently slicing the log at 1970.
    """
    if value in (None, "", False):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def range_window(value, since=None, until=None):
    """Resolve the dashboard's time-range selector into a (since, until) pair.

    Both bounds are cutoffs on a row's `at`; None means "unbounded on that
    side", and (None, None) - an unknown, empty or missing range - disables
    filtering entirely, which is what every caller did before windows existed,
    so an older panel keeps receiving the full history it used to get.

    today/week/month are calendar windows anchored to local midnight, matching
    the definition the analytics payload has always used for its Today
    figures: two different meanings of "today" on one page would be worse than
    either. The week starts on Monday. Rolling aliases ("7d", "30d") are
    deliberately absent - they would mean "the last seven days", which is a
    different window from "this week" and would make the button's label wrong
    on six days out of seven.

    custom takes the two epochs the panel sends. Either side may be missing
    ("from this date onwards" / "up to this date"), and reversed bounds are
    swapped rather than rejected, because the two inputs are independent and
    an empty end is the normal case.
    """
    v = str(value or "").strip().lower()
    if v in ("today", "day", "1d"):
        return _local_midnight(), None
    if v in ("week", "w"):
        return _local_midnight(days_back=time.localtime().tm_wday), None
    if v in ("month", "m"):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)), None
    if v == "custom":
        lo, hi = _epoch_or_none(since), _epoch_or_none(until)
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        return lo, hi
    return None, None


def range_query(query):
    """Pull the three range parameters out of a parsed query string.

    parse_qs hands every key back as a list, and an older panel that sends
    none of them at all is the normal case, so every lookup falls back to
    None - which range_window() reads as "no filter on that side".
    """
    def first(name):
        values = query.get(name) or [None]
        return values[0] if values else None
    return first("range"), first("since"), first("until")
def row_outcome(row):
    """Terminal state of a request row.

    Rows written before the outcome field existed only carry error/status,
    so they fall back to that: an error row is a failure, anything else is a
    completed request. One helper keeps every reader agreeing on the answer.
    """
    o = row.get("outcome")
    if o:
        return o
    return "failed" if row.get("error") else "completed"
def _best_cached_tokens(usage):
    """Best cache-hit value across every alias the upstreams emit (E3).

    Order mirrors the panel: prompt_tokens_details.cached_tokens >
    prompt_cache_hit_tokens > cache_read_input_tokens > cached_tokens >
    input_tokens_details.cached_tokens > completion_tokens_details.
    First positive value wins; zero aliases never shadow a real hit.
    """
    if not isinstance(usage, dict):
        return 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    input_details = usage.get("input_tokens_details") or {}
    details = usage.get("completion_tokens_details") or {}
    for value in (prompt_details.get("cached_tokens"),
                  usage.get("prompt_cache_hit_tokens"),
                  usage.get("cache_read_input_tokens"),
                  usage.get("cached_tokens"),
                  input_details.get("cached_tokens"),
                  details.get("cached_tokens")):
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            # Token counts must stay integers: the Codex client parses
            # response.completed strictly and rejects 123.0 (invalid number).
            return int(number)
    return 0


def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None, gen_ms=None, fp=None,
                account=None, outcome="completed", key=None, effort=None):
    """Record one finished request as exactly one JSONL row.

    A request without a usage block still gets a row (flagged usage_missing):
    skipping it entirely used to drop the request from the request count,
    success rate and latency samples, not just from the token totals.

    outcome is the terminal state: completed / client_aborted /
    upstream_aborted / failed. It is deliberately not called status, because
    status already means the HTTP status code on error rows.

    key is the settings id of the client API key that paid for the request,
    never the secret itself. It is passed in explicitly rather than read from
    a thread-local: one keep-alive thread serves many requests, so an implicit
    channel would attribute spend to the wrong key silently, while a missed
    call site only shows up as an extra "no key" row.

    effort is the reasoning effort the request actually ran at, as resolved by
    build_upstream_body(). It is written only when the model has one: a model
    without reasoning controls has nothing to report, and rows written before
    this field existed cannot be told apart from it anyway.
    """
    fields = _extract_usage(usage) or {}
    usage_missing = not fields
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "outcome": outcome,
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    if usage_missing:
        row["usage_missing"] = True
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
    acc = POOL.get(account) if (account and POOL) else None
    row["realm"] = acc.realm if acc else CURRENT_REALM
    # Always written, even when the caller presented no key. The empty value is
    # what separates "ran without a key" from rows written before the field
    # existed; the dashboard reports those as two different buckets, and a
    # missing field is the only evidence of the cutover that survives.
    row["key"] = key or ""
    # The price policy this request is measured against, stored as a reference
    # so the table can be de-duplicated and swept. A row written before the
    # table existed has no reference and falls back to the timeline.
    row["cost_policy"] = wb_pricing.current_policy_id(model)
    # Only when the request had one: the panel shows a chip for rows that carry
    # the field, and a model without reasoning controls has nothing to report.
    if effort:
        row["reasoning_effort"] = effort
    # Derived per-request rates (None-safe).
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(fields.get("completion_tokens", 0) / (gen_ms / 1000.0), 2)
    # Share the denominator with the aggregate view (compute_usage_analytics),
    # otherwise the per-request row and the rollup disagree on the same data.
    if fields.get("prompt_tokens", 0) > 0:
        row["cache_hit_pct"] = round(fields.get("cached_tokens", 0) * 100.0
                                     / fields["prompt_tokens"], 1)
    with _lock:
        _usage["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                _usage[k] += fields[k]
        if ttft_ms is not None:
            _usage["ttft_ms_sum"] += ttft_ms
            _usage["ttft_samples"] += 1
        if gen_ms is not None:
            _usage["gen_ms_sum"] += gen_ms
            _usage["gen_samples"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        per = _usage["by_model"].setdefault(model, {"requests": 0, **{k: 0 for k in USAGE_FIELDS}})
        per["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                per[k] += fields[k]
    _persist_usage(row, "usage persist failed")
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
        acc_tag = f" acct={account[:8]}" if account else ""
        speed_tag = f" {row.get('tokens_per_sec', 0)}t/s" if row.get("tokens_per_sec") else ""
        miss_tag = " usage=missing" if usage_missing else ""
        log(f"chat done: model={model}{acc_tag}{dur} tokens={t_tokens} (in={fields.get('prompt_tokens',0)} out={fields.get('completion_tokens',0)}){speed_tag}{miss_tag}", tag="chat")
    except Exception:
        pass
    return row


def _persist_usage(row, fail_label):
    """Append one usage row as a JSONL line.

    usage-summary.json used to be rewritten on every single request - a full
    json.dumps of the running totals, a uniquely named temp file and an
    os.replace, plus the deep copy that fed it. Nothing in the tree ever
    loads that file (every aggregate re-reads usage.jsonl), so the work was
    pure overhead on the request path. One append per request now.
    """
    try:
        os.makedirs(USAGE_DIR, exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:
        log("%s: %s" % (fail_label, exc))

def record_error(model, status, message, elapsed_ms=None, account=None,
                 usage=None, stream=None, ttft_ms=None, gen_ms=None, fp=None,
                 outcome="failed", key=None, effort=None):
    """Record one failed request as exactly one JSONL row.

    Passing the account uid records which account the request was bound to, so
    per-realm success rates attribute the failure by fact instead of falling
    back to guessing from the model name.

    The usage argument carries whatever the upstream had already reported when
    a stream broke. An aborted stream used to write an error row AND a usage
    row, so one request counted as both a failure and a success; the token
    totals stay accurate here without inflating the request count.

    status stays the HTTP status code; outcome is the terminal state, so the
    two never disagree about what the field means.

    key is the client API key's settings id, same as record_usage. It is the
    only way a rejected request can be attributed to a key, so failures on the
    key's model whitelist still land on the right row instead of vanishing
    into the unattributed bucket.

    effort optionally retains the resolved reasoning level, using the same
    field as successful usage rows. Existing callers may omit it.
    """
    fields = _extract_usage(usage) or {}
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "outcome": outcome,
        "status": status,
        "message": str(message)[:200],
        "elapsed_ms": elapsed_ms,
    }
    if stream is not None:
        row["stream"] = bool(stream)
    if ttft_ms is not None:
        row["ttft_ms"] = ttft_ms
    if gen_ms is not None:
        row["gen_ms"] = gen_ms
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
        acc = POOL.get(account) if POOL else None
        row["realm"] = acc.realm if acc else CURRENT_REALM
    row["key"] = key or ""
    if effort:
        row["reasoning_effort"] = effort
    with _lock:
        _usage["errors"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
    _persist_usage(row, "error persist failed")
    dur = f" {elapsed_ms:.0f}ms" if elapsed_ms is not None else ""
    log(f"request error: model={model}{dur} status={status} msg={str(message)[:180]}", level="ERROR", tag="chat")
    return row
def _pct(values, q):
    """Nearest-rank percentile (no interpolation) - good enough for latency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]
_perf_cache = {}
_perf_lock = threading.Lock()


def _perf_stats_scope(sample, realm, range, since, until):
    """(过滤用的 realm, since, until, 缓存键) —— perf_stats() 与它的 ETag 共用。

    抽出来只为一件事：取数和派生 ETag 必须走**同一套**键。两边各写一份的话，
    只要有一处不同（比如一处 realm_scope、一处原始 realm），tag 就会落到另一条
    缓存条目上，客户端拿到的是一个不描述当前响应体的校验符。
    """
    r = realm_scope(realm, CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    try:
        # The key carries the resolved bounds rather than a today/all flag:
        # this week and this month overlap, so a flag cannot tell them apart
        # and one window's latency would be served under the other's label.
        key = (int(sample), r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    except Exception:
        key = (5000, r or "all",
               lo if lo is not None else -1, hi if hi is not None else -1)
    return r, lo, hi, key


def perf_stats(sample=5000, realm=None, ttl=None, range=None, since=None, until=None):
    """Cached wrapper: parsing thousands of rows is CPU-heavy, and the
    dashboard polls this endpoint every few seconds.

    Rebuilds under the lock so a burst of pollers cannot each start their own
    scan of the log."""
    ttl = _STATS_TTL if ttl is None else ttl
    r, lo, hi, key = _perf_stats_scope(sample, realm, range, since, until)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _perf_stats_uncached(sample, r, since=lo, until=hi)
        _perf_cache[key] = (time.time(), data)
    return data


def perf_stats_etag(sample=5000, realm=None, range=None, since=None, until=None):
    """perf_stats() 当前缓存条目的 ETag；条目还不存在时返回 None。

    _json_cached() 会在取数前后各调用一次并比对，两次一致才把 tag 发出去
    （见那里的注释）。所以这里只回答"此刻这条缓存条目的戳是什么"，调用方不需要
    自己安排调用顺序。
    """
    _r, _lo, _hi, key = _perf_stats_scope(sample, realm, range, since, until)
    with _perf_lock:
        hit = _perf_cache.get(key)
    if hit is None:
        return None
    return _cache_etag("perf", repr(key), hit[0])


def _perf_stats_uncached(sample=5000, realm=None, since=None, until=None):
    """Latency percentiles + derived rates, computed from the JSONL log."""
    ttfts, gens, walls, rates, hits, tok_rates = [], [], [], [], [], []
    total = ok = err = aborted = 0
    # 按模型聚合性能指标
    m_buckets = {}
    # Same aggregation keyed by (model, realm), so the metrics table can
    # report a model's latency per exit when it ran through both.
    mr_buckets = {}
    # And keyed by (model, realm, account), so two accounts on one exit can
    # be shown as separate rows.
    ma_buckets = {}
    # 只读日志末尾 sample 行：原先 readlines() 会把整个日志读成字符串列表
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(USAGE_LOG, sample)]
    # The tail read stops at `sample` lines, so a window wider than the sample
    # is only described by its newest requests. Both facts are reported so the
    # matrix can say the latency columns cover a partial slice instead of
    # presenting them as the whole window.
    sample_capped = len(rows) >= sample
    sample_from = None
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if sample_from is None:
            sample_from = r.get("at")
        if realm and not row_matches_realm(r, realm):
            continue
        # Same window as the usage snapshot, so the latency and speed columns
        # of the matrix describe the same requests as its token columns.
        at = r.get("at") or 0
        if since and at < since:
            continue
        if until and at > until:
            continue
        total += 1
        outcome = row_outcome(r)
        # Every row reaches the model bucket, whatever its outcome, so a model
        # that only ever saw cancellations still shows up with a zero success
        # count instead of silently vanishing from the per-model table.
        m_id = r.get("model") or "unknown"
        r_realm = row_realm(r)
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                                        "ttfts": [], "gens": [], "walls": [],
                                        "tok_rates": [], "hits": []})
        rb = mr_buckets.setdefault(m_id, {}).setdefault(
            r_realm, {"total": 0, "ok": 0, "err": 0, "aborted": 0,
                      "ttfts": [], "gens": [], "walls": [],
                      "tok_rates": [], "hits": []})
        ab = ma_buckets.setdefault(m_id, {}).setdefault(r_realm, {}).setdefault(
            r.get("account") or "(unattributed)",
            {"total": 0, "ok": 0, "err": 0, "aborted": 0,
             "ttfts": [], "gens": [], "walls": [],
             "tok_rates": [], "hits": []})
        mb["total"] += 1
        rb["total"] += 1
        ab["total"] += 1
        # A client that walks away is not a gateway failure, so it counts as
        # neither ok nor err - it gets its own bucket instead of silently
        # dragging the success rate down.
        if outcome == "client_aborted":
            aborted += 1
            for b in (mb, rb, ab):
                b["aborted"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        if outcome != "completed":
            err += 1
            for b in (mb, rb, ab):
                b["err"] += 1
                if r.get("elapsed_ms"):
                    b["walls"].append(r["elapsed_ms"])
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        for b in (mb, rb, ab):
            b["ok"] += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
            for b in (mb, rb, ab):
                b["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
            for b in (mb, rb, ab):
                b["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
            for b in (mb, rb, ab):
                b["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
            for b in (mb, rb, ab):
                b["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
            for b in (mb, rb, ab):
                b["hits"].append(r["cache_hit_pct"])
    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": _pct(vals, 50),
            "p90": _pct(vals, 90),
            "p99": _pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }
    return {
        "sampled": total,
        # Where the sampled slice starts and whether it was cut short, so a
        # week/month view can admit that its latency columns do not reach back
        # to the window's own start.
        "sample_from": sample_from,
        "sample_capped": sample_capped,
        "success": ok,
        "errors": err,
        "client_aborted": aborted,
        # Success rate is measured against requests the gateway actually
        # finished; client cancellations are reported separately rather than
        # being counted as failures.
        "success_rate_pct": round(ok * 100.0 / (ok + err), 1) if (ok + err) else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "client_aborted": mb.get("aborted", 0),
                "success_rate_pct": round(mb["ok"] * 100.0 / (mb["ok"] + mb["err"]), 1)
                                     if (mb["ok"] + mb["err"]) else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
        "by_model_realm": {
            mid: {
                realm: {
                    "requests": rb["total"],
                    "errors": rb["err"],
                    "client_aborted": rb.get("aborted", 0),
                    "success_rate_pct": round(rb["ok"] * 100.0 / (rb["ok"] + rb["err"]), 1)
                                         if (rb["ok"] + rb["err"]) else None,
                    "ttft_ms": block(rb["ttfts"]),
                    "generation_ms": block(rb["gens"]),
                    "wall_ms": block(rb["walls"]),
                    "tokens_per_sec": block(rb["tok_rates"]),
                    "cache_hit_pct": block(rb["hits"]),
                } for realm, rb in realms.items()
            } for mid, realms in mr_buckets.items()
        },
        "by_model_acct": {
            mid: {
                realm: {
                    acct: {
                        "requests": ab["total"],
                        "errors": ab["err"],
                        "client_aborted": ab.get("aborted", 0),
                        "success_rate_pct": round(ab["ok"] * 100.0 / (ab["ok"] + ab["err"]), 1)
                                             if (ab["ok"] + ab["err"]) else None,
                        "ttft_ms": block(ab["ttfts"]),
                        "generation_ms": block(ab["gens"]),
                        "wall_ms": block(ab["walls"]),
                        "tokens_per_sec": block(ab["tok_rates"]),
                        "cache_hit_pct": block(ab["hits"]),
                    } for acct, ab in accts.items()
                } for realm, accts in realms.items()
            } for mid, realms in ma_buckets.items()
        }
    }
_snap_cache = {}
_snap_lock = threading.Lock()
# The dashboard polls every 5s. A TTL shorter than the poll interval makes
# every other poll do the full uncached scan; 15s means at most one rebuild
# per three polls while the numbers stay a few seconds stale at worst.
_STATS_TTL = float(os.environ.get("WB_STATS_TTL", 15))


# ---------------------------------------------------------------------------
# Daily usage counters
#
# The upstream caps a free window at a fixed token budget (code 6004), and by
# the time it answers 429 the window is already spent. This counter lets the
# operator park an account at a threshold instead: usage.jsonl is folded into
# uid -> tokens-since-local-midnight, plus two sibling views of the same rows
# - uid -> credit spent today (the daily credit guard) and
# uid -> {model: tokens} (the per-model daily guard).
# AccountPool.apply_daily_token_limit() / apply_daily_credit_limit() /
# apply_model_daily_token_limit() copy the numbers onto the accounts and
# ready() refuses them, so the next request rotates to another account. The
# scan is incremental (byte offset + per-day totals), so the hot path only
# reads rows that arrived since the last scan.
# ---------------------------------------------------------------------------
_daily_usage = {"day": "", "totals": None, "credits": None, "models": None,
                "offset": 0, "at": 0.0}
_daily_usage_lock = threading.Lock()


def _daily_state_copy(source):
    """Copy the cached per-account counters into a fresh scan state.

    Three views of the same rows: uid -> total tokens, uid -> credit spent,
    uid -> {model: tokens}. The copy keeps a later scan from mutating the
    cached dicts in place while readers hold them.
    """
    return {
        "tokens": dict(source.get("totals") or {}),
        "credits": dict(source.get("credits") or {}),
        "models": {k: dict(v) for k, v in (source.get("models") or {}).items()},
    }


def _scan_daily_usage(offset, state):
    """Fold rows at/after today's local midnight into `state`.

    Returns (state, new_offset). A line without its trailing newline is left
    for the next scan: rows are appended whole, so a partial tail only means
    this read raced the writer.
    """
    midnight = _local_midnight()
    with open(USAGE_LOG, encoding="utf-8") as fh:
        fh.seek(offset)
        while True:
            pos = fh.tell()
            line = fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                return state, pos
            offset = fh.tell()
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if (row.get("at") or 0) < midnight:
                continue
            # Same rule as the analytics scan: a client cancellation is not a
            # consumed request, and its token counts are incomplete.
            if row_outcome(row) == "client_aborted":
                continue
            uid = row.get("account")
            if not uid:
                continue
            tokens = row.get("total_tokens") or 0
            state["tokens"][uid] = state["tokens"].get(uid, 0) + tokens
            credit = row.get("credit") or 0
            if credit:
                state["credits"][uid] = state["credits"].get(uid, 0.0) + credit
            mid = row.get("model")
            if mid:
                per = state["models"].setdefault(uid, {})
                per[mid] = per.get(mid, 0) + tokens
    return state, offset


def daily_usage_stats(ttl=None):
    """Today's per-account usage folded from the log, cached for `ttl` seconds.

    Returns {"tokens": uid -> tokens, "credits": uid -> credit spent,
    "models": uid -> {model: tokens}}, or None when the log could not be read
    at all; callers keep that distinct from zero so a failed read never parks
    an account.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    with _daily_usage_lock:
        c = _daily_usage
        if c["day"] == day and c["totals"] is not None and (now - c["at"]) < ttl:
            return _daily_state_copy(c)
        # A new day keeps the byte offset: everything past it is today's, and
        # the midnight filter drops whatever old rows are still unread.
        if c["day"] == day and c["totals"] is not None:
            state = _daily_state_copy(c)
        else:
            state = {"tokens": {}, "credits": {}, "models": {}}
        offset = int(c["offset"] or 0)
        try:
            size = os.path.getsize(USAGE_LOG)
        except OSError:
            size = 0
        if offset > size:
            state = {"tokens": {}, "credits": {}, "models": {}}
            offset = 0
        try:
            state, offset = _scan_daily_usage(offset, state)
        except Exception as exc:
            log("daily token scan failed: %s" % exc)
            _daily_usage.update({"day": day, "totals": None, "offset": 0,
                                 "at": time.time()})
            return None
        _daily_usage.update({"day": day, "totals": state["tokens"],
                             "credits": state["credits"],
                             "models": state["models"], "offset": offset,
                             "at": time.time()})
        return _daily_state_copy(_daily_usage)


def daily_tokens_by_account(ttl=None):
    """uid -> tokens counted since local midnight, cached for `ttl` seconds.

    None means the log could not be read at all; callers keep that distinct
    from zero so a failed read never parks an account.
    """
    stats = daily_usage_stats(ttl=ttl)
    if stats is None:
        return None
    return stats["tokens"]


def seconds_until_local_midnight():
    """Seconds until the local day rolls over (at least a minute)."""
    lt = time.localtime()
    nxt = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return max(60, int(nxt - time.time()))


def apply_daily_token_limit(refresh=False):
    """Push the daily token setting and today's counts into the pool.

    The setting is resolved per realm, so the pool can guard the two exits
    with different numbers while a realm that carries no override still
    follows the global default.
    """
    if POOL is None:
        return 0
    limits = wb_settings.limit_values(ACCOUNTS_DIR, "daily_token_limit")
    usage = None
    if any(value > 0 for value in limits.values()):
        usage = daily_tokens_by_account(ttl=0 if refresh else None)
    return POOL.apply_daily_token_limit(limits, usage)


def apply_daily_credit_limit(refresh=False):
    """Push the daily credit setting, today's spend and the free-model view
    into the pool."""
    if POOL is None:
        return 0
    limits = wb_settings.limit_values(ACCOUNTS_DIR, "daily_credit_limit")
    credits = None
    free_models = None
    if any(value > 0 for value in limits.values()):
        stats = daily_usage_stats(ttl=0 if refresh else None)
        credits = stats["credits"] if stats is not None else None
        free_models = free_models_by_realm()
    return POOL.apply_daily_credit_limit(limits, credits, free_models)


def apply_model_daily_token_limit(refresh=False):
    """Push the per-model daily token setting and today's counts into the pool."""
    if POOL is None:
        return 0
    limits = wb_settings.limit_values(ACCOUNTS_DIR, "model_daily_token_limit")
    per_model = None
    if any(value > 0 for value in limits.values()):
        stats = daily_usage_stats(ttl=0 if refresh else None)
        per_model = stats["models"] if stats is not None else None
    return POOL.apply_model_daily_token_limit(limits, per_model)


_free_models_cache = {"at": 0.0, "data": None}
_FREE_MODELS_TTL = 60.0


def credits_is_free(value):
    """True when a catalogue credits string means "this one costs nothing".

    The same test curate_remote_catalog() uses to pick free siblings; a
    missing or unparsable value is NOT free, so an unknown model stays
    under the credit guard instead of slipping past it.
    """
    return str(value or "").strip().lower() in ("x0.00", "x0", "0", "0.00")


def free_models_by_realm():
    """realm -> set of model ids the catalogue marks free ("x0.00").

    Built from the bundled snapshot and the desktop cache file - both local
    reads, no network - and cached for a minute so the request path pays
    nothing. The daily credit guard uses it to tell paid models from free
    ones, per realm: the same id can be free on one exit and paid on the
    other.
    """
    now = time.time()
    data = _free_models_cache.get("data")
    if data is not None and (now - _free_models_cache.get("at", 0.0)) < _FREE_MODELS_TTL:
        return data
    out = {}
    for realm, source in (("intl", getattr(wb_catalog, "STATIC_INTL_MODELS", [])),
                          ("cn", getattr(wb_catalog, "STATIC_CN_MODELS", []))):
        free = set()
        for item in source or []:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if mid and credits_is_free(item.get("credits")):
                free.add(mid)
        cached = read_cached_remote_catalog(realm)
        if cached:
            for mid, item in (cached[1] or {}).items():
                if isinstance(item, dict) and credits_is_free(item.get("credits")):
                    free.add(str(mid))
        out[realm] = free
    _free_models_cache.update({"at": now, "data": out})
    return out


def _usage_snapshot_scope(realm, range, since, until):
    """(过滤用的 realm, since, until, 缓存键) —— usage_snapshot() 与它的 ETag 共用。

    同 _perf_stats_scope()：键只允许有一个来源，否则 ETag 可能指向另一条缓存。
    """
    r = realm_scope(realm, CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    # Bounds, not a today/all flag: this week and this month overlap, so a
    # flag would let one window serve the other's totals from the cache.
    key = "%s|%s|%s" % (r or "all",
                        lo if lo is not None else "", hi if hi is not None else "")
    return r, lo, hi, key


def usage_snapshot(realm=None, ttl=None, range=None, since=None, until=None):
    """Cached wrapper: the dashboard polls this every few seconds.

    The rebuild happens while holding the lock on purpose. Releasing it first
    let every concurrent caller run its own full scan of the JSONL when the
    entry expired, so a single dashboard refresh could trigger several scans
    of the same file.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    r, lo, hi, key = _usage_snapshot_scope(realm, range, since, until)
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _usage_snapshot_uncached(r, since=lo, until=hi)
        _snap_cache[key] = (time.time(), data)
    return data


def usage_snapshot_etag(realm=None, range=None, since=None, until=None):
    """usage_snapshot() 当前缓存条目的 ETag；条目不存在时返回 None。

    配对调用（取数前后各一次、一致才发）由 _json_cached() 负责，理由见那里。
    """
    _r, _lo, _hi, key = _usage_snapshot_scope(realm, range, since, until)
    with _snap_lock:
        hit = _snap_cache.get(key)
    if hit is None:
        return None
    return _cache_etag("usage", key, hit[0])


def _fold_cost(bucket, cost, model):
    """Fold one row's estimated cost into a stats bucket.

    Unpriced models land in cost_missing (id -> count) instead of quietly
    vanishing from the totals, so the panel can name what the price
    snapshot does not cover yet. A row the master switch left unpriced is not
    "missing a price" - the whole feature is off - so it is not listed there.
    """
    if cost["known"]:
        bucket["cost_cny"] = (bucket.get("cost_cny") or 0.0) + cost["cny"]
    elif not cost.get("disabled"):
        missing = bucket.setdefault("cost_missing", {})
        mid = model or "unknown"
        missing[mid] = missing.get(mid, 0) + 1


# ---------------------------------------------------------------------------
# Incremental all-time aggregates
#
# The dashboard's all-time panels used to re-read the whole usage.jsonl on
# every cache miss, and the log grows by thousands of rows a day. Those totals
# only change when a row is appended, so each aggregate keeps the byte offset
# it has already folded and a refresh reads just the rows past it - the shape
# daily_usage_stats() already uses for today's counters, truncation guard
# included. A windowed query still walks the file, but the pre-filter above
# drops out-of-window lines before the parse, so it pays only for the rows it
# keeps.
#
# A carried-over total is only valid while everything the fold read is
# unchanged, so a state also carries a fingerprint of its other inputs: the
# price tables (a new policy can re-price a row that has no reference of its
# own) and the realm a row without a realm field is attributed to. When one of
# those moves, the fold starts over - an offset alone would happily keep
# yesterday's prices forever.
#
# One state per realm and per aggregate: each holds a few models and a few
# accounts, so keeping one around costs nothing next to the scan it replaces.
# ---------------------------------------------------------------------------

# What wb_pricing hands back while its file is missing. Without these, every
# call would get a fresh empty dict, the identity test below would never
# match, and a deployment with no price files would rescan on every refresh.
_EMPTY_POLICIES = {}
_EMPTY_TIMELINE = []


def _usage_log_key():
    """(size, device, inode) of the usage log, or a zero key when it is gone.

    One stat answers both questions an incremental fold has to ask. The size
    says whether the bytes the cached offset counted are still there: a
    shorter file was truncated, or replaced by a shorter one. The device and
    inode say whether it is still the same file at all - a log copied over in
    place keeps its inode and is caught by the size test, while one moved into
    position is a different file and a stale offset would point into the
    middle of unrelated rows.
    """
    try:
        info = os.stat(USAGE_LOG)
        return (info.st_size, info.st_dev, info.st_ino)
    except OSError:
        return (0, 0, 0)


def _log_offset_holds(previous, current, offset):
    """True when `offset` still sits inside the file `previous` described.

    False means the cached fold counted bytes that are gone or belong to a
    different file now, so it has to start over.
    """
    return (previous is not None
            and previous[1] == current[1] and previous[2] == current[2]
            and current[0] >= offset)


# How many bytes of the already-counted prefix are read back on every
# refresh to prove that prefix still holds the same bytes. Long enough
# that a rewrite cannot match it by accident, short enough to cost
# nothing on the poll path.
_TAIL_SIGNATURE = 64


def _log_tail_signature(offset):
    """The last bytes before `offset`, or None when they cannot be read.

    An incremental fold assumes the bytes it has already counted are still
    there. Size and inode catch a log that was replaced or cut short, but
    not one that was emptied in place and has since grown past the old
    offset: same file, only longer, which is what truncating the log - or
    a log rotation with copytruncate - leaves behind. Reading the seam back
    is what tells those apart, because an append leaves it alone and a
    rewrite does not.
    """
    if offset <= 0:
        return b""
    try:
        with open(USAGE_LOG, "rb") as fh:
            start = max(0, offset - _TAIL_SIGNATURE)
            fh.seek(start)
            return fh.read(offset - start)
    except OSError:
        return None


def _log_resume_ok(state, log_key):
    """True when the cached offset still sits on the log's current prefix.

    False means the fold cannot be carried over: the file shrank, it is a
    different file, or the bytes it counted were rewritten under it.
    """
    if not _log_offset_holds(state["key"], log_key, state["offset"]):
        return False
    return _log_tail_signature(state["offset"]) == state["tail"]


def _pricing_inputs(pricing_on=None):
    """Everything cost_for_row() reads besides the row itself.

    Identity, not equality: wb_pricing caches each of these behind its own
    file key, so an edited file hands back a different object, and comparing
    identities keeps the check off the refresh path - walking the whole price
    table to notice a change would cost more than the change does. Holding the
    objects here is also what makes the test sound: a live object cannot have
    its id handed to a replacement.

    With the master switch off cost_for_row() never opens a table at all, so
    the flag is the whole input. It is read through the same no-argument
    call cost_for_row() makes - passing a directory here would answer from
    a different settings file whenever the two disagree, and the fingerprint
    would then watch an input the fold never read.
    """
    # pricing_on 由调用方传入时不再自己问一次总开关：一次扫描里它只该被问一次
    # （见 _usage_snapshot_uncached / _compute_usage_analytics_uncached），指纹
    # 与折叠读到的因此是同一个值。
    try:
        if not (wb_pricing.pricing_enabled() if pricing_on is None else pricing_on):
            return (False, None, None, None)
        return (True,
                wb_pricing.load_policies() or _EMPTY_POLICIES,
                wb_pricing.load_timeline() or _EMPTY_TIMELINE,
                wb_pricing.load_pricing())
    except Exception as exc:
        log("pricing inputs unreadable: %s" % exc)
        return None


def _pricing_unchanged(previous, current):
    """True when two _pricing_inputs() tuples describe the same prices."""
    if previous is None or current is None:
        return False
    return (previous[0] == current[0] and previous[1] is current[1]
            and previous[2] is current[2] and previous[3] is current[3])


def _realm_inputs():
    """What row_realm() consults for a row that carries no realm field.

    Rows written before the field existed - an error row with no account - are
    attributed to the account that served them, or to the model's home realm.
    Importing or dropping an account can therefore move such a row from one
    exit to the other, and a cached aggregate must not outlive that.

    这两样（当前出口 + uid→realm 归属）从**磁盘**同步读，而不是从活账号池的
    内存映射读。池是启动时从同一份磁盘加载的快照，但「它什么时候加载完」会让
    同一个进程在不同时刻算出不同的指纹：重启后第一刻的进程算出的是启动期那
    一份，于是刚写下的 checkpoint 反而被判无效——而那正是它存在的意义（重启
    后免冷扫）。从磁盘读，任何时刻、任何进程算出来都一样。

    池仍然要查，但只作为「这次折叠能不能被冷进程复现」的判据（见
    _realm_fold_reproducible），不再进指纹的值。
    """
    disk = _account_realm_map()
    if disk is None:
        return None
    return (CURRENT_REALM, disk)


_realm_disk_cache = {"key": None, "map": None}
_realm_disk_lock = threading.Lock()


def _account_files_key(directory):
    """账号目录的便宜指纹：文件名 + (size, mtime)；读不到返回 None。"""
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return None
    entries = []
    for name in names:
        try:
            info = os.stat(os.path.join(directory, name))
        except OSError:
            return None
        entries.append((name, info.st_size, info.st_mtime_ns))
    return (directory, tuple(entries))


def _account_realm_map(directory=None):
    """账号文件里的 uid→realm 映射（排序后的元组）；读不到返回 None。

    用账号池自己的加载器推导，而不是在这里复制一份 uid/realm 解析逻辑：复制
    出来的那份迟早会和 Account 里的逻辑漂移，而漂移意味着指纹描述的东西和
    折叠读的东西不再是同一个。整份读一次的开销由缓存键挡着——目录下每个
    *.json 的名字/size/mtime 都没变就直接用上次的结果。
    """
    directory = ACCOUNTS_DIR if directory is None else directory
    key = _account_files_key(directory)
    if key is None:
        return None
    with _realm_disk_lock:
        cached = _realm_disk_cache
        if cached["key"] == key:
            return cached["map"]
    try:
        pool = wb_accounts.AccountPool(directory, log=None)
        pool.load()
        value = tuple(sorted((a.uid, a.realm) for a in pool.accounts if a.uid))
    except Exception as exc:
        log("account realm map unreadable: %s" % exc)
        return None
    with _realm_disk_lock:
        _realm_disk_cache.update({"key": key, "map": value})
    return value


def _realm_fold_reproducible(disk):
    """这次折叠用的归属，冷启动的进程能不能按同一份磁盘原样复现。

    折叠读的是活账号池，指纹读的是磁盘。两者一致（正常情况：池就是启动时从
    这份磁盘加载的）时没有疑问；磁盘被改过、池还没重载时两者会短暂不一致，
    那种折叠写下的 checkpoint 会被冷进程按新归属当成有效，所以不写。池整个
    不存在时折叠根本不查账号映射——只有磁盘上也没有账号，两边才一致。
    """
    if POOL is None:
        return not disk
    try:
        pool = tuple(sorted((a.uid, a.realm) for a in POOL.accounts))
    except Exception as exc:
        log("realm inputs unreadable: %s" % exc)
        return False
    return pool == disk


def _scan_usage_from(offset, fold, stop_at=None, skip=None):
    """Fold every row past `offset` into `fold(row)`; returns (offset, error).

    The offset handed back is the end of the last row the pass got through.
    Rows are appended whole, so a line without its trailing newline is left
    for the next pass rather than half-parsed - the rule _scan_daily_usage()
    follows.

    `skip(line)` drops a line before the parse; the windowed readers pass
    _line_outside_window() here, so a line the range excludes costs a substring
    search instead of a json.loads.

    `stop_at` caps the read at the offset an all-time fold has already covered,
    so a windowed payload built on top of that fold describes exactly the same
    bytes as its all-time half.

    The log is read in binary and the offset advances by the length of each
    line: a tell() per line costs more than the parse it would be tracking,
    and an offset only has to be a byte count. Each line is decoded to text
    before it is looked at, which is the order the text-mode readers read in
    and the one the pre-filter expects.

    `error` is the exception that stopped the read, or None. A row the fold
    cannot swallow ends the pass exactly where it used to - the readers have
    always kept what they had folded so far - and the caller reports it with
    its own label. The offset handed back with an error is the end of that
    row: the fold may have half applied it before it raised, so the caller
    starts its own fold over instead of resuming after it, while a windowed
    pass built on the state still sees the row - and stops there - so the
    two halves keep describing the same bytes.
    """
    if stop_at is not None and offset >= stop_at:
        return offset, None
    try:
        with open(USAGE_LOG, "rb") as fh:
            fh.seek(offset)
            while True:
                if stop_at is not None and offset >= stop_at:
                    break
                raw = fh.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                end = offset + len(raw)
                if stop_at is not None and end > stop_at:
                    # The line straddles the covered bytes, so the all-time
                    # fold never saw it either: leave it out of this pass too.
                    break
                try:
                    # Decoded before anything else, exactly as the text-mode
                    # readers do: a line no reader can decode stops them where
                    # it stands, so it stops this pass too rather than letting
                    # it count rows the full scan never reached. Stripping the
                    # text (not the bytes) keeps the two agreeing on what
                    # counts as an empty line.
                    line = raw.decode("utf-8").strip()
                except UnicodeDecodeError as exc:
                    return end, exc
                if not line:
                    offset = end
                    continue
                if skip is not None and skip(line):
                    offset = end
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    offset = end
                    continue
                try:
                    fold(row)
                except Exception as exc:
                    return end, exc
                offset = end
    except FileNotFoundError:
        pass
    except Exception as exc:
        return offset, exc
    return offset, None


def _copy_usage_bucket(bucket):
    """Copy one by_model* bucket, its accounts dict included."""
    out = dict(bucket)
    out["accounts"] = dict(bucket["accounts"])
    return out


def _copy_usage_snapshot(snap):
    """A snapshot copy the caller may decorate and hand out.

    The fold is cached and shared, so what leaves this module is a copy - the
    same reason _daily_state_copy() exists for the daily counters. Only the
    three per-model views nest; every other value is a scalar.
    """
    out = dict(snap)
    out["cost_missing"] = dict(snap["cost_missing"])
    out["by_model"] = {m: _copy_usage_bucket(b) for m, b in snap["by_model"].items()}
    out["by_model_realm"] = {m: {rr: _copy_usage_bucket(b) for rr, b in realms.items()}
                             for m, realms in snap["by_model_realm"].items()}
    out["by_model_acct"] = {
        m: {rr: {acct: _copy_usage_bucket(b) for acct, b in accts.items()}
            for rr, accts in realms.items()}
        for m, realms in snap["by_model_acct"].items()}
    return out


def _fold_usage_snapshot(row, snap, r, since, until, pricing_on=None, also=None):
    """Fold one parsed row into a usage snapshot.

    One definition for both callers: the all-time fold (fed only the rows past
    its offset) and the windowed one (fed the whole file, out-of-window lines
    dropped before the parse). The realm and window tests come first, so every
    total below - requests, tokens, per-model and per-account breakdowns -
    describes the same slice of the log.

    pricing_on 是本次扫描开头问过一次的总开关：聚合只读 known/cny（_fold_cost
    再读 disabled），悬停明细构造出来就被丢掉，所以整扫不建明细、开关也不逐行
    重问。代价是设置改动从下一次扫描起生效；逐行展示路径不受影响，仍然每行
    查、改设置当场可见。

    `also` 是同一行还要折进的第二份同形状快照（按日分桶的窗口半）。过滤、
    算价与字段提取只做一次，两份各自累加：折叠本来就要求两侧各记一笔，而
    cost_for_row 与 realm 判定是每行最贵的那部分，只该付一次。
    """
    if r and not row_matches_realm(row, r):
        return
    at = row.get("at") or 0
    if since and at < since:
        return
    if until and at > until:
        return
    outcome = row_outcome(row)
    # Each row is priced against the version that was in force when it
    # happened, so a later price change cannot rewrite yesterday's totals.
    cost = wb_pricing.cost_for_row(row, details=False, enabled=pricing_on)
    model = row.get("model")
    targets = (snap,) if also is None else (snap, also)
    if outcome != "completed":
        for tgt in targets:
            tgt["errors"] += 1
            # Credit is money already spent: a request that failed after the
            # upstream had billed for it still consumed credit, so it is summed
            # here exactly like the analytics page sums it. Token totals keep
            # the completed-only rule this page has always used, and a client
            # abort is skipped because its usage block is incomplete.
            if outcome != "client_aborted":
                tgt["credit"] += (row.get("credit") or 0)
                _fold_cost(tgt, cost, model)
        return
    # Read each tracked field once: the row lands in four buckets below and
    # every one of them walks the same fields.
    present = [(k, row[k] or 0) for k in USAGE_FIELDS if k in row]
    m = model or "unknown"
    rr = row_realm(row)
    acct_id = row.get("account")
    acct_key = acct_id or "(unattributed)"
    known = cost["known"]
    cny = cost["cny"] if known else 0.0
    for tgt in targets:
        tgt["requests"] += 1
        for k, value in present:
            tgt[k] += value
        _fold_cost(tgt, cost, model)
        per = tgt["by_model"].setdefault(m, {"requests": 0, "accounts": {}, "cost_cny": 0.0, **{k: 0 for k in USAGE_FIELDS}})
        per_realm = tgt["by_model_realm"].setdefault(m, {}).setdefault(
            rr, {"requests": 0, "accounts": {}, "cost_cny": 0.0, **{k: 0 for k in USAGE_FIELDS}})
        per_acct = (tgt["by_model_acct"].setdefault(m, {})
                    .setdefault(rr, {})
                    .setdefault(acct_key, {"requests": 0, "accounts": {},
                                           "cost_cny": 0.0,
                                           **{k: 0 for k in USAGE_FIELDS}}))
        for bucket in (per, per_realm, per_acct):
            bucket["requests"] += 1
            for k, value in present:
                bucket[k] += value
            if known:
                bucket["cost_cny"] += cny
            if acct_id:
                accounts = bucket["accounts"]
                accounts[acct_id] = accounts.get(acct_id, 0) + 1


_usage_snap_state = {}
_usage_snap_state_lock = threading.Lock()


def _usage_snap_state_ready(r, pricing_on):
    """把 `r` 的 all-time 折叠推进到文件尾；返回 (state, offset, error)。

    调用方必须已持有 _usage_snap_state_lock。日桶与 all-time 折叠共用这一次
    扫描（见 _fold_usage_snapshot 的 also），所以窗口半与 all-time 半描述的
    永远是同一段字节——这正是旧实现用 stop_at 想保证的事。
    """
    state = _usage_snap_state.get(r)
    if state is None:
        state = _usage_snap_state[r] = {"snap": None, "offset": 0, "key": None,
                                        "pricing": None, "realm": None,
                                        "tail": b"", "days": {}, "days_floor": None}
    pricing = _pricing_inputs(pricing_on)
    realm = _realm_inputs()
    log_key = _usage_log_key()
    if state["snap"] is None:
        # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_snapshot(r, state, log_key, pricing, realm):
            state.update({"snap": _empty_stats(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
    elif (not _pricing_unchanged(state["pricing"], pricing)
            or state["realm"] != realm
            or not _log_resume_ok(state, log_key)):
        state.update({"snap": _empty_stats(), "offset": 0, "tail": b"",
                      "days": {}, "days_floor": None})
    state.update({"pricing": pricing, "realm": realm, "key": log_key})

    def fold(row):
        # 一行折两处：all-time 半 + 它所属那天的窗口半。过滤与算价在
        # _fold_usage_snapshot 里只做一次。
        _fold_usage_snapshot(row, state["snap"], r, None, None, pricing_on,
                             also=_usage_day_bucket(state, row, r,
                                                    _day_snapshot_bucket))

    offset, error = _scan_usage_from(state["offset"], fold)
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
    return state, offset, error


def _usage_alltime_snapshot(r, pricing_on=None):
    """A private copy of the cached all-time fold for realm filter `r`
    (None = every realm).

    One state per realm: the filter decides which rows the fold ever sees, so
    `intl` and `cn` are two folds and neither may inherit the other's rows.
    The state holds the raw fold only - `started`, `since`, `accounts_map` and
    the rest are re-derived on every call, exactly as the full scan re-derived
    them.
    """
    with _usage_snap_state_lock:
        state, offset, error = _usage_snap_state_ready(r, pricing_on)
        if error is None:
            # Copied while the lock is held: the caller decorates what it
            # gets, and a concurrent refresh must never be seen half-applied.
            snap = _copy_usage_snapshot(state["snap"])
        else:
            # A row the fold could not swallow may have half applied itself,
            # so the carried-over totals cannot be resumed from there: this
            # call answers what was folded - the full scan reports the same
            # partial totals - and the next refresh starts over, which is
            # what the full scan does on every call.
            log(f"usage snapshot read failed: {error}")
            snap = state["snap"]
            state.update({"snap": None, "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
    # 落盘决定在锁外做（见 checkpoint 一节）：写盘要逐把读三份状态，在状态
    # 锁里再取别的状态锁会构成锁序环。折叠失败的那次不写——它的 offset 已经
    # 归零，落盘函数也会跳过。
    if error is None:
        _usage_cache_maybe_save("snapshot", offset)
    return snap


def _scan_usage_snapshot_window(r, since, until, pricing_on=None):
    """One pass over the log, keeping only the rows inside the window.

    Nothing is carried over here - a window moves with the clock - but the
    pre-filter drops the parse for every line it can prove is outside it,
    which is what keeps a cold day or week view cheap on a multi-day log.

    The bounds reach the pre-filter through `or None` because that is the
    test the fold itself applies (`if since and at < since`): a bound of 0
    is a bound the fold ignores, and the pre-filter must not read it as
    "keep nothing after the epoch" and drop rows the fold would have
    counted.
    """
    snap = _empty_stats()
    _, error = _scan_usage_from(
        0,
        lambda row: _fold_usage_snapshot(row, snap, r, since, until, pricing_on),
        skip=lambda line: _line_outside_window(line, since or None,
                                                 until or None))
    if error is not None:
        log(f"usage snapshot read failed: {error}")
    return snap


def _usage_snapshot_window_from_days(r, day_key, pricing_on):
    """窗口半快照 = 窗口内的日桶之和；日桶不可用时返回 None。

    先把 all-time 折叠推进到文件尾：日桶与它同一次扫描推进，窗口半与
    all-time 半因此描述同一段字节。
    """
    with _usage_snap_state_lock:
        state, offset, error = _usage_snap_state_ready(r, pricing_on)
        if error is not None:
            # 折叠失败时状态已经归零，日桶跟着清空：这一次退回整段扫描，
            # 与「没有缓存」时的行为一致。
            log(f"usage snapshot read failed: {error}")
            state.update({"snap": None, "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
            return None
        floor = state.get("days_floor")
        if not _days_cover(state, day_key):
            return None
        snap = _empty_stats()
        days = state["days"]
        for key in sorted(days):
            if key >= day_key:
                _add_usage_snapshot(snap, days[key])
    _usage_cache_maybe_save("snapshot", offset)
    return snap


def _usage_snapshot_window(r, since, until, pricing_on=None):
    """窗口半快照：对齐窗口由日桶相加，其余保持整段扫描。

    只有「until 开放 + 起点是本地午夜」的窗口拆得成整天的并集（today /
    week / month 都是）。自定义区间与带 until 的区间一律走整段扫描：起点
    不是午夜就拆不开，而 until 落在某天中间时，那一天的桶里还有窗口外的
    行，加进去就是错的。
    """
    day_key = _window_day_key(since) if until is None else None
    if day_key is not None and _usage_day_buckets_enabled():
        snap = _usage_snapshot_window_from_days(r, day_key, pricing_on)
        if snap is not None:
            return snap
        # 日桶没盖住窗口起点（还没折过/被剪枝/折叠出错）：冷路径现折一份
        # 按日分组的窗口半，口径与日桶状态一致，两条路径的字节因此相同。
        return _scan_usage_snapshot_window_days(r, since, pricing_on)
    return _scan_usage_snapshot_window(r, since, until, pricing_on)


def _usage_snapshot_uncached(realm=None, since=None, until=None):
    # None means every realm; usage_snapshot() has already mapped "all"
    # onto it, so the filter below is simply skipped.
    r = realm
    rep = POOL.representative(realm=r) if POOL else current_account()
    # 总开关整扫只问一次，值一路传进折叠与价格指纹：每行重问一次就是每行一次
    # os.stat，2.7 万行上很可观（见 tests/_test_usage_aggregate_fold.py）。
    pricing_on = wb_pricing.pricing_enabled()
    if since is None and until is None:
        # An open window covers every row, and the rows only ever arrive at
        # the end of the log, so the all-time totals are carried over from the
        # last fold and only the rows appended since then are read.
        snap = _usage_alltime_snapshot(r, pricing_on)
    else:
        snap = _usage_snapshot_window(r, since, until, pricing_on)
    snap["started"] = _usage.get("started", time.time())
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.get("started", time.time())))
    snap["log_file"] = USAGE_LOG
    snap["realm"] = r or "all"
    # Cost figures are CNY; the panel divides by this rate to show USD
    # without a round trip.
    snap["usd_cny"] = wb_pricing.usd_cny()
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm} for a in POOL.accounts} if POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": (wb_accounts.jwt_issuer(rep.access_token) if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(POOL.accounts) if POOL else 0),
        "accounts_ready": (POOL.count_ready() if POOL else 0),
    }
    return snap
def _tail_lines(path, max_lines, chunk=256 * 1024):
    """Return up to the last `max_lines` non-empty lines, oldest first.

    The usage log passes 20MB within a day. Scanning it end to end on every
    dashboard poll was the dominant cost behind slow /usage/* responses.
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines


_count_cache = {}
_count_lock = threading.Lock()
# The count only feeds the "N records" label. The poll interval is 5s, so a
# TTL of the same length would miss on nearly every poll; 30s turns a full
# scan per poll into one scan per six polls while the label stays current
# enough for a record total that only ever grows.
_COUNT_TTL = float(os.environ.get("WB_COUNT_TTL", 30))


_series_cache = {}
_series_lock = threading.Lock()


def _usage_timeseries_scope(realm, range, since, until, bucket_seconds):
    """(realm, lo, hi, step, 缓存键) —— usage_timeseries() 与它的 ETag 共用。

    lo/hi 是这次真正要扫的窗口（未钉住的边界在这里补成"现在/往前 24 小时"），
    键只收**钉住的**边界，理由见下面 wrapper 里的注释。
    """
    r = realm_scope(realm, CURRENT_REALM)
    lo, hi = range_window(range, since, until)
    # Which bounds the caller pinned, captured before the fallbacks below fill
    # the rest in. Only a pinned bound may enter the cache key as a value:
    # "now" (and the 24h-before-now default lo) is recomputed on every call,
    # so a key holding it could never be reused while the panel polls the same
    # chart, and the cache would buy nothing. The "auto" sentinel keeps the two
    # kinds of bound apart, so a rolling window is never served a pinned
    # window's series.
    pinned_lo, pinned_hi = lo, hi
    if hi is None:
        hi = time.time()
    if lo is None:
        lo = hi - 86400
    span = max(1.0, hi - lo)
    if bucket_seconds:
        step = max(60, int(bucket_seconds))
    elif span <= 6 * 3600:
        step = 60
    elif span <= 14 * 86400:
        step = 3600
    else:
        step = 86400
    # Bounds, not a range flag: this week and this month overlap, so a flag
    # would let one window serve the other's series from the cache. The
    # bucket step joins the key for the same reason: an explicit bucket
    # changes every bucket's width, and the auto-scaled one flips with the
    # span, so two different steps are two different payloads.
    key = (r or "all",
           pinned_lo if pinned_lo is not None else "auto",
           pinned_hi if pinned_hi is not None else "auto",
           step)
    return r, lo, hi, step, key


def usage_timeseries(realm=None, range=None, since=None, until=None,
                     bucket_seconds=None, ttl=None):
    """Bucketed token/credit series for the analytics chart.

    Bucket size auto-scales with the window: minute (<=6h), hour (<=14d),
    day otherwise; an explicit bucket_seconds overrides it. Completed
    requests contribute tokens; every non-client-aborted row contributes
    credit (money already spent). The most recent credited requests ride
    along so the panel can show a credit history without another endpoint.

    Cached wrapper: the Token chart polls this endpoint whenever the metrics
    tab is open, and one uncached call re-reads and re-parses the whole log.
    Same shared TTL as its siblings, and the rebuild runs under the lock so a
    burst of pollers cannot each start their own scan.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    r, lo, hi, step, key = _usage_timeseries_scope(realm, range, since, until,
                                                   bucket_seconds)
    now = time.time()
    with _series_lock:
        hit = _series_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
        data = _usage_timeseries_uncached(r, lo, hi, step)
        _series_cache[key] = (time.time(), data)
    return data


def usage_timeseries_etag(realm=None, range=None, since=None, until=None,
                          bucket_seconds=None):
    """usage_timeseries() 当前缓存条目的 ETag；没有条目时返回 None。

    配对调用由 _json_cached() 负责。注意键里只有钉住的边界，所以"滚动窗口"这一类
    请求始终落在同一条目上：条目没重建，响应体（含它自己的 until / 桶边界）就逐
    字节不变，这正是可以放心回 304 的原因。
    """
    _r, _lo, _hi, _step, key = _usage_timeseries_scope(realm, range, since, until,
                                                       bucket_seconds)
    with _series_lock:
        hit = _series_cache.get(key)
    if hit is None:
        return None
    return _cache_etag("series", repr(key), hit[0])


# Every row this process writes starts with `{"at": <float>,` - record_usage
# and record_error put the timestamp first and json.dumps keeps that order -
# which is what makes the window pre-filter below possible.
_AT_PREFIX = '{"at": '


def _line_outside_window(line, lo, hi):
    """True when this log line's `at` is provably outside [lo, hi].

    Either bound may be None, which means "unbounded on that side": the
    ranges the dashboard asks for ("today", "this week") only pin a start,
    and an open side must never drop a row.

    The scan uses it to drop out-of-window lines without paying for
    json.loads, which is what made a cold pass over the log expensive. False
    means "parse it as before" - both for lines that match the shape and fall
    inside the window, and for anything the check cannot read exactly:

    * another key order or a hand-edited line: no leading `{"at": `, or no
      comma to end the value;
    * an integer token: json.loads would hand back an int, and int/float
      comparison stays exact where float(token) rounds once the value passes
      2**53 (a float token round-trips through float() exactly, which is why
      those are the only ones accepted);
    * a later `"at"` key: json.loads keeps the last duplicate, so the first
      value read here would be the wrong one. Escaped quotes inside string
      values (\\") cannot produce a false `"at"`, so this check only ever
      costs a parse - and it runs only for lines that would be skipped, so a
      line the scan is going to parse anyway never pays for it.
    """
    if not line.startswith(_AT_PREFIX):
        return False
    end = line.find(",", len(_AT_PREFIX))
    if end < 0:
        return False
    token = line[len(_AT_PREFIX):end]
    if "." not in token and "e" not in token and "E" not in token:
        return False
    try:
        at = float(token)
    except ValueError:
        return False
    if not ((lo is not None and at < lo) or (hi is not None and at > hi)):
        return False
    return line.find('"at"', end) < 0


def _usage_timeseries_scan(realm, lo, hi, step):
    """One uncached pass over the log, folding rows into fixed-width buckets.

    这是窗口序列的兜底路径：日/小时桶切片不成立（见 _series_slice）时用它，
    口径一字未改。
    """
    buckets = {}
    credits = []
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Cheap window test first: a day or week window covers a small
                # slice of a multi-day log, and the json.loads this skips is
                # what made a cold scan expensive. A line the check cannot read
                # exactly falls through to the full parse and the full test
                # below, so a skipped row is always one the parse would have
                # skipped as well - same output, fewer parses.
                if _line_outside_window(line, lo, hi):
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if realm and not row_matches_realm(row, realm):
                    continue
                at = row.get("at") or 0
                if at < lo or at > hi:
                    continue
                key = int((at - lo) // step)
                bucket = buckets.setdefault(key, {
                    "at": lo + key * step, "requests": 0, "errors": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "reasoning_tokens": 0, "cached_tokens": 0,
                    "total_tokens": 0, "credit": 0.0,
                })
                outcome = row_outcome(row)
                if outcome == "completed":
                    bucket["requests"] += 1
                    for field in ("prompt_tokens", "completion_tokens",
                                  "reasoning_tokens", "cached_tokens",
                                  "total_tokens"):
                        bucket[field] += (row.get(field) or 0)
                else:
                    bucket["errors"] += 1
                credit = row.get("credit") or 0
                if outcome != "client_aborted":
                    bucket["credit"] += credit
                if credit > 0:
                    credits.append({
                        "at": at, "iso": row.get("iso") or "",
                        "model": row.get("model") or "",
                        "account": row.get("account") or "",
                        "credit": credit,
                        "total_tokens": row.get("total_tokens") or 0,
                    })
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage timeseries read failed: %s" % exc)
    credits.sort(key=lambda item: item.get("at") or 0, reverse=True)
    return {
        "ok": True, "realm": realm or "all", "bucket_seconds": step,
        "since": lo, "until": hi,
        "series": [buckets[key] for key in sorted(buckets)],
        "credits": credits[:50],
    }


def _usage_timeseries_uncached(realm, lo, hi, step):
    """窗口序列：优先由日/小时桶切片，不成立时整段扫描。

    切片只在「窗口起点是本地午夜 + 桶宽 3600/86400」时才是同一件事，所以
    先按这两条判断要不要惊动状态：自定义区间与分钟桶根本不折状态，也就
    不会为它们白扫一遍。
    """
    day_key = _window_day_key(lo) if step in (3600, 86400) else None
    if day_key is not None and _usage_day_buckets_enabled():
        with _series_state_lock:
            state, offset, error = _usage_series_state_ready(realm)
            out = None
            if error is not None:
                # 折叠失败：状态归零（下一次从零折起），这次退回整段扫描。
                log("usage timeseries fold failed: %s" % error)
                state.update({"days": {}, "days_floor": None, "max_at": 0,
                              "credits": [], "offset": 0, "tail": b""})
            else:
                out = _series_slice(state, realm, lo, hi, step, day_key)
        if error is None:
            _usage_cache_maybe_save("series", offset)
        if out is not None:
            return out
    return _usage_timeseries_scan(realm, lo, hi, step)


def count_usage_rows(realm=None):
    """Cached row count - substring match instead of a full JSON parse.

    Rows written before the `realm` field existed (they are all error rows)
    have to fall back to the account/model heuristic in row_matches_realm,
    so those few are still parsed properly.

    This runs on every /usage/recent poll purely to render the page total,
    and a full scan of the file dominated that endpoint (measured at ~50% of
    its cost on a 45MB log). A short TTL keeps the number honest while
    removing the scan from the poll path.
    """
    # Normalise first: the needle below is built from this value, so a
    # literal "all" would search for a realm field that never exists.
    realm = realm_scope(realm)
    r = realm or ""
    now = time.time()
    with _count_lock:
        hit = _count_cache.get(r)
        if hit is not None and (now - hit[0]) < _COUNT_TTL:
            return hit[1]
    n = _count_usage_rows_uncached(realm)
    with _count_lock:
        _count_cache[r] = (time.time(), n)
    return n


def _count_usage_rows_uncached(realm=None):
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue          # realm 字段存在但值不同
                try:
                    if row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def recent_usage(limit=100, realm=None, page=1):
    """Paginated rows from the tail of the log (page 1 is latest).

    Reading backward in chunks keeps this in the millisecond range while
    accurately fetching any requested page without missing rows across realms.
    """
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    realm = realm_scope(realm)
    total = count_usage_rows(realm)
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    page = min(page, total_pages)
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if realm and not row_matches_realm(item, realm):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if not realm or row_matches_realm(item, realm):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_rows = matching[start_idx:end_idx]
    # Equivalent-token cost per row at OpenRouter list prices, computed here
    # so every consumer of /usage/recent gets the same number. Each row is
    # priced against the version that was in force when it happened, and says
    # where that price came from. cost_cny stays None for models the version
    # cannot price; cost_band is the index of the conditional band the row
    # landed in (None when the model has one flat price, or none matched).
    # cost_rates / cost_unit / cost_currency / cost_usd_cny / cost_or_id /
    # cost_via / cost_inherited_from / cost_override_from / cost_band_note /
    # cost_via_derived are the same figure taken apart for the panel's hover
    # card: which band's three unit prices, at which rate, matched how. They
    # are all None when the row is unpriced - the panel must say so rather
    # than show a made-up 0.
    for r in page_rows:
        cost = wb_pricing.cost_for_row(r)
        r["cost_cny"] = round(cost["cny"], 6) if cost["known"] else None
        r["cost_band"] = cost["band"] if cost["known"] else None
        # cost_policy stays as the row recorded it; cost_source is what the
        # lookup actually resolved to (the same id, or "builtin").
        r["cost_source"] = cost["source"] if cost["known"] else None
        r["cost_source_at"] = cost["source_at"] if cost["known"] else None
        r["cost_backfilled"] = bool(cost["backfilled"]) if cost["known"] else False
        r["cost_rates"] = cost["rates"] if cost["known"] else None
        r["cost_unit"] = cost["unit"] if cost["known"] else None
        r["cost_currency"] = cost["currency"] if cost["known"] else None
        r["cost_usd_cny"] = cost["usd_cny"] if cost["known"] else None
        r["cost_or_id"] = cost["or_id"] if cost["known"] else None
        r["cost_via"] = cost["via"] if cost["known"] else None
        r["cost_inherited_from"] = (cost["inherited_from"] if cost["known"]
                                    else None)
        r["cost_override_from"] = (cost["override_from"] if cost["known"]
                                   else None)
        r["cost_band_note"] = cost["band_note"] if cost["known"] else None
        r["cost_via_derived"] = (bool(cost["via_derived"]) if cost["known"]
                                 else None)
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "usd_cny": wb_pricing.usd_cny(),
        "rows": page_rows
    }
POOL = None
SCHEDULER = None
PRICING = None
CREDITS_REFRESHER = None
# Release discovery only - the checker never downloads or installs anything.
UPDATES = None
ACCOUNTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accounts')
def realm_state_file():
    """Path of the persisted realm switch.

    Computed on every access rather than cached in a module constant: the
    constant was built from the default ACCOUNTS_DIR at import time, so a
    later --accounts-dir (or a Docker volume pointing somewhere else) still
    read and wrote the realm switch next to the script - the panel then
    reported an exit that did not match the configured account store.
    """
    return os.path.join(ACCOUNTS_DIR, "active_realm.json")


def load_persisted_realm():
    global CURRENT_REALM
    path = realm_state_file()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
                r = d.get("realm")
                if r in ("intl", "cn"):
                    CURRENT_REALM = r
                    return CURRENT_REALM
        except Exception as e:
            log("could not load active realm: %s" % e)
    return CURRENT_REALM
def save_persisted_realm(realm):
    global CURRENT_REALM
    if realm in ("intl", "cn"):
        CURRENT_REALM = realm
        try:
            os.makedirs(ACCOUNTS_DIR, exist_ok=True)
            with open(realm_state_file(), "w", encoding="utf-8") as fh:
                json.dump({"realm": realm, "updated_at": time.time(), "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
            log("persisted active realm '%s' to disk" % realm)
        except Exception as exc:
            log("failed to persist active realm: %s" % exc)
    return CURRENT_REALM
API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
def import_desktop_accounts(realm=None):
    imported = []
    for p, r in wb_accounts.desktop_credential_candidates():
        if realm and r != realm:
            continue
        try:
            account = POOL.import_desktop_credential(path=p, realm=r)
            imported.append(account)
            log("imported %s (%s) from %s" % (account.uid[:8], account.realm, os.path.basename(p)))
        except Exception as exc:
            log("skip %s: %s" % (os.path.basename(p), exc))
    return imported
def desktop_credential_scan():
    """Read-only scan of the desktop client credentials on this machine."""
    return wb_accounts.scan_desktop_credentials()
def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not POOL:
        return []
    return POOL.list_public(realm=realm)

# --------------------------------------------------------------- 任务状态快照
# GET /tasks 每次都要向上游要两份数据（成长任务 + 汇总），实测约 2 秒；而看板切区域、
# 切账号、切页面都会各打一次，用户在界面上就是"点了要等几秒才变"。这里按账号放一份
# 短 TTL 快照：只有这条只读路径吃缓存，任何会改变任务状态的动作（执行/旅行/签到）都
# 先清掉它，所以"刚点完执行却看到旧状态"不会发生。TTL 可用 WB_TASKS_CACHE_TTL 调，
# 设 0 即关闭缓存、回到每次直连上游。
TASKS_CACHE_TTL = float(os.environ.get("WB_TASKS_CACHE_TTL") or "20")
_tasks_cache = {}
_tasks_cache_lock = threading.Lock()

def invalidate_tasks_cache():
    """任务状态变了就清掉快照，下一次读重新向上游取。"""
    with _tasks_cache_lock:
        _tasks_cache.clear()

def growth_snapshot(account):
    """成长任务 + 汇总；TTL 内直接复用上一份快照，过期或没缓存才请求上游。

    两条查询互不依赖（汇总那边自己还要问三个接口），所以并发发出：冷启动时这块
    从"两条串起来等"变成一个来回，实测约 2 秒降到 1.5 秒以内。
    """
    if TASKS_CACHE_TTL <= 0:
        from wb_tasks import fetch_growth_tasks, fetch_growth_summary
        return _both(fetch_growth_tasks, fetch_growth_summary, account)
    now = time.time()
    with _tasks_cache_lock:
        hit = _tasks_cache.get(account.uid)
        if hit and hit[0] > now:
            return hit[1]
    from wb_tasks import fetch_growth_tasks, fetch_growth_summary
    data = _both(fetch_growth_tasks, fetch_growth_summary, account)
    with _tasks_cache_lock:
        _tasks_cache[account.uid] = (time.time() + TASKS_CACHE_TTL, data)
    return data

def _both(tasks_fn, summary_fn, account):
    """并发跑两条上游查询；任一条抛错就照旧往外抛（不写进快照）。"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks_future = pool.submit(tasks_fn, account)
        summary_future = pool.submit(summary_fn, account)
        return tasks_future.result(), summary_future.result()



PROXY_DISCOVER_HOST = os.environ.get("WB_PROXY_DISCOVER_HOST") or "cli-proxy-mihomo"


def _proxy_port_range():
    raw = os.environ.get("WB_PROXY_DISCOVER_PORTS") or "17901-17910"
    if "-" in raw:
        lo, _, hi = raw.partition("-")
        if lo.strip().isdigit() and hi.strip().isdigit():
            return range(int(lo), int(hi) + 1)
    if raw.strip().isdigit():
        return [int(raw)]
    return range(17901, 17911)


def probe_proxy_exit(proxy_url, timeout=12):
    """Return (exit_ip, error) for one proxy URL."""
    try:
        opener = wb_accounts.opener_for_proxy(proxy_url)
        if opener is None:
            return "", "empty proxy url"
        req = urllib.request.Request("https://api.ipify.org", method="GET")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace").strip(), ""
    except Exception as exc:
        return "", str(exc)[:160]


def probe_proxy_intel(proxy_url, timeout=12):
    """Probe one proxy: its exit IP, and where and what that exit is.

    Both steps live here so the discovery list, the per-slot test and the stored
    slot all describe an exit the same way. `latency_ms` covers the proxy probe
    only - the lookup goes out directly and would otherwise inflate it.
    """
    started = time.time()
    exit_ip, error = probe_proxy_exit(proxy_url, timeout=timeout)
    latency_ms = int((time.time() - started) * 1000)
    intel = wb_ipintel.lookup(exit_ip) if exit_ip else wb_ipintel.empty()
    # Two steps, two outcomes. `ok` covers the proxy probe only: the lookup is a
    # separate third-party call that fails on its own (blocked, timeout, 429, a
    # reply without a status). Callers that already know an exit need to tell
    # "the lookup did not answer" from "the lookup answered with nothing", or a
    # blip at the geo service would erase what the last good probe learned.
    intel_ok = any(str(intel.get(field) or "").strip() for field in wb_ipintel.FIELDS)
    return {
        "ok": not error,
        "exit_ip": exit_ip,
        "latency_ms": latency_ms,
        "error": error,
        "intel_ok": intel_ok,
        "country": intel["country"],
        "country_code": intel["country_code"],
        "ip_type": intel["ip_type"],
        "isp": intel["isp"],
        "asn": intel["asn"],
    }


def discover_proxy_slots():
    """Probe the configured mihomo host/ports and report reachable exits."""
    out = []
    for port in _proxy_port_range():
        url = "http://%s:%d" % (PROXY_DISCOVER_HOST, port)
        probe = probe_proxy_intel(url)
        probe["url"] = url
        probe["reachable"] = probe["ok"]
        out.append(probe)
    return out


def slot_label(entry):
    """What to call a slot: the operator's name, else its exit, else its id."""
    name = str((entry or {}).get("name") or "").strip()
    if name:
        return name
    auto = wb_ipintel.slot_name(entry.get("country"), entry.get("ip_type"))
    return auto or str((entry or {}).get("id") or "")


def proxy_slots_view():
    """Proxy slots plus how many enabled accounts are bound to each."""
    counts = {}
    if POOL:
        for account in POOL.accounts:
            slot_id = account.proxy_slot
            if slot_id and account.enabled:
                counts[slot_id] = counts.get(slot_id, 0) + 1
    out = []
    for entry in wb_settings.proxy_slots(ACCOUNTS_DIR):
        item = dict(entry)
        item["bound"] = counts.get(entry["id"], 0)
        item["label"] = slot_label(entry)
        out.append(item)
    return out


_byacct_cache = {"at": 0.0, "data": None}
_byacct_lock = threading.Lock()


def usage_by_account(ttl=None):
    """Cached wrapper: full aggregation over the whole log is expensive.

    Rebuilds under the lock, same reasoning as usage_snapshot."""
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    with _byacct_lock:
        if _byacct_cache["data"] is not None and (now - _byacct_cache["at"]) < ttl:
            return _byacct_cache["data"]
        data = _usage_by_account_uncached()
        _byacct_cache["at"] = time.time()
        _byacct_cache["data"] = data
    return data


def usage_by_account_etag():
    """usage_by_account() 当前缓存条目的 ETag；没有条目时返回 None。

    这个视图只有一条缓存（没有窗口/区域参数），所以键是常量。配对调用由
    _json_cached() 负责。
    """
    with _byacct_lock:
        if _byacct_cache["data"] is None:
            return None
        built_at = _byacct_cache["at"]
    return _cache_etag("by-account", "all", built_at)


_byacct_state = {"buckets": None, "offset": 0, "key": None, "tail": b""}
# The wrapper above holds _byacct_lock while it rebuilds and then calls in
# here, and _usage_by_account_uncached() is also called straight from the
# bench and the tests; a lock of its own keeps the refresh from re-entering
# the wrapper's - that one is not reentrant, so sharing it hung the process
# on the first cache miss.
_byacct_state_lock = threading.Lock()


def _fold_usage_by_account(row, buckets):
    """Fold one row into the per-account buckets.

    No window and no realm filter here - this view has always described the
    whole log - so the fold only skips the rows the original skipped.
    """
    if row.get("error"):
        return
    key = row.get("account") or "(unattributed)"
    bucket = buckets.get(key)
    if bucket is None:
        bucket = buckets[key] = {
            "account": key, "requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0, "models": {},
        }
    bucket["requests"] += 1
    bucket["prompt_tokens"] += row.get("prompt_tokens") or 0
    bucket["completion_tokens"] += row.get("completion_tokens") or 0
    bucket["reasoning_tokens"] += row.get("reasoning_tokens") or 0
    bucket["cached_tokens"] += row.get("cached_tokens") or 0
    bucket["total_tokens"] += row.get("total_tokens") or 0
    model = row.get("model") or "?"
    models = bucket["models"]
    models[model] = models.get(model, 0) + 1


def _usage_by_account_buckets():
    """The cached per-account fold, refreshed and copied for the caller.

    The copy matters: _usage_by_account_uncached() replaces each bucket's
    `models` dict with a sorted list, and the next refresh must still find the
    counts dict where it left it.
    """
    with _byacct_state_lock:
        log_key = _usage_log_key()
        if _byacct_state["buckets"] is None:
            # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
            if not _usage_cache_adopt_by_account(_byacct_state, log_key):
                _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        elif not _log_resume_ok(_byacct_state, log_key):
            _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        _byacct_state["key"] = log_key
        offset, error = _scan_usage_from(
            _byacct_state["offset"],
            lambda row: _fold_usage_by_account(row, _byacct_state["buckets"]))
        buckets = _byacct_state["buckets"]
        if error is None:
            _byacct_state["offset"] = offset
            _byacct_state["tail"] = _log_tail_signature(offset)
        else:
            # The row that stopped the fold may have half applied
            # itself, so this view starts over next call - the full
            # scan it replaces does the same on every call.
            log("usage_by_account failed: %s" % error)
            _byacct_state.update({"buckets": {}, "offset": 0, "tail": b""})
        out = {k: dict(v) for k, v in buckets.items()}
    if error is None:
        _usage_cache_maybe_save("by_account", offset)
    return out


def _usage_by_account_uncached():
    """Aggregate the JSONL log per account id.

    Incremental: the fold is carried over and refreshed with the rows appended
    since the last call, so this costs what the new traffic costs instead of
    what the whole log costs.
    """
    buckets = _usage_by_account_buckets()
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out
_analytics_cache = {}
_analytics_lock = threading.Lock()


def _analytics_scope(realm, range, since, until):
    """(since, until, 缓存键) —— compute_usage_analytics() 与它的 ETag 共用。

    注意键里用的是**原始** realm（`realm or "all"`），与这个接口一直以来的键
    一致；折叠时用 realm_scope(realm) 另有其义。两者都不能顺手"统一"，否则就是
    换了一条缓存键。
    """
    lo, hi = range_window(range, since, until)
    cache_key = "%s|%s|%s" % (realm or "all",
                              lo if lo is not None else "", hi if hi is not None else "")
    return lo, hi, cache_key


def compute_usage_analytics(ttl=None, realm=None, range=None, since=None, until=None):
    """Cached analytics payload.

    Unlike perf_stats/usage_snapshot/usage_by_account this used to run
    uncached, re-reading the whole JSONL on every call while the metrics tab
    polls it every 5 seconds. Same shared TTL as its siblings now, and the
    rebuild runs under the lock so parallel pollers do not each scan the log.

    The window joins the cache key for the same reason it does in the other
    readers: the payload's window bucket is what the KPI cards print, and this
    week and this month overlap, so one entry cannot serve both.
    """
    ttl = _STATS_TTL if ttl is None else ttl
    now = time.time()
    lo, hi, cache_key = _analytics_scope(realm, range, since, until)
    with _analytics_lock:
        entry = _analytics_cache.get(cache_key)
        if entry is not None and (now - entry["at"]) < ttl:
            return entry["data"]
        data = _compute_usage_analytics_uncached(realm=realm_scope(realm), since=lo, until=hi)
        _analytics_cache[cache_key] = {"at": time.time(), "data": data}
    return data


def usage_analytics_etag(realm=None, range=None, since=None, until=None):
    """compute_usage_analytics() 当前缓存条目的 ETag；无条目返回 None。

    配对调用由 _json_cached() 负责，理由见那里。
    """
    _lo, _hi, cache_key = _analytics_scope(realm, range, since, until)
    with _analytics_lock:
        entry = _analytics_cache.get(cache_key)
    if entry is None:
        return None
    return _cache_etag("analytics", cache_key, entry["at"])


def _new_analytics_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "cached_tokens": 0, "total_tokens": 0,
            "credit": 0.0,
            "cost_cny": 0.0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }


# Rows the per-key table folds traffic into when no real key id applies.
# `before` and `anon` are told apart by whether the row carries a `key` field
# at all: rows written before this feature existed have none, and their number
# can only ever shrink, while "no key configured" deployments keep adding rows
# with an empty key. The two need different responses, so they never merge.
KEY_BUCKET_BEFORE = "__before_keys__"
KEY_BUCKET_ANON = "__no_key__"
KEY_BUCKET_UNKNOWN = "__unknown_key__"
# The per-key model breakdown is capped: a deployment with 50 keys would
# otherwise ship a few thousand pills to a page that repaints every 5 seconds.
# The accounts table can afford to list every model because it has one row per
# upstream account, not one per caller.
KEY_MODEL_TOP_N = 5


# One half of the analytics payload folds into four maps: the window summary,
# the per-account entries, the per-model entries and the per-key entries. The
# all-time half and the windowed half hold the same maps and differ only in
# which stat on each entry they own, so the row fold below is written once and
# told which half it is feeding.
_ANALYTICS_ALL = ("all_time", "all_models")
_ANALYTICS_WINDOW = ("window", "window_models")


def _new_analytics_maps():
    """The four maps one half of the payload folds into."""
    return {"summary": _new_analytics_stat(), "accts": {}, "models": {}, "keys": {}}


def _analytics_row_values(row):
    """The per-row numbers the folds below need, read once per row.

    feed()/bump_models() used to call row.get() for each of them on every
    invocation - eight stat objects per row, all walking the same handful of
    fields - which was the single biggest cost of a cold analytics pass.
    """
    return (row.get("prompt_tokens") or 0,
            row.get("completion_tokens") or 0,
            row.get("reasoning_tokens") or 0,
            row.get("cached_tokens") or 0,
            row.get("total_tokens") or 0,
            row.get("credit") or 0,
            row.get("ttft_ms"),
            row.get("tokens_per_sec"),
            row.get("elapsed_ms"))


def _feed_analytics(stat_obj, vals, is_err, cost_cny):
    """Fold one row's numbers into one analytics stat object.

    Token totals follow actual consumption, so a request that failed after the
    upstream had already billed for tokens still shows them; only the
    request/error counters depend on the outcome. An unpriced model passes
    cost_cny = 0.0, which adds nothing: the accumulator starts as a float in
    _new_analytics_stat(), so the sum is exactly what the guarded
    `if cost["known"]` used to leave behind.
    """
    if is_err:
        stat_obj["errors"] += 1
    else:
        stat_obj["requests"] += 1
    stat_obj["prompt_tokens"] += vals[0]
    stat_obj["completion_tokens"] += vals[1]
    stat_obj["reasoning_tokens"] += vals[2]
    stat_obj["cached_tokens"] += vals[3]
    stat_obj["total_tokens"] += vals[4]
    stat_obj["credit"] += vals[5]
    stat_obj["cost_cny"] += cost_cny
    if vals[6]:
        stat_obj["ttft_sum"] += vals[6]
        stat_obj["ttft_n"] += 1
    if vals[7]:
        stat_obj["speed_sum"] += vals[7]
        stat_obj["speed_n"] += 1
    if vals[8]:
        stat_obj["elapsed_sum"] += vals[8]
        stat_obj["elapsed_n"] += 1


def _bump_analytics_models(tgt, m_id, vals, is_err, cost_cny):
    """Fold one row into one {model: {...}} bucket.

    Model distribution counts successful requests only: a failed call
    attributed to a model would show up as demand for it when the caller got
    nothing.
    """
    if is_err:
        return
    tm = tgt.get(m_id)
    if tm is None:
        tm = tgt[m_id] = {"requests": 0, "tokens": 0, "reasoning": 0, "cost_cny": 0.0}
    tm["requests"] += 1
    tm["tokens"] += vals[4]
    tm["reasoning"] += vals[2]
    tm["cost_cny"] += cost_cny


def _fold_usage_analytics(row, realm, maps, half, pricing_on=None, also=None):
    """Fold one parsed row into one half of the analytics payload.

    `maps` holds the four maps that half folds into and `half` names the stat
    each entry of that half owns, so the incremental all-time fold (every
    entry's all_time stat) and the windowed pass (the same entries' window
    stat) share one definition of what a row contributes.

    The key axis is separate from the account axis on purpose: one key can be
    served by many upstream accounts, and one account can serve many keys, so
    the two tables are views of the same spend, not a decomposition of it.

    `also` 是同一行还要折进的第二份 (maps, half)（按日分桶的窗口半）。过滤、
    算价与字段提取只做一次，两份各自累加：折叠本来就要求两侧各记一笔，而
    cost_for_row 与 realm 判定是每行最贵的那部分，只该付一次。
    """
    if realm and not row_matches_realm(row, realm):
        return
    # Only a genuine gateway/upstream failure is an error. A client
    # cancellation is not: its token counts are incomplete, and folding them
    # into the ratios this page reports would understate cache hit and speed.
    # It is counted in perf_stats instead.
    outcome = row_outcome(row)
    if outcome == "client_aborted":
        return
    is_err = outcome != "completed"
    cost = wb_pricing.cost_for_row(row, details=False, enabled=pricing_on)
    cost_cny = cost["cny"] if cost["known"] else 0.0
    vals = _analytics_row_values(row)
    acct_uid = row.get("account") or "(unattributed)"
    m_id = row.get("model") or "(unknown)"
    # A row written before this feature existed has no `key` field at all; a
    # row from a deployment that never configured a key has one, and it is
    # empty.
    if "key" in row:
        k_id = row.get("key") or KEY_BUCKET_ANON
    else:
        k_id = KEY_BUCKET_BEFORE
    k_realm = row_realm(row) or ""
    at = row.get("at") or 0
    targets = ((maps, half),) if also is None else ((maps, half), also)
    for tgt, (stat_key, models_key) in targets:
        _feed_analytics(tgt["summary"], vals, is_err, cost_cny)

        entry = tgt["accts"].get(acct_uid)
        if entry is None:
            entry = tgt["accts"][acct_uid] = {
                "uid": acct_uid,
                "nickname": acct_uid,
                "realm": row.get("realm", ""),
                "domain": "",
                stat_key: _new_analytics_stat(),
                models_key: {},
            }
        _feed_analytics(entry[stat_key], vals, is_err, cost_cny)
        _bump_analytics_models(entry[models_key], m_id, vals, is_err, cost_cny)

        model = tgt["models"].get(m_id)
        if model is None:
            model = tgt["models"][m_id] = {"model": m_id,
                                           stat_key: _new_analytics_stat()}
        _feed_analytics(model[stat_key], vals, is_err, cost_cny)

        km = tgt["keys"].get(k_id)
        if km is None:
            km = tgt["keys"][k_id] = {
                "key": k_id,
                stat_key: _new_analytics_stat(),
                models_key: {},
                # realm -> row count. A key bound to one exit only ever sees
                # that exit; a key with no binding follows the model, and its
                # credit column then adds up two different products. Kept as a
                # dict because this ends up in JSON.
                "realms": {},
                "last_at": 0,
            }
        km["realms"][k_realm] = km["realms"].get(k_realm, 0) + 1
        if at and at > km["last_at"]:
            km["last_at"] = at
        _feed_analytics(km[stat_key], vals, is_err, cost_cny)
        _bump_analytics_models(km[models_key], m_id, vals, is_err, cost_cny)


_analytics_state = {}
_analytics_state_lock = threading.Lock()


def _analytics_all_time(realm, pricing_on=None):
    """The cached all-time fold for one realm filter; returns (maps, offset).

    One state per realm: the filter decides which rows the fold ever sees, so
    two realms are two folds and neither may inherit the other's rows. Only
    the all-time half is cached - the windowed half moves with the clock - and
    the offset comes back with it so a windowed pass can stop where the fold
    stopped.
    """
    with _analytics_state_lock:
        state, offset, error = _analytics_state_ready(realm, pricing_on)
        if error is None:
            # Copied under the lock, same rule as the snapshot state: what
            # the caller decorates must not be the live fold.
            maps = _copy_analytics_all_time(state["maps"])
        else:
            # Same rule as the snapshot state: the row that stopped the
            # fold may have half applied itself, so the state starts
            # over next call. The offset still goes back - it is the
            # end of that row - because the windowed half has to see
            # it, and stop there, to describe the same bytes the
            # all-time half already folded.
            log("compute_usage_analytics failed: %s" % error)
            maps = state["maps"]
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
    if error is None:
        _usage_cache_maybe_save("analytics", offset)
    return maps, offset


def _analytics_state_ready(realm, pricing_on=None):
    """把 `realm` 的 all-time 折叠推进到文件尾；返回 (state, offset, error)。

    调用方必须已持有 _analytics_state_lock。日桶与 all-time 折叠共用这一次
    扫描（见 _fold_usage_analytics 的 also），窗口半与 all-time 半因此描述
    同一段字节。
    """
    state = _analytics_state.get(realm)
    if state is None:
        state = _analytics_state[realm] = {"maps": _new_analytics_maps(),
                                           "offset": 0, "key": None,
                                           "pricing": None, "realm": None,
                                           "tail": b"", "days": {},
                                           "days_floor": None}
    pricing = _pricing_inputs(pricing_on)
    realm_inputs = _realm_inputs()
    log_key = _usage_log_key()
    if state["key"] is None:
        # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_analytics(realm, state, log_key, pricing, realm_inputs):
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
    elif (not _pricing_unchanged(state["pricing"], pricing)
            or state["realm"] != realm_inputs
            or not _log_resume_ok(state, log_key)):
        state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                      "days": {}, "days_floor": None})
    state.update({"pricing": pricing, "realm": realm_inputs, "key": log_key})

    def fold(row):
        # 一行折两处：all-time 半 + 它所属那天的窗口半（过滤与算价只做一次）。
        day = _usage_day_bucket(state, row, realm, _day_analytics_bucket)
        _fold_usage_analytics(
            row, realm, state["maps"], _ANALYTICS_ALL, pricing_on,
            also=None if day is None else (day, _ANALYTICS_WINDOW))

    offset, error = _scan_usage_from(state["offset"], fold)
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
    return state, offset, error


def _analytics_window_from_days(realm, day_key, pricing_on):
    """窗口半 maps = 窗口内的日桶之和；日桶不可用时返回 None。

    先确保 all-time 折叠推进到文件尾：日桶与它同一次扫描推进，窗口半与
    all-time 半因此描述同一段字节（旧实现用 stop_at 保证的同一件事）。
    """
    with _analytics_state_lock:
        state, offset, error = _analytics_state_ready(realm, pricing_on)
        if error is not None:
            log("compute_usage_analytics failed: %s" % error)
            state.update({"maps": _new_analytics_maps(), "offset": 0, "tail": b"",
                          "days": {}, "days_floor": None})
            return None
        floor = state.get("days_floor")
        if not _days_cover(state, day_key):
            return None
        maps = _new_analytics_maps()
        days = state["days"]
        for key in sorted(days):
            if key >= day_key:
                _add_analytics_window_maps(maps, days[key])
    _usage_cache_maybe_save("analytics", offset)
    return maps


def _analytics_window(realm, since, until, stop_at, pricing_on):
    """窗口半 analytics：对齐窗口由日桶相加，其余保持整段扫描。

    与 _usage_snapshot_window 同一条规矩：只有「until 开放 + 起点是本地
    午夜」的窗口拆得成整天的并集。
    """
    day_key = _window_day_key(since) if until is None else None
    if day_key is not None and _usage_day_buckets_enabled():
        maps = _analytics_window_from_days(realm, day_key, pricing_on)
        if maps is not None:
            return maps
        # 日桶没盖住窗口起点：冷路径现折一份按日分组的窗口半，口径一致。
        return _scan_usage_analytics_window_days(realm, since, stop_at,
                                                 pricing_on)
    return _scan_usage_analytics_window(realm, since, until, stop_at=stop_at,
                                        pricing_on=pricing_on)


def _scan_usage_analytics_window(realm, since, until, stop_at=None, pricing_on=None):
    """One pass over the log, folding only the rows inside the window.

    The all-time half is not rebuilt here - it comes from the incremental fold
    - and the read stops where that fold stopped, so both halves of the
    payload describe the same bytes even when a row lands mid-rebuild.
    """
    maps = _new_analytics_maps()

    def fold(row):
        # Same bounds as /usage and /usage/perf, so the three readers agree on
        # what the selected range contains.
        at = row.get("at", 0)
        if since is not None and at < since:
            return
        if until is not None and at > until:
            return
        _fold_usage_analytics(row, realm, maps, _ANALYTICS_WINDOW, pricing_on)

    _, error = _scan_usage_from(
        0, fold, stop_at=stop_at,
        skip=lambda line: _line_outside_window(line, since, until))
    if error is not None:
        log("compute_usage_analytics failed: %s" % error)
    return maps


def _copy_analytics_models(models):
    """Copy one {model: {...}} bucket; its inner dicts are only ever read."""
    return {mid: dict(entry) for mid, entry in models.items()}


def _copy_analytics_all_time(maps):
    """A private copy of the cached all-time maps.

    The payload build decorates what it is given (finalize writes the
    derived ratios into the stat dicts) and the cached fold keeps growing
    under a concurrent refresh, so the caller gets a copy taken while the
    state lock is held rather than a live view of it.
    """
    out = {"summary": dict(maps["summary"]), "accts": {}, "models": {}, "keys": {}}
    for uid, entry in maps["accts"].items():
        copy = dict(entry)
        copy["all_time"] = dict(entry["all_time"])
        copy["all_models"] = _copy_analytics_models(entry["all_models"])
        out["accts"][uid] = copy
    for m_id, entry in maps["models"].items():
        out["models"][m_id] = {"model": m_id, "all_time": dict(entry["all_time"])}
    for k_id, km in maps["keys"].items():
        copy = dict(km)
        copy["all_time"] = dict(km["all_time"])
        copy["all_models"] = _copy_analytics_models(km["all_models"])
        out["keys"][k_id] = copy
    return out


def _merge_analytics_halves(all_maps, win_maps):
    """Combine the cached all-time fold with the windowed one.

    `win_maps` is `all_maps` itself when no range was selected - every row is
    inside an open window, so the windowed half is the all-time fold over
    again. The halves are copied apart in that case, which is what a second
    pass would have produced without the second pass, and it keeps a caller
    that mutates one half out of the other (and out of the cache).

    An entry only the windowed half knows cannot exist: the windowed pass
    stops where the all-time fold stopped, so every row it saw is already in
    there.
    """
    shared = win_maps is all_maps
    acct_map = {}
    for uid, entry in all_maps["accts"].items():
        win = win_maps["accts"].get(uid)
        if shared:
            window_stat = dict(entry["all_time"])
            window_models = _copy_analytics_models(entry["all_models"])
        elif win is None:
            window_stat = _new_analytics_stat()
            window_models = {}
        else:
            window_stat = win["window"]
            window_models = win["window_models"]
        acct_map[uid] = {
            "uid": uid,
            "nickname": uid,
            "realm": entry["realm"],
            "domain": "",
            "window": window_stat,
            "all_time": dict(entry["all_time"]),
            "window_models": window_models,
            "all_models": _copy_analytics_models(entry["all_models"]),
        }
    model_map = {}
    for m_id, entry in all_maps["models"].items():
        stat = entry["all_time"]
        win = win_maps["models"].get(m_id)
        if shared:
            window_stat = dict(stat)
        elif win is None:
            window_stat = _new_analytics_stat()
        else:
            window_stat = win["window"]
        model_map[m_id] = {"model": m_id, "window": window_stat,
                           "all_time": dict(stat)}
    key_map = {}
    for k_id, km in all_maps["keys"].items():
        win = win_maps["keys"].get(k_id)
        if shared:
            window_stat = dict(km["all_time"])
            window_models = _copy_analytics_models(km["all_models"])
        elif win is None:
            window_stat = _new_analytics_stat()
            window_models = {}
        else:
            window_stat = win["window"]
            window_models = win["window_models"]
        key_map[k_id] = {
            "key": k_id,
            "window": window_stat,
            "all_time": dict(km["all_time"]),
            "window_models": window_models,
            "all_models": _copy_analytics_models(km["all_models"]),
            "realms": dict(km["realms"]),
            "last_at": km["last_at"],
        }
    return {"summary": dict(all_maps["summary"]),
            "window_summary": (dict(all_maps["summary"]) if shared
                               else win_maps["summary"]),
            "accts": acct_map, "models": model_map, "keys": key_map}


def _add_analytics_stat(dst, src):
    """Field-wise sum of two _new_analytics_stat() dicts, in place."""
    for field, value in src.items():
        if isinstance(value, (int, float)):
            dst[field] += value


# ---------------------------------------------------------------------------
# 按日分桶：窗口半的增量
#
# all-time 半已经是增量（按字节偏移折叠 + 落盘 checkpoint），但窗口半
# （range=today / week / month）仍然是「从头扫到窗口」：窗口越宽扫得越多，
# 路由器上一次 range=week 要 5 秒。窗口的边界全是本地午夜（range_window()），
# 而每行只属于一天，所以只要在折叠时顺手把每一行折进「它所属那天的桶」，
# 任何窗口就退化成「把窗口内的日桶相加」——O(天数)，checkpoint 带着日桶
# 时冷缓存也是瞬间。
#
# 时间边界只认一个规则：行的本地日由行自己的 at 决定（_local_day_key），
# 与 range_window() 的 today/week/month 用的是同一套本地午夜。折叠时刻不
# 参与切天——迟写/补写的行必须落进它时间戳所属的那天，按折叠时刻切会让
# 窗口相加错位。跨夏令时同理：日桶的键是本地日历日，只要窗口起点是本地
# 午夜，「日键 >= 窗口日键」就是「at >= since」的等价写法（见 _window_day_key）。
#
# 已封口的日桶不会再变（新行只落进今天那一桶），窗口相加时旧桶原样复用；
# 状态只在新建桶时剪枝（_prune_day_buckets），剪掉的只是任何窗口都够不到
# 的天。剪枝会抬高 days_floor，任何够不到的天都因此退回整段扫描——宁可多
# 扫一次，不能少算一天。
#
# 浮点求和的口径：窗口 = 日桶逐日相加，与旧实现「整段扫描逐行累加」的区别
# 只是浮点的结合顺序（credit / cost_cny / speed_sum 可能差最后一两个 ulp，
# 整数字段逐位相同）。逐行顺序无法从日聚合还原，这是日桶推导的固有代价；
# 本文件里所有对齐窗口的路径统一用「按日分组」的口径（冷扫描、日桶、加载
# checkpoint 三条路径给出的字节完全一致），把「像没有缓存一样正确」保成
# 路径之间的一致性。WB_USAGE_DAY_BUCKETS=0 可整体退回旧的逐行口径。
# ---------------------------------------------------------------------------

# 日桶保留天数：月窗口最多回溯 30 天（当月 1 号），32 天留两天余量。更早的
# 天没有任何窗口够得到，留着只让 checkpoint 白白变大——路由器上每次写盘都
# 要重写整份文件。调小它（WB_USAGE_DAY_KEEP_DAYS）会让更宽的窗口退回整段
# 扫描（结果仍然正确，只是慢）：对 flash 写入敏感的部署可以调到 8，只保周。
_USAGE_DAY_KEEP_DAYS = 32

# 日桶求和要跳过的字段：started 是每份快照各自的创建时刻，不是累计量。
# 与 _USAGE_CACHE_SNAPSHOT_NUMBERS 同源（同一份构造函数的数字字段），只是
# 在模块加载顺序上不能引用它（那段常量在文件更靠后），所以这里自己推导。
_USAGE_DAY_SNAPSHOT_FIELDS = tuple(
    k for k, v in _empty_stats().items()
    if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "started")


def _usage_day_keep_days():
    """日桶保留天数，默认 32；WB_USAGE_DAY_KEEP_DAYS 可调。

    每次调用都读环境变量，理由与 WB_USAGE_CACHE 相同（现场调、测试里来回切）。
    """
    try:
        return max(1, int(os.environ.get("WB_USAGE_DAY_KEEP_DAYS",
                                         _USAGE_DAY_KEEP_DAYS)))
    except (TypeError, ValueError):
        return _USAGE_DAY_KEEP_DAYS


def _usage_day_buckets_enabled():
    """日桶总开关，默认开；WB_USAGE_DAY_BUCKETS=0 退回逐行扫描口径。

    与 WB_USAGE_CACHE 同样的用法：现场紧急关掉、测试在一个进程里来回切。
    关掉时既不折日桶也不用它们，窗口路径与改动前逐字节一致。
    """
    return os.environ.get("WB_USAGE_DAY_BUCKETS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _local_day_key(ts):
    """ts 所属的本地日历日，"YYYY-MM-DD"。

    用字符串而不是午夜 epoch 当日桶的键：窗口与日桶的比较就是一次字符串
    比较（ISO 日期天然有序），查询不必再算一遍本地午夜，也不会被夏令时的
    日长变化影响——同一套本地日历日规则在折叠与查询两侧都成立。
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_key_start(key):
    """日键 -> 该日的本地午夜 epoch。"""
    lt = time.strptime(key, "%Y-%m-%d")
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _window_day_key(since):
    """窗口起点能由日桶推导时返回它的日键，否则 None。

    只有「起点正好是本地午夜」的窗口才拆得成整天的并集：at >= 本地午夜
    等价于「日键 >= 那天的键」。自定义区间的起点是任意时刻，拆不开——那条
    路径保持整段扫描（正确性优先）。
    """
    if since is None:
        return None
    if _local_midnight(since) != since:
        return None
    return _local_day_key(since)


def _day_snapshot_bucket(key, day_start):
    """一天一份的窗口半快照桶；形状与 _empty_stats() 一致。

    started 不参与求和（见 _add_usage_snapshot），形状保留只是让校验与
    all-time 半共用同一套 _usage_cache_snapshot_ok。
    """
    return _empty_stats()


def _day_analytics_bucket(key, day_start):
    """一天一份的窗口形状 analytics maps。"""
    return _new_analytics_maps()


def _day_series_bucket(key, day_start):
    """一天一份的时间序列桶（带小时子表，见 _fold_usage_series）。"""
    bucket = _new_series_bucket(day_start)
    bucket["hours"] = {}
    return bucket


def _usage_day_bucket(state, row, r, factory):
    """这一行该折进哪个日桶；必要时新建（顺带剪枝），不该折时返回 None。

    行的归属只看它自己的 at：补写/迟到的行必须落进它时间戳所属的那天。
    realm 不匹配的行折叠本来就会跳过，这里也不为它建桶；总开关关掉时连
    日桶都不折（窗口路径也不会用它们，见 _usage_day_buckets_enabled）。
    """
    if not _usage_day_buckets_enabled():
        return None
    if r and not row_matches_realm(row, r):
        return None
    at = row.get("at") or 0
    key = _local_day_key(at)
    floor = state.get("days_floor")
    if floor is not None and key < floor:
        # 剪枝线以下的天：那些桶已经被丢掉，而且任何可服务的窗口都从
        # days_floor 那天起，这一行不在其中——跳过不影响窗口。
        return None
    days = state["days"]
    bucket = days.get(key)
    if bucket is None:
        bucket = days[key] = factory(key, _day_key_start(key))
        _prune_day_buckets(state, days, key)
    return bucket


def _prune_day_buckets(state, days, newest_key):
    """丢掉任何窗口都够不到的旧日桶，并把 days_floor 抬到剪枝线。

    days_floor 的语义是「这份状态从哪天起是完整的」：它就是剪枝线（最新那天
    往前 _usage_day_keep_days() 天），没剪过时第一次折到新的一天也会把它记
    下来——比这条线更早的窗口一律退回整段扫描（时钟大幅回拨、日志里有远古
    补写行这类情况），宁可多扫一次，不能少算一天。
    """
    horizon = _local_day_key(_day_key_start(newest_key)
                             - _usage_day_keep_days() * 86400)
    for old in [k for k in days if k < horizon]:
        del days[old]
    floor = state.get("days_floor")
    if floor is None or floor < horizon:
        state["days_floor"] = horizon


def _days_cover(state, day_key):
    """这份状态的日桶能否服务以 day_key 为起点的窗口。

    days_floor 是剪枝线：比它早的窗口可能少了被剪掉的天，只能退回扫描。
    它最早也要折到第一个新的一天才会记下（没折过就是 None，此时谁都不服务
    ——调用方总是先折到文件尾，所以实际不会遇到）。
    """
    floor = state.get("days_floor")
    return floor is not None and day_key >= floor


def _add_usage_bucket(dst_map, key, src):
    """把一个 by_model* 桶并入同键的桶（不存在就整只拷进来）。"""
    dst = dst_map.get(key)
    if dst is None:
        dst_map[key] = _copy_usage_bucket(src)
        return
    for field in _USAGE_CACHE_BUCKET_NUMBERS:
        dst[field] += src.get(field) or 0
    for acct, count in src["accounts"].items():
        dst["accounts"][acct] = dst["accounts"].get(acct, 0) + count


def _add_usage_snapshot(dst, src):
    """把一份窗口半快照按字段并入另一份（日桶求和用）。

    逐字段相加，不是重折：日桶是不可变的聚合，窗口只是它们的和。started
    是每份快照各自的创建时刻，不是累计量，跳过。
    """
    for field in _USAGE_DAY_SNAPSHOT_FIELDS:
        dst[field] += src.get(field) or 0
    for mid, count in src["cost_missing"].items():
        dst["cost_missing"][mid] = dst["cost_missing"].get(mid, 0) + count
    for mid, bucket in src["by_model"].items():
        _add_usage_bucket(dst["by_model"], mid, bucket)
    for mid, realms in src["by_model_realm"].items():
        for rr, bucket in realms.items():
            _add_usage_bucket(dst["by_model_realm"].setdefault(mid, {}), rr, bucket)
    for mid, realms in src["by_model_acct"].items():
        for rr, accts in realms.items():
            tgt = dst["by_model_acct"].setdefault(mid, {}).setdefault(rr, {})
            for acct, bucket in accts.items():
                _add_usage_bucket(tgt, acct, bucket)


def _add_analytics_models(dst, src):
    """把一份 {model: {requests, tokens, reasoning, cost_cny}} 并入另一份。"""
    for mid, stat in src.items():
        tgt = dst.get(mid)
        if tgt is None:
            dst[mid] = dict(stat)
            continue
        for field in ("requests", "tokens", "reasoning", "cost_cny"):
            tgt[field] += stat.get(field) or 0


def _add_analytics_window_maps(dst, src):
    """把一份窗口形状的 analytics maps 并入另一份（日桶求和用）。

    日桶本身就是 _ANALYTICS_WINDOW 那一半的形状（window / window_models），
    所以这里只动这两个键；realms / last_at 照并，虽然 _merge_analytics_halves
    最终取的是 all-time 侧的值。
    """
    _add_analytics_stat(dst["summary"], src["summary"])
    for uid, entry in src["accts"].items():
        tgt = dst["accts"].get(uid)
        if tgt is None:
            tgt = dst["accts"][uid] = {
                "uid": uid, "nickname": uid, "realm": entry["realm"],
                "domain": "", "window": _new_analytics_stat(),
                "window_models": {}}
        _add_analytics_stat(tgt["window"], entry["window"])
        _add_analytics_models(tgt["window_models"], entry["window_models"])
    for mid, entry in src["models"].items():
        tgt = dst["models"].get(mid)
        if tgt is None:
            tgt = dst["models"][mid] = {"model": mid,
                                        "window": _new_analytics_stat()}
        _add_analytics_stat(tgt["window"], entry["window"])
    for kid, entry in src["keys"].items():
        tgt = dst["keys"].get(kid)
        if tgt is None:
            tgt = dst["keys"][kid] = {
                "key": kid, "window": _new_analytics_stat(),
                "window_models": {}, "realms": {}, "last_at": 0}
        _add_analytics_stat(tgt["window"], entry["window"])
        _add_analytics_models(tgt["window_models"], entry["window_models"])
        for rr, count in entry["realms"].items():
            tgt["realms"][rr] = tgt["realms"].get(rr, 0) + count
        if entry["last_at"] > tgt["last_at"]:
            tgt["last_at"] = entry["last_at"]


def _copy_analytics_window_maps(maps):
    """一份窗口形状 maps 的深拷贝（落盘用，形状同 _new_analytics_maps()）。"""
    out = {"summary": dict(maps["summary"]), "accts": {}, "models": {}, "keys": {}}
    for uid, entry in maps["accts"].items():
        copy = dict(entry)
        copy["window"] = dict(entry["window"])
        copy["window_models"] = _copy_analytics_models(entry["window_models"])
        out["accts"][uid] = copy
    for mid, entry in maps["models"].items():
        out["models"][mid] = {"model": mid, "window": dict(entry["window"])}
    for kid, km in maps["keys"].items():
        copy = dict(km)
        copy["window"] = dict(km["window"])
        copy["window_models"] = _copy_analytics_models(km["window_models"])
        copy["realms"] = dict(km["realms"])
        out["keys"][kid] = copy
    return out


def _scan_usage_snapshot_window_days(r, since, pricing_on=None):
    """窗口半的冷路径：整段扫一遍，先按日分桶再相加。

    日桶状态不可用（还没折过、被剪枝、折叠出错）时走这里。结果必须与日桶
    状态给出的窗口半逐字节一致：窗口起点是本地午夜，两边装的是同一批行、
    同一个「按日分组」的求和口径，只是桶一个现折、一个存量。
    """
    days = {}

    def fold(row):
        at = row.get("at") or 0
        if since and at < since:
            return
        key = _local_day_key(at)
        bucket = days.get(key)
        if bucket is None:
            bucket = days[key] = _empty_stats()
        _fold_usage_snapshot(row, bucket, r, None, None, pricing_on)

    _, error = _scan_usage_from(
        0, fold,
        skip=lambda line: _line_outside_window(line, since or None, None))
    if error is not None:
        log(f"usage snapshot read failed: {error}")
    snap = _empty_stats()
    for key in sorted(days):
        _add_usage_snapshot(snap, days[key])
    return snap


def _scan_usage_analytics_window_days(realm, since, stop_at=None, pricing_on=None):
    """窗口半 analytics 的冷路径：按日分桶再相加（见 snapshot 版）。"""
    days = {}

    def fold(row):
        at = row.get("at") or 0
        if since is not None and at < since:
            return
        key = _local_day_key(at)
        maps = days.get(key)
        if maps is None:
            maps = days[key] = _new_analytics_maps()
        _fold_usage_analytics(row, realm, maps, _ANALYTICS_WINDOW, pricing_on)

    _, error = _scan_usage_from(
        0, fold, stop_at=stop_at,
        skip=lambda line: _line_outside_window(line, since, None))
    if error is not None:
        log("compute_usage_analytics failed: %s" % error)
    out = _new_analytics_maps()
    for key in sorted(days):
        _add_analytics_window_maps(out, days[key])
    return out


# ---------------------------------------------------------------------------
# 时间序列的日/小时桶
#
# usage_timeseries() 本来就把行按时间装桶，但每次缓存未命中都要整段扫描
# （路由器上 range=week 约 2 秒）。窗口边界同样是本地午夜，而它的桶宽只有
# 3600 与 86400 两种能与窗口对齐（span<=6h 的分钟桶只出现在自定义区间，
# 那条路保持整段扫描）。折叠时顺手维护日桶与小时桶，窗口就是它们的切片：
# 每个输出桶恰好等于一个存量桶（不做跨桶相加），所以连浮点求和都是逐位
# 一致的——不像 snapshot/analytics 的窗口半需要把日桶相加。
# ---------------------------------------------------------------------------
_series_state = {}
_series_state_lock = threading.Lock()
# credit 历史只保留最近这么多条（整段扫描返回的也是窗口内的前 50 条）。
_SERIES_CREDITS_KEEP = 50


def _new_series_bucket(at):
    """一个时间序列桶；键序与 _usage_timeseries_uncached 的输出逐字一致。"""
    return {"at": at, "requests": 0, "errors": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "credit": 0.0}


def _feed_series_bucket(bucket, row):
    """一行折进一个序列桶（日桶或小时桶）。

    口径与 _usage_timeseries_uncached 的折叠逐条一致：completed 计 requests
    与令牌，其它结果（含客户端中止）计 errors；credit 只要不是客户端中止就
    累加。
    """
    outcome = row_outcome(row)
    if outcome == "completed":
        bucket["requests"] += 1
        for field in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                      "cached_tokens", "total_tokens"):
            bucket[field] += (row.get(field) or 0)
    else:
        bucket["errors"] += 1
    if outcome != "client_aborted":
        bucket["credit"] += (row.get("credit") or 0)


def _series_credit_push(credits, row, at):
    """把一条带 credit 的行放进「最近 50 条」缓冲。

    整段扫描返回的 credits 是窗口内按 at 倒序的前 50 条；缓冲里始终留着
    最新的一批（至少 50 条，涨到 100 条时裁回 50），于是任何后缀窗口
    （today/week/month 都是）的前 50 条都在这批里——比缓冲最旧一条还旧的
    行不可能是窗口的前 50 条。at 相同时两边都是稳定排序，顺序（日志顺序）
    一致。
    """
    credits.append({"at": at, "iso": row.get("iso") or "",
                    "model": row.get("model") or "",
                    "account": row.get("account") or "",
                    "credit": row.get("credit") or 0,
                    "total_tokens": row.get("total_tokens") or 0})
    if len(credits) > _SERIES_CREDITS_KEEP * 2:
        credits.sort(key=lambda item: item.get("at") or 0, reverse=True)
        del credits[_SERIES_CREDITS_KEEP:]


def _fold_usage_series(row, r, state):
    """一行折进时间序列状态：日桶 + 小时桶 + credit 缓冲。"""
    at = row.get("at") or 0
    if at > state["max_at"]:
        # 折过的最大 at：窗口上界（hi=now）只要不低于它，存量桶就都在窗口内。
        # 越界的行（时钟回拨/补写）会让它偏大，那只是更保守，方向安全。
        state["max_at"] = at
    bucket = _usage_day_bucket(state, row, r, _day_series_bucket)
    if bucket is None:
        return
    _feed_series_bucket(bucket, row)
    if at < bucket["at"]:
        # 本地午夜落在不存在的小时里（极少数时区的夏令时正好在午夜切换）：
        # 这一天的行放不进小时格点，把 hours 摘掉让切片对这一天整体退回扫描
        # （日桶本身照常，step=86400 的切片不受影响）。
        bucket.pop("hours", None)
        return
    # 小时子桶按「离那天的本地午夜多少秒」切：窗口起点是本地午夜时，窗口
    # 序列的桶序号 k = 天序号*24 + 小时序号（夏令时不在日边界上时也一样，
    # 因为两边都是纯 epoch 算术；日边界被夏令时挪动的情形由 _series_slice
    # 的格点校验挡住）。键写成字符串，与 JSON 往返后的形状一致。
    hour = str(int((at - bucket["at"]) // 3600))
    sub = bucket["hours"].get(hour)
    if sub is None:
        sub = bucket["hours"][hour] = _new_series_bucket(
            bucket["at"] + int(hour) * 3600)
    _feed_series_bucket(sub, row)
    if (row.get("credit") or 0) > 0:
        _series_credit_push(state["credits"], row, at)


def _usage_series_state_ready(r):
    """把 `r` 的时间序列状态推进到文件尾；返回 (state, offset, error)。

    调用方必须已持有 _series_state_lock。时间序列折叠不算价，所以状态只带
    realm 指纹（行没有 realm 字段时 row_realm() 会读它）。
    """
    state = _series_state.get(r)
    if state is None:
        state = _series_state[r] = {
            "days": {}, "days_floor": None, "max_at": 0, "credits": [],
            "offset": 0, "key": None, "realm": None, "tail": b""}
    realm = _realm_inputs()
    log_key = _usage_log_key()
    if state["key"] is None:
        # 全新状态（进程刚起来）：先问 checkpoint，问不到才从零折起。
        if not _usage_cache_adopt_series(r, state, log_key, realm):
            state.update({"days": {}, "days_floor": None, "max_at": 0,
                          "credits": [], "offset": 0, "tail": b""})
    elif state["realm"] != realm or not _log_resume_ok(state, log_key):
        state.update({"days": {}, "days_floor": None, "max_at": 0,
                      "credits": [], "offset": 0, "tail": b""})
    state.update({"realm": realm, "key": log_key})
    offset, error = _scan_usage_from(
        state["offset"], lambda row: _fold_usage_series(row, r, state))
    if error is None:
        state["offset"] = offset
        state["tail"] = _log_tail_signature(offset)
    return state, offset, error


def _series_output_bucket(bucket):
    """存量桶 -> 输出桶（键序固定；日桶的 hours 子表不进输出）。"""
    return {"at": bucket["at"], "requests": bucket["requests"],
            "errors": bucket["errors"], "prompt_tokens": bucket["prompt_tokens"],
            "completion_tokens": bucket["completion_tokens"],
            "reasoning_tokens": bucket["reasoning_tokens"],
            "cached_tokens": bucket["cached_tokens"],
            "total_tokens": bucket["total_tokens"], "credit": bucket["credit"]}


def _series_slice(state, realm, lo, hi, step, day_key):
    """窗口序列 = 日桶/小时桶的切片；不满足对齐条件时返回 None。

    成立条件（缺一不可，任何一条不成立就退回整段扫描——正确性优先）：
      * 窗口起点是本地午夜（day_key 由 _window_day_key 给出）；
      * 桶宽是 3600 或 86400（与窗口对齐的两种）；
      * days_floor 覆盖窗口起点（剪枝/更早的天没折过就只能整段扫）；
      * hi 不低于折过的最大 at，否则窗口上界会切掉桶里的行；
      * 每个桶的 at 落在 lo + k*step 的格点上。这一条同时挡住夏令时：日长
        不是 86400 秒的那天之后，每一天的本地午夜都不再落在 86400 的格点上，
        于是自动退回整段扫描。时区没有夏令时（cn/路由器）时恒真。
    """
    if step not in (3600, 86400):
        return None
    if hi < state["max_at"]:
        return None
    if not _days_cover(state, day_key):
        return None
    lo_i = int(lo)
    series = []
    for key in sorted(state["days"]):
        if key < day_key:
            continue
        bucket = state["days"][key]
        day_at = int(bucket["at"])
        if (day_at - lo_i) % 86400:
            return None
        if step == 86400:
            series.append(_series_output_bucket(bucket))
            continue
        hours = bucket.get("hours")
        if hours is None:
            # 这一天的小时格点不成立（见 _fold_usage_series 的午夜兜底）：
            # 整个窗口退回整段扫描，不能只跳过这一天。
            return None
        for hour in sorted(hours, key=int):
            sub = hours[hour]
            if int(sub["at"]) != day_at + int(hour) * 3600:
                return None
            if (int(sub["at"]) - lo_i) % 3600:
                return None
            series.append(_series_output_bucket(sub))
    # credit 历史：缓冲按 at 倒序稳定排序后取窗口内的前 50 条——与整段扫描
    # 的选择逐条相同（见 _series_credit_push 的论证）。
    credits = sorted(state["credits"], key=lambda item: item.get("at") or 0,
                     reverse=True)
    credits = [c for c in credits if (c.get("at") or 0) >= lo][:_SERIES_CREDITS_KEEP]
    return {"ok": True, "realm": realm or "all",
            "bucket_seconds": step, "since": lo, "until": hi,
            "series": series, "credits": credits}


def _top_models(bucket, top_n=KEY_MODEL_TOP_N):
    """Split a model bucket into the N busiest models plus one remainder.

    The remainder is flagged with `other` rather than being recognised by its
    label, so a model genuinely called "(其他)" cannot be mistaken for it.
    """
    items = sorted(bucket.items(), key=lambda kv: (-kv[1]["tokens"], -kv[1]["requests"], kv[0]))
    head = [{"model": mid, "requests": s["requests"], "tokens": s["tokens"],
             "reasoning": s["reasoning"]} for mid, s in items[:top_n]]
    rest = items[top_n:]
    other = None
    if rest:
        other = {
            "model": "(其他)",
            "other": True,
            "models": len(rest),
            "requests": sum(s["requests"] for _, s in rest),
            "tokens": sum(s["tokens"] for _, s in rest),
            "reasoning": sum(s["reasoning"] for _, s in rest),
        }
    return head, other


def _build_key_rows(key_map, realm=None):
    """Merge observed per-key traffic with the configured key roster.

    Every key the panel can still see gets a row even with no traffic in the
    window: a key that was used yesterday and not today is a fact about
    today's spend, and dropping it would read as "the key is gone". A key that
    is disabled *and* idle in this window is dropped, because that row says
    nothing about the selected range.

    The reverse direction matters just as much: an id seen in the log that the
    roster does not know still gets a row, so the per-key table always adds up
    to the per-account table. Those are folded into a single row - ids that
    cannot be named are an anomaly, not a dimension worth splitting.
    """
    key_map = key_map or {}
    rows = {}

    def empty_row(k_id, name, declared_realm, enabled, source):
        return {
            "key": k_id,
            "name": name,
            "realm": declared_realm,
            "enabled": enabled,
            "source": source,
            "window": _new_analytics_stat(),
            "all_time": _new_analytics_stat(),
            "window_models": {},
            "all_models": {},
            "realms": {},
            "last_at": 0,
        }

    for entry in configured_keys():
        k_id = entry.get("id") or ""
        if not k_id:
            continue
        declared = entry.get("realm") or ""
        # A key bound to the other exit can never have rows in this view.
        if realm and declared and declared != realm:
            continue
        rows[k_id] = empty_row(k_id, entry.get("name") or k_id, declared,
                               entry.get("enabled", True) is not False, "panel")
    # The launcher key (--api-key / API_KEY) lives in no settings file, so it
    # is only ever visible as an id in the log. It gets a row when it could
    # have been used at all, which is what keeps the totals reconcilable.
    if "launcher" not in rows and (API_KEY or "launcher" in key_map):
        rows["launcher"] = empty_row("launcher", "启动参数", "", True, "launcher")

    def adopt(row, km):
        row["window"] = km["window"]
        row["all_time"] = km["all_time"]
        row["window_models"] = km["window_models"]
        row["all_models"] = km["all_models"]
        row["realms"] = km["realms"]
        row["last_at"] = km["last_at"]

    unknown = None
    for k_id, km in key_map.items():
        if k_id == KEY_BUCKET_BEFORE:
            rows[k_id] = empty_row(k_id, "(切换前)", "", False, "bucket")
            adopt(rows[k_id], km)
        elif k_id == KEY_BUCKET_ANON:
            rows[k_id] = empty_row(k_id, "(无 key)", "", False, "bucket")
            adopt(rows[k_id], km)
        elif k_id in rows:
            adopt(rows[k_id], km)
        else:
            if unknown is None:
                unknown = rows[KEY_BUCKET_UNKNOWN] = empty_row(
                    KEY_BUCKET_UNKNOWN, "(未知 key)", "", False, "bucket")
            _add_analytics_stat(unknown["window"], km["window"])
            _add_analytics_stat(unknown["all_time"], km["all_time"])
            for tgt, src in ((unknown["window_models"], km["window_models"]),
                             (unknown["all_models"], km["all_models"])):
                for mid, s in src.items():
                    dst = tgt.setdefault(mid, {"requests": 0, "tokens": 0, "reasoning": 0})
                    dst["requests"] += s["requests"]
                    dst["tokens"] += s["tokens"]
                    dst["reasoning"] += s["reasoning"]
            for k_realm, count in km["realms"].items():
                unknown["realms"][k_realm] = unknown["realms"].get(k_realm, 0) + count
            unknown["last_at"] = max(unknown["last_at"], km["last_at"])

    out = []
    for row in rows.values():
        if (row["source"] == "panel" and not row["enabled"]
                and not row["window"]["requests"] and not row["window"]["errors"]):
            continue
        _finalize_analytics_stat(row["window"])
        _finalize_analytics_stat(row["all_time"])
        row["models"], row["models_other"] = _top_models(row["window_models"])
        # A key with no realm binding follows the model it is asked for, so it
        # can serve both exits - and then its credit column adds up two
        # different products' prices. The page has to be able to say so.
        row["cross_realm"] = len([x for x in row["realms"] if x]) > 1
        out.append(row)
    out.sort(key=lambda r: (-r["window"]["total_tokens"], -r["all_time"]["total_tokens"], r["name"]))
    return out


def _enrich_accounts_from_pool(acct_map, realm=None):
    """Attach nickname/realm/credits for accounts that saw no traffic."""
    if POOL:
        for a in POOL.accounts:
            if realm and a.realm != realm:
                continue
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                    acct_map[a.uid] = {
                        "uid": a.uid,
                        "nickname": a.nickname,
                        "realm": a.realm,
                        "domain": a.domain,
                        "credits": getattr(a, "credits", None) or {},
                        "window": _new_analytics_stat(),
                        "all_time": _new_analytics_stat(),
                        "window_models": {},
                        "all_models": {},
                    }


def _finalize_analytics_stat(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj

def _compute_usage_analytics_uncached(realm=None, since=None, until=None):
    """Detailed analytics for Token, Cache, and Reasoning metrics page."""
    # 与 /usage 快照同一条规矩：一次扫描里总开关只问一次，值传进折叠与指纹。
    pricing_on = wb_pricing.pricing_enabled()
    all_maps, offset = _analytics_all_time(realm, pricing_on)
    if since is None and until is None:
        # An open window covers every row, so the windowed half is the
        # all-time fold over again; the merge copies the two apart.
        win_maps = all_maps
    else:
        # The all-time half is not re-folded: the windowed half comes from
        # the day buckets that fold advanced (or, failing that, from a pass
        # that stops where that fold stopped), so one page never mixes two
        # reads.
        win_maps = _analytics_window(realm, since, until, offset, pricing_on)
    parts = _merge_analytics_halves(all_maps, win_maps)
    acct_map = parts["accts"]
    model_map = parts["models"]
    key_map = parts["keys"]
    _enrich_accounts_from_pool(acct_map, realm=realm)
    all_summary = parts["summary"]
    window_summary = parts["window_summary"]
    _finalize_analytics_stat(all_summary)
    _finalize_analytics_stat(window_summary)
    for a in acct_map.values():
        _finalize_analytics_stat(a["window"])
        _finalize_analytics_stat(a["all_time"])
    for m in model_map.values():
        _finalize_analytics_stat(m["window"])
        _finalize_analytics_stat(m["all_time"])
    accts_list = sorted(acct_map.values(), key=lambda a: (-a["window"]["total_tokens"], -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(), key=lambda m: (-m["window"]["total_tokens"], -m["all_time"]["total_tokens"]))
    keys_list = _build_key_rows(key_map, realm=realm)
    return {
        # The resolved window travels with the payload so the page can label
        # its first column from what the server actually applied, not from
        # what the panel hoped it sent.
        "window": {"since": since, "until": until},
        "realm": realm or "all",
        # Cost figures are CNY; the panel divides by this rate to show USD.
        "usd_cny": wb_pricing.usd_cny(),
        "summary": {"window": window_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
        # Same rows, folded by the API key that called the gateway instead of
        # by the upstream account that served the call. One key can be served
        # by several accounts and one account can serve several keys, so this
        # is a second view of the same spend, not a breakdown of it.
        "keys": keys_list,
    }


# ---------------------------------------------------------------------------
# 聚合状态落盘（重启后免冷扫）
#
# 上面的三份增量聚合（_usage_alltime_snapshot / _usage_by_account_buckets /
# _analytics_all_time）把状态放在模块级字典里，进程一重启就全丢：第一次刷新
# 仍是全量扫一遍日志。日志每天涨一万多行，冷扫描成本跟着线性上升——路由器
# 上 2.7 万行的一次冷启动要 6~8 秒，一个月后接近两分钟。这里把三份状态连同
# 它们的位置信息写进数据目录下的一个 JSON 文件，重启后第一次刷新只读文件
# 尾部的新增字节。
#
# 文件放在数据目录里是刻意的：它和日志同生命周期，跟着 sysupgrade 一起走，
# 也和日志一起被搬走、清掉。读写失败（只读文件系统、磁盘满、权限不足）一律
# 静默降级——checkpoint 只是加速，丢的是一次加速，绝不能影响服务本身。
#
# 加载校验复用 #189 已有的原语，不另造一套：offset 必须仍落在同一个文件的
# 现有字节里（_log_offset_holds），offset 前的最后 64 字节必须与写下时一致
# （_log_tail_signature），再加上价格/realm 指纹和 schema 版本。任何一项对不
# 上就当没有缓存、走全量——宁可多扫一次，也不能给出错的数字。
# ---------------------------------------------------------------------------

# 数据目录下的文件名。usage/*.json 已被 .gitignore 覆盖。
_USAGE_CACHE_NAME = "usage-aggregate-cache.json"
# 状态结构或折叠口径一变就 +1，旧文件整体作废。这是唯一挡在「旧结构喂进新
# 代码」前面的东西，改动状态形状时别忘了它。
# v2：写盘不再排序（v1 用 sort_keys 写，加载回来所有 dict 变字母序，响应
# 与冷启动逐字节不同）。键顺序算状态结构的一部分，所以旧文件必须整体作废
# ——否则那份字母序会一直粘在内存里，直到日志尾部被重写才会被清掉。
# v3：状态多了按日分桶（窗口半的增量）与时间序列的日/小时桶。载荷形状变了，
# 旧文件同样整体作废：升级后第一次刷新做一次全量折叠（一次性代价，之后
# checkpoint 照旧）——这正是把窗口半也变成增量的入场费。
_USAGE_CACHE_SCHEMA = 3


def _usage_cache_enabled():
    """落盘总开关，默认开；WB_USAGE_CACHE=0 关。

    每次调用都读环境变量而不是在导入时定死：现场用它紧急关掉，测试用它在一
    个进程里来回切，而一次 os.environ.get 的开销在刷新路径上可以忽略。
    """
    return os.environ.get("WB_USAGE_CACHE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _usage_cache_path():
    """checkpoint 的路径。

    每次调用重算，而不是在导入时算成常量：--usage-dir、测试和探针脚本都会
    在导入之后改 USAGE_DIR，常量会写到旧目录去。
    """
    return os.path.join(USAGE_DIR, _USAGE_CACHE_NAME)


def _usage_cache_min_bytes():
    """写盘节流：折叠推进不足这么多字节就不写。默认 1MB。

    1MB 是「重启后最多回放多少」的上限——路由器上扫描约 0.5s/MB，回放 1MB
    等于冷启动成本多半秒；而日志每天涨 6MB 上下，正常流量下一天也未必触发
    一次。写小了是写放大（路由器 flash 写入慢且费寿命），写大了重启变慢。
    """
    try:
        return max(0, int(os.environ.get("WB_USAGE_CACHE_MIN_BYTES", 1024 * 1024)))
    except (TypeError, ValueError):
        return 1024 * 1024


def _usage_cache_min_seconds():
    """写盘节流：距上次尝试不足这么多秒就不写。默认 900s（15 分钟）。

    这条兜住「字节阈值还没到、但偏移已经推进了一点」的情况：不设它，一段
    短流量之后停下的日志要等到下次凑满 1MB 才会落盘。15 分钟让面板开着时
    一天最多约百次写盘（每次几十 KB），对 flash 友好；重启回放的上限则是
    min(1MB, 15 分钟流量)。
    """
    try:
        return max(0.0, float(os.environ.get("WB_USAGE_CACHE_MIN_SECONDS", 900)))
    except (TypeError, ValueError):
        return 900.0


_usage_cache_lock = threading.Lock()
# 写盘串行锁：只被 _usage_cache_maybe_save() 拿一次、且永远是最外层（拿它
# 之后才拿状态锁/节流锁），所以它不参与任何锁序环。
_usage_cache_write_lock = threading.Lock()
# 进程启动后只读一次 checkpoint：读到就记下来（按 realm 逐份取用），读不到
# 也记 None——坏文件、只读文件系统都不该让每次刷新都去解析一遍。
_usage_cache_loaded = False
_usage_cache_read_result = None
# 写盘节流状态，全部由 _usage_cache_lock 保护：
#   progress[kind]     —— 本进程见过该类状态折叠到的最远 offset；
#   checkpointed[kind] —— 该类状态上次「尝试」落盘时的 offset（失败也记，
#                         只读文件系统上不能每次刷新都去试写一遍）；
#   last_attempt       —— 上次尝试写盘的时刻，时间阈值与失败退避都靠它。
# 按「类」记账而不是记一个总数：某类状态第一次折叠（或中途重扫）时 offset
# 从 0 跳到文件尾，这一类要尽快落盘，而刚采用过 checkpoint 的类不能被别的
# 类的推进带着白写一遍。
_usage_cache_progress = {}
_usage_cache_checkpointed = {}
_usage_cache_last_attempt = time.time()


def _usage_cache_data():
    """checkpoint 的解析结果，或 None。

    调用点可能在状态锁内（采用阶段），所以本函数会取 _usage_cache_lock；
    反向的锁序不存在——写盘路径取状态锁之前一定先放开本锁，见
    _usage_cache_maybe_save。
    """
    global _usage_cache_loaded, _usage_cache_read_result
    if not _usage_cache_enabled():
        return None
    if _usage_cache_loaded:
        return _usage_cache_read_result
    with _usage_cache_lock:
        if not _usage_cache_loaded:
            _usage_cache_read_result = _usage_cache_read()
            _usage_cache_loaded = True
        return _usage_cache_read_result


def _usage_cache_read():
    """读 checkpoint 文件并做顶层校验；任何一步不成立都返回 None。

    None 和「文件不存在」同义：调用方按没有缓存处理，绝不因此报错。
    """
    try:
        with open(_usage_cache_path(), "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # 先看类型再看值：JSON 的 true 在 Python 里等于 1，schema: true 不能算
    # 「版本 1」。
    if not _usage_cache_int(data.get("schema")):
        return None
    if data["schema"] != _USAGE_CACHE_SCHEMA:
        return None
    return data


_usage_cache_digest_memo = {}


def _object_digest(obj):
    """一段 JSON 内容摘要，按对象身份记忆；算不出来返回 None。

    记忆表的值强引用对象本身，所以 id() 不会被回收复用；超过几条就整体清空
    ——正常进程里价表对象只在价格变化时换代，这里只是给长期运行兜底。
    """
    hit = _usage_cache_digest_memo.get(id(obj))
    if hit is not None and hit[0] is obj:
        return hit[1]
    try:
        payload = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha1(payload).hexdigest()
    except Exception:
        return None
    if len(_usage_cache_digest_memo) > 8:
        _usage_cache_digest_memo.clear()
    _usage_cache_digest_memo[id(obj)] = (obj, digest)
    return digest


def _pricing_inputs_key(inputs):
    """把 _pricing_inputs() 的元组压成一段跨进程可比的价格指纹。

    进程内那套靠对象身份（_pricing_unchanged），身份活不过重启，所以这里存
    内容摘要。摘要取的是取价函数真正返回的对象而不是价表文件：文件损坏时会
    回退到内联表，折叠读到的也是内联表，指纹必须描述折叠真正读的东西；文件
    不在时内联表有没有变过，也只有摘要看得出来。
    """
    if inputs is None:
        return None
    if not inputs[0]:
        # 总开关关着：取价路径一个价表都不读，开关本身就是全部输入。
        return ["off"]
    digests = []
    for obj in inputs[1:]:
        digest = _object_digest(obj)
        if digest is None:
            return None
        digests.append(digest)
    return ["on"] + digests


def _realm_inputs_key(inputs):
    """realm 指纹本来就是值比较（不是身份），JSON 化后原样可比。"""
    if inputs is None:
        return None
    realm, accounts = inputs
    return [realm, [[uid, r] for uid, r in accounts]]


# 折叠会就地累加/读取的字段集合直接从构造函数推导：上游往 _empty_stats() 或
# _new_analytics_stat() 里加字段时这里自动跟着收——漏一个，放行的缓存就会在
# 折叠半路抛 KeyError，那次刷新报出半份数字（折叠的错误路径只保留已折的
# 部分）。
_USAGE_CACHE_SNAPSHOT_NUMBERS = tuple(
    k for k, v in _empty_stats().items()
    if isinstance(v, (int, float)) and not isinstance(v, bool))
_USAGE_CACHE_SNAPSHOT_MAPS = ("cost_missing", "by_model", "by_model_realm", "by_model_acct")
_USAGE_CACHE_ANALYTICS_NUMBERS = tuple(_new_analytics_stat().keys())
_USAGE_CACHE_BUCKET_NUMBERS = ("requests", "cost_cny") + USAGE_FIELDS
# 时间序列桶的数字字段（_new_series_bucket 的键，含 at）。从构造函数推导，
# 与上面两条同一个道理：上游加字段时校验自动跟着收。
_USAGE_CACHE_SERIES_NUMBERS = tuple(_new_series_bucket(0).keys())


def _usage_cache_number(value):
    """折叠累加的数字。bool 是 int 的子类，必须挡掉。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usage_cache_int(value):
    """严格整数：JSON 里写成 12345.0 的不算。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _usage_cache_counts_ok(counts):
    """{str: 数字} 形状的计数表（cost_missing / models / realms）。"""
    if not isinstance(counts, dict):
        return False
    for key, value in counts.items():
        if not isinstance(key, str) or not _usage_cache_number(value):
            return False
    return True


def _usage_cache_bucket_ok(bucket):
    """一个 by_model* 桶：折叠会就地累加这些键。"""
    if not isinstance(bucket, dict):
        return False
    for field in _USAGE_CACHE_BUCKET_NUMBERS:
        if not _usage_cache_number(bucket.get(field)):
            return False
    return _usage_cache_counts_ok(bucket.get("accounts"))


def _usage_cache_snapshot_ok(snap):
    """snapshot 折叠的形状。

    少一个键，折叠或 _copy_usage_snapshot() 就会半路抛错，那次刷新报出半份
    数字——校验必须挡在采用之前。
    """
    if not isinstance(snap, dict):
        return False
    for field in _USAGE_CACHE_SNAPSHOT_NUMBERS:
        if not _usage_cache_number(snap.get(field)):
            return False
    for field in _USAGE_CACHE_SNAPSHOT_MAPS:
        if not isinstance(snap.get(field), dict):
            return False
    if not _usage_cache_counts_ok(snap["cost_missing"]):
        return False
    for _, bucket in snap["by_model"].items():
        if not _usage_cache_bucket_ok(bucket):
            return False
    for _, realms in snap["by_model_realm"].items():
        if not isinstance(realms, dict):
            return False
        for _, bucket in realms.items():
            if not _usage_cache_bucket_ok(bucket):
                return False
    for _, realms in snap["by_model_acct"].items():
        if not isinstance(realms, dict):
            return False
        for _, accts in realms.items():
            if not isinstance(accts, dict):
                return False
            for _, bucket in accts.items():
                if not _usage_cache_bucket_ok(bucket):
                    return False
    return True


def _usage_cache_stat_ok(stat):
    """一个 analytics 统计桶：折叠会就地累加它的每一个字段。"""
    if not isinstance(stat, dict):
        return False
    for field in _USAGE_CACHE_ANALYTICS_NUMBERS:
        if not _usage_cache_number(stat.get(field)):
            return False
    return True


def _usage_cache_models_ok(models):
    """analytics 的 {model: {requests, tokens, reasoning, cost_cny}} 表。"""
    if not isinstance(models, dict):
        return False
    for mid, stat in models.items():
        if not isinstance(mid, str) or not isinstance(stat, dict):
            return False
        for field in ("requests", "tokens", "reasoning", "cost_cny"):
            if not _usage_cache_number(stat.get(field)):
                return False
    return True


def _usage_cache_analytics_window_ok(maps):
    """窗口形状的 analytics maps（日桶）：同一批表，装的是 window 那一半。

    与 _usage_cache_analytics_ok 是两份形状，不能互相顶替：日桶只折窗口半，
    少 all_time/all_models 两个键，用 all-time 的校验器会把整份 analytics
    entry 拒掉（连它自己那份 all-time 也一起丢），窗口就退回整段扫描。
    """
    if not isinstance(maps, dict):
        return False
    if not _usage_cache_stat_ok(maps.get("summary")):
        return False
    accts, models, keys = maps.get("accts"), maps.get("models"), maps.get("keys")
    if not isinstance(accts, dict) or not isinstance(models, dict) or not isinstance(keys, dict):
        return False
    for uid, entry in accts.items():
        if not isinstance(uid, str) or not isinstance(entry, dict):
            return False
        for field in ("nickname", "realm", "domain"):
            if not isinstance(entry.get(field), str):
                return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
        if not _usage_cache_models_ok(entry.get("window_models")):
            return False
    for mid, entry in models.items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("model"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
    for kid, entry in keys.items():
        if not isinstance(kid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("key"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("window")):
            return False
        if not _usage_cache_models_ok(entry.get("window_models")):
            return False
        if not _usage_cache_counts_ok(entry.get("realms")):
            return False
        if not _usage_cache_number(entry.get("last_at")):
            return False
    return True


def _usage_cache_analytics_ok(maps):
    """analytics all-time maps：summary/accts/models/keys 四张表。"""
    if not isinstance(maps, dict):
        return False
    if not _usage_cache_stat_ok(maps.get("summary")):
        return False
    accts, models, keys = maps.get("accts"), maps.get("models"), maps.get("keys")
    if not isinstance(accts, dict) or not isinstance(models, dict) or not isinstance(keys, dict):
        return False
    for uid, entry in accts.items():
        if not isinstance(uid, str) or not isinstance(entry, dict):
            return False
        for field in ("nickname", "realm", "domain"):
            if not isinstance(entry.get(field), str):
                return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
        if not _usage_cache_models_ok(entry.get("all_models")):
            return False
    for mid, entry in models.items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("model"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
    for kid, entry in keys.items():
        if not isinstance(kid, str) or not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("key"), str):
            return False
        if not _usage_cache_stat_ok(entry.get("all_time")):
            return False
        if not _usage_cache_models_ok(entry.get("all_models")):
            return False
        if not _usage_cache_counts_ok(entry.get("realms")):
            return False
        if not _usage_cache_number(entry.get("last_at")):
            return False
    return True


def _usage_cache_buckets_ok(buckets):
    """by_account 的 {account: 桶}：折叠会就地累加每个桶。"""
    if not isinstance(buckets, dict):
        return False
    for key, bucket in buckets.items():
        if not isinstance(key, str) or not isinstance(bucket, dict):
            return False
        if not isinstance(bucket.get("account"), str):
            return False
        for field in ("requests", "prompt_tokens", "completion_tokens",
                      "reasoning_tokens", "cached_tokens", "total_tokens"):
            if not _usage_cache_number(bucket.get(field)):
                return False
        if not _usage_cache_counts_ok(bucket.get("models")):
            return False
    return True


def _usage_cache_day_key_ok(key):
    """日键必须是 "YYYY-MM-DD"：窗口与日桶的比较靠的就是这个形状。"""
    return (isinstance(key, str) and len(key) == 10 and key[4] == "-"
            and key[7] == "-")


def _usage_cache_days_ok(days, floor, bucket_ok):
    """按日分桶的表：{日键: 桶}，floor 是「对哪些天完整」的下界（日键或 None）。

    桶的形状由调用方给（snapshot / analytics / 时间序列各一套），日键与
    floor 的形状在这里统一校验：键不是日期的桶永远不会被窗口选中，但也说明
    这份状态不是我们写出来的，直接拒绝更干净。
    """
    if not isinstance(days, dict):
        return False
    if floor is not None and not _usage_cache_day_key_ok(floor):
        return False
    for key, bucket in days.items():
        if not _usage_cache_day_key_ok(key) or not bucket_ok(bucket):
            return False
    return True


def _usage_cache_series_bucket_ok(bucket):
    """时间序列桶：9 个数字；日桶还带 hours 子表（小时键是数字字符串）。

    小时键写成字符串是 JSON 往返决定的（JSON 的对象键只能是字符串），折叠
    侧因此从一开始就用 str(小时序号)，加载回来的状态与现折的状态形状一致。
    """
    if not isinstance(bucket, dict):
        return False
    for field in _USAGE_CACHE_SERIES_NUMBERS:
        if not _usage_cache_number(bucket.get(field)):
            return False
    hours = bucket.get("hours")
    if hours is None:
        return True
    if not isinstance(hours, dict):
        return False
    for hour, sub in hours.items():
        if not (isinstance(hour, str) and hour.isdigit()):
            return False
        if not isinstance(sub, dict) or "hours" in sub:
            return False
        for field in _USAGE_CACHE_SERIES_NUMBERS:
            if not _usage_cache_number(sub.get(field)):
                return False
    return True


def _usage_cache_credits_ok(credits):
    """credit 缓冲：{at, iso, model, account, credit, total_tokens} 的列表。"""
    if not isinstance(credits, list):
        return False
    for item in credits:
        if not isinstance(item, dict):
            return False
        for field in ("at", "credit", "total_tokens"):
            if not _usage_cache_number(item.get(field)):
                return False
        for field in ("iso", "model", "account"):
            if not isinstance(item.get(field), str):
                return False
    return True


def _usage_cache_take(kind, realm=None):
    """从 checkpoint 数据里取出一份状态（取过即删），没有合适的返回 None。

    snapshot/analytics 按 realm 匹配（JSON 的 null 表示不限 realm），
    by_account 只有一份。逐份取用：同一份不会被第二次采用，也就不存在
    「先取走、校验失败、下个 realm 又拿到一份脏状态」的路径。
    """
    data = _usage_cache_data()
    if not data:
        return None
    entries = data.get(kind)
    if not isinstance(entries, list):
        return None
    # 取走这一步也要在锁里：两个 realm 的首次刷新可能同时到这里，一个
    # 边遍历边 pop、另一个也在 pop，遍历就会跳过条目。跳过的后果只是这次
    # 不采用（安全方向），但少一次竞争就少一个要解释的路径。
    with _usage_cache_lock:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            if kind != "by_account":
                # realm 字段必须显式存在且类型正确：realm=None 的折叠（全部
                # realm）和某个具体 realm 的折叠是两份不同的状态，缺字段的
                # 条目一旦被 None 匹配走，就会把「只折了一个 realm」的数字
                # 当成「全部 realm」报出去。
                entry_realm = entry.get("realm")
                if "realm" not in entry or not (entry_realm is None
                                                or isinstance(entry_realm, str)):
                    continue
                if entry_realm != realm:
                    continue
            entries.pop(index)
            return entry
    return None


def _usage_cache_position_ok(entry, log_key):
    """校验一份状态的 (offset, key, tail)；不合格返回 None。

    复用 #189 的两个原语而不是另造一套：offset 必须仍落在同一个文件的现有
    字节里（dev/ino 相同、当前 size >= offset），并且 offset 前的最后 64 字节
    与写下时一致——文件被替换、截短、copytruncate 后重写都会被这两条挡住。
    """
    offset = entry.get("offset")
    key = entry.get("key")
    tail_hex = entry.get("tail")
    if not _usage_cache_int(offset) or offset <= 0:
        return None
    if not isinstance(key, list) or len(key) != 3 or not all(
            _usage_cache_int(v) for v in key):
        return None
    if not isinstance(tail_hex, str):
        return None
    try:
        tail = bytes.fromhex(tail_hex)
    except ValueError:
        return None
    if not _log_offset_holds(tuple(key), log_key, offset):
        return None
    if _log_tail_signature(offset) != tail:
        return None
    return offset, tail


def _usage_cache_fingerprints_ok(entry, pricing_key, realm_key):
    """价格与 realm 指纹必须和当前进程读到的一致。

    任一侧取不到（None）都算不一致：取不到就意味着「折叠当时读了什么」已经
    无法确认，缓存不能当有效用。
    """
    if pricing_key is None or realm_key is None:
        return False
    return entry.get("pricing") == pricing_key and entry.get("realm_inputs") == realm_key


def _usage_cache_note_adopted(kind, offset):
    """采用成功后的节流初始化：刚加载的 offset 不算「有新字节要写」。

    不初始化的话，重启后第一次刷新会把「offset 从 0 涨到文件尾」当成一次大
    推进，白白重写一份内容相同的文件。
    """
    global _usage_cache_last_attempt
    with _usage_cache_lock:
        if offset > _usage_cache_progress.get(kind, 0):
            _usage_cache_progress[kind] = offset
        if offset > _usage_cache_checkpointed.get(kind, 0):
            _usage_cache_checkpointed[kind] = offset
        _usage_cache_last_attempt = time.time()


def _usage_cache_adopt_snapshot(r, state, log_key, pricing, realm):
    """把 checkpoint 里对应 realm 的 snapshot 折叠装进 state；成功返回 True。

    调用方持有 _usage_snap_state_lock，并且只在 state 全新时调用：采用只把
    offset/tail 往前挪，折叠逻辑一行不动。
    """
    entry = _usage_cache_take("snapshot", r)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    if not _usage_cache_fingerprints_ok(entry, _pricing_inputs_key(pricing),
                                        _realm_inputs_key(realm)):
        return False
    snap = entry.get("snap")
    if not _usage_cache_snapshot_ok(snap):
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_snapshot_ok):
        return False
    offset, tail = position
    state.update({"snap": snap, "offset": offset, "tail": tail,
                  "days": days, "days_floor": floor})
    _usage_cache_note_adopted("snapshot", offset)
    return True


def _usage_cache_adopt_by_account(state, log_key):
    """by_account 折叠的采用；这一份没有价格/realm 指纹（折叠不读它们）。"""
    entry = _usage_cache_take("by_account")
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    buckets = entry.get("buckets")
    if not _usage_cache_buckets_ok(buckets):
        return False
    offset, tail = position
    state.update({"buckets": buckets, "offset": offset, "tail": tail})
    _usage_cache_note_adopted("by_account", offset)
    return True


def _usage_cache_adopt_analytics(realm, state, log_key, pricing, realm_inputs):
    """analytics all-time 折叠的采用。"""
    entry = _usage_cache_take("analytics", realm)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    if not _usage_cache_fingerprints_ok(entry, _pricing_inputs_key(pricing),
                                        _realm_inputs_key(realm_inputs)):
        return False
    maps = entry.get("maps")
    if not _usage_cache_analytics_ok(maps):
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_analytics_window_ok):
        return False
    offset, tail = position
    state.update({"maps": maps, "offset": offset, "tail": tail,
                  "days": days, "days_floor": floor})
    _usage_cache_note_adopted("analytics", offset)
    return True


def _usage_cache_adopt_series(r, state, log_key, realm_inputs):
    """时间序列状态的采用；这一份只有 realm 指纹（折叠不算价）。"""
    entry = _usage_cache_take("series", r)
    if entry is None:
        return False
    position = _usage_cache_position_ok(entry, log_key)
    if position is None:
        return False
    realm_key = _realm_inputs_key(realm_inputs)
    if realm_key is None or entry.get("realm_inputs") != realm_key:
        return False
    days = entry.get("days")
    floor = entry.get("days_floor")
    if not _usage_cache_days_ok(days, floor, _usage_cache_series_bucket_ok):
        return False
    max_at = entry.get("max_at")
    if not _usage_cache_number(max_at):
        return False
    credits = entry.get("credits")
    if not _usage_cache_credits_ok(credits):
        return False
    offset, tail = position
    state.update({"days": days, "days_floor": floor, "max_at": max_at,
                  "credits": credits, "offset": offset, "tail": tail})
    _usage_cache_note_adopted("series", offset)
    return True


def _usage_cache_days_copy(days, copy_bucket):
    """日桶表的深拷贝（序列化在锁外做，桶必须和活状态脱钩）。"""
    return {key: copy_bucket(bucket) for key, bucket in (days or {}).items()}


def _copy_series_day(bucket):
    """一个时间序列日桶的深拷贝（含 hours 子表）。"""
    copy = dict(bucket)
    copy["hours"] = {h: dict(sub) for h, sub in (bucket.get("hours") or {}).items()}
    return copy


def _usage_cache_snapshot_entry(realm, state):
    """一份 snapshot 状态的 JSON 形态；不适合落盘时返回 None。

    只写「折叠成功过」的状态：错误路径会把 offset 归零，这里直接跳过——
    offset 为 0 的缓存没有任何加速作用，写进去只会让加载端白校验一遍。
    """
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes) or state.get("snap") is None:
        return None
    pricing_key = _pricing_inputs_key(state.get("pricing"))
    realm_inputs = state.get("realm")
    realm_key = _realm_inputs_key(realm_inputs)
    if pricing_key is None or realm_key is None:
        return None
    if not _realm_fold_reproducible(realm_inputs[1]):
        # 这次折叠的归属冷进程复现不了（见 _realm_fold_reproducible）：写下去
        # 会被按新归属当成有效。宁可这次不写，等池就绪/重载后再写。
        return None
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "pricing": pricing_key, "realm_inputs": realm_key,
            # 深拷贝在锁内做，序列化在锁外做（见 _usage_cache_collect）。
            "snap": _copy_usage_snapshot(state["snap"]),
            # 按日分桶的窗口半：与 all-time 半同一次扫描折出来的，位置也
            # 跟着同一条 offset/tail 校验，不另设一套。
            "days": _usage_cache_days_copy(state.get("days"),
                                           _copy_usage_snapshot),
            "days_floor": state.get("days_floor")}


def _usage_cache_by_account_entry(state):
    """by_account 折叠的 JSON 形态；同上，只写折叠成功过的。"""
    offset = state.get("offset") or 0
    tail = state.get("tail")
    buckets = state.get("buckets")
    if offset <= 0 or not isinstance(tail, bytes) or not isinstance(buckets, dict):
        return None
    return {"offset": offset, "key": list(state["key"] or (0, 0, 0)),
            "tail": tail.hex(),
            # 深拷贝：序列化在锁外做，桶里的 models 必须和活状态脱钩。
            "buckets": {k: dict(v, models=dict(v.get("models") or {}))
                        for k, v in buckets.items()}}


def _usage_cache_analytics_entry(realm, state):
    """analytics all-time 折叠的 JSON 形态；同上。"""
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes):
        return None
    pricing_key = _pricing_inputs_key(state.get("pricing"))
    realm_inputs = state.get("realm")
    realm_key = _realm_inputs_key(realm_inputs)
    if pricing_key is None or realm_key is None:
        return None
    if not _realm_fold_reproducible(realm_inputs[1]):
        return None          # 同 snapshot：这次折叠的归属冷进程复现不了
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "pricing": pricing_key, "realm_inputs": realm_key,
            "maps": _copy_analytics_all_time(state["maps"]),
            "days": _usage_cache_days_copy(state.get("days"),
                                           _copy_analytics_window_maps),
            "days_floor": state.get("days_floor")}


def _usage_cache_series_entry(realm, state):
    """时间序列状态的 JSON 形态；同上，只写折叠成功过的。

    这一份最大（日桶 + 24 小时子桶 × 保留天数），所以深拷贝只拷桶本身，
    credit 缓冲是 dict 列表、直接浅拷外层。
    """
    offset = state.get("offset") or 0
    tail = state.get("tail")
    if offset <= 0 or not isinstance(tail, bytes):
        return None
    realm_key = _realm_inputs_key(state.get("realm"))
    if realm_key is None:
        return None
    return {"realm": realm, "offset": offset,
            "key": list(state["key"] or (0, 0, 0)), "tail": tail.hex(),
            "realm_inputs": realm_key,
            "days": _usage_cache_days_copy(state.get("days"), _copy_series_day),
            "days_floor": state.get("days_floor"),
            "max_at": state.get("max_at") or 0,
            "credits": [dict(item) for item in (state.get("credits") or [])]}


def _usage_cache_collect():
    """逐把取四份状态，做锁外可序列化的深拷贝；返回 (payload, offsets) 或 None。

    每份状态单独加锁、单独拷贝，几份之间不强求同一个瞬间：加载端对每份分别
    校验 offset/tail/指纹，一份新一份旧也各自成立。锁是逐把拿、随即放开的，
    不存在嵌套——写盘路径不能制造「snapshot -> analytics」这样的锁序边。
    """
    snapshot_entries = []
    by_account_entries = []
    analytics_entries = []
    series_entries = []
    offsets = {}
    with _usage_snap_state_lock:
        for realm, state in _usage_snap_state.items():
            entry = _usage_cache_snapshot_entry(realm, state)
            if entry is not None:
                snapshot_entries.append(entry)
                offsets["snapshot"] = max(offsets.get("snapshot", 0), state["offset"])
    with _byacct_state_lock:
        entry = _usage_cache_by_account_entry(_byacct_state)
        if entry is not None:
            by_account_entries.append(entry)
            offsets["by_account"] = _byacct_state["offset"]
    with _analytics_state_lock:
        for realm, state in _analytics_state.items():
            entry = _usage_cache_analytics_entry(realm, state)
            if entry is not None:
                analytics_entries.append(entry)
                offsets["analytics"] = max(offsets.get("analytics", 0), state["offset"])
    with _series_state_lock:
        for realm, state in _series_state.items():
            entry = _usage_cache_series_entry(realm, state)
            if entry is not None:
                series_entries.append(entry)
                offsets["series"] = max(offsets.get("series", 0), state["offset"])
    if not (snapshot_entries or by_account_entries or analytics_entries
            or series_entries):
        return None
    payload = {"schema": _USAGE_CACHE_SCHEMA, "written_at": time.time(),
               "snapshot": snapshot_entries, "by_account": by_account_entries,
               "analytics": analytics_entries, "series": series_entries}
    return payload, offsets


def _usage_cache_write(payload):
    """原子写：同目录临时文件 + os.replace。

    直接在目标文件上写，崩在中间会留下半份 JSON；加载端虽然能识别（解析失败
    按没有缓存处理），但一份完整的旧文件更省事。不 fsync：这是一份加速件，
    掉电后写坏或丢掉的，加载端一律当没有缓存，不值得为它多刷一次盘。

    不排序键：checkpoint 里 dict 的键顺序就是折叠时的插入顺序（先出现的先
    排），加载回来必须与冷启动逐字节一致——响应的 JSON 字节里键序是可见的，
    而冷启动折叠出的顺序正是这份插入顺序。排序只留给 _object_digest() 那种
    「只要内容一样就算一样」的指纹。
    """
    path = _usage_cache_path()
    tmp = path + ".tmp"
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _usage_cache_maybe_save(kind, offset):
    """一次折叠成功后，按节流阈值决定要不要把三份状态落盘。

    调用点必须在状态锁之外：这里要逐把取三份状态，在状态锁里再取别的状态锁
    会构成锁序环（snapshot -> analytics 与反向同时存在）。任何异常都吞掉——
    落盘失败只损失一次加速，不能影响这次刷新。
    """
    global _usage_cache_last_attempt
    if not _usage_cache_enabled():
        return
    try:
        # 串行化整段写盘：两个请求线程可能同时判定「该写」，而临时文件名是
        # 固定的（path + ".tmp"，和 save_runtime_override 一致），两个写者
        # 同时打开同一个临时文件会互相截断。等锁的一方在锁里会重新判定一遍，
        # 第一个写者已经把 checkpointed 推上去，于是它多半直接返回。
        with _usage_cache_write_lock:
            now = time.time()
            with _usage_cache_lock:
                if offset > _usage_cache_progress.get(kind, 0):
                    _usage_cache_progress[kind] = offset
                min_bytes = _usage_cache_min_bytes()
                min_seconds = _usage_cache_min_seconds()
                due = any(_usage_cache_progress.get(k, 0)
                          - _usage_cache_checkpointed.get(k, 0) >= min_bytes
                          for k in _usage_cache_progress)
                if not due and (now - _usage_cache_last_attempt) < min_seconds:
                    return
                # 记「尝试」而不是「成功」：只读文件系统上失败会一直成立，
                # 不退避的话每次刷新都去试写一遍。
                _usage_cache_last_attempt = now
            collected = _usage_cache_collect()
            if collected is None:
                return
            payload, offsets = collected
            _usage_cache_write(payload)
            with _usage_cache_lock:
                for k, value in offsets.items():
                    if value > _usage_cache_checkpointed.get(k, 0):
                        _usage_cache_checkpointed[k] = value
    except Exception as exc:
        log("usage aggregate cache write failed: %s" % exc)


def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    for entry in configured_keys():
        raw = entry.get("key") or ""
        keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "models": list(entry.get("models") or []),
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8 else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
        })
    # Deleted keys are read-only history: the secret is gone, so the panel can
    # only list them (name and dates) and must not offer a copy button.
    deleted_keys = []
    for entry in wb_settings.api_keys(ACCOUNTS_DIR, include_deleted=True):
        if not entry.get("deleted_at"):
            continue
        deleted_keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "created_at": entry.get("created_at") or "",
            "deleted_at": entry.get("deleted_at") or "",
        })
    return {
        "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "deleted_api_keys": deleted_keys,
        "limits": wb_settings.limits_snapshot(ACCOUNTS_DIR),
        "reserve_credits": wb_settings.reserve_credits(ACCOUNTS_DIR),
        "daily_token_limit": wb_settings.daily_token_limit(ACCOUNTS_DIR),
        "daily_credit_limit": wb_settings.daily_credit_limit(ACCOUNTS_DIR),
        "model_daily_token_limit": wb_settings.model_daily_token_limit(ACCOUNTS_DIR),
        "pricing_refresh_minutes": wb_settings.pricing_refresh_minutes(ACCOUNTS_DIR),
        "credits_refresh_hours": wb_settings.credits_refresh_hours(ACCOUNTS_DIR),
        "ui_language": wb_settings.ui_language(ACCOUNTS_DIR),
        "pricing_variant_inherit": wb_settings.pricing_variant_inherit(ACCOUNTS_DIR),
        "pricing_enabled": wb_settings.pricing_enabled(ACCOUNTS_DIR),
        "auto_switch_product": wb_settings.auto_switch_product(ACCOUNTS_DIR),
        "daily_chat_web": wb_settings.daily_chat_web(ACCOUNTS_DIR),
        "local_web_tools": wb_settings.local_web_tools(ACCOUNTS_DIR),
        "accounts_collapsed": wb_settings.accounts_collapsed(ACCOUNTS_DIR),
        "update_check_enabled": wb_settings.update_check_enabled(ACCOUNTS_DIR),
        "upstream": wb_settings.upstream_config(ACCOUNTS_DIR),
        "prompt": wb_settings.prompt_config(ACCOUNTS_DIR),
        "accounts_dir": ACCOUNTS_DIR,
        "usage_dir": USAGE_DIR,
        "settings_file": wb_settings.settings_path(ACCOUNTS_DIR),
        "version": "1.6.19",
    }
def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return POOL.representative() if POOL else None
# ---------------------------------------------------------------------------
# Prefix-based session affinity (PATCHED-BY-OPS)
# ---------------------------------------------------------------------------
# 上游 prompt cache 是【账号级】的：只有同一个账号再次看到相同前缀才会命中。
# 实测证据（wk 实例 11 个号）：8 次完全相同的前缀请求被轮询分散到 8 个账号，
# 缓存率全部为 0%；而带上会话标识固定落到同一账号时，第 2 次起缓存率即 95.2%。
#
# sub2api / DSH 等客户端并不发送 X-Conversation-Id 之类的会话标识，
# 于是 hub 走纯轮询，同一对话每一轮都换账号，缓存必然归零。
#
# 这里在缺少显式会话键时，用【对话稳定前缀】派生亲和键：
# 取消息列表的前两条（system + 首条 user），它们在整段对话生命周期内不变，
# 因此同一对话的每一轮都会落到同一账号；而不同对话的首条 user 不同，
# 依旧会分散到各账号，负载均衡不受影响。
AFFINITY_BY_PREFIX = os.environ.get("WB_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("WB_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")
# 亲和的上限：对话超过这么多条消息后不再绑定账号。
#
# 为什么需要上限：亲和把整段对话钉在同一个账号上，而对话的上下文是单调
# 增长的，于是那一个账号要反复接收越来越大的请求体。实测（wk4 实例，
# 2026-10-08，333 个请求）请求体与上游断连率的关系：
#
#     msgs <150    82 个请求   断连率  0.0%
#     msgs 150-300 59 个请求   断连率  3.4%
#     msgs 300-400 64 个请求   断连率  7.8%
#     msgs 400-500 50 个请求   断连率 10.0%
#
# 断连（TimeoutError / RemoteDisconnected）会触发重试，把一个 8.8s 的请求
# 拖到 11.6s，首字延迟随之翻倍。超过阈值后放弃亲和，让这个对话重新参与
# 轮询：代价是它丢掉前缀缓存，收益是断连和重试消失。
#
# 阈值不能设得太低，否则会波及正常长度的对话（本实例 94% 的请求缓存命中率
# 来自亲和）。设 0 表示不限制，保持 1.6.x 的原有行为。
AFFINITY_MAX_MSGS = int(os.environ.get("WB_AFFINITY_MAX_MSGS", "400") or 0)
def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.
    The first two messages (system + first user turn) stay byte-identical for
    the whole life of a conversation, so hashing them pins every later turn of
    that conversation to the same upstream account - exactly what prompt
    caching needs. Distinct conversations differ in their first user turn and
    therefore still spread across the pool.

    Conversations longer than AFFINITY_MAX_MSGS deliberately get no key: they
    are the ones whose oversized bodies make the upstream drop the connection,
    and pinning them only guarantees the next turn is oversized too.
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        if AFFINITY_MAX_MSGS > 0 and len(msgs) > AFFINITY_MAX_MSGS:
            if AFFINITY_DEBUG:
                log("affinity: skip %d msgs (> %d), letting the pool rotate"
                    % (len(msgs), AFFINITY_MAX_MSGS))
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None
def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.
    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}

LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0

def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in ("error", "exception", "failed", "11128", "11101", "11140", "traceback", "errno", "fatal")):
            level = "ERROR"
        elif any(k in lower for k in ("warn", "warning", "retry", "timeout")):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        if "chat:" in lower or "chat done" in lower or "/v1/chat" in lower or "/chat/completions" in lower or "responses" in lower:
            tag = "chat"
        elif "scheduler" in lower or "调度器" in lower:
            tag = "scheduler"
        elif "task" in lower or "任务" in lower or "打卡" in lower or "猫猫" in lower or "travel" in lower:
            tag = "tasks"
        elif "account" in lower or "账号" in lower or "pool" in lower or "imported" in lower:
            tag = "accounts"
        elif "model" in lower or "catalog" in lower or "模型" in lower:
            tag = "catalog"
        elif "auth" in lower or "token" in lower or "oauth" in lower:
            tag = "auth"
        elif "settings" in lower or "设置" in lower:
            tag = "settings"
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    return entry

def log(msg, level=None, tag=None):
    sys.stderr.write(f"[wb-proxy] {time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()
    add_log_entry(msg, level=level, tag=tag)

def get_logs(limit=200, level="", tag="", search="", since_id=0):
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}

def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()

# ---------------------------------------------------------------------------
# upstream helpers
# ---------------------------------------------------------------------------
#: Auxiliary models the API advertises but that are not usable for chat.
#: "lite" backs internal helpers (title generation, compaction) and upstream
#: rejects it with 11102; the codewise/completion entries are text-completion
#: or IDE-inline models, not chat models.
# Exclude WorkBuddy virtual aliases / quick presets
VIRTUAL_ALIAS_MODELS = {
    "default-model",
    "fast-model",
    "balanced-model",
    "primary-model",
    "deep-model",
    # The domestic exit's auto-router entry: the picker shows it, but it is
    # not a model a client can pin, so it stays out of the advertised list.
    "auto",
}
NON_CHAT_MODELS = {"lite"} | VIRTUAL_ALIAS_MODELS
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")
def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True
CN_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.2",
    "glm-5.1",
    "glm-5v-turbo",
    "minimax-m3",
    "kimi-k3-1",
    "kimi-k2.8-preview",
    "kimi-k2.7",
    "kimi-k2.6",
    "deepseek-v4-pro",
]
INTL_UI_ORDER = [
    "hy4-preview-f",
    "hy3",
    "deepseek-v4.1-flash",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "grok-4.7",
    "gemini-3.5-flash",
    "glm-5.3-flash",
    "glm-5.3",
    "glm-5.2",
    "kimi-k3",
    "kimi-k2.6",
    "kimi-k2.8-preview",
]
def merge_reasoning(base, live):
    """Field-level merge of one model's reasoning block.

    The live catalogue is the source of truth, but it does not always restate
    every field: the desktop endpoint sometimes gives a bare `effort` where the
    bundled table knows the level is selectable. A top-level update would drop
    `supportedEfforts`, and an `effort` without it reads as a pin - the model
    then looks unselectable in /v1/models and every request to it is reported at
    the wrong level. So the two blocks are merged field by field, and a block
    that ends up with `supportedEfforts` is selectable: its `effort` is the
    default restated, not a pin.
    """
    out = dict(base) if isinstance(base, dict) else {}
    if isinstance(live, dict):
        out.update(live)
    if out.get("supportedEfforts") and out.get("effort"):
        out.setdefault("defaultEffort", out["effort"])
        out.pop("effort", None)
    return out
def merge_catalog(primary, realm=None, extras=False):
    r = realm or CURRENT_REALM
    merged = {}
    # "all" is the union of both realms. The analytics dashboard lists every
    # model the gateway has served, so it must not drop the ones that only
    # one side's catalog knows about.
    if r == "all":
        source_static = list(wb_catalog.STATIC_INTL_MODELS) + list(wb_catalog.STATIC_CN_MODELS)
    else:
        source_static = getattr(wb_catalog, "STATIC_CN_MODELS" if r == "cn" else "STATIC_INTL_MODELS", wb_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("id")
        # First catalog wins for a shared id, so the intl entry is not
        # overwritten by its cn counterpart when both are merged.
        if mid and is_chat_model(mid) and mid not in merged:
            merged[mid] = dict(item)
    for mid, meta in primary or []:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            base_reasoning = base.get("reasoning")
            base.update(meta)
            if isinstance(meta.get("reasoning"), dict):
                base["reasoning"] = merge_reasoning(base_reasoning, meta["reasoning"])
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    if r == "all":
        order = list(INTL_UI_ORDER) + [m for m in CN_UI_ORDER if m not in INTL_UI_ORDER]
    seen = set()
    for mid in order:
        if mid in merged and mid not in seen:
            seen.add(mid)
            out.append((mid, merged[mid]))
    if extras:
        # A model the curated table has never heard of still ships when the
        # *live* catalogue lists it - that is how a newly added upstream model
        # reaches /v1/models without a release. The bundled snapshot alone is
        # not enough: it also carries legacy entries the picker may not show.
        for mid, _meta in primary or []:
            if mid in merged and mid not in seen:
                seen.add(mid)
                out.append((mid, merged[mid]))
    return out
_catalog_lock = threading.Lock()

_catalog_fallback_log = {}
def note_bundled_reasoning(realm, live, entries):
    """Say once when a model's reasoning controls come from the bundled table.

    The live catalogue is the source of truth and the snapshot is only the
    fallback, so values the snapshot alone carries can be no fresher than the
    snapshot. Two shapes put them there: a model the live catalogue gives no
    reasoning block at all, and one it gives a bare `effort` for where the
    snapshot knows the level is selectable. One line per change is enough.
    """
    live_meta = dict(live or [])

    def from_snapshot(mid, meta):
        reasoning = meta.get("reasoning") or {}
        if not reasoning:
            return False
        live_reasoning = (live_meta.get(mid) or {}).get("reasoning") or {}
        if not live_reasoning:
            return True
        return bool(reasoning.get("supportedEfforts")) \
            and not live_reasoning.get("supportedEfforts")

    missing = sorted(mid for mid, meta in entries if from_snapshot(mid, meta))
    if not missing:
        _catalog_fallback_log.pop(realm, None)
        return
    if _catalog_fallback_log.get(realm) == frozenset(missing):
        return
    _catalog_fallback_log[realm] = frozenset(missing)
    shown = ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
    log("catalog    : bundled table fills in reasoning controls the live "
        "catalogue omits for %d model(s): %s" % (len(missing), shown))
def fetch_models(realm=None):
    r = realm or CURRENT_REALM
    with _lock:
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    # One upstream walk per realm even when several callers miss the cache at
    # the same moment: a batch of /v1/models requests must not turn into a
    # batch of upstream requests.
    with _catalog_lock:
        with _lock:
            c = _models_cache.get(r) or {"at": 0.0, "data": None}
            if c["data"] and time.time() - c["at"] < 300:
                return c["data"]
        live, extras = curated_live_sources(r)
        if not live and r in ("intl", "all"):
            # The narrow endpoint is not the desktop catalogue, so it keeps the
            # old whitelist behaviour: only names the order table knows.
            live = [(m, {}) for m in fetch_endpoint_models()]
            extras = False
        entries = merge_catalog(live, realm=r, extras=extras)
        note_bundled_reasoning(r, live, entries)
        with _lock:
            _models_cache[r] = {"at": time.time(), "data": entries}
        return entries
def model_entry(mid, meta):
    """Build a rich /v1/models entry from the desktop app catalog metadata.
    The OpenAI spec only names id/object/created/owned_by, so capability data is
    convention-driven. Several shapes are emitted at once so that different
    clients (OpenRouter-style, LobeChat-style, plain-flag readers) all find
    what they look for.
    """
    meta = meta or {}
    item = {
        "id": mid,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "workbuddy",
    }
    name = meta.get("name")
    if name:
        item["name"] = name
    desc = meta.get("descriptionEn") or meta.get("descriptionZh")
    if desc:
        item["description"] = desc
    # ---- modality / capability ----
    # disabledMultimodal explicitly turns image input off; absent means allowed.
    vision = bool(meta.get("supportsImages")) and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall"))
    thinks = bool(meta.get("supportsReasoning"))
    inputs = ["text"] + (["image"] if vision else [])
    # Capability flags under every spelling the common clients look for.
    # /v1/models has no standard for this, so each convention is emitted at
    # once rather than guessing which one a given client reads:
    #   capabilities.vision      generic
    #   supports_vision/images   LobeChat-style flat flags
    #   vision                   Cherry Studio / NextChat style
    #   abilities.vision         LobeChat
    #   multimodal               misc
    #   *_modalities             OpenRouter
    item["capabilities"] = {
        "vision": vision,
        "tool_calls": tools,
        "reasoning": thinks,
    }
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {
        "vision": vision,
        "functionCall": tools,
        "function_call": tools,
        "reasoning": thinks,
    }
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    # OpenRouter-shaped block, read by several multi-provider clients.
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # ---- limits ----
    # Four-level lookup: remote > built-in knowledge table > local model.json
    # cache > models.dev (asynchronously warmed; never blocks). context_length
    # always has a value (1M when unknown - a high estimate is safer than a
    # low one); max_output_tokens is omitted when unknown.
    ctx_value, out_value, _limit_source = wb_modelsdev.lookup(
        mid, meta.get("maxInputTokens"), meta.get("maxOutputTokens"),
        directory=ACCOUNTS_DIR)
    item["context_length"] = ctx_value
    item["max_input_tokens"] = ctx_value
    if out_value:
        item["max_output_tokens"] = out_value
        item["max_completion_tokens"] = out_value
    # Annotate the measured upstream clamp (from output_probes.json) without
    # overriding the model's spec value; the panel shows "钳制 N×" beside it.
    clamp = wb_probes.clamp_for(ACCOUNTS_DIR, mid)
    if clamp:
        item["output_clamp"] = clamp
        item["max_output_tokens_clamped"] = clamp
    ctx = (meta.get("contextWindow") or {}).get("supportedLengths")
    if ctx:
        item["context_windows"] = ctx
    # ---- reasoning controls ----
    reasoning = meta.get("reasoning") or {}
    efforts = reasoning.get("supportedEfforts")
    if efforts:
        item["reasoning_efforts"] = efforts
    if reasoning.get("effort"):
        item["reasoning_fixed_effort"] = reasoning["effort"]
    if reasoning.get("defaultEffort"):
        item["reasoning_default_effort"] = reasoning["defaultEffort"]
    if reasoning.get("canDisableThinking") is not None:
        item["reasoning_can_disable"] = reasoning["canDisableThinking"]
    if meta.get("onlyReasoning") is not None:
        item["always_reasoning"] = bool(meta.get("onlyReasoning"))
    # ---- misc ----
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    if meta.get("vendor"):
        item["vendor"] = meta["vendor"]
    if meta.get("temperature") is not None:
        item["temperature"] = meta["temperature"]
    if meta.get("top_p") is not None:
        item["top_p"] = meta["top_p"]
    if meta.get("isDefault"):
        item["is_default"] = True
    tags = [t for t in (meta.get("tags") or []) if isinstance(t, str) and not t.startswith("badge:")]
    if tags:
        item["tags"] = tags
    return item
def read_product_config_models(realm=None):
    """Read the desktop app's cached catalog: [(id, meta), ...].

    "all" reads both apps when they are installed, so the combined view gets
    each side's metadata instead of only the domestic one.
    """
    r = realm or CURRENT_REALM
    if r == "all":
        out = _read_product_config_dir(".workbuddy-ai")
        seen = set(mid for mid, _ in out)
        for mid, meta in _read_product_config_dir(".workbuddy"):
            if mid not in seen:
                out.append((mid, meta))
        return out
    return _read_product_config_dir(".workbuddy-ai" if r == "intl" else ".workbuddy")


def _read_product_config_dir(cache_dir):
    home = os.path.expanduser("~")
    p = os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        return []
    def find(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models and isinstance(models[0], dict) and models[0].get("id"):
                return models
            for value in node.values():
                hit = find(value)
                if hit:
                    return hit
        return None
    models = find(cfg) or []
    out = []
    for m in models:
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            out.append((mid, m))
    return out
#: The desktop client's own product-config endpoint. The cache file that
#: read_product_config_models() reads is this response written to disk, so
#: calling it directly is what lets a machine without the desktop app
#: (Docker, NAS, a headless server) advertise the live catalogue - live
#: multipliers included - instead of the narrower endpoint or the bundled
#: snapshot.
REMOTE_CONFIG_PATH = "/v3/config"

#: Suffixes that mark a variant of a name the catalogue already carries: the
#: regional build (deepseek-v4.1-flash-sg) and the experimental one (hy3-x).
#: Measured on both exits: the plain name is the free (x0.00) one and the
#: variant is the paid one, so the plain name is what gets advertised.
VARIANT_SUFFIXES = ("-sg", "-x")


def remote_config_headers(account, realm, ua=None):
    """Headers for the product-config call.

    The UA decides which catalogue comes back and only the desktop UA returns
    the full list (an unknown one is a hard 400, code 12403), so this uses a
    realm's fixed desktop UA rather than the account's current identity.
    """
    cfg = wb_accounts.get_realm_config(realm)
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ua or cfg["chat_ua"],
        "Origin": cfg["origin"],
        "Referer": cfg["origin"] + "/",
        "Authorization": "Bearer " + account.access_token,
        "X-User-Id": account.uid,
    }


def _agent_model_lists(payload):
    """Every agent's bare-string model list, cli-named agents first.

    The catalogue the picker shows rides in agents[].models. The endpoint
    answers with it under a "data" key while the desktop cache file is the
    same document written to disk without that envelope, so both are read.
    """
    roots = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        roots.append(data)
    cli, other = [], []
    for root in roots:
        agents = root.get("agents")
        if isinstance(agents, dict):
            entries = list(agents.items())
        elif isinstance(agents, list):
            entries = [((entry.get("name") if isinstance(entry, dict) else None),
                        entry) for entry in agents]
        else:
            continue
        for name, entry in entries:
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not (isinstance(models, list) and models
                    and isinstance(models[0], str)):
                continue
            ids = [str(m).strip() for m in models if isinstance(m, str)]
            ids = [m for m in ids if m]
            if not ids:
                continue
            (cli if str(name or "").strip().lower() == "cli" else other).append(ids)
    return cli, other


def parse_remote_catalog(payload):
    """(ids, meta) from a /v3/config response, or None when it carries none.

    The picker's list rides in agents[].models as bare ids - under "data" in
    the endpoint's answer, at the top level in the desktop cache file. The
    per-model metadata (credits, limits, copy) lives in a separate models
    array. An unusable credential answers HTTP 200 with an *empty* list, so
    an empty catalogue is reported as None and the caller falls back instead
    of publishing "this exit has no models".
    """
    if not isinstance(payload, dict):
        return None
    cli_lists, other_lists = _agent_model_lists(payload)
    pool = cli_lists or other_lists
    best = max(pool, key=len) if pool else None
    ids, seen = [], set()
    for mid in best or []:
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if not ids:
        return None

    meta = {}

    def walk(node):
        if isinstance(node, dict):
            models = node.get("models")
            if isinstance(models, list) and models \
                and isinstance(models[0], dict) and models[0].get("id"):
                for item in models:
                    mid = str(item.get("id") or "").strip()
                    if mid:
                        meta.setdefault(mid, item)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return ids, meta


def snapshot_credits():
    """id -> credits from the bundled catalogue (both realms, intl first).

    Used to answer "is there a free sibling?" for a variant the remote lists
    but whose sibling it no longer does: the free hy4-preview-f, for example,
    is what the cn picker keeps while the remote only names the paid one.
    """
    out = {}
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                           getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid:
                out.setdefault(mid, str(item.get("credits") or "").strip().lower())
    return out


def curate_remote_catalog(realm, ids, meta=None):
    """Trim a remote catalogue to the models the picker should offer.

      - virtual aliases (default-model ... auto) are not models;
      - "-sg" / "-x" builds are the paid variant of a name the list already
        carries;
      - when a free ("x0.00") sibling exists, the free one is the one the
        picker shows, so the paid sibling is dropped;
      - everything else keeps its upstream order. Names the upstream does not
        list at all stay available through the curated order tables and the
        bundled snapshot, which merge_catalog() keeps.
    """
    credits = snapshot_credits()
    for mid, item in (meta or {}).items():
        if isinstance(item, dict):
            credits[mid] = str(item.get("credits") or "").strip().lower()
    order = CN_UI_ORDER if realm == "cn" else INTL_UI_ORDER
    known = set(ids) | set(credits) | set(order)

    def free(mid):
        return credits.get(mid) in ("x0.00", "x0", "0", "0.00")

    out = []
    for mid in ids:
        if not is_chat_model(mid):
            continue
        if mid.endswith(VARIANT_SUFFIXES):
            continue
        if mid.endswith("-f"):
            base = mid[:-2]
            if base in known and free(base) and not free(mid):
                continue
        elif (mid + "-f") in known and free(mid + "-f") and not free(mid):
            continue
        out.append(mid)
    return out


def fetch_remote_product_config(realm):
    """(ids, meta) from the realm's own product-config endpoint, or None.

    At most two 10s attempts bound the wait: one per desktop UA, because the
    endpoint sits behind the WAF where a dropped connection is normal, and
    every caller has a fallback (the desktop cache file, the narrow model
    endpoint, the bundled snapshot).
    """
    if realm not in ("intl", "cn") or POOL is None:
        return None
    account = POOL.representative(realm=realm)
    if account is None or not account.access_token:
        log("remote catalog: no usable %s account, skipping" % realm)
        return None
    cfg = wb_accounts.get_realm_config(realm)
    url = cfg["chat_upstream"] + REMOTE_CONFIG_PATH
    # The chat UA is the desktop identity the rest of the gateway uses; the
    # plain app UA is the second try, for a build that answers only to it.
    uas = [cfg["chat_ua"]]
    if cfg.get("billing_ua") and cfg["billing_ua"] != cfg["chat_ua"]:
        uas.append(cfg["billing_ua"])
    last = None
    for ua in uas:
        headers = remote_config_headers(account, realm, ua)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with wb_accounts.urlopen(req, timeout=10, proxy=account.proxy) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            continue
        parsed = parse_remote_catalog(payload)
        if parsed:
            return parsed
        last = "empty catalogue"
    log("remote catalog: %s fetch failed (%s)" % (realm, last))
    return None


def product_config_path(realm):
    """The desktop cache file for a realm (the intl app writes its own)."""
    home = os.path.expanduser("~")
    cache_dir = ".workbuddy-ai" if realm == "intl" else ".workbuddy"
    return os.path.join(home, cache_dir, "cache", "acc-product-config-v3.json")


def read_cached_remote_catalog(realm):
    """(ids, meta) from the desktop cache file, parsed like the remote."""
    if realm == "all":
        first = read_cached_remote_catalog("intl")
        second = read_cached_remote_catalog("cn")
        if not first:
            return second
        if not second:
            return first
        ids = list(first[0]) + [m for m in second[0] if m not in set(first[0])]
        meta = dict(second[1])
        meta.update(first[1])
        return ids, meta
    try:
        with open(product_config_path(realm), encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    return parse_remote_catalog(payload)


def curated_live_sources(realm):
    """(entries, extras) for the realm's live catalogue, already curated.

    Remote first, then the desktop cache file - the cache is this very
    response written to disk, so both go through the same parser and the same
    rules. `extras` says the entries came from the desktop catalogue, whose
    membership may add a model the curated tables have never seen; the legacy
    readers keep the old whitelist behaviour.
    """
    remote = None
    try:
        remote = fetch_remote_product_config(realm)
    except Exception as exc:
        log("remote catalog: %s failed (%s)" % (realm, exc))
    source = remote or read_cached_remote_catalog(realm)
    if source:
        ids, meta = source
        return ([(mid, meta.get(mid) or {})
                for mid in curate_remote_catalog(realm, ids, meta)], True)
    legacy = read_product_config_models(realm=realm)
    if legacy:
        ids = [mid for mid, _ in legacy]
        meta = dict((mid, m) for mid, m in legacy if isinstance(m, dict))
        keep = set(curate_remote_catalog(realm, ids, meta))
        return ([(mid, m) for mid, m in legacy if mid in keep], False)
    return [], False


def fetch_endpoint_models():
    account = POOL.pick(realm="intl") if POOL else None
    if account is None:
        log("model discovery skipped: no usable account")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    req = urllib.request.Request(UPSTREAM + MODELS_PATH, method="GET", headers=account.headers())
    try:
        with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        log(f"model discovery failed: {exc}")
        cached = _models_cache.get("intl", {}).get("data")
        return [m for m, _ in (cached or [])]
    ids, seen = [], set()
    for agent in (payload.get("data") or {}).get("agents") or []:
        for mid in agent.get("models") or []:
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids
def strip_data_prefix(line):
    line = line.strip()
    # SSE comment / heartbeat / keepalive / empty line
    if not line or line.startswith(":"):
        return ""
    while line.startswith("data:"):
        line = line[5:].strip()
    # Handle possible "data: : heartbeat"
    if not line or line.startswith(":"):
        return ""
    return line
_RETRY_LOCK = threading.Lock()
_RETRY_COUNTS = Counter()
_RETRY_STARTED_AT = time.time()


def retry_counters_file():
    return os.path.join(USAGE_DIR, "retry-counters.json")


def _write_retry_snapshot_locked():
    os.makedirs(USAGE_DIR, exist_ok=True)
    path = retry_counters_file()
    tmp = path + ".%s.tmp" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as sink:
            json.dump({"pid": os.getpid(), "started": _RETRY_STARTED_AT,
                       "updated": time.time(), "counters": dict(_RETRY_COUNTS),
                       "total": sum(_RETRY_COUNTS.values())}, sink)
        os.replace(tmp, path)
    except OSError as exc:
        log("retry metrics persistence failed: %s" % type(exc).__name__, level="WARN")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def reset_retry_counters_file():
    global _RETRY_STARTED_AT
    with _RETRY_LOCK:
        _RETRY_COUNTS.clear()
        _RETRY_STARTED_AT = time.time()
        _write_retry_snapshot_locked()


def retry_bump(kind):
    with _RETRY_LOCK:
        _RETRY_COUNTS[str(kind)] += 1
        _write_retry_snapshot_locked()
    log("retry kind=%s" % kind, level="INFO", tag="retry")


def retry_snapshot():
    with _RETRY_LOCK:
        return dict(_RETRY_COUNTS)


def retry_health_snapshot(limit=200):
    try:
        limit = max(1, min(10000, int(limit)))
    except (ValueError, TypeError):
        limit = 200
    stats = {"retries": retry_snapshot(), "pid": os.getpid(), "ops_patch_id": "v1.6.19-ops.20261011.2",
             "loops": loop_health_snapshot(),
             "process_started": _RETRY_STARTED_AT, "turns": 0,
             "reasoning_only": 0, "empty_output": 0, "failed": 0,
             "recent_retry_log": [row for row in list(LOG_BUFFER)
                                  if row.get("tag") == "retry"][-40:]}
    try:
        with open(USAGE_LOG, encoding="utf-8", errors="replace") as source:
            lines = deque(source, maxlen=limit)
    except OSError:
        return stats
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("error"):
            stats["failed"] += 1
            continue
        stats["turns"] += 1
        output = row.get("completion_tokens") or 0
        reasoning = row.get("reasoning_tokens") or 0
        if not output:
            stats["empty_output"] += 1
        elif reasoning and output <= reasoning + 2:
            stats["reasoning_only"] += 1
    return stats


_LOOP_STATS_LOCK = threading.Lock()
_LOOP_STATS = Counter()
_LOOP_EVENTS = deque(maxlen=64)
_FAILED_REASONING = FailedReasoningCache()
_LOOP_CIRCUIT = LoopRetryCircuit()


def _retry_scope(handler):
    # API-key scope stays private even in memory-only circuit keys.
    value = json.dumps(getattr(handler, "key_entry", None), sort_keys=True, default=str)
    return hashlib.sha256(value.encode()).hexdigest()


def _loop_context_key(holder):
    if (os.environ.get("WB_OPS_LOOP_CIRCUIT", "1").lower() in ("0", "false", "off") or
            not holder.get("session_key") or not holder.get("base_body")):
        return None
    if "loop_context_key" not in holder:
        # Exact context, including tools and options: any substantive change
        # gets its own budget. No guessing from a shared first user message.
        value = [holder.get("retry_owner"), holder["session_key"], holder.get("realm"), holder["base_body"]]
        holder["loop_context_key"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return holder["loop_context_key"]


def _loop_event(kind, rule=None, chars=None, metrics=None, reason=None):
    # Only categories and sizes, never prompts, reasoning excerpts, keys or IDs.
    event = {"kind": kind, "time": int(time.time())}
    if rule in ("fast-period", "fast-intent", "exact", "churn", "mixed-stall"):
        event["rule"] = rule
    if chars is not None:
        event["received_chars"] = max(0, int(chars))
    if reason in ("time", "bytes", "chars", "frames", "metadata", "role", "content", "tool",
                  "terminal", "protocol", "error", "refusal", "tool_seen", "content_seen",
                  "passthrough", "eof", "transport_error", "loop"):
        event["buffer_reason"] = reason
    # Fixed integer-only schema: no text, IDs, field values, or dynamic labels.
    if isinstance(metrics, dict):
        for key in ("buffered_chars", "buffered_bytes", "buffered_storage_bytes", "trigger_storage_bytes", "buffered_frames", "buffered_items", "buffered_records", "buffered_ms",
                    "content_chars", "closed_boundary", "loop_hit",
                    "trigger_bytes", "trigger_chars", "window_chars", "window_lines",
                    "intent_lines", "dominant_count", "distinct_intents", "received_chars",
                    "path", "neutral_lines", "neutral_dominant_count", "substantive_lines", "substantive_chars"):
            value = metrics.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                event[key] = min(9007199254740991, max(0, value))
    with _LOOP_STATS_LOCK:
        _LOOP_STATS[kind] += 1
        if rule:
            _LOOP_STATS["rule_" + str(event.get("rule", "other"))] += 1
        _LOOP_EVENTS.append(event)


def loop_health_snapshot():
    with _LOOP_STATS_LOCK:
        return {"counts": dict(_LOOP_STATS), "recent": [dict(e) for e in _LOOP_EVENTS]}


def _finish_loop_retry(holder, success):
    if success:
        _LOOP_CIRCUIT.success(_loop_context_key(holder))
    if holder.pop("loop_retry_active", False):
        _loop_event("retry_recovered" if success else "retry_failed")


def _try_loop_continuation(holder, model, failure, reasoning, content=None, calls=None):
    if failure != "reasoning_loop_detected":
        return False
    safe = (str(model or "").lower().startswith("deepseek-") and holder.get("loop_evidence") and
            (holder.get("base_body") or {}).get("n", 1) == 1 and
            not holder.get("ops_output_seen") and not holder.get("ops_tool_seen") and
            not holder.get("client_content_seen") and not holder.get("client_tool_calls_seen") and
            not content and not calls)
    if not safe:
        _loop_event("retry_blocked_output_or_protocol")
        return False
    if os.environ.get("WB_OPS_FAILED_REASONING_CLEANUP", "1").lower() not in ("0", "false", "off"):
        _FAILED_REASONING.remember(reasoning)
    if os.environ.get("WB_OPS_LOOP_RETRY", "1").lower() in ("0", "false", "off"):
        _loop_event("retry_disabled")
        return False
    if not holder.get("retry_call") or holder.get("retried") or holder.get("continuation_attempts", 0) >= CONTINUATION_MAX_ATTEMPTS:
        _loop_event("retry_budget_exhausted")
        return False
    if not _LOOP_CIRCUIT.allow(_loop_context_key(holder)):
        _loop_event("retry_cooldown")
        return False
    if not _start_empty_continuation(holder, kind="reasoning_loop"):
        _loop_event("retry_open_failed")
        holder["incomplete_reason"] = holder.get("continuation_failure_code") or failure
        return False
    holder.pop("incomplete_reason", None)
    holder["loop_retry_active"] = True
    _loop_event("retry_started")
    return True


def _stream_socket(upstream):
    fp = getattr(upstream, "fp", None)
    raw = getattr(fp, "raw", None)
    return getattr(raw, "_sock", None)


def _interrupt_upstream(upstream):
    sock = _stream_socket(upstream)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    else:
        try:
            upstream.close()
        except Exception:
            pass


def _close_upstream(upstream):
    try:
        upstream.close()
    except Exception:
        pass


_STREAM_GUARD_CODES = {"reasoning_loop_detected", "content_loop_detected", "generation_timeout"}


def _generation_deadline(holder):
    if "generation_deadline" not in holder:
        try:
            seconds = float(os.environ.get("WB_OPS_GENERATION_TIMEOUT", "300"))
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("invalid timeout")
        except ValueError:
            seconds = 300
        holder["generation_deadline"] = time.monotonic() + seconds if seconds else None
    return holder["generation_deadline"]


def _buffer_protocol_event(detail):
    """Bounded shape diagnostics; never serialize values or arbitrary paths."""
    reason = detail.get("reason", "unknown")
    event = {"kind": "reasoning_buffer_protocol_detail", "time": int(time.time()),
             "reason": reason, "shape": detail.get("shape", {})}
    with _LOOP_STATS_LOCK:
        _LOOP_STATS["reasoning_buffer_protocol_" + reason] += 1
        _LOOP_EVENTS.append(event)


def _reasoning_buffer_boundary(frame, detail=None):
    """None for supported pure-reasoning/role frames, else a fixed category.

    Standard nullable fields are not output. Real tool IDs/names/arguments,
    content, refusal, finish markers and unfamiliar shapes must be released.
    This only controls withholding, never the separate replay safety gates.
    """
    def protocol(reason, value):
        if detail is not None:
            shape = {"type": type(value).__name__}
            if isinstance(value, dict):
                shape["count"] = len(value)
                # Only identifier-shaped field names; values and dynamic IDs
                # are never logged. Limit both payload size and cardinality.
                shape["fields"] = [{"name": k if isinstance(k, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", k) else "<redacted>",
                                    "type": type(v).__name__}
                                   for k, v in itertools.islice(value.items(), 12)]
            elif isinstance(value, list):
                shape["count"] = len(value)
            detail.update(reason=reason, shape=shape)
        return "protocol"

    if not isinstance(frame, dict):
        return protocol("frame_type", frame)
    if frame.get("error"):
        return "error"
    choices = frame.get("choices")
    if choices is None or choices == []:
        if set(frame) - {"choices", "usage", "id", "object", "model", "created", "system_fingerprint", "service_tier"}:
            return protocol("metadata_fields", frame)
        return "metadata"
    if not isinstance(choices, list) or len(choices) != 1:
        return protocol("choices_shape", choices)
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("index", 0) != 0:
        return protocol("choice_shape", choice)
    if choice.get("finish_reason"):
        return "terminal"
    delta = choice.get("delta")
    if delta is None:
        delta = {}
    if not isinstance(delta, dict):
        return protocol("delta_type", delta)
    if delta.get("content") not in (None, ""):
        return "content"
    if delta.get("refusal") not in (None, ""):
        return "refusal"
    if delta.get("reasoning_content") is not None and not isinstance(delta["reasoning_content"], str):
        return protocol("reasoning_type", delta)
    if delta.get("role") not in (None, "", "assistant"):
        return protocol("role_value", delta)
    # Observed upstream placeholder: accept exactly JSON null, not every falsy
    # value. Non-null extension semantics remain unknown and are not buffered.
    if delta.get("extra_fields") is not None:
        return protocol("extra_fields_value", delta)
    if set(delta) - {"role", "reasoning_content", "content", "tool_calls", "function_call", "refusal", "extra_fields"}:
        return protocol("delta_fields", delta)
    calls = delta.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        return protocol("calls_type", calls)
    for call in calls:
        if not isinstance(call, dict):
            return protocol("call_type", call)
        if call.get("id") not in (None, ""):
            return "tool"
        fn = call.get("function")
        if fn is None:
            fn = {}
        if not isinstance(fn, dict):
            return protocol("function_type", fn)
        if fn.get("name") not in (None, "") or fn.get("arguments") not in (None, ""):
            return "tool"
        if (set(call) - {"index", "id", "type", "function"} or
                set(fn) - {"name", "arguments"} or call.get("type") not in (None, "", "function")):
            return protocol("call_fields", call)
    legacy = delta.get("function_call")
    if legacy is not None:
        if not isinstance(legacy, dict):
            return protocol("legacy_type", legacy)
        if legacy.get("name") not in (None, "") or legacy.get("arguments") not in (None, ""):
            return "tool"
        if set(legacy) - {"name", "arguments"}:
            return protocol("legacy_fields", legacy)
    return None


class _GuardedStream:
    """Shared Chat/Responses/nonstream guard; never inspects tool arguments.

    A watchdog shuts down the actual socket even when readline is blocked or
    upstream trickles a single unfinished SSE line. Callers close in finally.
    Synthetic error frames preserve each adapter's existing failure lifecycle.
    """

    def __init__(self, upstream, holder):
        self.upstream = upstream
        self.holder = holder
        holder.pop("loop_evidence", None)
        self.deadline = _generation_deadline(holder)
        self.expired = threading.Event()
        self.timer = None
        self.closed = False
        self.reason = ReasoningLoopGuard()
        self.mixed_stall = (MixedStallGuard() if
                            str(holder.get("model") or "").strip().lower().rsplit("/", 1)[-1] == DEEPSEEK_V41_MODEL
                            and (holder.get("base_body") or {}).get("n", 1) == 1
                            and os.environ.get("WB_OPS_LOOP_GUARD", "1").lower() not in ("0", "false", "off")
                            and os.environ.get("WB_OPS_MIXED_STALL_GUARD", "1").lower()
                            not in ("0", "false", "off") else None)
        self.stall_buffer_signal = False
        self.stall_observer = (StallObserver() if str(holder.get("model") or "").lower().startswith("deepseek-")
                               and os.environ.get("WB_OPS_STALL_OBSERVE", "1").lower() not in ("0", "false", "off") else None)
        self.content = ReasoningLoopGuard(conservative=True)
        self.leak = LeakedThinkingGuard()
        self.leak_reported = False
        self.received_chars = 0
        self.reason_chars = 0
        self.received_cjk = 0
        self.reason_cjk = 0
        self.received_usage = None
        self.reason_scrubber = ReasoningScrubber()
        self.content_scrubber = ReasoningScrubber()
        self.protocol_tail = ""
        self.recovery = ReasoningAnswerRecovery(holder.get("model"))
        if (holder.get("base_body") or {}).get("n", 1) != 1:
            self.recovery.block()
        if self.deadline is not None:
            sock = _stream_socket(upstream)
            if sock is not None:
                sock.settimeout(min(upstream_timeouts()[1], max(.001, self.deadline - time.monotonic())))
            self.timer = threading.Timer(max(0, self.deadline - time.monotonic()), self._expire)
            self.timer.daemon = True
            self.timer.start()

    def _expire(self):
        self.expired.set()
        _interrupt_upstream(self.upstream)

    def _timed_out(self):
        return self.expired.is_set() or (self.deadline is not None and time.monotonic() >= self.deadline)

    def _failure(self, code):
        retry_bump(code)
        _interrupt_upstream(self.upstream)
        return ("data: " + json.dumps({"error": {"code": code,
                "message": "Upstream safeguard: " + code}}) + "\n\n").encode()

    def _observe_stall(self, text="", final=False):
        if self.stall_observer is not None:
            metrics = self.stall_observer.feed(text, final=final)
            if metrics:
                self.stall_buffer_signal = True
                _loop_event("reasoning_stall_observed", metrics=metrics)

    def __iter__(self):
        # Bound both the initial prefix and a newly suspicious short-intent
        # segment. Content/tools, caps, or the next frame after two seconds
        # flush unchanged. Never buffer a whole turn or manufacture an answer.
        pending, pending_bytes, pending_chars = CompactSSEBuffer(), 0, 0
        pending_frames, buffer_started_at = 0, None
        started = None
        enabled = (str(self.holder.get("model") or "").lower().startswith("deepseek-") and
                   os.environ.get("WB_OPS_REASONING_BUFFER", "1").lower() not in ("0", "false", "off"))
        buffering, previous_suspicion = enabled, False
        observation_used = False
        skipped = set()
        protocol_seen = set()
        neutral_seen = set()

        def skip(reason):
            # Once per attempt/category: never log every token or field value.
            if reason not in skipped:
                skipped.add(reason)
                _loop_event("reasoning_buffer_skip_" + reason)

        def buffer_metrics(trigger_bytes=0, trigger_chars=0, trigger_storage_bytes=0):
            # Independent measurement clock; never used to decide a release.
            elapsed = time.perf_counter() - buffer_started_at if buffer_started_at is not None else 0
            return {"buffered_chars": pending_chars, "buffered_bytes": pending_bytes,
                    "buffered_storage_bytes": pending.storage_bytes, "trigger_storage_bytes": trigger_storage_bytes,
                    "buffered_frames": pending_frames, "buffered_items": len(pending),
                    "buffered_records": pending.record_count,
                    "buffered_ms": int(max(0, elapsed) * 1000),
                    "trigger_bytes": trigger_bytes, "trigger_chars": trigger_chars}

        def released(reason, trigger_bytes=0, trigger_chars=0, trigger_storage_bytes=0):
            _loop_event("reasoning_buffer_released", chars=pending_chars,
                        metrics=buffer_metrics(trigger_bytes, trigger_chars, trigger_storage_bytes), reason=reason)
            _loop_event("reasoning_buffer_release_" + reason)

        if not enabled:
            skip("disabled" if str(self.holder.get("model") or "").lower().startswith("deepseek-") else "model")
        try:
            for raw in self._frames():
                detail = {}
                line = raw.decode("utf8", "replace") if isinstance(raw, bytes) else str(raw)
                size = len(raw.encode("utf8")) if isinstance(raw, str) else len(raw)
                neutral = ("separator" if not line.strip() else
                           "heartbeat" if line.lstrip().startswith(":") else None)
                if neutral:
                    # HTTPResponse iterates lines, not complete SSE events.
                    # A separator is framing, not a new metadata event. Keep
                    # it with withheld data; comments can pass without output.
                    # Count each category once per attempt, never per token.
                    if neutral not in neutral_seen:
                        neutral_seen.add(neutral)
                        _loop_event("reasoning_buffer_neutral_" + neutral)
                    if pending:
                        prepared = pending.prepare(raw, attach_separator=neutral == "separator")
                        storage_cost = prepared[-1] if prepared is not None else size + pending.HEADER.size
                        reason = ("time" if started is not None and time.monotonic()-started >= 2 else
                                  "bytes" if not pending.fits(prepared) else
                                  "frames" if not pending.has_room(prepared) else None)
                        if reason:
                            released(reason, size, 0, storage_cost)
                            yield from pending
                            pending.clear()
                            pending_bytes = pending_chars = 0
                            buffering, started = False, None
                        elif neutral == "separator":
                            pending.append_prepared(prepared)
                            pending_bytes += size
                            continue
                    yield raw
                    continue
                try:
                    payload = strip_data_prefix(line).strip()
                    if payload == "[DONE]":
                        data = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    else:
                        data = json.loads(payload)
                except (ValueError, TypeError):
                    data = {}
                    detail = {"reason": "invalid_json", "shape": {"type": "unparsed"}}
                if not isinstance(data, dict):
                    _reasoning_buffer_boundary(data, detail)
                    data = {}
                code = (data.get("error") or {}).get("code")
                if code == "reasoning_loop_detected":
                    if pending:
                        _loop_event("reasoning_buffer_discarded", chars=pending_chars,
                                    metrics=buffer_metrics(), reason="loop")
                        pending.clear()
                    # The withheld generation still consumed tokens. Preserve
                    # provider usage or explicitly estimate the entire attempt.
                    completion = self.received_cjk + max(1, int((self.received_chars-self.received_cjk)/3.6))
                    reasoning = self.reason_cjk + max(1, int((self.reason_chars-self.reason_cjk)/3.6))
                    prompt = max(1, estimate_tokens(str((self.holder.get("base_body") or {}).get("messages") or "")))
                    usage = self.received_usage or {"prompt_tokens": prompt, "completion_tokens": completion,
                             "total_tokens": prompt+completion, "completion_tokens_details": {"reasoning_tokens": reasoning}}
                    yield ("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode()
                    yield raw
                    return
                choices = data.get("choices") or []
                chars = sum(len(str((c.get("delta") or {}).get("reasoning_content") or "")) for c in choices)
                boundary = "protocol" if detail else _reasoning_buffer_boundary(data, detail)
                if detail and detail["reason"] not in protocol_seen and len(protocol_seen) < 8:
                    protocol_seen.add(detail["reason"])
                    _buffer_protocol_event(detail)
                pure = boundary is None
                now = time.monotonic()
                # These frames carry no output. Preserve wire order by flushing
                # held frames first, but keep eligibility for the next thought.
                # Do not re-enable a segment already exhausted by a size/time cap.
                if boundary == "metadata" or (pure and not chars):
                    if pending:
                        released("metadata" if boundary else "role", size, chars)
                        yield from pending
                        pending.clear()
                        pending_bytes = pending_chars = 0
                        started = None
                    if enabled:
                        skip("metadata" if boundary else "role")
                    yield raw
                    continue
                suspicious = len(self.reason.fast.phrases) >= 6
                observed = self.stall_buffer_signal and not observation_used
                if (enabled and not buffering and (observed or (suspicious and not previous_suspicion)) and pure and
                        not self.holder.get("ops_tool_seen") and not self.holder.get("ops_output_seen")):
                    buffering, started = True, now
                    pending_bytes, pending_chars = 0, 0
                    if observed:
                        observation_used = True
                        _loop_event("reasoning_buffer_observation")
                    else:
                        _loop_event("reasoning_buffer_suspicion")
                previous_suspicion = suspicious
                if buffering and pure and started is None:
                    started = now
                prepared = pending.prepare(raw) if buffering and pure else None
                storage_cost = (prepared[-1] if prepared is not None else size + pending.HEADER.size) if buffering and pure else 0
                reason = (boundary or
                          ("tool_seen" if self.holder.get("ops_tool_seen") else None) or
                          ("content_seen" if self.holder.get("ops_output_seen") else None) or
                          ("time" if started is not None and now-started >= 2 else None) or
                          ("chars" if pending_chars+chars > 8192 else None) or
                          ("bytes" if buffering and pure and not pending.fits(prepared) else None) or
                          ("frames" if buffering and pure and not pending.has_room(prepared) else None))
                if buffering and reason is None:
                    if not pending:
                        pending_frames, buffer_started_at = 0, time.perf_counter()
                        _loop_event("reasoning_buffer_entered")
                    pending_frames += 1
                    pending.append_prepared(prepared); pending_bytes += size; pending_chars += chars
                    continue
                if pending:
                    released(reason or "passthrough", size, chars, storage_cost)
                    yield from pending
                    pending.clear()
                elif enabled and reason:
                    skip(reason)
                buffering = False
                yield raw
            if pending:
                released("eof")
                yield from pending
        except Exception:
            # Transport failure is not safe recovery: preserve partial output
            # and let the adapter's existing explicit error path run.
            if pending:
                released("transport_error")
                yield from pending
            raise

    def _frames(self):
        iterator = iter(self.upstream)
        while True:
            if self._timed_out():
                yield self._failure("generation_timeout")
                return
            try:
                raw = next(iterator)
            except StopIteration:
                self._observe_stall(final=True)
                if self._timed_out():
                    yield self._failure("generation_timeout")
                elif self.mixed_stall is not None:
                    metrics = self.mixed_stall.feed("", final=True)
                    if metrics:
                        self.holder["loop_evidence"] = {"rule": "mixed-stall", "chars": self.reason.total_chars}
                        _loop_event("reasoning_loop_detected", "mixed-stall", self.reason.total_chars, metrics=metrics)
                        yield self._failure("reasoning_loop_detected")
                return
            except Exception:
                if self._timed_out():
                    yield self._failure("generation_timeout")
                    return
                raise
            if self._timed_out():
                yield self._failure("generation_timeout")
                return
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            data = strip_data_prefix(line)
            try:
                frame = json.loads(data)
            except (ValueError, TypeError):
                frame = {}
            choices = (frame.get("choices") or []) if isinstance(frame, dict) else []
            if isinstance(frame, dict) and frame.get("usage"):
                self.received_usage = frame["usage"]
            if ((isinstance(frame, dict) and frame.get("error")) or len(choices) > 1 or
                    any(c.get("index", 0) != 0 for c in choices)):
                self.recovery.block()
                self.holder["ops_tool_seen"] = True  # uncertain protocol: no replay
            for choice in choices:
                delta = choice.get("delta") or {}
                for field in ("content", "reasoning_content"):
                    value = str(delta.get(field) or "")
                    cjk = sum(1 for c in value if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
                    self.received_chars += len(value)
                    self.received_cjk += cjk
                    if field == "reasoning_content":
                        self.reason_chars += len(value)
                        self.reason_cjk += cjk
                calls = delta.get("tool_calls") or []
                legacy = delta.get("function_call") or {}
                if choice.get("finish_reason") and choice["finish_reason"] != "stop":
                    self.holder["ops_tool_seen"] = True
                protocol = self.protocol_tail + str(delta.get("reasoning_content") or "")
                if re.search(TAG_START, protocol):
                    self.holder["ops_tool_seen"] = True
                self.protocol_tail = protocol[-32:]
                if str(delta.get("content") or "").strip():
                    self.holder["ops_output_seen"] = True
                if (any(c.get("id") or (c.get("function") or {}).get("name") or
                        (c.get("function") or {}).get("arguments") for c in calls) or
                        (isinstance(legacy, dict) and (legacy.get("name") or legacy.get("arguments")))):
                    self.holder["ops_tool_seen"] = True
                if (str(delta.get("content") or "").strip() or
                        any(c.get("id") or (c.get("function") or {}).get("name") or
                            (c.get("function") or {}).get("arguments") for c in calls) or
                        (isinstance(legacy, dict) and (legacy.get("name") or legacy.get("arguments")))):
                    self.recovery.block()
                self.recovery.feed(delta.get("reasoning_content") or "")
                reason = self.reason_scrubber.feed(delta.get("reasoning_content") or "")
                content = self.content_scrubber.feed(delta.get("content") or "")
                self._observe_stall(reason, final=bool(choice.get("finish_reason")))
                leaked_loop = False
                if (str(self.holder.get("model") or "").lower().startswith("deepseek-") and
                        os.environ.get("WB_OPS_LEAKED_THINKING_GUARD", "1").lower() not in ("0", "false", "off")):
                    leaked_loop = self.leak.feed(content)
                    if choice.get("finish_reason"):
                        leaked_loop = self.leak.finish() or leaked_loop
                    if self.leak.detected and not self.leak_reported:
                        self.leak_reported = True
                        _loop_event("thinking_in_content")
                mixed_metrics = (self.mixed_stall.feed(reason, final=bool(choice.get("finish_reason")))
                                 if self.mixed_stall is not None else None)
                code = ("reasoning_loop_detected" if self.reason.feed(reason) or mixed_metrics else
                        "content_loop_detected" if self.content.feed(content) or leaked_loop else None)
                if code:
                    detector = self.reason if code == "reasoning_loop_detected" else (self.leak.guard if leaked_loop else self.content)
                    if leaked_loop:
                        _loop_event("leaked_thinking_loop_detected")
                    rule = "mixed-stall" if mixed_metrics and not self.reason.hit else detector.rule
                    self.holder["loop_evidence"] = {"rule": rule, "chars": detector.total_chars}
                    _loop_event(code, rule, detector.total_chars, metrics=mixed_metrics)
                    yield self._failure(code)
                    return
                if choice.get("finish_reason") and choice["finish_reason"] != "stop":
                    self.recovery.block()
                    self.holder["ops_tool_seen"] = True  # length/refusal/unknown terminal
                if choice.get("finish_reason") == "stop":
                    answer = self.recovery.answer()
                    self.recovery.block()
                    if answer:
                        self.holder["ops_output_seen"] = True
                        if self.content.feed(answer):
                            yield self._failure("content_loop_detected")
                            return
                        retry_bump("reasoning_answer_recovered")
                        repaired = {k: frame[k] for k in ("id", "created", "model") if k in frame}
                        repaired["choices"] = [{"index": 0, "delta": {"content": answer}, "finish_reason": None}]
                        yield ("data: " + json.dumps(repaired, ensure_ascii=False) + "\n\n").encode("utf-8")
            yield raw

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.leak_reported:
            _loop_event("thinking_content_summary",
                        metrics={"content_chars": self.received_chars-self.reason_chars,
                                 "closed_boundary": int(self.leak.state == "done"),
                                 "loop_hit": int(self.leak.guard.hit or self.content.hit)})
        if self.timer is not None:
            self.timer.cancel()
            self.timer.join()
        _close_upstream(self.upstream)


class _ProbedStream:
    def __init__(self, raw, iterator, prefix):
        self.raw = raw
        self.iterator = itertools.chain(prefix, iterator)

    def __iter__(self):
        return self.iterator

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def close(self):
        _close_upstream(self.raw)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def probe_first_frame(upstream, retry_call=None, tried=None, deadline_at=None, require_frame=False):
    """Validate and replay the first SSE data frame before sending HTTP 200."""
    if isinstance(upstream, _ProbedStream):
        return upstream, None
    excluded = list(tried or [])
    account = None
    deadline_at = deadline_at or time.monotonic() + FIRST_FRAME_DEADLINE
    for attempt in range(FIRST_FRAME_MAX_ATTEMPTS):
        timer = None
        try:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("first-frame deadline exceeded")
            sock = _stream_socket(upstream)
            if sock is not None:
                sock.settimeout(remaining)
            timer = threading.Timer(remaining, _interrupt_upstream, args=(upstream,))
            timer.daemon = True
            timer.start()
            iterator = iter(upstream)
            prefix = []
            preamble_bytes = 0
            for raw in iterator:
                if time.monotonic() >= deadline_at:
                    raise TimeoutError("first-frame deadline exceeded")
                prefix.append(raw)
                preamble_bytes += len(raw)
                if len(prefix) > 512 or preamble_bytes > 262144:
                    raise ValueError("upstream sent excessive preamble before its first frame")
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                data = strip_data_prefix(line)
                if not data:
                    continue
                if data == "[DONE]":
                    raise ValueError("upstream ended before its first response frame")
                frame = json.loads(data)
                if isinstance(frame, dict) and isinstance(frame.get("error"), dict):
                    error = frame["error"]
                    if str(error.get("code")) in ("403", "11140"):
                        rejected = ContentRejected(detail=str(error.get("message") or "content rejected"))
                        rejected.account_uid = excluded[-1] if excluded else None
                        raise rejected
                if not isinstance(frame, dict) or frame.get("error"):
                    raise ValueError("upstream returned an error as its first frame")
                if not frame.get("choices"):
                    continue
                timer.cancel()
                if sock is not None:
                    sock.settimeout(upstream_timeouts()[1])
                return _ProbedStream(upstream, iterator, prefix), account
            # Socket shutdown at the deadline may produce EOF rather than an
            # OSError (notably on Linux). An expired probe is never successful.
            if time.monotonic() >= deadline_at:
                raise TimeoutError("first-frame deadline exceeded")
            if retry_call is None and not prefix and not require_frame:
                return _ProbedStream(upstream, iterator, []), account
            raise ValueError("upstream closed without a response frame")
        except ContentRejected:
            _close_upstream(upstream)
            raise
        except Exception:
            _close_upstream(upstream)
            if retry_call is None:
                raise
            if attempt + 1 >= FIRST_FRAME_MAX_ATTEMPTS or time.monotonic() >= deadline_at:
                retry_bump("first_frame_giveup")
                return None, None
            retry_bump("first_frame")
            if getattr(retry_call, "_ops_deadline", False):
                upstream, account = retry_call(excluded, deadline_at=deadline_at)
            else:
                upstream, account = retry_call(excluded)
            if upstream is None:
                retry_bump("first_frame_giveup")
                return None, None
            uid = getattr(account, "uid", None)
            if uid and uid not in excluded:
                excluded.append(uid)
        finally:
            if timer is not None:
                timer.cancel()
    return None, None


def _loop_recovery_body(body):
    """Opt-in experiment: append an ordinary user hint to a detached request.

    Keep all history and reasoning untouched. Decline uncertain tool pairing;
    this is not a context compressor or a bypass for the replay safety gates.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    pending = set()
    for message in messages:
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if role == "assistant":
            if pending or message.get("function_call"):
                return None
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list):
                return None
            for call in calls:
                uid = call.get("id") if isinstance(call, dict) else None
                if not isinstance(uid, str) or not uid or uid in pending:
                    return None
                pending.add(uid)
        elif role == "tool":
            uid = message.get("tool_call_id")
            if not isinstance(uid, str) or uid not in pending:
                return None
            pending.remove(uid)
        elif pending or role not in ("system", "developer", "user"):
            return None
    if pending or messages[-1].get("role") not in ("user", "tool"):
        return None
    result = copy.deepcopy(body)
    result["messages"].append({"role": "user", "content": (
        "The previous generation was interrupted for repetitive planning. "
        "Continue the original task using the available evidence. Complete the "
        "answer or issue a necessary tool call instead of repeatedly announcing "
        "the next action. Preserve the original constraints, do not invent tool "
        "results, and state any missing information or uncertainty.")})
    return result


def _make_retry_call(body, session_key, realm):
    def retry_call(excluded, deadline_at=None, recovery_hint=False):
        request_body = body
        if recovery_hint:
            candidate = _loop_recovery_body(body)
            if candidate is not None:
                request_body = candidate
                _loop_event("retry_hint_applied")
            else:
                _loop_event("retry_hint_skipped_shape")
        # Public v1.6.17 API returns effort as a third item. Recovery keeps
        # the same request effort; its internal callback contract stays a pair.
        return open_upstream(request_body, session_key=session_key, target_realm=realm,
                             exclude_uids=excluded, deadline_at=deadline_at)[:2]
    retry_call._ops_deadline = True
    retry_call._ops_recovery_hint = True
    return retry_call


def clean_chunk(raw):
    """Drop the empty noise fields the WorkBuddy gateway pads deltas with."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    changed = False
    if isinstance(obj.get("usage"), dict):
        before = dict(obj["usage"])
        normalize_usage_cache_aliases(obj["usage"])
        if obj["usage"] != before:
            changed = True
    for choice in obj.get("choices") or []:
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        # PATCHED-BY-OPS: 原判断 `if not delta.get("function_call")` 对
        # {"name":"","arguments":""} 为假（非空 dict 是真值），空占位删不掉。
        # 改为显式检查：name 与 arguments 均空才视为占位噪音。
        if isinstance(delta.get("reasoning_content"), str):
            safe = strip_dsml_from_text(delta["reasoning_content"])
            if safe != delta["reasoning_content"]:
                delta["reasoning_content"] = safe
                changed = True
        fc = delta.get("function_call")
        if fc is not None:
            fc_empty = False
            if isinstance(fc, dict):
                fc_empty = not fc.get("name") and not fc.get("arguments")
            else:
                fc_empty = not fc
            if fc_empty:
                delta.pop("function_call", None)
                changed = True
        if isinstance(delta.get("tool_calls"), list) and not delta["tool_calls"]:
            delta.pop("tool_calls")
            changed = True
        for key in NOISE_KEYS:
            if key in delta and not delta.get(key):
                delta.pop(key)
                changed = True
        if not delta and not choice.get("finish_reason"):
            return ""
    return json.dumps(obj, ensure_ascii=False) if changed else raw
def _strip_empty_fc(obj):
    """PATCHED-BY-OPS: 递归剔除空 function_call 占位（Responses/chat 通用）。"""
    changed = False
    if isinstance(obj, dict):
        fc = obj.get("function_call")
        if isinstance(fc, dict) and not fc.get("name") and not fc.get("arguments"):
            obj.pop("function_call", None)
            changed = True
        tc = obj.get("tool_calls")
        if isinstance(tc, list) and not tc:
            obj.pop("tool_calls", None)
            changed = True
        for v in list(obj.values()):
            if _strip_empty_fc(v):
                changed = True
    elif isinstance(obj, list):
        for v in obj:
            if _strip_empty_fc(v):
                changed = True
    return changed
def clean_responses_frame(frame):
    """PATCHED-BY-OPS: 清洗 Responses SSE 帧（bytes）。
    输入 b'event: x\ndata: {...}\n\n'；只改写 data: 行的 JSON，
    event: 行原样保留。解析失败原样返回（不破坏未知格式）。
    """
    if not frame:
        return frame
    try:
        text = frame.decode("utf-8")
    except Exception:
        return frame
    out, changed = [], False
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("data:"):
            payload = st[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    if _strip_empty_fc(obj):
                        line = "data: " + json.dumps(obj, ensure_ascii=False)
                        changed = True
                except Exception:
                    pass
        out.append(line)
    return ("\n".join(out) + "\n\n").encode("utf-8") if changed else frame
def normalize_roles(messages):
    """Map role names the upstream rejects onto ones it accepts.
    WorkBuddy only knows system / user / assistant / tool. OpenAI's newer
    "developer" role (used by the Codex CLI and current SDKs) is the same thing
    as "system", but sending it verbatim fails with code 11128.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        item = m
        if m.get("role") == "developer":
            item = dict(m)
            item["role"] = "system"
        out.append(item)
    return out
# ---------------------------------------------------------------------------
# Fingerprint Sanitization (immunizes against Codex / Claude Code WAF patterns)
# ---------------------------------------------------------------------------
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",
)
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("11128", "11-128"),
)
SANITIZE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\r\n]*;?\s*")
SANITIZE_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
SANITIZE_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\r\n]*;?\s*")
# WorkBuddy upstream returns 11128 ("Illegal API invocation from an unapproved
# channel") when this exact OmO identity fingerprint appears as a contiguous
# substring in a system message. A/B tests show the match is case-insensitive,
# survives surrounding prefix/suffix text, and stops matching when the phrase
# structure is changed. Rewrite only this confirmed fingerprint, leaving the
# agent identity and behaviour intact while dropping the framework attribution.
SANITIZE_OMO_JUNIOR_RE = re.compile(
    r"Sisyphus-Junior - Focused executor from OhMyOpenCode", re.IGNORECASE
)
def has_fingerprint(text):
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    return bool(SANITIZE_BARE_HDR_RE.search(text) or SANITIZE_OMO_JUNIOR_RE.search(text))
def sanitize_text(text):
    if not isinstance(text, str) or not text:
        return text
    if not has_fingerprint(text):
        return text
    # Keep the rewrite deliberately narrow: do not globally remove
    # "OhMyOpenCode" or "Sisyphus-Junior", because either token alone is
    # accepted by the upstream. Only the confirmed contiguous fingerprint is
    # neutralized.
    text = SANITIZE_OMO_JUNIOR_RE.sub("Sisyphus-Junior - Focused executor", text)
    for old, new in SANITIZE_REWRITES:
        text = text.replace(old, new)
    text = SANITIZE_HDR_RE.sub("", text)
    if "cc_" in text:
        prev = ""
        while prev != text:
            prev = text
            text = SANITIZE_KV_RE.sub("", text)
    text = SANITIZE_BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()
def sanitize_content(content):
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text" and "text" in part:
                p = dict(part)
                p["text"] = sanitize_text(p["text"])
                out.append(p)
            else:
                out.append(part)
        return out
    return content
def sanitize_tool_calls(tool_calls):
    if not isinstance(tool_calls, list):
        return tool_calls
    out = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            item = dict(tc)
            fn = item.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn = dict(fn)
                fn["arguments"] = sanitize_text(fn["arguments"])
                item["function"] = fn
            out.append(item)
        else:
            out.append(tc)
    return out
def sanitize_messages(messages):
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            item = dict(m)
            if "content" in item:
                item["content"] = sanitize_content(item["content"])
            if isinstance(item.get("reasoning_content"), str):
                item["reasoning_content"] = strip_dsml_from_text(
                    sanitize_text(item["reasoning_content"]))
            if isinstance(item.get("reasoning"), str):
                item["reasoning"] = strip_dsml_from_text(sanitize_text(item["reasoning"]))
            if "tool_calls" in item:
                item["tool_calls"] = sanitize_tool_calls(item["tool_calls"])
            out.append(item)
        else:
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Tool-call pairing repair
# ---------------------------------------------------------------------------
DEEPSEEK_V41_MODEL = "deepseek-v4.1-flash"
DEEPSEEK_TOOL_IMAGE_PLACEHOLDER = (
    "[image omitted: DeepSeek Chat accepts images only in user messages]"
)
DEEPSEEK_REASONING_HISTORY_LIMIT = int(
    os.environ.get("WB_DEEPSEEK_REASONING_HISTORY_LIMIT", 65536)
)
DEEPSEEK_REASONING_PLACEHOLDER = " "


def sanitize_deepseek_tool_images(messages, model):
    """Retain the NAS-only V4.1 tool-image conversion, not user images."""
    model_id = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if model_id != DEEPSEEK_V41_MODEL or not isinstance(messages, list):
        return messages
    out = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            out.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        changed = False
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                parts.append({"type": "text", "text": DEEPSEEK_TOOL_IMAGE_PLACEHOLDER})
                changed = True
            else:
                parts.append(part)
        if not changed:
            out.append(message)
            continue
        item = dict(message)
        item["content"] = parts
        out.append(item)
    return out


def clean_deepseek_stalled_history(messages, model):
    """Remove confirmed neutral repetition only from outgoing V4.1 traces.

    Client history and visible text/tool pairs are never modified. Both trace
    aliases are cleaned so one cannot restore the other's repeated phrases.
    """
    model_id = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if (model_id != DEEPSEEK_V41_MODEL or not isinstance(messages, list) or
            os.environ.get("WB_OPS_MIXED_STALL_HISTORY", "1").lower() in ("0", "false", "off")):
        return messages
    out, removed = list(messages), 0
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        item = message
        cleaned_traces = {}
        for field in ("reasoning_content", "reasoning"):
            trace = message.get(field)
            if not isinstance(trace, str):
                continue
            if trace not in cleaned_traces:
                cleaned_traces[trace] = clean_stalled_reasoning(trace)
            cleaned = cleaned_traces[trace]
            if cleaned != trace:
                if item is message:
                    item = dict(message)
                item[field] = cleaned if cleaned.strip() else DEEPSEEK_REASONING_PLACEHOLDER
                removed += max(0, len(trace) - len(item[field]))
        out[index] = item
    if removed:
        _loop_event("history_stall_removed", chars=removed)
    return out


def compact_deepseek_reasoning_history(messages, model):
    """Retain the previously verified NAS V4.1 old-reasoning budget."""
    model_id = str(model or "").strip().lower().rsplit("/", 1)[-1]
    if model_id != DEEPSEEK_V41_MODEL or not isinstance(messages, list):
        return messages
    try:
        budget = max(0, int(DEEPSEEK_REASONING_HISTORY_LIMIT))
    except (TypeError, ValueError):
        budget = 65536
    total = sum(
        len(m.get("reasoning_content") or "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant"
        and isinstance(m.get("reasoning_content"), str)
    )
    if total <= budget:
        return messages
    out = list(messages)
    kept = 0
    for index in range(len(out) - 1, -1, -1):
        message = out[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        trace = message.get("reasoning_content")
        if not isinstance(trace, str) or not trace:
            continue
        if kept + len(trace) <= budget:
            kept += len(trace)
            continue
        item = dict(message)
        item["reasoning_content"] = DEEPSEEK_REASONING_PLACEHOLDER
        if "reasoning" in item:
            item["reasoning"] = DEEPSEEK_REASONING_PLACEHOLDER
        out[index] = item
    return out


def _empty_content(value):
    """True when an assistant message carries no content at all.

    None / "" / [] all mean "no content": clients disagree on which empty
    shape they emit, and a batch split into adjacent assistant messages must
    merge for either shape (a missed [] was the original 11148 case).
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, list):
        return len(value) == 0
    return False


def _merge_reasoning_content(dst, src):
    """Fold src's reasoning_content into dst, newline-joined when both exist."""
    rc = src.get("reasoning_content")
    if not isinstance(rc, str) or not rc:
        return
    prev = dst.get("reasoning_content")
    if isinstance(prev, str) and prev:
        dst["reasoning_content"] = prev + "\n" + rc
    else:
        dst["reasoning_content"] = rc


def merge_adjacent_tool_calls(messages):
    """Merge back-to-back assistant tool_calls messages into one.

    Some OpenAI-compatible agent clients replay a parallel batch as several
    adjacent assistant messages, each carrying one tool_call. DeepSeek-family
    upstreams answer 400 / code 11148 (tool_call_sequence_broken) for that
    shape and retire the conversation, because every retry replays the same
    history and switching accounts cannot help. Merging the declarations
    restores the shape the upstream accepts.

    Conditions are strict, so no semantics are invented:
      - the messages must be adjacent;
      - the trailing message must have empty content (None/""/[]);
      - the leading message must already be an assistant with tool_calls.

    A second form folds a plain assistant's string content into a preceding
    tool_calls assistant that has no content of its own (the same split batch
    replayed in the other order). reasoning_content is preserved either way,
    because DeepSeek multi-turn thinking requires it back.
    """
    if not isinstance(messages, list) or len(messages) < 2:
        return messages, False
    out = []
    changed = False
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        if m.get("role") == "assistant" and out:
            prev = out[-1] if isinstance(out[-1], dict) else None
            if prev is not None and prev.get("role") == "assistant":
                tcs = m.get("tool_calls")
                prev_calls = prev.get("tool_calls")
                # Form 1: this message declares tool calls and has no content.
                if (isinstance(tcs, list) and tcs
                        and _empty_content(m.get("content"))
                        and isinstance(prev_calls, list) and prev_calls):
                    prev["tool_calls"] = prev_calls + tcs
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
                # Form 2: plain string content folds into a preceding
                # content-less tool_calls assistant.
                if ("tool_calls" not in m
                        and isinstance(m.get("content"), str) and m["content"]
                        and isinstance(prev_calls, list) and prev_calls
                        and _empty_content(prev.get("content"))):
                    prev["content"] = m["content"]
                    _merge_reasoning_content(prev, m)
                    changed = True
                    continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def repack_tool_result_blocks(messages):
    """Keep a tool_calls batch and its results adjacent.

    The upstream requires the role:"tool" results to follow the assistant
    message that requested them with nothing in between. Codex's
    image_resize_notice, for one, arrives as a developer message right after a
    tool output; with parallel calls it lands between two results, the pairing
    reads as broken and the upstream rejects the whole request (code 11148),
    retiring the conversation. This only reorders: same results, same relative
    order, the intruders moved behind the batch.
    """
    if not isinstance(messages, list) or len(messages) < 3:
        return messages, False
    out = []
    changed = False
    i = 0
    while i < len(messages):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "assistant":
            out.append(m)
            i += 1
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            out.append(m)
            i += 1
            continue
        want = set()
        for tc in calls:
            if isinstance(tc, dict):
                tid = tc.get("id")
                if isinstance(tid, str) and tid:
                    want.add(tid)
        out.append(m)
        i += 1
        results = []
        between = []
        saw_non_tool = False
        while i < len(messages):
            mm = messages[i]
            if not isinstance(mm, dict):
                break
            role = mm.get("role")
            if role == "tool":
                tid = mm.get("tool_call_id")
                if not (isinstance(tid, str) and tid in want):
                    break
                results.append(mm)
                if saw_non_tool:
                    changed = True
                i += 1
                continue
            if not results:
                break
            # A following assistant.tool_calls opens the next batch: it must go
            # back to the outer loop, or its own results never get repacked.
            if role == "assistant" and isinstance(mm.get("tool_calls"), list) \
                    and mm["tool_calls"]:
                break
            between.append(mm)
            saw_non_tool = True
            i += 1
        out.extend(results)
        out.extend(between)
    if not changed:
        return messages, False
    return out, True


def cleanup_orphan_tool_calls(messages):
    """Drop tool calls that have no result, and results that have no call.

    A failed tool call (bad arguments, timeout, unknown tool) leaves the client
    with an assistant tool_calls entry it can never answer: the result message
    is never written, yet the entry rides along with the history on every later
    turn and the upstream rejects each one (code 11148), so a single failed
    call can retire a whole conversation. Both sides are trimmed against the
    same set of ids, so no half-pairing can survive the repair.
    """
    if not isinstance(messages, list) or not messages:
        return messages, False
    call_ids = set()
    result_ids = set()
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            calls = m.get("tool_calls")
            if isinstance(calls, list):
                for tc in calls:
                    if isinstance(tc, dict):
                        tid = tc.get("id")
                        if isinstance(tid, str) and tid:
                            call_ids.add(tid)
    if not call_ids and not result_ids:
        return messages, False
    keep = call_ids & result_ids
    changed = False
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        calls = m.get("tool_calls")
        if not isinstance(calls, list) or not calls:
            continue
        kept = [tc for tc in calls
                if isinstance(tc, dict) and isinstance(tc.get("id"), str)
                and tc["id"] in keep]
        if len(kept) == len(calls):
            continue
        changed = True
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls", None)
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if not (isinstance(tid, str) and tid in keep):
                changed = True
                continue
        out.append(m)
    if not changed:
        return messages, False
    return out, True


def is_truncated_arguments(raw):
    """True when a non-empty tool-arguments string is not valid JSON.

    A stream cut off by max_tokens or a dropped connection leaves the last
    tool call with half-written JSON. An empty/whitespace string is a legal
    no-argument tool, and any parseable JSON (including null/scalars/arrays)
    is the model's own output for the client to validate - only non-empty
    unparsable strings count as truncation.
    """
    if not isinstance(raw, str):
        return False
    trimmed = raw.strip()
    if not trimmed:
        return False
    try:
        json.loads(trimmed)
        return False
    except Exception:
        return True


def drop_truncated_tool_calls(calls):
    """Return the tool calls whose arguments are not half-written JSON."""
    if not isinstance(calls, list):
        return calls
    kept = []
    for call in calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        fn = call.get("function")
        if isinstance(fn, dict) and is_truncated_arguments(fn.get("arguments")):
            continue
        kept.append(call)
    return kept


# ---------------------------------------------------------------------------
# DeepSeek Multi-turn Consistency: reasoning_content backfill
# ---------------------------------------------------------------------------
# Upstream (code 11155 "the reasoning content from the previous turn must be
# passed back in thinking mode") requires every assistant message to carry a
# `reasoning_content` string while thinking is on. Two halves gate the fix,
# mirroring the official client's ReasoningContentBackfillRule:
#   - thinkingEnabled: deepseek + thinking enabled -> always backfill, even
#     when a third-party client dropped reasoning entirely (this was the bug:
#     only the hasTrace half existed, so zero-trace histories were forwarded
#     untouched and rejected).
#   - hasTrace: any existing reasoning trace -> backfill regardless of the
#     thinking flag.
# Upstream also validates len(reasoning) > 0, so an empty placeholder is not
# enough on its own: `reasoning` is mirrored with a non-empty value.
def backfill_reasoning_content(messages, model, thinking_enabled=None):
    if not model or not str(model).lower().startswith("deepseek"):
        return _scrub_dsml_reasoning_messages(messages)
    if thinking_enabled is None:
        thinking_enabled = False
    has_trace = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        reasoning = m.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not thinking_enabled and not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            if os.environ.get("WB_OPS_FAILED_REASONING_CLEANUP", "1").lower() not in ("0", "false", "off"):
                for field in ("reasoning_content", "reasoning"):
                    value = item.get(field)
                    cleaned = _FAILED_REASONING.clean(value)
                    if cleaned != value:
                        item[field] = cleaned
                        _loop_event("history_prefix_removed", chars=len(value) - len(cleaned))
            rc = item.get("reasoning_content")
            if not isinstance(rc, str):
                # Non-string (null/number/absent) counts as missing, matching
                # the official `typeof !== "string"` check.
                legacy = item.get("reasoning")
                rc = legacy if isinstance(legacy, str) else ""
            rc = strip_dsml_from_text(rc)
            item["reasoning_content"] = rc
            # Mirror onto `reasoning` with a non-empty value: upstream rejects
            # an empty/absent reasoning, while a whitespace placeholder passes
            # its length check and carries no model-visible semantics.
            existing = item.get("reasoning")
            if isinstance(existing, str):
                existing = strip_dsml_from_text(existing)
                item["reasoning"] = existing
            if not (isinstance(existing, str) and existing):
                item["reasoning"] = rc if rc else " "
            out.append(item)
        else:
            out.append(m)
    return out
# ---------------------------------------------------------------------------
# Tool & Tool Choice Normalization (avoids code 11101 on object tool_choice)
# ---------------------------------------------------------------------------
def normalize_tool_choice(obj):
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            # 这里曾经把 tools/functions 一起删掉，那正是 Agent 死循环的成因：
            # 工具声明没了，模型拿不到函数签名、又没有结构化工具通道，却仍被要求
            # 完成任务，于是把调用降级成 DSML / 伪 JSON 文本塞进 content
            # （tool_calls 为空、finish_reason=stop）。客户端解析不到调用只能再
            # 追问一轮，模型又重复一遍 "I'll do it"，上下文每轮 +2 条消息、token
            # 线性膨胀，直到撑爆窗口或用户手动断开。
            #
            # tool_choice="none" 的语义是「本轮不许调用工具」，这层意思由
            # tool_choice 字段本身表达就够了，不需要抹掉能力声明。
            # 上游把 tool_choice 声明为 string（发对象会 11101），所以保持字符串
            # 原样透传，同时保留 tools。
            #
            # 取舍：实测本上游并不真正遵守 tool_choice="none"（保留 tools 后它
            # 仍返回 tool_calls）。但对比两条路 —— 删 tools 会让模型输出不可解析
            # 的文本、Agent 原地空转；留 tools 则走正常 tool_calls 通道，客户端能
            # 正常执行与回填 —— 后者明显更好。确实需要禁止调用时，客户端不传
            # tools 即可。
            obj["tool_choice"] = "none"
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            # 同上：保留 tools 声明。上游只认字符串，对象形式必须降级成
            # "none"，否则 11101。
            obj["tool_choice"] = "none"
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)
def normalize_tools(obj):
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Wrap top-level name tool definition into Chat Completions function schema
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm
# ---------------------------------------------------------------------------
# DeepSeek DSML Tool Calls Fallback Parser
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)
DSML_INVOKE_RE = re.compile(TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>", re.DOTALL)
DSML_PARAM_RE = re.compile(TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>", re.DOTALL)
def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean
def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass
# ---------------------------------------------------------------------------
# 模型封锁表
#
# 背景：客户端除了使用者的对话，还会自己发背景请求（记忆整理、自动复核等）。
# 这些请求不经过模型选单，而是直接使用目录上的模型 ID，因此可能在使用者
# 没有实际操作时，用付费模型消耗额度。
#
# 对策（选用）：把要拒绝的模型填进 ALLOWED_MODELS / BANNED_SUBSTRING /
#               EXTRA_BANNED；命中的请求在本机直接回 400，完全不碰上游。
#               预设全部为空 = 不封锁任何模型，行为与原版相同。
#
# 调整方式：
#   要放行某个模型 -> 加进 ALLOWED_MODELS 或 ALLOWED_PREFIXES
#   要连非 gpt 的模型一起挡 -> 加进 EXTRA_BANNED
# ---------------------------------------------------------------------------

# 允许放行的模型（你要用的）
ALLOWED_MODELS = {
    # 预设不封锁任何模型；填入模型 id 即可只放行这些
}

# 允许前缀：涵盖 -high / -preview / [1M] 等变体
ALLOWED_PREFIXES = ()

# 封锁字串：模型名里含这个就拒绝
BANNED_SUBSTRING = ""

# 额外封锁的内部模型（不在 gpt- 前缀内，但也会烧点）
EXTRA_BANNED = set()


def is_model_banned(model):
    """True 表示这个模型名不该被送去上游。

    规则：ALLOWED_MODELS / ALLOWED_PREFIXES 命中就放行；其余只要命中
    BANNED_SUBSTRING 或 EXTRA_BANNED 就拒绝，没命中则照常送往上游。
    三个设定预设都是空的，所以预设不封锁任何模型。
    """
    if not model:
        return False
    m = str(model).strip().lower()
    # 白名单优先（含 -high / -preview / [1M] 这类变体）
    if m in ALLOWED_MODELS:
        return False
    if any(m.startswith(a) for a in ALLOWED_PREFIXES):
        return False
    # 命中封锁字串就拒绝
    if BANNED_SUBSTRING and BANNED_SUBSTRING in m:
        return True
    # 其他已知会烧点的内部模型
    if m in EXTRA_BANNED:
        return True
    return False


# ---------------------------------------------------------------------------
# 背景请求拦截
#
# Codex App 除了使用者的对话，还会自己发背景请求（记忆整理、环境建议、自动复核…）。
# 这些请求不经过模型选单，所以单靠模型白名单挡不住 —— 它们可能直接用目录上
# 的付费模型（例如 gpt-6-astra 这类），在使用者没有实际操作时照样消耗额度。
#
# Codex 会在 client_metadata 里带 x-codex-turn-metadata，内容像：
#   {"request_kind":"memory","thread_source":"memory_consolidation",
#    "turn_trigger":"memory_consolidation"}
# 这里就靠这个标记判断：命中背景关键字 -> 本地直接拒绝，不碰上游、不扣点。
# ---------------------------------------------------------------------------

# 要不要拦截背景请求（False = 全部放行，维持原行为）
BLOCK_BACKGROUND_REQUESTS = False

# 命中任一关键字就视为背景请求（不分大小写、子字串比对）
BACKGROUND_TRIGGER_KEYWORDS = (
    "memory_consolidation",
    "memory-write",
    "memory_write",
    "memorywriting",
    "ambient",
    "suggestion",
    "auto_review",
    "auto-review",
    "autoreview",
    "title",
    "compaction",
    "compact",
    "summariz",
)


# Thread sources that belong to a job the client started on its own. A
# compaction request carries one of these when the client triggered it, and the
# user's own thread when the operator pressed "compact the context" - so the
# source has to be read before the keyword list, where "compaction" matches
# both and would otherwise refuse the button.
BACKGROUND_THREAD_SOURCES = (
    "memory_consolidation",
    "memory",
    "ambient",
    "suggestion",
    "auto_review",
    "autoreview",
    "title",
)


def turn_metadata_fields(payload):
    """Flatten the request_kind / turn_trigger / thread_source hints we get.

    Codex sends them either as plain client_metadata keys or as a JSON string
    under a metadata key of its own, so both shapes are read. Returns {} when
    the payload carries none of them.
    """
    if not isinstance(payload, dict):
        return {}
    meta = payload.get("client_metadata")
    if not isinstance(meta, dict):
        return {}

    # 收集所有可能的来源/触发栏位
    fields = {}
    for key, value in meta.items():
        if isinstance(value, str) and value.strip().startswith("{"):
            try:
                inner = json.loads(value)
            except Exception:
                inner = None
            if isinstance(inner, dict):
                for k in ("request_kind", "turn_trigger", "thread_source", "kind", "trigger", "source"):
                    if k in inner:
                        fields[k] = inner[k]
        if key in ("request_kind", "turn_trigger", "thread_source"):
            fields[key] = value
    return fields


def is_compaction_request(payload):
    """True for the operator's own "compact the context" request.

    request_kind=compaction carries the same word as the background keyword, but
    this request is one the user asked for: the client sends it on the user's
    thread, while a compaction the client started by itself names the job that
    started it. Refusing this one takes the context-compaction button away.
    """
    fields = turn_metadata_fields(payload)
    kind = str(fields.get("request_kind") or "").strip().lower()
    if "compact" not in kind:
        return False
    source = str(fields.get("thread_source") or "").strip().lower()
    return source not in BACKGROUND_THREAD_SOURCES


def background_request_reason(payload):
    """若这是 Codex 自己发的背景请求，回传说明字串；否则回传 ""。

    只看 client_metadata，不碰讯息内容。
    """
    fields = turn_metadata_fields(payload)
    if not fields:
        return ""

    if is_compaction_request(payload):
        return ""

    blob = " ".join(str(v) for v in fields.values()).lower()
    for kw in BACKGROUND_TRIGGER_KEYWORDS:
        if kw in blob:
            return "%s=%s" % (
                ",".join(sorted(fields.keys())),
                ",".join(str(fields[k]) for k in sorted(fields)),
            )
    return ""


def client_effort_of(body):
    """The effort the request itself carries, under either client spelling.

    build_upstream_body() reads both "reasoning_effort" and "reasoningEffort",
    and only fills in the model default for the models it injects for - so a
    camelCase request keeps the camelCase key, and a request to a model the
    catalog pins to one level carries no effort at all.
    """
    if not isinstance(body, dict):
        return None
    return body.get("reasoning_effort") or body.get("reasoningEffort")


def upstream_effort_of(body, model=None):
    """The reasoning effort a request actually runs at, or None when unknown.

    Resolved the way the upstream will apply it:

      - a model the catalog pins to one level (reasoning.effort, no
        supportedEfforts) always runs there: the picker offers no choice for it,
        so a value the request carries anyway does not change the answer;
      - a request that switched thinking off, or asked for "none", ran without
        reasoning and nothing below overrides that;
      - otherwise the client's own value wins, under either spelling;
      - otherwise the model's declared defaultEffort applies.

    Reading the body alone is not enough for the last two: the gateway only
    writes an effort into the body for the models it injects for, so a plain
    request to a pinned model would otherwise be reported as "no effort".
    """
    given = client_effort_of(body)
    if model:
        fixed = model_fixed_effort(model)
        if fixed:
            return fixed
    thinking = (body or {}).get("thinking") if isinstance(body, dict) else None
    if isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled":
        return "none"
    if str(given or "").strip().lower() == "none":
        return "none"
    if given:
        return given
    return model_default_effort(model) if model else None


def background_request_message(reason):
    return ("這是客戶端自己發的背景請求（%s），本機代理已擋下，"
            "避免在沒有實際操作時消耗上游額度。"
            "要放行請把 wb_proxy.py 的 BLOCK_BACKGROUND_REQUESTS 改成 False。"
            % reason)


def banned_model_message(model):
    allowed = "、".join(sorted(ALLOWED_MODELS))
    return ("模型 %s 已被本機代理封鎖（依 ALLOWED_MODELS / BANNED_SUBSTRING 設定）。"
            "目前允許：%s。要放行請編輯 wb_proxy.py 的 ALLOWED_MODELS。"
            % (model, allowed))


def key_model_message(entry, model):
    """Explain a per-key model restriction the same way the global ban does."""
    name = (entry or {}).get("name") or "未命名"
    allowed = "、".join((entry or {}).get("models") or []) or "-"
    asked = str(model or "").strip() or "(未指定模型)"
    return ("API Key「%s」的模型限制不允許 %s。該 Key 目前允許：%s。"
            "請在看板「設置」頁修改這個 Key 的模型限制，或改用允許該模型的 Key。"
            % (name, asked, allowed))


# Process-memory degradation window: content-blocked passthrough/append
# traffic switches to the minimal neutral prompt until the next 00:00 CST
# (panel degrade.go). Restarts clear it, which is fine - the next rejection
# re-triggers it.
PROMPT_DEGRADE = wb_prompt.DegradeGate()


def prompt_mode_config():
    """Prompt mode settings, fail-open to passthrough on any read error."""
    try:
        cfg = wb_settings.prompt_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        cfg = {"mode": "passthrough", "file": ""}
    return cfg


def prompt_degrade_enabled():
    """Changing client instructions on HTTP 403 requires explicit opt-in."""
    return os.environ.get("WB_OPS_ALLOW_PROMPT_DEGRADE", "0").strip() == "1"


def apply_prompt_mode(messages):
    """Apply the configured prompt mode to one request's messages.

    passthrough is the legacy behaviour (client system prompts ride through);
    custom/append are opt-in. While the degrade window is active, passthrough
    and append switch to the minimal neutral prompt; custom never degrades.
    An unreadable prompt file fails open to passthrough rather than blocking.
    """
    cfg = prompt_mode_config()
    mode = cfg.get("mode") or "passthrough"
    degraded = prompt_degrade_enabled() and PROMPT_DEGRADE.active()
    if mode in ("custom", "append"):
        try:
            text = wb_prompt.load_prompt(mode, cfg.get("file"))
        except Exception:
            text = ""
        if text:
            return wb_prompt.apply_mode(messages, mode, text, degraded=degraded)
        return messages
    if degraded:
        return wb_prompt.apply_mode(messages, mode, "", degraded=True)
    return messages


def build_upstream_body(payload):
    model = payload.get("model") or ""
    # Resolve the effective thinking state before the backfill below: while
    # thinking is on, the upstream requires reasoning_content on every
    # assistant message, whether or not the client kept a reasoning trace.
    thinking = payload.get("thinking")
    thinking_type = ""
    if isinstance(thinking, dict):
        thinking_type = str(thinking.get("type") or "").strip().lower()
    effort = payload.get("reasoning_effort") or payload.get("reasoningEffort")
    thinking_enabled = False
    if str(model).lower().startswith("deepseek"):
        if thinking_type == "enabled":
            thinking_enabled = True
        elif thinking_type != "disabled" and str(effort or "").strip().lower() != "none":
            thinking_enabled = True
    messages = normalize_roles(payload.get("messages") or [])
    # Prompt mode runs before sanitize/backfill so the gateway prompt is the
    # one the upstream sees, with the client's fingerprint-y system text gone.
    messages = apply_prompt_mode(messages)
    messages = sanitize_messages(messages)
    messages = sanitize_deepseek_tool_images(messages, model)
    messages = backfill_reasoning_content(
        messages, model, thinking_enabled=thinking_enabled
    )
    if not messages or (messages[0].get("role") != "system"):
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
    body = dict(payload)
    # Private request markers ride along on the chat body for the Responses
    # path (the namespace map, the local-web-tools flag). They are not part of
    # the upstream protocol, so drop them here rather than trusting the
    # upstream to ignore unknown keys.
    for _marker in [k for k in body if str(k).startswith("_")]:
        body.pop(_marker, None)
    # dict(payload) 会把原始模型名一起带过去，所以别名要在这里覆盖回去
    body["model"] = model
    body["messages"] = messages
    # Repair tool-call pairing before the body leaves: a call whose result never
    # came back, results split from their batch by an interleaved message, or a
    # parallel batch split into adjacent assistant messages makes the upstream
    # reject every later turn of that conversation. Order matters: merge the
    # split declarations first, so repack sees one complete batch.
    repaired, _merged = merge_adjacent_tool_calls(body["messages"])
    repaired, _repacked = repack_tool_result_blocks(repaired)
    repaired, _cleaned = cleanup_orphan_tool_calls(repaired)
    # Apply the verified history budget first. Scanning old discarded traces
    # on every tool round would add work proportional to the entire conversation.
    body["messages"] = clean_deepseek_stalled_history(
        compact_deepseek_reasoning_history(repaired, model), model)
    translate_max_completion_tokens(body)
    normalize_tool_choice(body)
    normalize_tools(body)
    if "max_tokens" not in body:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            body["max_tokens"] = default_max
    # Thinking injection for DeepSeek models.
    #
    # thinking.type=enabled on its own does not switch the reasoning trace on:
    # the upstream still answers without one unless an effort level rides along.
    # Measured against the live upstream on deepseek-v4.1-flash, same prompt:
    #   enabled + no effort   -> reasoning_tokens 0,  reasoning_content len 0
    #   reasoning_effort=high -> reasoning_tokens 37, reasoning_content len 117
    # The client's own choice always wins; an effort level is only filled in
    # when it left the field out, and never for a request that opted out.
    if str(model).lower().startswith("deepseek"):
        thinking = body.get("thinking")
        opted_out = isinstance(thinking, dict) and \
            str(thinking.get("type") or "").strip().lower() == "disabled"
        effort = body.get("reasoning_effort") or body.get("reasoningEffort")
        if not opted_out and str(effort or "").strip().lower() != "none":
            if "thinking" not in body:
                body["thinking"] = {"type": "enabled"}
            if not effort:
                body["reasoning_effort"] = model_default_effort(model) or "high"
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
    return body


def model_catalog_meta(model):
    """The catalog metadata dict for a model, or {}.

    Read from the same merged catalog that /v1/models advertises.
    Deliberately side-effect free: it reads the already-populated model cache
    and the shipped static tables only. Calling fetch_models() here would let a
    cold cache trigger an upstream discovery round-trip from inside request
    handling, turning one chat call into a network fetch.
    """
    if not model:
        return {}
    try:
        realm = detect_model_realm(model) or CURRENT_REALM
        entries = (_models_cache.get(realm) or {}).get("data")
        if not entries:
            name = "STATIC_CN_MODELS" if realm == "cn" else "STATIC_INTL_MODELS"
            table = getattr(wb_catalog, name, None) or wb_catalog.STATIC_MODELS
            entries = [(m.get("id"), m) for m in table if isinstance(m, dict)]
        m_lower = str(model).strip().lower()
        for mid, meta in entries:
            if str(mid).strip().lower() == m_lower:
                return meta or {}
    except Exception as exc:
        log("catalog meta lookup failed for '%s': %s" % (model, exc))
    return {}


def model_reasoning_meta(model):
    """The catalog's reasoning block for a model, or {}."""
    return model_catalog_meta(model).get("reasoning") or {}


def model_default_max_output_tokens(model):
    """The max output tokens the catalog declares for a model, or None."""
    val = model_catalog_meta(model).get("maxOutputTokens")
    if val is not None:
        try:
            val = int(val)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return None


def model_default_effort(model):
    """The effort the catalog applies when the client asks for none, or None."""
    effort = model_reasoning_meta(model).get("defaultEffort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def model_fixed_effort(model):
    """The effort the catalog pins a model to, or None when it is selectable.

    reasoning.effort without supportedEfforts means the model always runs at
    that level: /v1/models advertises it as reasoning_fixed_effort and the
    picker offers no choice for it, so an effort the request carries anyway does
    not change what ran. A model that declares supportedEfforts is selectable
    whatever else its block carries - an `effort` beside it is the default, not
    a pin.
    """
    reasoning = model_reasoning_meta(model)
    if reasoning.get("supportedEfforts"):
        return None
    effort = reasoning.get("effort")
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def prompt_cache_key_enabled():
    """Whether to inject prompt_cache_key into outbound requests.

    Off by default. The upstream turns out to cache repeated prefixes on its
    own: with an identical ~8k-token prefix sent twice, the second call already
    reports prompt_cache_hit_tokens=9600 and the same credit with or without
    this field, on both exits (www.workbuddy.ai and copilot.tencent.com) and on
    both a free and a billed model. Injecting it changed neither the hit rate
    nor the charge, so it is left as an opt-in for experimenting rather than
    added to every request.
    """
    return os.environ.get("WB_PROMPT_CACHE_KEY", "0").strip().lower() in (
        "1", "true", "yes", "on")


def inject_prompt_cache_key(body, uid, conversation):
    """Add the upstream prompt_cache_key so its prefix cache can be reused.

    Kept for experimentation only: measurement on this upstream showed the
    prefix cache working without it (see prompt_cache_key_enabled). The key
    still carries the account uid, because the upstream cache is scoped per
    account -- a key shared between accounts would let one account's request
    read another's cached prefix, so this must never be made a fixed string.

    Priority matches the client's intent: an explicit prompt_cache_key is never
    overwritten; then the body's own conversation id; then the session key the
    gateway resolved (header or conversation-prefix derived).
    """
    if not isinstance(body, dict):
        return body
    existing = body.get("prompt_cache_key")
    if isinstance(existing, str) and existing.strip():
        return body
    conv = conversation if isinstance(conversation, str) else ""
    for field in ("conversation_id", "conversationId"):
        value = body.get(field)
        if isinstance(value, str) and value.strip():
            conv = value.strip()
            break
    uid8 = (uid or "")[:8] or "-"
    seed = "%s|%s" % (uid or "", conv)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    out = dict(body)
    out["prompt_cache_key"] = "wb2a-%s-%s" % (uid8, digest)
    return out
class ContentRejected(Exception):
    """Upstream content review rejected this request (403 / code 11140).

    Not an account problem: another credential gets the same 403 for the same
    content, so it is passed straight through instead of cooling the pool.
    """

    def __init__(self, http_error=None, detail=""):
        self.http_error = http_error
        self.detail = detail or ""
        super().__init__("upstream rejected the request content (403)")

    @property
    def code(self):
        return 403


class RateLimited(Exception):
    """Upstream throttled this model (429 / code 6004). Distinct from a dead
    pool: the credential is fine, only the model is cooling down for a while."""

    def __init__(self, http_error=None, detail="", wait=60, message=""):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        # 429s answered without an upstream call (the pool is parked by the
        # daily token guard) carry their own text instead of the upstream
        # wording.
        self.message = message or ""
        super().__init__("upstream rate limit: %s" % (self.detail[:200] or "429"))


def gateway_hint(status, message):
    """A gateway-side note that sits beside the raw upstream message.

    The hint never replaces the upstream wording (clients keep parsing the
    same envelope); it only explains what the gateway could classify, so a
    user staring at "400 Bad Request" learns whether the fix is a smaller
    context, a different model, a re-login or simply waiting. An empty string
    means "nothing to add" - no hint is invented for unknown failures.

    Covers the shapes the hub can classify from the upstream body/status:
    11133 model_param_invalid, 11135 invalid_image_data, rate limits, content
    review, exhausted credits, dead sessions, missing models, prompt-length
    rejections, WAF blocks and the local no-usable-account case.
    """
    text = str(message or "")
    lower = text.lower()
    if status == 503 and "concurrent chat limit" in lower:
        return "gateway is busy at its concurrency limit; retry shortly"
    if ("11133" in lower or "model_param_invalid" in lower
            or "invalid request parameters" in lower):
        return ("request parameters were rejected by the model provider; "
                "check message format and model capabilities")
    if ("11135" in lower or "invalid_image_data" in lower
            or "replace the image" in lower):
        return ("image data rejected by upstream; use a real/valid image, "
                "may need a new conversation")
    if "no usable account" in lower or "no healthy account" in lower:
        return "no healthy account available in pool; check /status or retry later"
    if ("context length" in lower or "context_length" in lower
            or "prompt too long" in lower or "too many tokens" in lower
            or "maximum context" in lower):
        return ("request context exceeds the model's limit; reduce "
                "history/message size")
    if status == 429 or "rate limit" in lower or "frequency limit" in lower:
        return "rate limited by upstream; retry after reset"
    if status == 402 or ("insufficient" in lower and "credit" in lower):
        return ("account credits exhausted at upstream; waiting for daily "
                "check-in to restore")
    if "session" in lower and ("not found" in lower or "expired" in lower):
        return ("account session expired at upstream; the account is disabled "
                "until re-login")
    if "content" in lower and ("reject" in lower or "policy" in lower):
        return ("request content was rejected by content policy; adjust the "
                "prompt and retry")
    if ("no such model" in lower or "model not found" in lower
            or "unsupported model" in lower):
        return ("upstream has no such model on this backend; switch model or "
                "retry on another account")
    if status == 403 and "waf" in lower:
        return "upstream WAF blocked the gateway; retry after the block window"
    return ""


# ---------------------------------------------------------------------------
# 出站身分自动切换
#
# 官方有三套身分（workbuddy / vscode / cli），端点与配额通道各不相同，
# 对照表见 wb_identity._ENDPOINTS。
#
# 某模型在某条通道被限流（429 / code 6004）时，换成另一套身分通常还能继续
# 用 —— 那是另一条配额线。每轮最多切 MAX_PRODUCT_SWITCHES 次，避免来回弹跳。
#
# 身分会写进凭证档并在重启后读回（issue #76）：面板手动切换当下就落盘，
# 这里的自动切换则在下一次任何 save() 时一并写入。
#
# 这个开关交给面板设定决定（issue #67），预设关闭：自动切换会吃掉重试预算，
# 也会把帐号留在操作者没主动选过的身分上，要用的话自己开。
# ---------------------------------------------------------------------------

MAX_PRODUCT_SWITCHES = 4
_SWITCH_LOG = {}


def auto_switch_product_enabled():
    """Whether a 429 may rotate the outbound identity (panel setting, off by default)."""
    return wb_settings.auto_switch_product(ACCOUNTS_DIR)


def _switch_count(account, model):
    """这一轮已经切过几次（60 秒内的切换算同一轮）。"""
    entry = _SWITCH_LOG.get((account.uid, model))
    if not entry:
        return 0
    count, last = entry
    if time.time() - last > 60:
        return 0
    return count


def _try_switch_product(account, model):
    """429 时换身分重试。回传 True 表示已切换、可以重试。

    同一请求内最多切 MAX_PRODUCT_SWITCHES 次：
      cli -> workbuddy -> cli -> workbuddy
    四次都不行就放弃，让呼叫端回报真正的 429。
    """
    count = _switch_count(account, model)
    if count >= MAX_PRODUCT_SWITCHES:
        return False
    current = getattr(account, "product", wb_identity.PRODUCT_DESKTOP)
    if current == wb_identity.PRODUCT_DESKTOP:
        target = wb_identity.PRODUCT_VSCODE
    elif current == wb_identity.PRODUCT_VSCODE:
        target = wb_identity.PRODUCT_CLI
    else:
        target = wb_identity.PRODUCT_DESKTOP
    try:
        changed = account.set_product(target)
    except Exception as exc:
        log("product switch failed: %s" % exc, level="WARN")
        return False
    if not changed:
        return False
    _SWITCH_LOG[(account.uid, model)] = (count + 1, time.time())
    if len(_SWITCH_LOG) > 500:
        _SWITCH_LOG.clear()
    log("account %s: %s 被限流，自動切換身分 -> %s (第 %d/%d 次)"
        % (account.uid[:8], current, target, count + 1, MAX_PRODUCT_SWITCHES),
        level="WARN")
    return True


def reset_switch_counter(account, model):
    """成功之后归零，下一次请求重新享有 4 次切换额度。"""
    _SWITCH_LOG.pop((account.uid, model), None)


def retry_after_seconds(model, realm):
    """Shortest wait until any account of this realm can serve `model` again."""
    if not POOL:
        return 60
    waits = [a.throttle_wait(model=model) for a in POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True when accounts exist and are healthy but all are cooling this model.

    Only a *model* cooldown counts. A plain account cooldown usually comes from
    a transient network error, and reporting that as "rate limited" told
    clients to back off from a model that was never throttled.
    """
    if not POOL:
        return (False, 0)
    existing = [a for a in POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    waits = []
    for a in existing:
        wait = getattr(a, "model_cooldowns", {}).get(model, 0.0) - time.time()
        waits.append(max(0.0, wait))
    if waits and all(w > 0 for w in waits):
        return (True, int(min(waits)))
    return (False, 0)


def is_transient(exc):
    """Network-level flakiness that deserves a retry, not a cooldown.

    Upstream occasionally drops a TLS handshake mid-stream
    (SSL: UNEXPECTED_EOF_WHILE_READING / Remote end closed connection).
    Treating that as a dead account took the only intl account offline for 60s
    and turned one hiccup into a 502 storm.
    """
    t = ("%s %s" % (type(exc).__name__, exc)).lower()
    markers = (
        "ssl", "unexpected_eof", "eof occurred", "remote end closed",
        "connection reset", "connection aborted", "connectionreseterror",
        "connectionabortederror", "timed out", "timeout", "temporarily unavailable",
        "bad gateway", "502", "503", "504", "incompleteread",
    )
    return any(m in t for m in markers)


# The shapes the upstream uses for a rate limit. A body naming one is a soft
# limit; it still says nothing about scope, so the caller decides that from the
# reset clock (one model), from credential wording (the account), or - when
# neither is present - from repetition, and then only on one model.
SOFT_LIMIT_MARKERS = ("usage exceeds frequency limit", "too many requests",
                      "请求过于频繁")
SOFT_LIMIT_CODES = (14003,)     # intl, observed on a real 429

# Wording that scopes a 429 to the credential itself. These are matched as whole
# words or explicit phrases, never as bare substrings: a 429 body carries
# metadata such as "accountId", "account_id" or "credentialId", and a substring
# test reads those field names as a scope statement and hands the credential the
# 10m -> 2h ladder. Provisional: no captured body has shown yet what a genuine
# account-level 429 says, so the list stays short and fails towards the narrow
# model window.
ACCOUNT_SCOPE_PHRASES = (
    "account-level", "account level", "account-wide", "account scope",
    "account quota", "account rate limit", "account limit",
    "per account", "per-account", "this account", "your account",
    "the account", "an account",
    "credential-level", "credential level", "credential scope",
    "credential limit", "per credential", "this credential",
    "the credential", "your credential",
    "账号级", "账户级", "该账号", "此账号", "该账户", "此账户",
    "账号限流", "账户限流", "凭证级", "该凭证",
)


def credential_scope_phrase(detail):
    """The scope phrase the body uses, or None when it names no scope.

    ASCII phrases are anchored on word boundaries, so an identifier like
    `accountId` cannot satisfy one. The Chinese phrases are plain substrings:
    they have no word boundary to anchor to and cannot occur inside an
    identifier.
    """
    lowered = (detail or "").lower()
    for phrase in ACCOUNT_SCOPE_PHRASES:
        if phrase.isascii():
            if re.search(r"\b%s\b" % re.escape(phrase), lowered):
                return phrase
        elif phrase in lowered:
            return phrase
    return None


def rate_limit_code(detail):
    """The `code` field of a JSON 429 body, or None when it is not JSON."""
    try:
        obj = json.loads(detail)
    except Exception:
        return None
    return obj.get("code") if isinstance(obj, dict) else None


def rate_limit_is_soft_shape(detail):
    """True when a 429 body names a rate limit, whatever its scope.

    Both shapes the incident environment produced are covered: the bare "usage
    exceeds frequency limit" body (229 of the 263 recorded 429s) and the intl
    code 14003 body ("too many requests"). Recognising them is what lets the
    caller answer with a window on one model rather than on the credential.
    """
    text = (detail or "").lower()
    if any(marker in text for marker in SOFT_LIMIT_MARKERS):
        return True
    return rate_limit_code(detail) in SOFT_LIMIT_CODES


def rate_limit_is_account_level(detail, reset_at):
    """True only with positive evidence that the 429 is credential-scoped.

    The model-scoped form names a reset wall clock (code 6004). Treating the
    *absence* of one as account scope made every 429 without a timestamp - which
    includes the most common body the upstream sends - buy the whole credential
    a 600s cooldown doubling to 7200s, so a handful of them left every account of
    a realm cooling at once while a panel test kept reporting the credential
    healthy. Absence is evidence of nothing: the credential is parked only when
    the body itself says the limit is on the credential; everything else gets a
    window on one model, decided by the caller.

    Provisional: the phrase list is short because no captured body has shown yet
    what a genuine account-level 429 looks like, so it is meant to be re-checked
    against one rather than grown speculatively.
    """
    if reset_at is not None:
        return False
    return credential_scope_phrase(detail) is not None


def parse_rate_limit_reset(detail):
    """Pull the reset time out of an upstream 429 body, if it names one.

    Upstream answers code 6004 with a reset wall clock, but the wording is
    per-realm: the intl form is "... your usage will reset at
    2026-09-19 18:29:03 UTC+8 ...", the cn form is "... 将在
    2026-10-09 14:44:59 UTC+8 重置 ...". Matching only the English form left
    every cn 429 without a reset time, so it read as an account-level soft
    limit and cooled the whole credential instead of parking just the
    throttled model. Returns an epoch or None. Kept tolerant on purpose: an
    unparseable body must not break the request path.
    """
    if not detail:
        return None
    m = re.search(r"(?:reset at|将在)\s*"
                  r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", detail)
    if not m:
        return None
    stamp = m.group(1).replace("T", " ")
    tz = re.search(r"UTC([+-]\d{1,2})(?::?(\d{2}))?", detail)
    offset = 0
    if tz:
        hours = int(tz.group(1))
        minutes = int(tz.group(2) or 0)
        offset = hours * 3600 + (minutes * 60 if hours >= 0 else -minutes * 60)
    try:
        base = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) - time.timezone
        return base - offset
    except Exception:
        return None


def upstream_timeouts():
    """(header, idle) seconds for the chat upstream socket.

    header covers connect/TLS/first byte; idle bounds each mid-stream
    read, so active data keeps the connection alive while a silent
    stream fails out and releases its in-flight lease. There is no
    total-duration cap, matching the panel project's semantics.
    """
    try:
        cfg = wb_settings.upstream_config(ACCOUNTS_DIR)
    except Exception:
        cfg = None
    header = 120.0
    idle = 300.0
    if isinstance(cfg, dict):
        try:
            header = float(cfg.get("header_timeout_seconds") or header)
        except (TypeError, ValueError):
            pass
        try:
            idle = float(cfg.get("idle_timeout_seconds") or idle)
        except (TypeError, ValueError):
            pass
    return max(1.0, header), max(1.0, idle)


def _apply_stream_idle_timeout(response, seconds):
    """Switch the upstream socket to the mid-stream idle timeout.

    urllib's timeout covers the initial request; the same socket is then
    read for the whole SSE stream. Setting the socket timeout to the idle
    value bounds each read instead of the whole stream. Best-effort: an
    unknown socket shape keeps the header timeout.
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return False
    if seconds <= 0:
        return False
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        sock = getattr(fp, "_sock", None)
    if sock is None:
        return False
    try:
        sock.settimeout(seconds)
        return True
    except Exception:
        return False


def open_upstream(payload, session_key=None, target_realm=None,
                  exclude_uids=None, deadline_at=None):
    # Refresh the daily token guard before picking. The scan underneath is
    # incremental and TTL-cached, so this is a stat() plus a cached dict on
    # the hot path, and an account parked by any of the guards is skipped
    # like any other unusable one.
    apply_daily_token_limit()
    apply_daily_credit_limit()
    apply_model_daily_token_limit()
    realm = target_realm or detect_model_realm(payload.get("model")) or CURRENT_REALM
    model = str(payload.get("model") or "")
    upstream_body = build_upstream_body(payload)
    # PATCHED-BY-OPS: 客户端未提供会话标识时，用对话稳定前缀兜底。
    # 位置放在 build_upstream_body 之后，保证键与真正发往上游的消息一致
    # （该函数可能在最前面插入 SYSTEM_PROMPT）。
    if not session_key:
        session_key = derive_affinity_key(upstream_body.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(upstream_body.get("messages") or [])))
    total = max(1, POOL.count_ready(realm, model=model)) if POOL else 1
    excluded = set(exclude_uids or ())
    tried = set(excluded)
    last_error = None
    last_uid = None
    last_429 = None
    last_429_detail = ""
    last_403_detail = ""
    transient_hits = 0
    degraded_retried = False
    auth_refreshed = set()
    retry_uid = None
    # Read once per request, not per attempt: this is a panel setting, and a
    # settings read on every retry would be pure overhead.
    auto_switch = auto_switch_product_enabled()
    # Panel parity: header/idle timeouts are read once per request, not
    # once per retry, so a settings read never lands in the retry loop.
    header_timeout, idle_timeout = upstream_timeouts()
    max_attempts = max(2, total) + 1 + (MAX_PRODUCT_SWITCHES if auto_switch else 0)
    _attempt = -1
    while _attempt + 1 < max_attempts:
        _attempt += 1
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TimeoutError("upstream connection deadline exceeded")
        account = None
        if retry_uid is not None and POOL:
            # A refreshed account must still pass official quota/admission checks.
            only_retry = tried | {a.uid for a in POOL.accounts if a.uid != retry_uid}
            account = POOL.pick_for_session(realm=realm, session_key=session_key,
                                            exclude=only_retry, model=model)
            if account is None:
                tried.add(retry_uid)
            retry_uid = None
        if account is None:
            account = POOL.pick_for_session(realm=realm, session_key=session_key,
                                            exclude=tried, model=model) if POOL else None
        if account is None:
            if transient_hits and _attempt < max_attempts - 1:
                tried = set(excluded)
                time.sleep(min(1.5 * transient_hits, 3.0))
                continue
            break
        if account.realm != realm:
            if session_key and POOL: POOL.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        last_uid = account.uid
        cfg = wb_accounts.get_realm_config(account.realm)
        chat_url = account.chat_base_url() + CHAT_PATH
        # The cache key is account scoped, so it is rebuilt per candidate rather
        # than once up front. Opt-in only: measurement showed the upstream
        # caches prefixes without it (see prompt_cache_key_enabled).
        if prompt_cache_key_enabled():
            attempt_body = inject_prompt_cache_key(upstream_body, account.uid, session_key)
        else:
            attempt_body = upstream_body
        attempt_data = json.dumps(attempt_body, ensure_ascii=False).encode("utf-8")
        headers = account.headers(purpose="chat")
        sent_access_token = headers.get("Authorization", "").removeprefix("Bearer ")
        req = urllib.request.Request(chat_url, data=attempt_data, method="POST",
                                     headers=headers)
        try:
            timeout = header_timeout
            if deadline_at is not None:
                timeout = min(timeout, max(.01, deadline_at - time.monotonic()))
            resp = wb_accounts.urlopen(req, timeout=timeout, proxy=account.proxy)
            _apply_stream_idle_timeout(resp, idle_timeout)
            account.note_success(model=model)
            reset_switch_counter(account, model)
            # The third element is the reasoning effort this request ran at: the
            # body is rebuilt per attempt, but the effort is a property of the
            # model and the request, and the callers record it on the usage row.
            return resp, account, upstream_effort_of(upstream_body, model)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                reset_at = parse_rate_limit_reset(detail)
                if rate_limit_is_account_level(detail, reset_at):
                    wait = account.note_soft_rate("HTTP 429 (account soft rate)")
                    log("account %s soft-rate limited (%r), cooling %.0fs (streak %d)"
                        % (account.uid[:8], credential_scope_phrase(detail), wait,
                           account.soft_streak))
                    if session_key and POOL:
                        POOL.affinity.unbind(session_key)
                    last_error = exc
                    last_429 = exc
                    last_429_detail = detail
                    continue
                # No reset clock, so nothing here says whether the limit is on
                # the credential or on one model. For a recognised soft-limit
                # shape the repetition is the evidence: the window goes on this
                # model and grows on the short ladder (60s up to 10m) instead of
                # the credential's 600s -> 2h one. An unrecognised body gets the
                # same 60s on one model, with no streak at all.
                wait = max(1.0, reset_at - time.time()) if reset_at else 60.0
                if reset_at is None and rate_limit_is_soft_shape(detail):
                    wait = account.note_unscoped_rate(model)
                    log("account %s: unscoped 429, parking '%s' for %.0fs "
                        "(unscoped streak %d)"
                        % (account.uid[:8], model, wait, account.unscoped_streak))
                # Model-scoped: only this model is throttled for this account,
                # so sibling models stay serviceable on the same credential.
                account.note_error("HTTP 429 (model throttled)", model=model, until=reset_at,
                                   cooldown=wait, detail=detail)
                if auto_switch and _try_switch_product(account, model):
                    # 换了身分就等于换了一条配额线：要把它从「已试过」拿掉，
                    # 并清掉刚刚记下的模型冷却，否则下一轮回圈会找不到帐号。
                    tried.discard(account.uid)
                    try:
                        account.clear_error(model=model)
                    except Exception:
                        pass
                    if session_key and POOL:
                        POOL.affinity.unbind(session_key)
                    continue
                log("account %s throttled on '%s' (429), retry in %ds"
                    % (account.uid[:8], model, int(wait)))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code == 403:
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                # Panel degrade.go: a content rejection in passthrough/append
                # mode is usually a system-prompt fingerprint false positive.
                # Switch to the minimal neutral prompt until next 00:00 CST
                # and retry this turn once; custom mode opted out.
                if (prompt_degrade_enabled() and not degraded_retried
                        and prompt_mode_config().get("mode") in ("passthrough", "append")):
                    PROMPT_DEGRADE.trigger()
                    degraded_retried = True
                    upstream_body = build_upstream_body(payload)
                    tried.discard(account.uid)
                    log("upstream 403 (content review) -> degraded prompt retry")
                    continue
                log("upstream 403 for '%s' (content review), passing through" % model)
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                last_403_detail = detail
                break
            if exc.code == 402:
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_balance_cooled(detail or "HTTP 402 (insufficient credits)")
                log("account %s out of credits (402), parked until 04:00"
                    % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                continue
            if exc.code == 401:
                try:
                    exc.close()
                except Exception:
                    pass
                refreshed = False
                if (account.uid not in auth_refreshed and len(auth_refreshed) < total
                        and getattr(account, "refresh_token", "")):
                    auth_refreshed.add(account.uid)
                    budget = 30.0
                    if deadline_at is not None:
                        budget = min(budget, deadline_at - time.monotonic())
                        if budget <= 0:
                            raise TimeoutError("upstream connection deadline exceeded")
                    try:
                        refreshed = account.refresh_after_401(sent_access_token, timeout=budget)
                    except Exception as refresh_exc:
                        log("account %s refresh after 401 failed (%s)"
                            % (account.uid[:8], type(refresh_exc).__name__),
                            level="WARN", tag="auth")
                if refreshed:
                    retry_bump("auth_401_refresh_success")
                    tried.discard(account.uid)
                    retry_uid = account.uid
                    max_attempts += 1
                    last_error = exc
                    log("account %s refreshed after HTTP 401; retrying request"
                        % account.uid[:8], level="INFO", tag="auth")
                    continue
                if account.uid in auth_refreshed:
                    retry_bump("auth_401_refresh_failed")
                log("account %s rejected (HTTP 401), rotating" % account.uid[:8])
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                account.note_error("HTTP 401",
                                   cooldown=60,
                                   single_account=(total <= 1))
                last_error = exc
                continue
            if exc.code in (500, 502, 503, 504):
                transient_hits += 1
                account.note_failure("HTTP %d" % exc.code)
                log("upstream %s for '%s', retrying (fails=%d)"
                    % (exc.code, model, account.fails))
                if session_key and POOL:
                    POOL.affinity.unbind(session_key)
                last_error = exc
                continue
            raise
        except Exception as exc:
            if session_key and POOL:
                POOL.affinity.unbind(session_key)
            if is_transient(exc):
                transient_hits += 1
                account.note_unknown_failure("connection: %s" % type(exc).__name__)
                # 带上 UID：这一行同时是一次扣分（连续 3 次就把账号熔断 30
                # 分钟），不写清楚是谁挨的这一下，事后只能看着一串 hiccup 猜
                # 是哪几个账号被关掉、池子为什么突然空了。
                log("account %s: upstream connection hiccup for '%s' (%s), "
                    "retrying (degrade=%d)"
                    % (account.uid[:8], model, type(exc).__name__, account.degrade_count))
                last_error = exc
                time.sleep(min(0.6 * transient_hits, 2.0))
                continue
            account.note_unknown_failure(str(exc)[:120])
            account.note_error(str(exc)[:120], cooldown=60, single_account=(total <= 1))
            last_error = exc
            continue
    if last_error is not None:
        # Carry the account that produced the failure out to the caller, so
        # the error row can name it even though the local variable that would
        # have held it was never assigned in the caller.
        try:
            last_error.account_uid = last_uid
        except Exception:
            pass
        if last_429 is not None:
            exc = RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
            exc.account_uid = last_uid
            raise exc
        if last_403_detail:
            exc = ContentRejected(last_error, last_403_detail)
            exc.account_uid = last_uid
            raise exc
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    enabled = [a for a in POOL.accounts
               if a.realm == realm and a.enabled and a.access_token] if POOL else []
    if enabled and all(a.daily_limit_blocked() for a in enabled):
        reason = ("every usable account reached today's token limit (%s per "
                  "account); the pool resumes after local midnight"
                  % wb_settings.daily_token_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.credit_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's credit limit (%s per "
                  "account); paid models resume after local midnight, free "
                  "models keep working"
                  % wb_settings.daily_credit_limit(ACCOUNTS_DIR, realm))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    if enabled and model and all(a.model_token_limit_blocked(model) for a in enabled):
        reason = ("every usable account reached today's token limit for %s "
                  "(%s per account); the model resumes after local midnight"
                  % (model, wb_settings.model_daily_token_limit(ACCOUNTS_DIR, realm)))
        raise RateLimited(None, reason,
                          wait=seconds_until_local_midnight(), message=reason)
    raise RuntimeError("no usable account for realm '%s': all are disabled, cooling down, "
                       "expired or parked by the daily token limit%s"
                       % (realm, pool_unavailable_detail(realm, model)))

def pool_unavailable_detail(realm, model=None):
    """把该区域每个账号不可用的原因拼成一句，附在「没有可用账号」后面。

    这句话以前只列了四种可能（停用 / 冷却 / 过期 / 日限额），而 v1.6.16 之后
    账号还会被熔断与降权窗口挡住——恰好这两种一个字都没提。于是生产上出现过
    一场很难解释的报错：上游成片丢连接，几个账号各连踩 3 次触发 30 分钟熔断，
    池子瞬间空掉、请求 11ms 就回 503，而看板上那些账号仍然显示「可用」，
    排查的人只能对着「明明有 9 个账号」的池子猜。

    这里不改变判定，只把判定结果说出来：每个不可用账号「八位 UID + 原因」。
    """
    if POOL is None:
        return ""
    accounts = [a for a in POOL.accounts if a.realm == realm]
    if not accounts:
        return "；该区域没有任何账号"
    reasons = []
    for account in accounts:
        try:
            reason = account.unavailable_reason(model=model)
        except Exception:
            reason = ""
        if reason:
            reasons.append("%s %s" % ((account.uid or "?")[:8], reason))
    if not reasons:
        return ""
    return "；各账号：%s" % "，".join(reasons)

def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None

def estimate_tokens(text):
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff' or '\u3400' <= c <= '\u4dbf')
    other = len(text) - cjk
    return cjk + max(1, int(other / 3.6)) if text else 0

def aggregate_stream(raw_iter, model, resp_id, holder=None):
    holder = holder if holder is not None else {}
    holder["model"] = model
    guarded = _GuardedStream(raw_iter, holder)
    try:
        return _aggregate_stream_impl(guarded, model, resp_id)
    finally:
        guarded.close()


def _aggregate_stream_impl(raw_iter, model, resp_id):
    """Fold an SSE stream into one non-streaming chat.completion object."""
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = None
    started = time.time()
    first_chunk_at = None
    saw_done = False
    scrubber = ReasoningScrubber()
    saw_finish = False
    failed_reason = None
    for line in raw_iter:
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            if data == "[DONE]":
                saw_done = True
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if chunk.get("error"):
            code = (chunk["error"] or {}).get("code")
            if code in _STREAM_GUARD_CODES:
                failed_reason, finish = code, "length"
                break
            raise RuntimeError("upstream returned an SSE error")
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning_content"):
                safe = scrubber.feed(delta["reasoning_content"])
                if safe:
                    reasoning.append(safe)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            # PATCH2-BY-OPS: 上游会在流末尾发 function_call:{"name":"","arguments":""}
            # 占位。原判断对空 dict 成立，会凭空生成 tool_call 并伪造 id，
            # 导致 finish_reason 被改成 "tool_calls"（参数全空）→ 严格客户端死等。
            # 故：只要 name 为空即视为无效占位直接跳过。
            fc_is_empty = (not isinstance(fc, dict)) or (
                not fc.get("name") and not fc.get("arguments"))
            if fc and isinstance(fc, dict) and not fc_is_empty:
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {
                            "name": fc.get("name") or "",
                            "arguments": fc.get("arguments") or "",
                        }
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
                saw_finish = True
        if failed_reason:
            break
    tail = scrubber.feed(final=True)
    if tail and not failed_reason:
        reasoning.append(tail)
    if not saw_finish and not failed_reason:
        failed_reason = "upstream_truncated"
        finish = "length"
    full_text = "".join(content)
    dsml_calls, clean_text = parse_dsml_tool_calls(full_text)
    if not tool_calls_map and dsml_calls:
        for index, call in enumerate(dsml_calls):
            tool_calls_map[index] = {"id": call.get("id") or _new_id("call_"),
                                     "type": "function", "function": {
                                         "name": call.get("name"),
                                         "arguments": call.get("arguments") or "{}"}}
    if re.search(TAG_START, clean_text):
        clean_text, _ = quarantine_dsml_text(clean_text)
    content[:] = [clean_text]
    message = {"role": "assistant", "content": clean_text}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # PATCH2-BY-OPS: 二次防御——剔除「无函数名且无参数」的空 tool_call。
    # 即使上游以 tool_calls 数组形式发空占位，也不会泄漏给客户端。
    if tool_calls_map:
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if (v.get("function") or {}).get("name")
        }
    # A stream cut off by max_tokens / a dropped connection leaves the last
    # tool call with half-written JSON. Drop those calls instead of handing
    # the client unparsable arguments; only non-empty unparsable strings
    # count as truncated, so no-argument tools survive.
    if tool_calls_map and (finish == "length" or not saw_done):
        tool_calls_map = {
            k: v for k, v in tool_calls_map.items()
            if not is_truncated_arguments((v.get("function") or {}).get("arguments"))
        }
    if failed_reason:
        tool_calls_map.clear()
    if tool_calls_map:
        ordered_tcs = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered_tcs
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        # 占位被全部过滤掉，无实际工具调用，降级为正常结束，防止客户端无限挂起等待
        finish = "stop"
    if usage is None or (usage.get("total_tokens") or 0) == 0:
        full_c = "".join(content)
        full_r = "".join(reasoning)
        if full_c or full_r:
            comp = estimate_tokens(full_c) + estimate_tokens(full_r)
            prompt_est = max(1, comp // 2)
            usage = {
                "prompt_tokens": prompt_est,
                "completion_tokens": comp,
                "total_tokens": prompt_est + comp,
                "completion_tokens_details": {"reasoning_tokens": estimate_tokens(full_r)},
                "prompt_tokens_details": {"cached_tokens": 0},
            }
    out = {
        "id": resp_id or "chatcmpl-wb",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        normalize_usage_cache_aliases(usage)
        out["usage"] = usage
    if failed_reason:
        out["_ops_failure"] = failed_reason
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out
# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
#
# Kelivo and other clients can speak OpenAI's newer Responses API. The upstream
# gateway only speaks Chat Completions, so those requests are translated down,
# and the reply is translated back up into Responses objects / SSE events.
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found
def _new_id(prefix):
    return prefix + uuid.uuid4().hex
def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = f"data:{mime};base64," + piece["data"]
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return chr(10).join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Some clients - most notably Codex 0.15x - declare their file-editing tool as a
# *custom* (freeform) tool rather than a JSON-schema function:
#
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
#
# and expect the model to answer with a custom_tool_call item carrying the raw
# payload in "input", then feed the result back as custom_tool_call_output.
#
# The upstream chat endpoint has no notion of custom tools, so we downgrade them
# to ordinary function tools with a single "input" string parameter on the way
# out, and re-inflate them to custom_tool_call on the way back. Without this the
# tool is silently ignored: the model emits the payload as ordinary prose and the
# client never sees a tool call (measured: 52 text deltas, 0 tool items).
# ---------------------------------------------------------------------------

CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


# ---------------------------------------------------------------------------
# namespace 工具拒绝
#
# Codex 会把 MCP server / 外挂工具用 type="namespace" 的形式送出来。实测行为：
#   反代「接受」namespace 工具 -> app 把 MCP／外挂工具当成不可执行
#                                -> 每一次呼叫都回 "unsupported call"
#   反代「拒绝」namespace 工具 -> app 自动 fallback 成 flat function 清单
#                                -> 全部工具恢复正常
# （此行为在 Command Code proxy.mjs 的 CC_REJECT_NAMESPACE_TOOLS 实验里有记载，
#   Agent Router 也是靠直接拒绝这类请求才正常的。）
#
# 所以在这里主动回一个格式明确的 400，逼 app 走 fallback。
# 想还原成「照单全收」就把 REJECT_NAMESPACE_TOOLS 改成 False。
# ---------------------------------------------------------------------------

REJECT_NAMESPACE_TOOLS = False  # 保持关闭：正解是展开+还原 namespace

NAMESPACE_TOOL_MESSAGE = (
    'Unsupported tool type "namespace": this endpoint only supports flat '
    '"function" tools. Resend the tools as individual function entries.'
)


def find_namespace_tool(tools):
    """回传第一个 type=="namespace" 的工具名称，没有就回 None。"""
    for t in tools or []:
        if isinstance(t, dict) and str(t.get("type") or "").lower() == "namespace":
            return str(t.get("name") or t.get("server_label") or "(unnamed)")
    return None


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools):
    """Names of tools declared as freeform/custom in a Responses request."""
    names = set()
    for t in tools or []:
        if _is_custom_tool(t) and t.get("name"):
            names.add(str(t["name"]))
    return names


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = chr(10) + chr(10) + "Grammar:" + chr(10) + str(fmt["definition"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + chr(10) + chr(10) + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


# ---------------------------------------------------------------------------
# namespace 工具：展开 + 还原
#
# 新版 Codex App 把 MCP／外挂工具用 namespace 形式送出：
#   {"type":"namespace","name":"codex_app","tools":[{name:"list_threads",...}]}
#
# 上游 Chat Completions 只认 flat function，看不懂 namespace。
# 但 App 回程是用 (name, namespace) 两个栏位找执行器 ——
# 只给 flat name，App 一律回 "unsupported call"（实测 js / list_threads 全灭）。
#
# 三件事：
#   1. Expand   送上游前把 namespace 展开成 flat function，记住 name -> namespace
#   2. Normalise 模型回传的 name 可能是 js / ns__js / ns::js，都要能解析
#   3. Restore   回程的 function_call / custom_tool_call 补上 namespace 栏位
#
# 参考：某开源 CodeBuddy/WorkBuddy 反向代理项目的 tool-namespaces 说明
# ---------------------------------------------------------------------------

NAMESPACE_MAX_DEPTH = 4
_NS_SEP = "__"


def expand_namespace_tools(tools, _depth=0):
    """把 namespace 展开成 flat function 清单，其余工具原样保留。

      * 子工具可能在 tools / children / functions 任一栏位
      * namespace 子工具常常没有 type 栏位，展开时补成上游认得的 flat function
      * custom / web_search 等非 function 项目原样留下，交给既有管线处理
      * 同名只留第一个
      * 回传 (flat_tools, name_to_namespace)
    """
    flat = []
    mapping = {}
    seen = set()
    max_depth = max(0, int(_depth) + NAMESPACE_MAX_DEPTH)

    def collect(entry, depth, ns_name):
        if not isinstance(entry, dict) or depth > max_depth:
            return
        etype = str(entry.get("type") or "").lower()
        if etype == "namespace":
            subs = entry.get("tools")
            if not isinstance(subs, list):
                subs = entry.get("children")
            if not isinstance(subs, list):
                subs = entry.get("functions")
            if not isinstance(subs, list):
                subs = []
            child_ns = str(entry.get("name") or ns_name or "")
            for sub in subs:
                collect(sub, depth + 1, child_ns)
            return
        if ns_name and etype in ("", "function"):
            fn = entry.get("function") if isinstance(entry.get("function"), dict) else None
            if fn is None:
                fn = {
                    "name": entry.get("name"),
                    "description": entry.get("description") or "",
                    "parameters": entry.get("parameters") or entry.get("input_schema")
                                  or {"type": "object", "properties": {}},
                }
            name = str(fn.get("name") or "").strip()
            if not name or name in seen:
                return
            seen.add(name)
            mapping[name] = ns_name
            flat_fn = {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
            if "strict" in fn:
                flat_fn["strict"] = fn["strict"]
            flat.append(flat_fn)
            return
        fn = entry.get("function") if isinstance(entry.get("function"), dict) else {}
        name = str(entry.get("name") or fn.get("name") or "").strip()
        if name:
            if name in seen:
                return
            seen.add(name)
            if ns_name:
                mapping[name] = ns_name
        flat.append(entry)

    for entry in tools or []:
        collect(entry, 0, "")
    return flat, mapping


def resolve_namespaced_name(name, mapping):
    """把模型回传的名字解析回 (bare_name, namespace)。接受 js / ns__js / ns::js。"""
    if not name:
        return name, ""
    name = str(name)
    if name in mapping:
        return name, mapping[name]
    if "::" in name:
        idx = name.find("::")
        if idx > 0:
            tail = name[idx + 2:]
            head = name[:idx]
            if tail in mapping:
                return tail, mapping[tail]
            return tail, head

    # ns__tool 用精确比对，避免 namespace 内含 '__'（如 codex_apps__github）时切错
    for tool, ns in mapping.items():
        if name == ns + _NS_SEP + tool:
            return tool, ns

    return name, ""


def stamp_namespace(item, mapping):
    """把模型回传的扁平工具名还原成 (name, namespace)。

    串流的 response.output_item.done 事件才是客户端派发工具呼叫的依据，
    所以每个 function_call / custom_tool_call 项目都要在送出前补上 namespace。
    """
    if not mapping or not isinstance(item, dict):
        return item
    bare, ns = resolve_namespaced_name(item.get("name"), mapping)
    if ns:
        item["name"] = bare
        item["namespace"] = ns
    return item


def apply_namespace_to_calls(output_items, mapping):
    """替 Responses 的 function_call / custom_tool_call 补上 namespace。"""
    if not mapping or not isinstance(output_items, list):
        return output_items, 0
    fixed = 0
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("function_call", "custom_tool_call"):
            continue
        if item.get("namespace"):
            continue
        bare, ns = resolve_namespaced_name(item.get("name"), mapping)
        if ns:
            item["name"] = bare
            item["namespace"] = ns
            fixed += 1
    return output_items, fixed

def _tools_for_chat(tools):
    """Downgrade custom tools; leave everything else untouched."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(_downgrade_custom_tool(t) if _is_custom_tool(t) else t)
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args

# The gateway can run web_search / web_fetch itself; the panel switch decides.
#
# Some clients (Codex App and similar harnesses) declare web_search as a
# server-side tool, but the upstream has no executor for it: forwarding the
# declaration leaves the model answering as if no tool had been offered. With
# the switch on, the gateway swaps the declaration for a function of its own,
# swallows the calls and runs them locally (wb_webtools), then feeds the
# results back.
#
# Off by default: the declaration is forwarded untouched and a client that
# declares its own search tool receives the call - the behaviour since v1.5.3.
# Turning it on means the gateway itself fetches URLs a model asks for, so the
# egress policy is the operator's call.
def local_web_tools_enabled():
    """Panel switch: does this gateway run web_search / web_fetch itself?

    Read per request, so flipping the panel takes effect on the next one
    without a restart.
    """
    try:
        return wb_settings.local_web_tools(ACCOUNTS_DIR) is True
    except Exception:
        return False


def web_tools_active(body):
    """True when this request's tools were swapped for the gateway's own.

    Interception only applies to a request whose definitions the gateway
    injected: with the switch off, a client's own same-named function must be
    forwarded instead of being swallowed here.
    """
    return isinstance(body, dict) and body.get("_web_tools") is True


def sum_usage(total, part):
    """把一轮的 token 用量累加起来。

    代跑网路工具会多跑好几次上游，那些 token 是真的花掉的，所以记帐要加总，
    不能让最后一轮盖掉前面几轮。
    """
    if not isinstance(part, dict):
        return total
    if not isinstance(total, dict):
        total = {}
    for key, value in part.items():
        if isinstance(value, dict):
            total[key] = sum_usage(total.get(key), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = (total.get(key) or 0) + value
        elif key not in total:
            total[key] = value
    return total


_CITATION_MD_RE = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


def build_citations(text, sources):
    """把模型实际引用到的来源转成 url_citation annotations。

    只标注真的有出现在工具输出里的网址 —— 模型自己编的连结不会被当成引用。
    """
    text = str(text or "")
    if not text or not sources:
        return []
    by_url = {}
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if not url:
            continue
        by_url.setdefault(url, s)
        by_url.setdefault(url.rstrip("/"), s)

    anns = []
    seen = set()

    def add(url, title, start, end):
        key = (url, start, end)
        if key in seen or start < 0 or end <= start:
            return
        seen.add(key)
        anns.append({
            "type": "url_citation",
            "url": url,
            "title": title or url,
            "start_index": start,
            "end_index": end,
        })

    md_spans = []
    for m in _CITATION_MD_RE.finditer(text):
        url = m.group(2)
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        md_spans.append((m.start(0), m.end(0)))
        add(url, src.get("title") or m.group(1), m.start(0), m.end(0))

    for m in re.finditer(r"https?://[^\s<>()\[\]]+", text):
        if any(m.start(0) >= s and m.end(0) <= e for s, e in md_spans):
            continue
        url = m.group(0).rstrip(".,;:!?")
        src = by_url.get(url) or by_url.get(url.rstrip("/"))
        if not src:
            continue
        add(url, src.get("title"), m.start(0), m.start(0) + len(url))

    anns.sort(key=lambda a: (a["start_index"], a["end_index"]))
    return anns


def follow_up_with_tool_results(internal_calls, holder, model, session_key, t_start,
                                drop_tools=False):
    """执行反代自己代跑的网路工具，把结果喂回模型，回传新的上游连线。

    drop_tools=True 表示这是最后一轮：把网路工具从工具清单收回，模型没有东西
    可以再呼叫，只能用手上的结果把话讲完。旧版在回合用尽时合成一个
    resp_wrapup（status=completed、output=[]）收尾，那等于把失败伪装成正常
    结束，客户端看到的就是「讲到一半断掉」——issue #43。
    """
    convo = holder.get("convo_messages")
    if convo is None:
        convo = list(holder.get("base_messages") or [])
        holder["convo_messages"] = convo

    tool_calls = []
    for i, c in enumerate(internal_calls):
        tool_calls.append({
            "id": "call_web_%d_%d" % (int(t_start * 1000) % 1000000, i),
            "type": "function",
            "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"},
        })
    convo.append({"role": "assistant", "content": None, "tool_calls": tool_calls})

    for tc in tool_calls:
        nm = tc["function"]["name"]
        result = wb_webtools.execute(nm, tc["function"]["arguments"])
        found = wb_webtools.sources_from_result(result)
        if found:
            holder.setdefault("web_sources", []).extend(found)
        log("web tool %s -> %d chars, %d citeable source(s)"
            % (nm, len(result or ""), len(found)), level="INFO")
        convo.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": nm,
            "content": result,
        })

    body = dict(holder.get("base_body") or {})
    if drop_tools:
        body["tools"] = [t for t in (body.get("tools") or [])
                         if not wb_webtools.is_internal_tool(tool_name_of(t))]
        convo.append({
            "role": "system",
            "content": ("The web tools are no longer available. Answer the user now with "
                        "what you already have. Do not say that you are searching again."),
        })
    body["messages"] = convo
    body["stream"] = True
    holder["retry_body"] = body
    holder["retry_call"] = _make_retry_call(body, session_key, holder.get("realm"))
    if drop_tools:
        holder["web_tools_withdrawn"] = True
    return open_upstream(body, session_key=session_key,
                         target_realm=holder.get("realm"))


def internal_calls_from_chat(chat_obj, web_tools=False):
    """Calls in an aggregated chat completion the gateway runs itself.

    Only a request whose definitions the gateway injected can carry such a
    call; with the switch off a client's own same-named function stays the
    client's, so this answers empty.
    """
    message = ((chat_obj.get("choices") or [{}])[0] or {}).get("message") or {}
    out = []
    if not web_tools:
        return out
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if wb_webtools.is_internal_tool(name):
            out.append({"name": name, "arguments": fn.get("arguments") or "{}"})
    return out


def tool_name_of(tool):
    """Tool name, whichever of the two shapes the entry uses."""
    if not isinstance(tool, dict):
        return ""
    if isinstance(tool.get("function"), dict):
        return str((tool.get("function") or {}).get("name") or "")
    return str(tool.get("name") or "")


def _responses_input_to_messages(payload):
    """Turn the Responses input items into chat messages."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message", "user"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # If this is assistant text and the previous message is an assistant
                    # message (e.g. from an adjacent function_call), merge them so
                    # tool_calls and text stay in one message without breaking tool sequence.
                    if role == "assistant" and messages and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + chr(10) + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                # Reasoning item from previous assistant turn in Responses API.
                # In standard Chat Completions, reasoning is either backfilled into
                # the assistant message's reasoning_content or omitted.
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = chr(10).join(
                        p.get("text", "") for p in summ if isinstance(p, dict) and p.get("text")
                    )
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if not item.get("call_id"):
                    if isinstance(raw_out, list):
                        _txt = _flatten_content(raw_out)
                    elif isinstance(raw_out, dict):
                        _txt = json.dumps(raw_out, ensure_ascii=False)
                    else:
                        _txt = str(raw_out or "")
                    if isinstance(_txt, list):
                        # _flatten_content returns structured chat parts when
                        # the output carries images. Keep them as multimodal
                        # user content instead of crashing on .strip().
                        try:
                            log("[wb-proxy] orphan function_call_output kept as multimodal parts (%d)"
                                % len(_txt))
                        except Exception:
                            pass
                        messages.append({
                            "role": "user",
                            "content": ([{"type": "text", "text":
                                          "[Message from another task - treat this "
                                          "as a user instruction]"}] + _txt),
                        })
                        continue
                    _txt = (_txt or "").strip()
                    if _txt:
                        messages.append({
                            "role": "user",
                            "content": ("[Message from another task - treat this "
                                        "as a user instruction]" + chr(10) + chr(10) + _txt),
                        })
                        continue
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                # Freeform tool call coming back as conversation history.
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input}, ensure_ascii=False),
                    },
                }
                # Merge into previous assistant message if adjacent
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tc_item],
                    }
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                # Result of a freeform tool call (e.g. apply_patch output).
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "agent_message":
                parts = item.get("content")
                if isinstance(parts, list):
                    text = chr(10).join(
                        str((p or {}).get("text") or (p or {}).get("encrypted_content") or "")
                        if isinstance(p, dict) else str(p)
                        for p in parts
                    ).strip()
                else:
                    text = str(parts or "").strip()
                if text:
                    messages.append({
                        "role": "user",
                        "content": ("[Message from another task - treat this as "
                                    "a user instruction]" + chr(10) + chr(10) + text),
                    })
            else:
                log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    return _scrub_dsml_reasoning_messages(messages)


def responses_to_chat(payload):
    """Translate a Responses API request body into a Chat Completions body."""
    messages = _responses_input_to_messages(payload)
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    else:
        default_max = model_default_max_output_tokens(payload.get("model"))
        if default_max:
            chat["max_tokens"] = default_max
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    if payload.get("tools"):
        flat_tools, ns_map = expand_namespace_tools(payload["tools"])
        chat["tools"] = _tools_for_chat(flat_tools)
        chat["_namespace_map"] = ns_map
    # 客户端宣告 web_search / web_fetch 时，把那份宣告换成我们的
    # function（见 wb_webtools.install_tool_defs）。
    # 看板开关关闭时原样透传，客户端自己的同名工具不受影响。
    if local_web_tools_enabled():
        wants = wb_webtools.client_wants_web(payload.get("tools"))
        if wants["search"] or wants["fetch"]:
            chat["tools"] = wb_webtools.install_tool_defs(chat.get("tools") or [], wants)
            chat["_web_tools"] = True
    if payload.get("tool_choice"):
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat


# ---------------------------------------------------------------------------
# Anthropic Messages API (/v1/messages) <-> Chat Completions
# ---------------------------------------------------------------------------
def anthropic_error_type(status):
    """Map an HTTP status to the Anthropic error.type vocabulary."""
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = 500
    if status in (400, 422):
        return "invalid_request_error"
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status in (503, 529):
        return "overloaded_error"
    return "api_error"


def anthropic_error_obj(status, message, error_type=None):
    return {
        "type": "error",
        "error": {
            "type": error_type or anthropic_error_type(status),
            "message": str(message or "upstream error"),
        },
    }


def anthropic_sse_frame(event, payload):
    body = dict(payload or {})
    body["type"] = event
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return ("event: %s\ndata: %s\n\n" % (event, raw)).encode("utf-8")


def _anthropic_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _anthropic_json_text(value):
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _anthropic_parse_tool_input(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value:
            return {}
        try:
            parsed = json.loads(value)
        except Exception:
            return {"raw": value}
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    return {"value": value}


def _anthropic_content_to_text(value, is_error=None):
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, dict):
                continue
            typ = str(item.get("type") or "")
            if typ == "image":
                part = _anthropic_image_part(item)
                parts.append(_anthropic_text((part.get("image_url") or {}).get("url")))
            elif typ == "document":
                parts.append(_anthropic_document_text(item))
            else:
                parts.append(_anthropic_block_text(item))
        text = "".join(parts)
    elif isinstance(value, dict):
        text = _anthropic_block_text(value)
    else:
        text = _anthropic_text(value)
    if is_error is True:
        return "error: " + text
    return text


def _anthropic_block_text(block):
    if not isinstance(block, dict):
        return ""
    if "text" in block:
        return _anthropic_text(block.get("text"))
    if "content" in block:
        return _anthropic_content_to_text(block.get("content"))
    return ""


def _anthropic_image_part(block):
    source = block.get("source") if isinstance(block, dict) else None
    url = ""
    if isinstance(source, dict):
        stype = str(source.get("type") or "")
        if stype == "base64":
            media = _anthropic_text(source.get("media_type") or "image/png")
            data = _anthropic_text(source.get("data"))
            url = "data:%s;base64,%s" % (media, data)
        elif stype == "url":
            url = _anthropic_text(source.get("url"))
        elif stype == "file":
            url = _anthropic_text(source.get("file_id") or source.get("url"))
    return {"type": "image_url", "image_url": {"url": url}}


def _anthropic_document_text(block):
    if not isinstance(block, dict):
        return ""
    title = _anthropic_text(block.get("title"))
    source = block.get("source") if isinstance(block.get("source"), dict) else {}
    stype = str(source.get("type") or "")
    body = ""
    if stype == "text":
        body = _anthropic_text(source.get("data"))
    elif stype == "content":
        body = _anthropic_content_to_text(source.get("content"))
    elif stype == "base64":
        media = _anthropic_text(source.get("media_type") or "application/octet-stream")
        body = "[document %s omitted]" % media
    elif stype == "url":
        body = _anthropic_text(source.get("url"))
    elif stype == "file":
        body = _anthropic_text(source.get("file_id") or source.get("url"))
    if title:
        return title + "\n" + body
    return body


def _anthropic_system_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, dict):
                continue
            text = _anthropic_text(item.get("text"))
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _anthropic_tool_choice(value):
    if isinstance(value, str):
        val = value.strip().lower()
        return {"auto": "auto", "any": "required", "none": "none",
                "required": "required"}.get(val, val)
    if isinstance(value, dict):
        typ = str(value.get("type") or "").strip().lower()
        if typ == "auto":
            return "auto"
        if typ == "any":
            return "required"
        if typ == "none":
            return "none"
        if typ == "tool":
            return {"type": "function",
                    "function": {"name": _anthropic_text(value.get("name"))}}
    return None


def anthropic_tools_to_chat(tools):
    out = []
    skipped = []
    if not isinstance(tools, list):
        return out, skipped
    for item in tools:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("function"), dict):
            out.append(item)
            continue
        name = _anthropic_text(item.get("name") or item.get("type"))
        schema = item.get("input_schema")
        if schema is None:
            schema = item.get("parameters")
        if schema is None:
            if name:
                skipped.append(name)
            continue
        fn = {"name": name}
        if item.get("description") is not None:
            fn["description"] = item.get("description")
        fn["parameters"] = schema
        out.append({"type": "function", "function": fn})
    return out, skipped


def anthropic_effort_from_messages(payload):
    if not isinstance(payload, dict):
        return None
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        effort = _anthropic_text(output_config.get("effort")).strip()
        if effort:
            return effort
    thinking = payload.get("thinking")
    if not isinstance(thinking, dict):
        return None
    typ = _anthropic_text(thinking.get("type")).strip().lower()
    if typ == "disabled":
        return "none"
    if typ != "enabled":
        return None
    try:
        budget = int(thinking.get("budget_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    if budget >= 32000:
        return "xhigh"
    if budget >= 16000:
        return "high"
    if budget >= 8000:
        return "medium"
    return "low"


def _anthropic_blocks_to_messages(role, blocks):
    tool_calls = []
    tool_messages = []
    content_parts = []
    texts = []
    has_non_text = False
    for block in blocks:
        if not isinstance(block, dict):
            continue
        typ = str(block.get("type") or "")
        if typ in ("thinking", "redacted_thinking"):
            continue
        if typ in ("tool_use", "server_tool_use"):
            tool_calls.append({
                "id": _anthropic_text(block.get("id")),
                "type": "function",
                "function": {
                    "name": _anthropic_text(block.get("name")),
                    "arguments": _anthropic_json_text(block.get("input")),
                },
            })
            continue
        if typ == "tool_result":
            tool_messages.append({
                "role": "tool",
                "tool_call_id": _anthropic_text(block.get("tool_use_id")),
                "content": _anthropic_content_to_text(block.get("content"),
                                                      block.get("is_error")),
            })
            continue
        if typ == "image":
            part = _anthropic_image_part(block)
            if not (part.get("image_url") or {}).get("url"):
                continue
            has_non_text = True
            content_parts.append(part)
            continue
        if typ == "document":
            has_non_text = True
            content_parts.append({"type": "text", "text": _anthropic_document_text(block)})
            continue
        text = _anthropic_block_text(block)
        texts.append(text)
        content_parts.append({"type": "text", "text": text})
    out = list(tool_messages)
    if not tool_calls and not texts and not has_non_text:
        return out
    msg = {"role": role or "user"}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    if has_non_text:
        msg["content"] = content_parts
    elif texts:
        msg["content"] = "".join(texts)
    else:
        msg["content"] = ""
    out.append(msg)
    return out


def messages_to_chat(payload):
    """Translate an Anthropic Messages request into Chat Completions."""
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    model = _anthropic_text(payload.get("model")).strip()
    if not model:
        raise ValueError("model is required")
    messages_in = payload.get("messages")
    if not isinstance(messages_in, list):
        raise ValueError("messages must be a list")
    if not messages_in:
        raise ValueError("messages must not be empty")
    chat = {"model": model, "messages": []}
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_tokens") is not None:
        chat["max_tokens"] = payload["max_tokens"]
    else:
        default_max = model_default_max_output_tokens(model)
        if default_max:
            chat["max_tokens"] = default_max
    if payload.get("stream") is not None:
        chat["stream"] = bool(payload.get("stream"))
    stops = payload.get("stop_sequences")
    if isinstance(stops, list) and stops:
        chat["stop"] = stops
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata.get("user_id") is not None:
        chat["user"] = metadata.get("user_id")
    tools, skipped = anthropic_tools_to_chat(payload.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _anthropic_tool_choice(payload.get("tool_choice"))
    if choice is not None:
        chat["tool_choice"] = choice
    raw_choice = payload.get("tool_choice")
    if isinstance(raw_choice, dict) and raw_choice.get("disable_parallel_tool_use") is True:
        chat["parallel_tool_calls"] = False
    effort = anthropic_effort_from_messages(payload)
    if effort:
        chat["reasoning_effort"] = effort
    messages = []
    system = _anthropic_system_text(payload.get("system"))
    if skipped:
        note = ("These Anthropic server tools are not available here: "
                + ", ".join(skipped) + ". Do not call them.")
        system = (system + "\n" + note) if system else note
    if system:
        messages.append({"role": "system", "content": system})
    for index, item in enumerate(messages_in):
        if not isinstance(item, dict):
            continue
        role = _anthropic_text(item.get("role")).strip() or "user"
        content = item.get("content")
        if role in ("system", "developer"):
            # Claude Code >= 2.1.286 turns on Anthropic's mid-conversation
            # system beta and carries system messages inside `messages`. The
            # upstream knows the system role, so keep the message where the
            # client put it rather than failing the whole request.
            text = _anthropic_system_text(content) or _anthropic_content_to_text(content)
            if text:
                messages.append({"role": "system", "content": text})
            continue
        if role not in ("user", "assistant"):
            raise ValueError(
                "messages[%d].role %r must be user or assistant; "
                "pass a system prompt in the top-level system field" % (index, role))
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            messages.extend(_anthropic_blocks_to_messages(role, content))
        elif content is not None:
            text = _anthropic_text(content)
            if text:
                messages.append({"role": role, "content": text})
    chat["messages"] = messages
    return chat


def _anthropic_usage(usage):
    out = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    if not isinstance(usage, dict):
        return out
    try:
        out["input_tokens"] = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        pass
    try:
        out["output_tokens"] = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        pass
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = _best_cached_tokens(usage)
    try:
        out["cache_read_input_tokens"] = int(cached or 0)
    except (TypeError, ValueError):
        pass
    try:
        out["cache_creation_input_tokens"] = int(usage.get("prompt_cache_write_tokens") or 0)
    except (TypeError, ValueError):
        pass
    service_tier = usage.get("service_tier")
    if service_tier:
        out["service_tier"] = service_tier
    return out


def _anthropic_message_id(value):
    value = _anthropic_text(value)
    if not value:
        return _new_id("msg_")
    return value if value.startswith("msg_") else "msg_" + value


def _anthropic_stop_reason(finish_reason):
    value = str(finish_reason or "").strip().lower()
    if value == "length":
        return "max_tokens"
    if value in ("tool_calls", "function_call"):
        return "tool_use"
    if value in ("content_filter", "refusal"):
        return "refusal"
    return "end_turn"


def chat_to_messages(obj):
    """Fold one Chat Completions object into an Anthropic Messages object."""
    if not isinstance(obj, dict):
        obj = {}
    choices = obj.get("choices") if isinstance(obj.get("choices"), list) else []
    message = {}
    finish = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        finish = choices[0].get("finish_reason") or ""
    content = []
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(_anthropic_block_text(part) for part in text if isinstance(part, dict))
    text = _anthropic_text(text)
    if text:
        content.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            content.append({
                "type": "tool_use",
                "id": _anthropic_text(call.get("id")) or _new_id("toolu_"),
                "name": _anthropic_text(fn.get("name")),
                "input": _anthropic_parse_tool_input(fn.get("arguments")),
            })
    return {
        "id": _anthropic_message_id(obj.get("id")),
        "type": "message",
        "role": "assistant",
        "model": _anthropic_text(obj.get("model")),
        "content": content,
        "stop_reason": _anthropic_stop_reason(finish),
        "stop_sequence": None,
        "usage": _anthropic_usage(obj.get("usage")),
    }


def _anthropic_estimate_chat_tokens(chat):
    values = []
    if isinstance(chat, dict):
        for msg in chat.get("messages") or []:
            if isinstance(msg, dict):
                values.append(_anthropic_json_text(msg.get("content")))
                for call in msg.get("tool_calls") or []:
                    if isinstance(call, dict):
                        values.append(_anthropic_json_text(call))
        for tool in chat.get("tools") or []:
            values.append(_anthropic_json_text(tool))
    return sum(estimate_tokens(value) for value in values if value)


def stream_messages_events(raw_iter, model, holder=None):
    """Yield Anthropic Messages SSE frames from a chat-completions SSE stream."""
    holder = holder if isinstance(holder, dict) else {}
    state = {
        "started": False,
        "done": False,
        "failed": False,
        "id": "",
        "model": model,
        "stop": "end_turn",
        "text_open": False,
        "text_idx": None,
        "next": 0,
        "tools": {},
        "tool_order": [],
        "usage": None,
    }

    def usage_payload():
        return _anthropic_usage(state.get("usage"))

    def ensure_start(chunk=None):
        if state["started"]:
            return None
        state["started"] = True
        if isinstance(chunk, dict):
            state["id"] = _anthropic_text(chunk.get("id"))
            state["model"] = _anthropic_text(chunk.get("model")) or state["model"]
        return anthropic_sse_frame("message_start", {
            "message": {
                "id": _anthropic_message_id(state["id"]),
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": state["model"],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        })

    def ensure_text():
        if state["text_open"]:
            return None
        state["text_idx"] = state["next"]
        state["next"] += 1
        state["text_open"] = True
        return anthropic_sse_frame("content_block_start", {
            "index": state["text_idx"],
            "content_block": {"type": "text", "text": ""},
        })

    for line in raw_iter:
        if state["done"] or state["failed"]:
            break
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data:
            continue
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if isinstance(chunk.get("error"), dict):
            state["failed"] = True
            err = chunk["error"]
            yield anthropic_sse_frame("error", {
                "error": {
                    "type": anthropic_error_type(502),
                    "message": _anthropic_text(err.get("message") or "upstream error"),
                }
            })
            return
        start = ensure_start(chunk)
        if start:
            yield start
        # Capture usage before the choices guard: the final upstream frame
        # carries both the usage block and the finish_reason (and no delta),
        # so skipping it here would zero out every streaming usage row.
        if isinstance(chunk.get("usage"), dict):
            state["usage"] = chunk["usage"]
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        if not choices:
            continue
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            frame = ensure_text()
            if frame:
                yield frame
            yield anthropic_sse_frame("content_block_delta", {
                "index": state["text_idx"],
                "delta": {"type": "text_delta", "text": text},
            })
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            try:
                idx = int(call.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            entry = state["tools"].get(idx)
            if entry is None:
                entry = {"idx": idx, "block": None, "id": "", "name": "", "opened": False}
                state["tools"][idx] = entry
                state["tool_order"].append(idx)
            if call.get("id"):
                entry["id"] = _anthropic_text(call.get("id"))
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            if fn.get("name"):
                entry["name"] = _anthropic_text(fn.get("name"))
            args = fn.get("arguments") if fn.get("arguments") is not None else ""
            if not entry["opened"] and (entry["id"] or entry["name"] or args):
                if state["text_open"]:
                    state["text_open"] = False
                    yield anthropic_sse_frame("content_block_stop", {"index": state["text_idx"]})
                entry["opened"] = True
                entry["block"] = state["next"]
                state["next"] += 1
                if not entry["id"]:
                    entry["id"] = "toolu_" + str(idx)
                yield anthropic_sse_frame("content_block_start", {
                    "index": entry["block"],
                    "content_block": {
                        "type": "tool_use",
                        "id": entry["id"],
                        "name": entry["name"],
                        "input": {},
                    },
                })
            if entry["opened"] and args:
                yield anthropic_sse_frame("content_block_delta", {
                    "index": entry["block"],
                    "delta": {"type": "input_json_delta", "partial_json": _anthropic_text(args)},
                })
        finish = choice.get("finish_reason")
        if finish:
            state["stop"] = _anthropic_stop_reason(finish)
    if state["failed"]:
        return
    if not state["started"]:
        start = ensure_start(None)
        if start:
            yield start
    if state["text_open"]:
        state["text_open"] = False
        yield anthropic_sse_frame("content_block_stop", {"index": state["text_idx"]})
    for idx in state["tool_order"]:
        entry = state["tools"][idx]
        if not entry["opened"]:
            continue
        yield anthropic_sse_frame("content_block_stop", {"index": entry["block"]})
    state["done"] = True
    # Hand the raw upstream usage block back to the caller: the stream
    # response records it on the usage row, and without this every streamed
    # Messages request was logged as usage_missing.
    holder["usage"] = state.get("usage")
    yield anthropic_sse_frame("message_delta", {
        "delta": {"stop_reason": state["stop"], "stop_sequence": None},
        "usage": usage_payload(),
    })
    yield anthropic_sse_frame("message_stop", {})


def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {"reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }
def chat_to_response(chat_obj, model, custom_names=None, request_meta=None, namespace_map=None, sources=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names is the set of tool names the client declared as freeform
    ("custom"). Calls to those tools are re-inflated into custom_tool_call
    items so clients such as Codex recognise them.

    request_meta echoes the request-level capabilities (tools, tool_choice,
    parallel_tool_calls) back on the response. They used to be hardcoded to
    tools=[], tool_choice=auto and parallel_tool_calls=true, so a client that
    asked for something else was told the opposite of what it requested.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = dsml_safe_prefix(msg.get("reasoning_content") or "", final=True)[0]
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            })
        else:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            })
    # DeepSeek DSML tool calls fallback
    if not (msg.get("tool_calls")):
        dsml_calls, clean_t = parse_dsml_tool_calls(text)
        if dsml_calls:
            for dc in dsml_calls:
                output.append({
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                })
            text = clean_t
    if re.search(TAG_START, text):
        text, _ = quarantine_dsml_text(text)
    if text or not output:
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": build_citations(text, sources)}] if text else [],
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "metadata": {},
    }
    if namespace_map:
        output, _ns_fixed = apply_namespace_to_calls(output, namespace_map)
        obj["output"] = output
    meta = request_meta or {}
    obj["parallel_tool_calls"] = meta.get("parallel_tool_calls", True)
    obj["tool_choice"] = meta.get("tool_choice", "auto")
    obj["tools"] = meta.get("tools") or []
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    if chat_obj.get("_ops_failure"):
        obj["status"] = "failed"
        obj["error"] = {"code": chat_obj["_ops_failure"],
                        "message": "Upstream response could not complete: " + chat_obj["_ops_failure"]}
        obj.pop("incomplete_details", None)
    return obj
class _ClientStreamClosed(Exception):
    pass


def _write_stream_frame(handler, frame):
    try:
        handler.wfile.write(frame)
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as exc:
        raise _ClientStreamClosed() from exc


def _attempt_recorder(holder, model, fp, t_start, stream=True, usage_field="usage"):
    def record(outcome=None):
        if holder.get("usage_recorded"):
            return
        account = holder.get("account")
        uid = getattr(account, "uid", None) or holder.get("account_uid")
        outcome = {"retry": "failed", "web_tool_followup": "completed"}.get(outcome, outcome or "completed")
        record_usage(model, holder.get(usage_field), stream=stream,
                     elapsed_ms=int((time.time() - t_start) * 1000), fp=fp,
                     account=uid, outcome=outcome,
                     key=holder.get("key_id"), effort=holder.get("effort"))
        holder["usage_recorded"] = True
        apply_daily_token_limit(refresh=True)
    return record


def _chat_frame(holder, delta=None, finish=None, usage=None):
    frame = {"id": holder.setdefault("chat_id", "chatcmpl-wb-" + uuid.uuid4().hex),
             "object": "chat.completion.chunk", "created": holder.setdefault("chat_created", int(time.time())),
             "model": holder["model"], "choices": [
                 {"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        frame["usage"] = usage
    return ("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n").encode("utf-8")


def _chat_error_frames(holder, code):
    error = {"error": {"message": "Upstream response could not complete: " + code,
                       "type": "server_error", "code": code}}
    yield ("data: " + json.dumps(error) + "\n\n").encode("utf-8")
    yield _chat_frame(holder, finish="length", usage=holder.get("usage"))


def _stream_chat_events(upstream, model, holder):
    """One Chat response, one terminal chunk, and no replay after usable output."""
    holder["model"] = model
    current = upstream
    total_usage = None
    try:
        while True:
            current = _GuardedStream(current, holder)
            text, reason = [], []
            holder["text_parts"], holder["reason_parts"] = text, reason
            holder["usage_this_round"] = None
            holder["round_accounted"] = False
            holder["usage_recorded"] = False
            scrubber = ReasoningScrubber()
            held_finish, finish, saw_finish = None, "stop", False
            for raw in current:
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                data = strip_data_prefix(line)
                if data == "[DONE]":
                    break
                if not data:
                    continue
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                if frame.get("error"):
                    code = (frame["error"] or {}).get("code")
                    if code in _STREAM_GUARD_CODES:
                        holder["incomplete_reason"] = code
                        break
                    raise RuntimeError("upstream returned an SSE error")
                if frame.get("id"):
                    holder.setdefault("chat_id", frame["id"])
                if frame.get("created"):
                    holder.setdefault("chat_created", frame["created"])
                if frame.get("usage"):
                    holder["usage_this_round"] = frame["usage"]
                terminal = False
                for choice in frame.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        safe = scrubber.feed(delta["reasoning_content"])
                        delta["reasoning_content"] = safe
                        if safe:
                            reason.append(safe)
                    if delta.get("content"):
                        text.append(delta["content"])
                        if str(delta["content"]).strip():
                            holder["client_content_seen"] = True
                    calls = delta.get("tool_calls") or []
                    legacy = delta.get("function_call") or {}
                    if any(c.get("id") or (c.get("function") or {}).get("name") or
                           (c.get("function") or {}).get("arguments") for c in calls) or (
                               isinstance(legacy, dict) and (legacy.get("name") or legacy.get("arguments"))):
                        holder["client_tool_calls_seen"] = True
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
                        saw_finish, terminal = True, True
                        choice["finish_reason"] = None
                if holder.get("incomplete_reason"):
                    break
                frame["id"] = holder.setdefault("chat_id", "chatcmpl-wb-" + uuid.uuid4().hex)
                frame["created"] = holder.setdefault("chat_created", int(time.time()))
                frame["model"] = model
                if terminal:
                    held_finish = finish
                cleaned = clean_chunk(json.dumps(frame, ensure_ascii=False))
                if cleaned:
                    yield ("data: " + cleaned + "\n\n").encode("utf-8")
            tail = scrubber.feed(final=True)
            if tail and not holder.get("incomplete_reason"):
                reason.append(tail)
                yield _chat_frame(holder, {"reasoning_content": tail})
            if not saw_finish and not holder.get("incomplete_reason"):
                holder["incomplete_reason"] = "upstream_truncated"
            usage = holder.get("usage_this_round")
            if not usage or not usage.get("total_tokens"):
                completion = estimate_tokens("".join(text + reason))
                prompt = max(1, estimate_tokens(str((holder.get("base_body") or {}).get("messages") or "")))
                usage = {"prompt_tokens": prompt, "completion_tokens": completion,
                         "total_tokens": prompt + completion,
                         "completion_tokens_details": {"reasoning_tokens": estimate_tokens("".join(reason))}}
            holder["usage_this_round"] = usage
            total_usage = sum_usage(total_usage, usage)
            holder["usage"] = total_usage
            holder["round_accounted"] = True
            uid = getattr(holder.get("account"), "uid", None)
            if uid:
                ledger = holder.setdefault("account_usage", {})
                ledger[uid] = sum_usage(ledger.get(uid), usage)
            gate = reasoning_only_turn(text, holder.get("client_tool_calls_seen"), finish,
                                       holder.get("incomplete_reason"))
            gate = gate and not holder.get("client_content_seen")
            holder["thinking_only"] = bool(gate)
            _close_upstream(current)
            if _try_loop_continuation(holder, model, holder.get("incomplete_reason"),
                                      "".join(reason), "".join(text).strip(), holder.get("client_tool_calls_seen")):
                current = holder.pop("continue_upstream")
                holder["account"] = holder.pop("continue_account")
                holder["retried"] = True
                continue
            if gate and _start_empty_continuation(holder):
                current = holder.pop("continue_upstream")
                holder["account"] = holder.pop("continue_account")
                holder["retried"] = True
                continue
            if gate:
                holder["incomplete_reason"] = holder.get("continuation_failure_code") or (
                    "reasoning_only_turn" if reason else "empty_output_turn")
            _finish_loop_retry(holder, not holder.get("incomplete_reason") and finish != "length" and
                               bool(holder.get("client_content_seen") or holder.get("client_tool_calls_seen")))
            if holder.get("incomplete_reason"):
                yield from _chat_error_frames(holder, holder["incomplete_reason"])
            else:
                yield _chat_frame(holder, finish=held_finish or finish, usage=total_usage)
            yield b"data: [DONE]\n\n"
            return
    finally:
        _close_upstream(current)
        _finish_loop_retry(holder, False)


def _start_empty_continuation(holder, kind="thinking_only"):
    callback = holder.get("retry_call")
    if not callback or holder.get("retried") or holder.get("continuation_attempts", 0) >= CONTINUATION_MAX_ATTEMPTS:
        return False
    holder["continuation_attempts"] = holder.get("continuation_attempts", 0) + 1
    retry_bump(kind)
    if holder.get("record_attempt"):
        holder["record_attempt"]("retry")
    deadline_at = time.monotonic() + FIRST_FRAME_DEADLINE
    generation_end = holder.get("generation_deadline")
    if generation_end is not None:
        deadline_at = min(deadline_at, generation_end)
        if time.monotonic() >= deadline_at:
            holder["continuation_failure_code"] = "generation_timeout"
            return False
    upstream = None
    probing = False
    try:
        uid = getattr(holder.get("account"), "uid", None) or holder.get("account_uid")
        excluded = [uid] if kind == "reasoning_loop" and uid else []
        if excluded and POOL is not None:
            body = holder.get("base_body") or {}
            model = body.get("model") or holder.get("model")
            realm = holder.get("realm") or detect_model_realm(model) or CURRENT_REALM
            # Do not exclude the only usable account. The original selector
            # still enforces quota, region, cooldown and identity policies.
            if POOL.count_ready(realm, model=model) <= 1:
                excluded = []
        if getattr(callback, "_ops_deadline", False):
            options = {"deadline_at": deadline_at}
            if (kind == "reasoning_loop" and getattr(callback, "_ops_recovery_hint", False) and
                    os.environ.get("WB_OPS_LOOP_RECOVERY_HINT", "0").lower() in ("1", "true", "on")):
                options["recovery_hint"] = True
            upstream, account = callback(excluded, **options)
        else:
            upstream, account = callback(excluded)
        if upstream is None:
            raise RuntimeError("no upstream for the empty-turn retry")
        probing = True
        probed, replacement = probe_first_frame(
            upstream, retry_call=None if kind == "reasoning_loop" else callback, tried=[getattr(account, "uid", "")],
            deadline_at=deadline_at, require_frame=(kind == "reasoning_loop"))
        if probed is None:
            holder["continuation_failure_code"] = "continuation_probe_failed"
            raise RuntimeError("empty-turn retry failed its first-frame probe")
        holder["continue_upstream"] = probed
        holder["continue_account"] = replacement or account
        holder["will_continue"] = True
        return True
    except Exception as exc:
        if upstream is not None:
            _close_upstream(upstream)
        holder["continuation_error"] = type(exc).__name__
        if isinstance(exc, ContentRejected):
            holder["continuation_failure_code"] = "upstream_content_rejected"
        elif isinstance(exc, RateLimited):
            holder["continuation_failure_code"] = "upstream_rate_limited"
        elif generation_end is not None and time.monotonic() >= generation_end:
            holder["continuation_failure_code"] = "generation_timeout"
        elif isinstance(exc, (TimeoutError, socket.timeout)):
            holder["continuation_failure_code"] = "continuation_timeout"
        else:
            holder.setdefault("continuation_failure_code", "continuation_probe_failed" if probing else "continuation_request_failed")
        retry_bump(kind + "_giveup")
        return False


def _aggregate_with_recovery(upstream, model, account, holder):
    """One shared recovery budget for empty stops and safe reasoning loops."""
    current = upstream
    try:
        while True:
            holder["account"] = account
            holder["usage_recorded"] = False
            obj = aggregate_stream(current, model, None, holder)
            holder["usage"] = obj.get("usage")
            holder["total_usage"] = sum_usage(holder.get("total_usage"), obj.get("usage"))
            message = ((obj.get("choices") or [{}])[0].get("message") or {})
            finish = ((obj.get("choices") or [{}])[0].get("finish_reason") or "stop")
            failure = obj.get("_ops_failure")
            gate = reasoning_only_turn(message.get("content"), message.get("tool_calls"), finish, failure)
            _close_upstream(current)
            if _try_loop_continuation(holder, model, failure, message.get("reasoning_content", ""),
                                      (message.get("content") or "").strip(), message.get("tool_calls")):
                current = holder.pop("continue_upstream")
                account = holder.pop("continue_account")
                holder["retried"] = True
                continue
            if gate and _start_empty_continuation(holder):
                current = holder.pop("continue_upstream")
                account = holder.pop("continue_account")
                holder["retried"] = True
                continue
            if gate:
                obj["_ops_failure"] = holder.get("continuation_failure_code") or (
                    "reasoning_only_turn" if message.get("reasoning_content") else "empty_output_turn")
            if failure == "reasoning_loop_detected" and holder.get("continuation_failure_code"):
                obj["_ops_failure"] = holder["continuation_failure_code"]
            _finish_loop_retry(holder, not obj.get("_ops_failure") and finish != "length" and
                               bool(message.get("content") or message.get("tool_calls")))
            return obj, account
    finally:
        _close_upstream(current)
        _finish_loop_retry(holder, False)
        pending = holder.pop("continue_upstream", None)
        if pending is not None:
            _close_upstream(pending)


def _response_failed_frame(holder, model, code):
    holder["terminal_sent"] = True
    seq = holder.get("sequence_number", 0) + 1
    holder["sequence_number"] = seq
    response = {"id": holder.get("resp_id") or _new_id("resp_"),
                "object": "response", "created_at": holder.get("created") or int(time.time()),
                "status": "failed", "model": model,
                "output": [item for item in holder.get("response_outputs", []) if item],
                "output_text": "".join(holder.get("prior_text_parts", []) + holder.get("text_parts", [])),
                "error": {"code": code, "message": "Upstream response could not complete: " + code}}
    data = {"type": "response.failed", "sequence_number": seq, "response": response}
    return ("event: response.failed\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")


def stream_responses_events(upstream, model, holder):
    holder["model"] = model
    guarded = _GuardedStream(upstream, holder)
    try:
        yield from _stream_responses_impl(guarded, model, holder)
    finally:
        guarded.close()


def _stream_responses_impl(upstream, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks."""
    resp_id = holder.setdefault("resp_id", _new_id("resp_"))
    created = holder.setdefault("created", int(time.time()))
    msg_id, rs_id = _new_id("msg_"), _new_id("rs_")
    seq = holder.get("sequence_number", 0)
    text_parts, reason_parts = [], []
    holder["text_parts"], holder["reason_parts"] = text_parts, reason_parts
    outputs = holder.setdefault("response_outputs", [])
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    saw_done = False
    scrubber = ReasoningScrubber()
    saw_finish = False
    custom_names = set(holder.get("custom_names") or ())
    ns_map = holder.get("namespace_map") or {}
    # 由反代代跑的网路工具呼叫，收集起来不转发给客户端
    _internal_calls = {}
    # Only reach for same-named calls when this request's definitions were the
    # gateway's own (see web_tools_active); otherwise they belong to the client.
    _own_web_tools = web_tools_active(holder.get("base_body"))
    # Echo the request capabilities the client actually sent, same as the
    # non-streaming path; these were hardcoded before.
    meta = holder.get("request_meta") or {}
    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(holder.get("prior_text_parts", []) + text_parts),
            "parallel_tool_calls": meta.get("parallel_tool_calls", True),
            "tool_choice": meta.get("tool_choice", "auto"),
            "tools": meta.get("tools") or [],
            "metadata": {},
        }
        u = _responses_usage(sum_usage(holder.get("prior_usage"), usage))
        if u:
            obj["usage"] = u
        if ns_map:
            obj["output"], _nsf = apply_namespace_to_calls(obj.get("output") or [], ns_map)
        return obj
    def ev(etype, payload):
        nonlocal seq
        seq += 1
        holder["sequence_number"] = seq
        if etype == "response.output_text.delta" and str(payload.get("delta") or "").strip():
            holder["client_content_seen"] = True
        if etype == "response.output_item.added" and (payload.get("item") or {}).get("type") in ("function_call", "custom_tool_call"):
            holder["client_tool_calls_seen"] = True
        data = {"type": etype, "sequence_number": seq}
        data.update(payload)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + chr(10) + "data: " + body + chr(10) + chr(10)).encode("utf-8")
    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }
    def _annotations():
        """引用来源：只认工具真的回传过的网址。"""
        try:
            return build_citations("".join(text_parts), holder.get("web_sources") or [])
        except Exception:
            return []

    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text", "text": "".join(text_parts),
                              "annotations": _annotations()}]
        return item
    def emit_reason(piece):
        nonlocal reason_index
        if not piece:
            return
        if reason_index is None:
            reason_index = len(outputs)
            outputs.append(None)
            yield ev("response.output_item.added",
                     {"output_index": reason_index, "item": reason_item("in_progress")})
            yield ev("response.reasoning_summary_part.added", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            })
        reason_parts.append(piece)
        yield ev("response.reasoning_summary_text.delta", {
            "item_id": rs_id, "output_index": reason_index,
            "summary_index": 0, "delta": piece,
        })

    def _finalize():
        # Close out the stream: reasoning item, structured tool calls,
        # DSML fallback, the message item and response.completed.
        nonlocal msg_index, text_buffer
        yield from emit_reason(scrubber.feed(final=True))
        if reason_index is not None and outputs[reason_index] is None:
            full_r = "".join(reason_parts)
            yield ev("response.reasoning_summary_text.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0, "text": full_r,
            })
            yield ev("response.reasoning_summary_part.done", {
                "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                "part": {"type": "summary_text", "text": full_r},
            })
            outputs[reason_index] = reason_item("completed")
            yield ev("response.output_item.done",
                     {"output_index": reason_index, "item": outputs[reason_index]})
        # 1. Emit completed structured tool calls
        complete_calls = saw_finish and not holder.get("incomplete_reason")
        for idx in (sorted(tool_calls_map.keys()) if complete_calls else ()):
            entry = tool_calls_map[idx]
            if entry.get("custom"):
                yield ev("response.custom_tool_call_input.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "custom_tool_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "input": _unwrap_custom_input(entry["arguments"]),
                }
            else:
                yield ev("response.function_call_arguments.done", {
                    "output_index": entry["output_index"],
                    "item_id": entry["item_id"],
                    "call_id": entry["id"],
                    "arguments": entry["arguments"],
                })
                fc_item = {
                    "id": entry["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": entry["id"],
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                }
            stamp_namespace(fc_item, ns_map)
            outputs[entry["output_index"]] = fc_item
            yield ev("response.output_item.done", {
                "output_index": entry["output_index"],
                "item": fc_item,
            })
        # Flush remaining buffered text if any
        if text_buffer:
            calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
            if calls_rem:
                dsml_tool_calls.extend(calls_rem)
            if clean_rem:
                if re.search(TAG_START, clean_rem):
                    clean_rem, _ = quarantine_dsml_text(clean_rem)
                text_parts.append(clean_rem)
                if msg_index is not None:
                    yield ev("response.output_text.delta", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0, "delta": clean_rem,
                    })
            text_buffer = ""
        # 2. DSML fallback: emit buffered/parsed DSML tool calls if no structured tool_calls were emitted
        full_text = "".join(text_parts)
        dsml_calls = dsml_tool_calls
        if not dsml_calls:
            extra_calls, clean_text = parse_dsml_tool_calls(full_text)
            if extra_calls:
                dsml_calls = extra_calls
                full_text = clean_text
            elif re.search(TAG_START, clean_text):
                full_text, _ = quarantine_dsml_text(clean_text)
        if dsml_calls and not tool_calls_map and complete_calls:
            for dc in dsml_calls:
                # DSML 形状的网路工具呼叫一样由反代执行
                if _own_web_tools and wb_webtools.is_internal_tool(dc.get("name")):
                    entry = _internal_calls.setdefault(dc.get("id") or _new_id("call_"),
                                                       {"name": dc.get("name"), "arguments": "{}"})
                    entry["name"] = dc.get("name") or entry["name"]
                    entry["arguments"] = dc.get("arguments") or entry.get("arguments") or "{}"
                    continue
                out_idx = len(outputs)
                fc_item = {
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                }
                stamp_namespace(fc_item, ns_map)
                outputs.append(fc_item)
                yield ev("response.output_item.added", {
                    "output_index": out_idx,
                    "item": dict(fc_item, status="in_progress", arguments=""),
                })
                yield ev("response.function_call_arguments.delta", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "delta": fc_item["arguments"],
                })
                yield ev("response.function_call_arguments.done", {
                    "output_index": out_idx,
                    "item_id": fc_item["id"],
                    "call_id": fc_item["call_id"],
                    "arguments": fc_item["arguments"],
                })
                yield ev("response.output_item.done", {
                    "output_index": out_idx,
                    "item": fc_item,
                })
        # 这一轮如果有代跑的网路工具呼叫，就把完成事件留给下一轮，
        # 否则客户端会以为整个回合已经结束（旧版是在回合用尽时补一个合成的
        # resp_wrapup，那才是 issue #43 真正的病灶）。
        if _internal_calls and holder.get("web_tools_withdrawn"):
            holder["incomplete_reason"] = "web_tool_budget_exhausted"
        if _internal_calls and saw_finish and not holder.get("incomplete_reason"):
            holder.setdefault("internal_calls", []).extend(
                {"name": v["name"], "arguments": v["arguments"]}
                for v in _internal_calls.values()
            )
            holder["suppress_completion"] = True
            # 让 App 画出原生的「已搜寻网路」卡片：对每个代跑的呼叫送出
            # web_search_call 项目与生命周期事件。
            for _v in _internal_calls.values():
                _nm = str(_v.get("name") or "")
                try:
                    _a = json.loads(_v.get("arguments") or "{}")
                except Exception:
                    _a = {}
                if not isinstance(_a, dict):
                    _a = {}
                if _nm == wb_webtools.WEB_FETCH_NAME:
                    _action = {"type": "open_page", "url": wb_webtools.url_arg(_a)}
                else:
                    _action = {"type": "search", "query": wb_webtools.query_args(_a)}
                _ws_id = _new_id("ws_")
                _ws_idx = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": _ws_idx,
                    "item": {"id": _ws_id, "type": "web_search_call",
                             "status": "in_progress"},
                })
                yield ev("response.web_search_call.in_progress", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                yield ev("response.web_search_call.searching", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
                _ws_item = {"id": _ws_id, "type": "web_search_call", "status": "completed"}
                if _action.get("query") or _action.get("url"):
                    _ws_item["action"] = _action
                outputs[_ws_idx] = _ws_item
                yield ev("response.output_item.done", {
                    "output_index": _ws_idx, "item": _ws_item,
                })
                yield ev("response.web_search_call.completed", {
                    "output_index": _ws_idx, "item_id": _ws_id,
                })
        # Empty attempts must not leave a phantom message in the final response.
        if msg_index is not None or full_text:
            if msg_index is None:
                msg_index = len(outputs)
                outputs.append(None)
                yield ev("response.output_item.added", {
                    "output_index": msg_index,
                    "item": {"id": msg_id, "type": "message", "status": "in_progress",
                             "role": "assistant", "content": []},
                })
                yield ev("response.content_part.added", {
                    "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                })
            yield ev("response.output_text.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0, "text": full_text,
            })
            yield ev("response.content_part.done", {
                "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                "part": {"type": "output_text", "text": full_text,
                         "annotations": _annotations()},
            })
            outputs[msg_index] = msg_item("completed")
            yield ev("response.output_item.done", {"output_index": msg_index, "item": outputs[msg_index]})
        nonlocal usage
        if usage is None or (usage.get("total_tokens") or 0) == 0:
            out_txt = "".join(text_parts)
            rs_txt = "".join(reason_parts)
            if out_txt or rs_txt:
                comp = estimate_tokens(out_txt) + estimate_tokens(rs_txt)
                hint = holder.get("retry_body") or holder.get("base_body") or {}
                prompt_est = max(1, estimate_tokens(str(hint.get("messages") or meta.get("input") or "")))
                usage = {
                    "prompt_tokens": prompt_est,
                    "completion_tokens": comp,
                    "total_tokens": prompt_est + comp,
                    "completion_tokens_details": {"reasoning_tokens": estimate_tokens(rs_txt)},
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
                holder["usage"] = usage
        holder["text"], holder["reasoning"] = "".join(text_parts), "".join(reason_parts)
        failure = holder.get("incomplete_reason")
        if not saw_finish and not failure:
            failure = "upstream_truncated"
        gate = reasoning_only_turn(text_parts, tool_calls_map or dsml_calls or _internal_calls,
                                   finish, failure)
        gate = gate and not holder.get("client_content_seen") and not holder.get("client_tool_calls_seen")
        holder["thinking_only"], holder["gate_ok"] = bool(gate), bool(gate)
        if failure == "reasoning_loop_detected":
            _close_upstream(upstream)
            if _try_loop_continuation(holder, model, failure, "".join(reason_parts),
                                      "".join(text_parts).strip(), tool_calls_map or dsml_calls or _internal_calls):
                failure = None
            else:
                failure = holder.get("continuation_failure_code") or failure
        if gate:
            _close_upstream(upstream)
            if not _start_empty_continuation(holder):
                failure = holder.get("continuation_failure_code") or (
                    "reasoning_only_turn" if reason_parts else "empty_output_turn")
        holder["incomplete_reason"] = failure
        status = "completed" if finish != "length" and not dropped_truncated else "incomplete"
        final = resp_obj(status)
        if finish == "length":
            final["incomplete_details"] = {"reason": "max_output_tokens"}
        if not holder.get("suppress_completion") and not holder.get("will_continue"):
            _finish_loop_retry(holder, not failure and finish != "length" and
                               bool(text_parts or tool_calls_map or dsml_calls or _internal_calls))
            holder["terminal_sent"] = True
            if failure:
                final["status"] = "failed"
                final["error"] = {"code": failure, "message": "Upstream response could not complete: " + failure}
                final.pop("incomplete_details", None)
                yield ev("response.failed", {"response": final})
            else:
                yield ev("response.completed", {"response": final})
        holder["prior_text_parts"] = holder.get("prior_text_parts", []) + text_parts
        holder["prior_usage"] = sum_usage(holder.get("prior_usage"), usage)

    # 只有第一轮开场。第二轮以后再送一次 response.created，客户端会
    # 看到同一则回应被开了两次。
    if not holder.get("suppress_lifecycle") and holder.get("emit_created", True):
        yield ev("response.created", {"response": resp_obj("in_progress")})
        yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in upstream:
        data = strip_data_prefix(raw.decode("utf-8", "replace"))
        if data == "[DONE]":
            saw_done = True
            break
        if not data:
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if chunk.get("error"):
            code = (chunk["error"] or {}).get("code")
            if code in _STREAM_GUARD_CODES:
                holder["incomplete_reason"] = code
                break
            raise RuntimeError("upstream returned an SSE error")
        u = chunk.get("usage")
        if u:
            if usage is None or (u.get("total_tokens") or 0) >= (usage.get("total_tokens") or 0):
                usage = u
                holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                yield from emit_reason(scrubber.feed(piece))
                if holder.get("incomplete_reason"):
                    break
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                if not (call_id or fn_name or fn_args):
                    continue
                # web_search / web_fetch 由反代执行，不转发给客户端
                if idx in _internal_calls or (
                        _own_web_tools and fn_name
                        and wb_webtools.is_internal_tool(fn_name)):
                    entry = _internal_calls.setdefault(idx, {"name": fn_name, "arguments": ""})
                    if fn_name:
                        entry["name"] = fn_name
                    if fn_args:
                        entry["arguments"] += fn_args
                    continue
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {
                        "id": entry["item_id"],
                        "status": "in_progress",
                        "call_id": c_id,
                        "name": fn_name,
                    }
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    # namespace 必须在 output_item.added 就带上（照 CiderCC-UwU
                    # proxy.mjs openItem 的做法）。事后才补只会改到 done，
                    # 客户端早就从 added 事件派发过了。
                    stamp_namespace(item, ns_map)
                    yield ev("response.output_item.added", {
                        "output_index": out_idx,
                        "item": item,
                    })
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                        if entry.get("custom"):
                            yield ev("response.custom_tool_call_input.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
                        else:
                            yield ev("response.function_call_arguments.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
            piece = delta.get("content")
            if piece:
                yield from emit_reason(scrubber.feed(final=True))
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index, "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index, "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message", "status": "in_progress",
                                 "role": "assistant", "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index, "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": _annotations()},
                    })
                # DSML tool call buffering: do not stream raw DSML tags to client
                text_buffer += piece
                while text_buffer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx+30]
                    is_cand = ("DSML" in cand) or (len(cand) < 10 and not any(c in cand for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx+1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
                saw_finish = True
        if holder.get("incomplete_reason"):
            break
    # Truncated streams (max_tokens, dropped connection) can leave tool-call
    # arguments as half-written JSON. Do not close those calls out as
    # completed: drop them before finalize so the client never receives a
    # function_call_arguments.done / output_item.done with unparsable
    # arguments. Complete calls in the same batch are kept.
    dropped_truncated = 0
    if tool_calls_map and (finish == "length" or not saw_done):
        for idx in sorted(tool_calls_map.keys()):
            entry = tool_calls_map[idx]
            if is_truncated_arguments(entry.get("arguments")):
                tool_calls_map.pop(idx, None)
                dropped_truncated += 1
    yield from _finalize()

# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def _if_none_match_hit(header_value, etag):
    """If-None-Match 头是否命中给定的 ETag。

    RFC 7232 §3.2：字段值是一个逗号分隔的 entity-tag 列表，且 If-None-Match
    用弱比较——W/ 前缀忽略，所以 W/"x" 与 "x" 等同；"*" 匹配任何已存在的表示。
    """
    for token in header_value.split(","):
        token = token.strip()
        if token == "*":
            return True
        if token.startswith("W/"):
            token = token[2:].strip()
        if token and token == etag:
            return True
    return False


def _cache_etag(kind, key, built_at):
    """给带 TTL 缓存的只读接口派生一个强校验符（ETag）。

    由 (接口名, 缓存键, 缓存条目的构建时刻) 三者派生，**不是**对响应字节做哈希：

      - 构建时刻在两次重建之间是恒定的，所以"数字没变"的轮询回 304；缓存一重建
        它必然变化，客户端立刻拿到新数据。哈希响应字节恰好相反——payload 里有
        time.time() 派生的字段（/usage 的 started/since、/usage/timeseries 的
        桶边界与 until），同一份缓存被序列化两次都不逐字节相同，tag 会永远不命中，
        每次轮询照样重传 10~100KB，还白搭一次全量序列化 + 哈希的 CPU。
      - (键, 时刻) 都是现成的，一次 sha1 只哈希几十字节的键文本；哈希整个响应体
        则要先把 JSON 序列化出来，在 5 秒一轮的热路径上反而更贵。

    键文本进摘要而不是直接拼进 tag：缓存键里含调用方给的 realm，它可能带引号 /
    反斜杠（`?realm=a"b` 一路原样进键），拼出来会发出一个畸形的 ETag 头；摘要
    则永远是合法的 etagc 字符。摘要只取 16 个十六进制位：它只需要在"同一实例的
    少量缓存条目"之间区分，不是安全边界。
    """
    stamp = "%x" % int(built_at * 1_000_000)
    digest = hashlib.sha1(("%s\x1f%s" % (kind, key)).encode("utf-8")).hexdigest()[:16]
    return '"%s-%s"' % (stamp, digest)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None
    # 本次 _json() 响应要附带的校验符（由 _json_cached() 设置）。放成实例属性
    # 而不是参数，是因为测试里大量 Handler 桩只覆写了 _json(code, obj) 这两个
    # 位置参数——加参数会让它们全部报错，而属性默认 None 对它们完全无感。
    _json_etag = None
    # The stdlib default caps the request line at 64KB and answers an opaque
    # bare "414 Request-URI Too Long" for anything longer. Raise it and reply in
    # the normal JSON error shape so an over-long URL is diagnosable.
    max_request_line = 1024 * 1024
    def handle_one_request(self):
        # Reset per-request auth state. HTTP/1.1 keeps the connection alive, so
        # one Handler instance serves many requests; a request that authenticates
        # via the panel token never reassigns key_entry, and without this reset
        # it inherited the realm binding of whatever API key used the connection
        # before it - sending that request to the wrong upstream exit.
        self.key_entry = None
        # 校验符同理：留着上一轮的 tag，下一次 _json() 就会把别的请求的 ETag
        # 发出去（keep-alive 上同一个 Handler 实例服务一整条连接）。
        self._json_etag = None
        # Body-tracking state must also start clean for every request, otherwise
        # a later drain would skip a body that has not been read yet.
        self._body_consumed = False
        try:
            self.raw_requestline = self.rfile.readline(self.max_request_line + 1)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
            return
        except Exception:
            self.close_connection = True
            return
        if len(self.raw_requestline) > self.max_request_line:
            self.requestline = ''
            self.request_version = ''
            self.command = ''
            # The cap is enforced by reading at most max_request_line + 1
            # bytes, so the rest of the oversized line is still in the socket.
            # Replying and then closing with unread data pending makes the OS
            # send an RST, which discards the buffered reply - the client sees
            # a reset and no error at all. Drain a bounded amount first so the
            # 414 actually arrives.
            self._drain_oversized_request_line()
            try:
                self._error(414, "request line too long (limit %d bytes); "
                                 "put long content in the POST body, not the URL"
                            % self.max_request_line, "invalid_request_error")
            except Exception:
                pass
            self.close_connection = True
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        mname = 'do_' + self.command
        if not hasattr(self, mname):
            self.send_error(501, "Unsupported method (%r)" % self.command)
            return
        getattr(self, mname)()
        self.wfile.flush()
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
    server_version = "wb-proxy/1.6.19"
    def log_message(self, fmt, *args):
        # 静默过滤前端看板高频定时心跳的正常 200 GET 请求（/logs、/usage、/accounts 轮询等）
        # 避免自增死循环刷屏与日志污染。遇 4xx/5xx 异常或所有非 GET 业务操作依然如实记录。
        try:
            status_code = int(args[1]) if len(args) > 1 and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                req_path = (getattr(self, "path", None) or (args[0] if args else "")).split("?")[0]
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm", "/favicon.ico"
                )
                if any(req_path == p or req_path.startswith(p + "/") for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 只有 _json_cached() 覆盖的只读接口才有校验符；其余调用点保持原样，
        # 一个头都不多发。
        etag = self._json_etag
        if etag is not None:
            # no-cache 而不是 no-store：允许浏览器存一份，但每次使用前必须回源
            # 校验。配合 ETag，校验命中就是一次 304（无 body），而不是重传
            # 10~100KB 的 JSON；no-store 会让浏览器连存都不存，条件请求无从谈起。
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
        # self.path is unset when parse_request() never ran (an over-long
        # request line is rejected before it), so fall back to "".
        if cors_origin_allowed(getattr(self, "path", "") or ""):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
        # Flush here rather than relying on the caller: with HTTP/1.1
        # keep-alive the client blocks until the response is actually on the
        # wire, and an error reply only flushed at the end of the handler looks
        # like a hung request.
        try:
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
    def _json_cached(self, code, data_fn, etag_fn):
        """带 TTL 缓存的只读接口：_json() + 条件请求（命中 If-None-Match 回 304）。

        data_fn() 取数、etag_fn() 取"当前缓存条目的校验符"，两者在这里配对调用，
        而且是**取数前后各取一次戳**：只有两次相同，才敢把这个 tag 发出去。这个
        相等就是"这份响应体确实出自该 tag 所描述的那条缓存条目"的证明——

          - 两次读取之间若发生了一次重建，戳就会变，于是这一次不发校验符，走普通
            200；客户端下一轮就重新同步（下一轮取数前取到的就是新条目的戳，前后
            一致，tag 生效）。代价是每次重建后多一次全量响应，换来的是绝不会发出
            一个不描述本次响应体的 tag。
          - 反过来（先取数、后取戳）才有真问题：拿到的是新条目的戳配旧条目的数据，
            客户端会带着这个 tag 回来，命中 304，于是把过期数字一直显示到下一次
            重建为止。

        两个可调用对象都无参，参数由调用方用闭包带上；顺序由这里保证，调用方不
        必（也不该）自己拼这个顺序。任何解析/取值异常都退化成"没有校验符"的普通
        200，绝不让响应路径崩掉。
        """
        try:
            before = etag_fn()
        except Exception:
            before = None
        data = data_fn()
        try:
            after = etag_fn()
        except Exception:
            after = None
        etag = after if (after is not None and after == before) else None
        if etag is not None:
            try:
                inm = self.headers.get("If-None-Match")
            except Exception:
                # 没有 headers 对象（测试桩 / 未走 parse_request）时按不匹配处理。
                inm = None
            if inm is not None and _if_none_match_hit(inm, etag):
                # 304 不带 body，也不带 Content-Length：RFC 7230 §3.3.3 规定 304
                # 在空行处结束，再报一个全量长度反而会诱使客户端 / 代理去等一个
                # 永远不会来的 body。校验符和 Cache-Control 必须原样重发
                # （RFC 7232 §4.1），否则缓存会丢掉状态、下次又整份重取。
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._json_etag = etag
        try:
            return self._json(code, data)
        finally:
            # 无论 _json() 是否抛异常，都不能把这次请求的 tag 留给下一次。
            self._json_etag = None
    def _discard_body(self):
        """Drain the request body so the connection stays in sync.

        A POST rejected before its body is read (401, 404, a panel route) leaves
        the payload sitting in the socket. On a keep-alive connection the next
        request then starts by parsing that leftover JSON as the request line,
        which surfaces as a bogus "414 Request-URI Too Long" - with an empty
        request line in the log - on an otherwise healthy connection.

        Handles both Content-Length and Transfer-Encoding: chunked, since
        clients switch to the latter for large bodies.
        """
        if getattr(self, "_body_consumed", False):
            # The handler already read the body (e.g. an error raised after
            # _read_payload). Reading Content-Length bytes again would block
            # until the client gives up, turning an instant reply into a hang.
            return
        # No parsed request means no headers object and nothing buffered to
        # drain: the over-long request line is rejected before parse_request()
        # ever runs. Reading self.headers here would raise out of _error() and
        # leave the client with no reply at all.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        transfer_encoding = (headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                self._drain_chunked_body()
                return
            length = int(headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return
        if length > MAX_PAYLOAD_BYTES:
            # The client announced a body we refuse (413). Reading it would
            # block until it finishes sending gigabytes, so close instead and
            # let it see the reply plus the disconnect.
            self.close_connection = True
            return
        remaining = length
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:
            # A short read means the peer went away; nothing left to align.
            pass
    def _drain_chunked_body(self):
        """Consume a chunked body (terminated by a zero-length chunk)."""
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    return
                size_field = line.split(b";", 1)[0].strip()
                if not size_field:
                    continue
                size = int(size_field, 16)
                if size == 0:
                    # Optional trailers, then the final blank line.
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            return
                remaining = size
                while remaining > 0:
                    data = self.rfile.read(min(remaining, 65536))
                    if not data:
                        return
                    remaining -= len(data)
                self.rfile.read(2)  # trailing CRLF after each chunk
        except Exception:
            self.close_connection = True
    # How much of an over-long request line to read before giving up. The peer
    # is already misbehaving; this only needs to be enough that a normal client
    # (which sent one line and is waiting for an answer) sees the reply.
    OVERSIZED_DRAIN_LIMIT = 8 * 1024 * 1024

    def _drain_oversized_request_line(self):
        """Consume the rest of a too-long request line, within a budget.

        Without this the reply is lost to an RST (see the caller). The newline
        ends the line; past the budget the peer is clearly not going to stop,
        so give up and let the connection close.
        """
        budget = self.OVERSIZED_DRAIN_LIMIT
        try:
            while budget > 0:
                chunk = self.rfile.readline(min(budget, 65536))
                if not chunk:
                    return
                budget -= len(chunk)
                if chunk.endswith(b"\n"):
                    return
        except Exception:
            pass

    def _handle_expect_continue(self):
        """Answer 'Expect: 100-continue' before deciding to reject a body.

        Clients that send this header wait for the interim response before
        transmitting a large payload. Rejecting outright (or draining first)
        made both sides wait on each other until the socket timed out.
        """
        # No parsed request means no headers object; there is no interim
        # response to send, and touching self.headers here would raise out of
        # the error reply the caller is trying to produce.
        headers = getattr(self, "headers", None)
        if headers is None:
            return
        expect = (headers.get("Expect") or "").lower()
        if "100-continue" not in expect:
            return
        try:
            self.send_response_only(100)
            self.end_headers()
            self.wfile.flush()
        except Exception:
            pass
    def _error(self, code, message, err_type="server_error"):
        # Every early rejection funnels through here, so draining the body in
        # one place covers all of them. Unblock any client still waiting on
        # "Expect: 100-continue" first, otherwise it never sends the body and
        # the drain below waits for data that will never arrive.
        self._handle_expect_continue()
        self._discard_body()
        error = {"message": message, "type": err_type, "code": code}
        hint = gateway_hint(code, message)
        if hint:
            error["gateway_hint"] = hint
        self._json(code, {"error": error})
    def _anthropic_error(self, code, message, err_type=None):
        """Reply with the Anthropic JSON error envelope, not OpenAI's."""
        self._handle_expect_continue()
        self._discard_body()
        return self._json(code, anthropic_error_obj(code, message, err_type))
    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering.

        The upstream body names the reset time; when it does not, fall back to
        the shortest model cooldown we know about.
        """
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        # A 429 raised without an upstream call (the pool is parked by the
        # daily token guard) carries its own text; everything else keeps the
        # upstream wording.
        custom = getattr(exc, "message", "")
        text = custom or (
            "upstream rate limit reached for this model; retry in %ds" % wait)
        # The upstream detail only decorates the upstream wording; a local
        # message would only repeat itself.
        detail = ""
        if exc.detail and not custom:
            detail = " - " + exc.detail[:200]
        # 429 can be answered before the body is read (the model cooldown is
        # checked on the way in), so drain it exactly like _error does.
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps({
            "error": {
                "message": text + detail,
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _anthropic_rate_limited(self, exc):
        """429 with Retry-After in the Anthropic error envelope."""
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        text = (getattr(exc, "message", "") or
                "upstream rate limit reached for this model; retry in %ds" % wait)
        if getattr(exc, "detail", "") and not getattr(exc, "message", ""):
            text += " - " + str(exc.detail)[:200]
        self._handle_expect_continue()
        self._discard_body()
        body = json.dumps(anthropic_error_obj(429, text, "rate_limit_error"),
                          ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename, obj):
        """Send a JSON document as a browser download.
        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
    def _supplied_key(self):
        """The key the caller presented.

        Accepts the spellings clients actually send: the Authorization header
        with or without the "Bearer" scheme, the x-api-key / api-key headers
        used by several OpenAI-compatible clients, and the ?key= query the
        dashboard falls back to when it cannot set headers.
        """
        # The auth scheme is case-insensitive per RFC 7235, so "bearer sk-x"
        # and "BEARER sk-x" must strip just like "Bearer sk-x". The old
        # removeprefix("Bearer ") left the scheme attached for other casings
        # and the whole "bearer sk-x" string was then compared as a key.
        header = (self.headers.get("Authorization") or "").strip()
        supplied = ""
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
            else:
                supplied = header
            # Tolerate a quoted credential, which some SDKs add.
            if len(supplied) >= 2 and supplied[0] == supplied[-1] and supplied[0] in "\"'":
                supplied = supplied[1:-1].strip()
        if supplied:
            return supplied
        for name in ("x-api-key", "api-key", "x-auth-token"):
            value = (self.headers.get(name) or "").strip()
            if value:
                return value
        # Browsers cannot set headers on a top-level navigation, so accept the
        # key as a query parameter too - the dashboard uses this when opened
        # from another device.
        try:
            query = parse_qs(urlparse(self.path).query)
            for name in ("key", "api_key", "api-key"):
                value = (query.get(name) or [""])[0].strip()
                if value:
                    return value
            return ""
        except Exception:
            return ""
    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # An authenticated panel session also unlocks the management APIs,
        # so the browser never has to keep the API key in localStorage.
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False
    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""
    def _key_id(self):
        """Settings id of the key that paid for this request, or None.

        None covers a panel session and a deployment that runs without any key
        configured - both are real, and the usage log keeps them apart from
        rows written before the key field existed.
        """
        return (getattr(self, "key_entry", None) or {}).get("id") or None
    def _cross_realm_error(self, model, realm):
        """Explain a model/exit mismatch instead of letting upstream reject it.
        Sending gpt-6-astra to the domestic exit (or deepseek-v4-pro to the
        international one) earns an opaque 403 from upstream, so catch it here
        and say which key is bound where.
        """
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为「跟随面板切换」。"
                % (model, served, name, used))
    def _banned_model_error(self, model):
        """被封锁的模型直接报错，不碰上游、不扣任何点数。"""
        if not is_model_banned(model):
            return ""
        return banned_model_message(model)

    def _key_model_error(self, model):
        """Per-key model restriction: reject before the request reaches upstream.

        A key that lists no models stays unrestricted, so this is a no-op
        unless the operator asked for a limit.
        """
        entry = self.key_entry
        if not entry:
            return ""
        if wb_settings.key_allows_model(entry, model):
            return ""
        return key_model_message(entry, model)
    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.
        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None
    def _authorized(self):
        if self._key_ok():
            return True
        # Say how a key must be presented, so a key that merely looks identical
        # (masked copy, trailing whitespace) is diagnosable straight from the
        # client error. Deliberately does not echo key names or values.
        hint = ("send it as 'Authorization: Bearer <key>' or '?key=<key>'; "
                "copy the value from the panel's 设置 page")
        try:
            if not any(k.get("enabled") for k in configured_keys()) and not API_KEY:
                hint = ("no key is configured - open the dashboard and add one, "
                        "or restart with --api-key")
        except Exception:
            pass
        self._error(401, "invalid api key - " + hint, "invalid_request_error")
        return False
    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.
        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        token = (self.headers.get("X-Panel-Token") or "").strip()
        return token
    def _panel_ok(self):
        return PANEL.valid(self._panel_token())
    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.
        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/activity"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/updates"):
            return True
        if path.startswith("/logs"):
            return True
        # The pricing table is the panel's own view of the estimate, not part
        # of the OpenAI-compatible surface: its writes (/pricing/refresh,
        # /pricing/mapping) were already panel-only, and the read side returns
        # the same management state plus resolved project-local file paths.
        if path.startswith("/pricing"):
            return True
        if path.startswith("/agents"):
            return True
        return False
    def do_OPTIONS(self):
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path in ("/", "/dashboard", "/ui"):
            return self._get_dashboard()
        if path == "/panel/status":
            return self._get_panel_status()
        if path == "/health":
            return self._get_health()
        if path == "/health/retry":
            if not self._authorized():
                return
            return self._json(200, retry_health_snapshot((query.get("limit") or [200])[0]))
        if path == "/realm":
            return self._get_realm()
        if path in ("/v1/models", "/models"):
            return self._get_v1_models()
        if path in ("/usage", "/v1/usage"):
            return self._get_v1_usage(query)
        if path == "/usage/recent":
            return self._get_usage_recent(query)
        if path == "/accounts/credits/detail":
            return self._get_account_credits_detail(query)
        if path == "/accounts/credits":
            return self._get_accounts_credits()
        if path == "/accounts":
            return self._get_accounts(query)
        if path == "/accounts/export":
            return self._get_accounts_export(query)
        if path == "/accounts/login/poll":
            return self._get_accounts_login_poll(query)
        if path == "/usage/analytics":
            return self._get_usage_analytics(query)
        if path == "/usage/by-account":
            return self._get_usage_by_account()
        if path == "/usage/perf":
            return self._get_usage_perf(query)
        if path == "/usage/timeseries":
            return self._get_usage_timeseries(query)
        if path == "/activity/history":
            return self._get_activity_history(query)
        if path == "/tasks":
            return self._get_tasks(query)
        if path == "/scheduler":
            return self._get_scheduler()
        if path == "/pricing":
            return self._get_pricing()
        if path == "/settings":
            return self._get_settings()
        if path == "/updates":
            return self._get_updates()
        if path == "/proxy/slots":
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/logs":
            return self._get_logs(query)
        if path == "/logs/export":
            return self._get_logs_export()
        if path == "/settings/reveal":
            return self._get_settings_reveal(query)
        if path == "/agents":
            return self._get_agents()
        return self._error(404, "not found", "invalid_request_error")
    def _get_dashboard(self):
        return self._dashboard()

    def _get_panel_status(self):
        # Answer without a token: the dashboard needs to know whether to
        # show the login screen before it can hold a session.
        info = {
            "panel_password_required": True,
            "panel_password_is_default": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            "authenticated": self._panel_ok(),
        }
        # Whether a key exists is not a secret; its value never leaves the
        # process, and the settings endpoint only reports a masked form.
        info["api_key_set"] = bool(API_KEY)
        return self._json(200, info)

    def _get_health(self):
        # Always answer (the launcher uses this to detect a running copy),
        # but only expose account identity to an authorised caller.
        rep = current_account()
        info = {
            "ok": True,
            # Report the realm actually in use; this used to be the
            # literal "intl" and drifted from the panel switch.
            "realm": CURRENT_REALM,
            "accounts": len(POOL.accounts) if POOL else 0,
            "accounts_ready": POOL.count_ready() if POOL else 0,
            "api_key_required": auth_required(),
        }
        if self._key_ok():
            info.update({
                "uid": rep.uid if rep else None,
                "domain": rep.domain if rep else None,
                "issuer": wb_accounts.jwt_issuer(rep.access_token) if rep else None,
                "credential_file": os.path.basename(rep.path) if rep and rep.path else None,
                "expires_at": rep.expires_at if rep else None,
            })
        return self._json(200, info)
    # Accept the conventional /v1 prefix and the bare path, because clients
    # differ in whether they append "/v1" themselves.

    def _get_realm(self):
        return self._json(200, {"current": CURRENT_REALM, "options": ["intl", "cn"]})

    def _get_v1_models(self):
        if not self._authorized():
            return
        req_realm = self._request_realm() or CURRENT_REALM
        try:
            entries = fetch_models(realm=req_realm)
        except Exception as exc:
            return self._error(502, str(exc))
        # Level 4 is best effort: warm the local cache in the background at
        # most once per cooldown; offline deployments just keep the fallback.
        try:
            wb_modelsdev.refresh_async(ACCOUNTS_DIR, log=log)
        except Exception:
            pass
        data = [model_entry(mid, meta) for mid, meta in entries]
        return self._json(200, {"object": "list", "data": data, "realm": req_realm or CURRENT_REALM})

    def _get_v1_usage(self, query):
        if not self._authorized():
            return
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        # 取数与取校验符必须看到同一套参数（否则 tag 落到另一条缓存条目上），
        # 但"先取戳、再取数、再取一次戳"的配对顺序由 _json_cached 保证。
        return self._json_cached(
            200,
            lambda: usage_snapshot(realm=req_realm, range=req_range,
                                   since=req_since, until=req_until),
            lambda: usage_snapshot_etag(realm=req_realm, range=req_range,
                                        since=req_since, until=req_until))

    def _get_usage_recent(self, query):
        if not self._authorized():
            return
        try:
            limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
        except ValueError:
            limit = 100
        try:
            page = max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            page = 1
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        return self._json(200, recent_usage(limit, realm=req_realm, page=page))

    def _get_activity_history(self, query):
        """账号每日活动的结构化历史（issue #34）。只读，供面板的签到记录用。

        筛选条件写错时返回 400 而不是静默忽略：调用方拿着一个被忽略的
        result=failed 会以为「这几天没有失败」，实际上它拿回的是全部结果。
        """
        if not self._authorized():
            return

        def first(name):
            values = query.get(name) or [""]
            return (values[0] if values else "") or ""

        try:
            payload = wb_activity.query(range_key=first("range") or None,
                                        uid=first("uid"),
                                        task=first("task"),
                                        result=first("result"),
                                        limit=first("limit") or None)
        except ValueError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        return self._json(200, payload)

    def _get_accounts_credits(self):
        if not self._authorized():
            return
        # Refresh credits for all accounts
        for a in (POOL.accounts if POOL else []):
            a.fetch_credits()
        return self._json(200, {"accounts": account_views()})

    def _get_account_credits_detail(self, query):
        if not self._authorized():
            return
        uid = (query.get("uid") or [""])[0]
        refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
        if not POOL:
            return self._json(200, {"ok": False, "error": "账号池未初始化"})
        account = POOL.get(uid) if uid else None
        if not account:
            if POOL.accounts:
                account = POOL.accounts[0]
            else:
                return self._json(200, {"ok": False, "error": "未找到指定账号"})

        if refresh or not account.credits or not account.credits.get("packages"):
            res = account.fetch_credits()
            if not res.get("ok"):
                return self._json(200, {
                    "ok": False,
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "realm": account.realm,
                    "credits": account.credits,
                    "error": res.get("error", "获取积分明细失败")
                })

        return self._json(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname,
            "realm": account.realm,
            "credits": account.credits
        })

    def _get_accounts(self, query):
        if not self._authorized():
            return
        # Fold the usage log before building the view, so the 日限额 badge and
        # the parked count describe right now instead of the last request.
        apply_daily_token_limit()
        apply_daily_credit_limit()
        apply_model_daily_token_limit()
        return self._json(200, {
            "accounts": account_views(realm=query.get('realm', [None])[0] or CURRENT_REALM),
            "storage": ACCOUNTS_DIR,
            "usable": POOL.count_ready() if POOL else 0,
        })

    def _get_accounts_export(self, query):
        if not self._authorized():
            return
        # ?download=1 makes the browser save it as a file; without it the
        # document is returned inline so the dashboard can show a summary.
        # ?uid= narrows it to specific accounts (repeatable, comma-joined),
        # which is how the per-row "export" button works.
        realm = (query.get("realm") or [None])[0] or None
        if realm not in ("intl", "cn"):
            realm = None
        include_secrets = (query.get("secrets") or ["1"])[0] not in ("0", "false", "no")
        uids = []
        for raw in query.get("uid") or []:
            uids.extend(part.strip() for part in str(raw).split(",") if part.strip())
        if uids:
            known = {a.uid for a in (POOL.accounts if POOL else [])}
            missing = [u for u in uids if u not in known]
            if missing:
                return self._error(404, "no such account: %s" % ", ".join(missing[:5]),
                                   "invalid_request_error")
        doc = wb_accounts.build_export_document(
            POOL.accounts if POOL else [],
            realm=realm,
            include_secrets=include_secrets,
            uids=uids or None,
        )
        if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if len(uids) == 1:
                # Name a single-account export after the account, so a
                # folder of them stays readable.
                label = uids[0][:8]
            else:
                label = realm + "-" if realm else ""
            name = "workbuddy-accounts-%s%s.json" % (label, stamp)
            return self._download(name, doc)
        return self._json(200, doc)

    def _get_accounts_login_poll(self, query):
        if not self._authorized():
            return
        state = (query.get("state") or [""])[0]
        return self._json(200, POOL.poll_login(state))

    def _get_usage_analytics(self, query):
        if not self._authorized():
            return
        req_realm = query.get("realm", [None])[0] or None
        req_range, req_since, req_until = range_query(query)
        return self._json_cached(
            200,
            lambda: compute_usage_analytics(realm=req_realm, range=req_range,
                                            since=req_since, until=req_until),
            lambda: usage_analytics_etag(realm=req_realm, range=req_range,
                                         since=req_since, until=req_until))

    def _get_usage_by_account(self):
        if not self._authorized():
            return
        return self._json_cached(200,
                                 lambda: {"accounts": usage_by_account()},
                                 usage_by_account_etag)

    def _get_usage_timeseries(self, query):
        if not self._authorized():
            return
        req_realm = (query.get('realm', [None])[0]
                     or self.headers.get('X-Realm') or CURRENT_REALM)
        req_range, req_since, req_until = range_query(query)
        bucket = (query.get("bucket") or [None])[0]
        try:
            bucket_seconds = int(bucket) if bucket else None
        except (TypeError, ValueError):
            bucket_seconds = None
        return self._json_cached(
            200,
            lambda: usage_timeseries(realm=req_realm, range=req_range,
                                     since=req_since, until=req_until,
                                     bucket_seconds=bucket_seconds),
            lambda: usage_timeseries_etag(realm=req_realm, range=req_range,
                                          since=req_since, until=req_until,
                                          bucket_seconds=bucket_seconds))

    def _get_usage_perf(self, query):
        if not self._authorized():
            return
        try:
            sample = max(10, min(20000, int((query.get("sample") or ["5000"])[0])))
        except ValueError:
            sample = 5000
        req_realm = query.get('realm', [None])[0] or self.headers.get('X-Realm') or CURRENT_REALM
        req_range, req_since, req_until = range_query(query)
        return self._json_cached(
            200,
            lambda: perf_stats(sample, realm=req_realm, range=req_range,
                               since=req_since, until=req_until),
            lambda: perf_stats_etag(sample, realm=req_realm, range=req_range,
                                    since=req_since, until=req_until))

    def _get_tasks(self, query):
        if not self._authorized():
            return
        cn_accounts = [a for a in (POOL.accounts if POOL else []) if a.realm == "cn" and a.enabled]
        if not cn_accounts:
            return self._json(200, {"tasks": [], "summary": {}, "accounts": [], "msg": "未找到可用的国内版账号"})
        uid = (query.get("uid") or [None])[0]
        acc = None
        if uid and uid != "all":
            target = POOL.get(uid) if POOL else None
            if target and target.realm == "cn":
                acc = target
        if not acc:
            acc = cn_accounts[0]
        tasks, summary = growth_snapshot(acc)
        acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8]} for a in cn_accounts]
        return self._json(200, {
            "tasks": tasks,
            "summary": summary,
            "account": acc.public(),
            "accounts": acct_list,
        })

    def _get_scheduler(self):
        if not self._authorized():
            return
        return self._json(200, SCHEDULER.status() if SCHEDULER else {"enabled": False, "msg": "未运行"})

    def _get_pricing(self):
        if not self._authorized():
            return
        if PRICING:
            return self._json(200, PRICING.status())
        return self._json(200, {
            "interval_minutes": 0.0, "enabled": False, "running": False,
            "master_enabled": wb_settings.pricing_enabled(ACCOUNTS_DIR),
            "policies": 0, "models": 0, "current": {}, "logs": [],
            "gaps": [], "gap_summary": {"total": 0, "or_missing": 0,
                                        "variant_unmatched": 0, "aliases": 0,
                                        "variants_enabled":
                                            wb_settings.pricing_variant_inherit(ACCOUNTS_DIR)},
            "variant_inherit": wb_settings.pricing_variant_inherit(ACCOUNTS_DIR),
            "overrides_file": wb_pricing.overrides_path(),
            "policies_file": wb_pricing.policies_path(),
            "timeline": wb_pricing.timeline_path(), "msg": "未运行",
        })

    def _get_settings(self):
        if not self._authorized():
            return
        return self._json(200, runtime_settings_view())

    def _get_updates(self):
        """What the running build is, and whether a newer stable release exists.

        Read-only and cheap: it reports the last check's outcome, never runs
        one. The panel's "check now" is the POST below.
        """
        if not self._authorized():
            return
        if UPDATES:
            return self._json(200, UPDATES.status())
        return self._json(200, {
            "current_version": running_version(),
            "latest_version": None, "update_available": False,
            "enabled": wb_settings.update_check_enabled(ACCOUNTS_DIR),
            "checking": False, "last_attempt": None, "last_success": None,
            "last_error": "", "release_url": "", "published_at": "",
            "msg": "更新检查未运行",
        })

    def _get_logs(self, query):
        if not self._authorized():
            return
        try:
            limit = int(query.get("limit", ["200"])[0])
        except (ValueError, TypeError):
            limit = 200
        level = query.get("level", [""])[0]
        tag = query.get("tag", [""])[0]
        search = query.get("search", [""])[0]
        try:
            since_id = int(query.get("since_id", ["0"])[0])
        except (ValueError, TypeError):
            since_id = 0
        return self._json(200, get_logs(limit=limit, level=level, tag=tag, search=search, since_id=since_id))

    def _get_logs_export(self):
        if not self._authorized():
            return
        log_data = get_logs(limit=5000)
        lines = [f"[{item['ts']}] [{item['level']}] [{item['tag']}] {item['msg']}" for item in log_data["logs"]]
        text_content = "\n".join(lines).encode("utf-8")
        filename = f"wb-proxy-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(text_content)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(text_content)
        return

    def _get_settings_reveal(self, query):
        # The panel only ever draws masked keys, so copying one needs an
        # explicit request. Panel session required, API key is not enough.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        wanted = (query.get("id") or [""])[0]
        for entry in configured_keys():
            if entry.get("id") == wanted:
                return self._json(200, {"id": wanted, "key": entry.get("key") or ""})
        return self._error(404, "no such key", "invalid_request_error")

    def _dashboard(self):
        try:
            with open(DASHBOARD_HTML, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self._error(500, f"dashboard.html unavailable: {exc}")
        # The static file carries a placeholder; replace it with the instance
        # default so the first paint already uses the right language.
        language = wb_settings.ui_language(ACCOUNTS_DIR)
        body = body.replace(
            b'data-ui-language="__WB_UI_LANGUAGE__"',
            ('data-ui-language="%s"' % language).encode("utf-8"),
        )
        # 面板首页是纯静态资源（服务端逐字节原样发出、不含任何机密），所以可以
        # 允许浏览器存储副本、每次打开再回源校验：校验命中回 304（无 body），
        # 省下整页（约 470KB）的重复下载，手机 / Tailscale 远程访问体感最明显。
        #
        # 校验符必须覆盖**所有**影响响应体的输入，不只是文件本身：body 还取决于
        # 上面注入的 ui_language——用户改一次界面语言，文件没动、body 却变了；
        # 若 tag 只看文件，客户端带旧 tag 回来会拿到 304 + 旧语言的页面。所以 tag
        # 由 (文件 mtime_ns, size, language) 三者派生：前两个代表文件字节（本文件
        # 只在应用更新时被整体替换、从不原地修改），第三个就是本次实际注入的值，
        # 三者组合变化 ⇔ 响应字节变化。mtime 用纳秒精度，同一秒内的两次替换也能
        # 得到不同 tag。
        etag = None
        try:
            st = os.stat(DASHBOARD_HTML)
            # 强校验符（不带 W/ 前缀）：响应是「文件字节 + 本次语言」的精确副本，
            # tag 变 ⇔ 字节变。
            etag = '"%x-%x-%s"' % (st.st_mtime_ns, st.st_size, language)
        except Exception:
            # stat 取不到（或时间戳无法表示）不是致命错误：退化为一律按普通
            # 200 处理，只是这一次没有条件请求支持，绝不让面板页本身打不开。
            etag = None
        # 只认 If-None-Match，不发送、也不理会 If-Modified-Since。Last-Modified
        # 只能描述文件的 mtime，而响应体还取决于语言：日期无法表达这个输入，
        # 一旦发布出去，偏好日期的客户端就会拿它校验，语言一变就拿到过期的
        # 304。ETag 覆盖全部输入、单靠它就足够完备，所以干脆不提供日期——
        # 不发布它，客户端就没有用它的理由（RFC 7232 §2.2 里 Last-Modified 只是
        # SHOULD，响应体并非单一文件、没有可一致表达的修改日期，只发 ETag 完备）。
        if etag is not None:
            inm = self.headers.get("If-None-Match")
            if inm is not None and _if_none_match_hit(inm, etag):
                # 304 不带 body：浏览器手里已有一份，一个字节都不用再传。也不带
                # Content-Length：304 按 RFC 7230 §3.3.3 在空行处结束，再报全量
                # 长度反而会诱使客户端 / 代理等待一个永远不会来的 body。校验符和
                # Cache-Control 必须原样重发（RFC 7232 §4.1），否则缓存会丢掉状态。
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.end_headers()
                return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # no-cache 而不是 no-store：允许存储，但每次使用前必须回源校验；配合
        # 上面的校验符，校验命中的代价是一次 304 而不是整页约 470KB。不加
        # max-age：没有它浏览器每次打开都会校验，应用更新后新版页面立即生效。
        self.send_header("Cache-Control", "no-cache")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)
    def _read_chunked_body(self, max_bytes=MAX_PAYLOAD_BYTES):
        """Decode a Transfer-Encoding: chunked body into bytes.

        Some OpenAI-compatible clients stream large requests with chunked
        encoding instead of a Content-Length. Reading only Content-Length saw an
        empty body and answered 400 invalid JSON.
        """
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                break
            size_field = line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            try:
                size = int(size_field, 16)
            except ValueError:
                raise BadJSON()
            if size == 0:
                # Consume optional trailers up to the terminating blank line.
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > max_bytes:
                # Keep draining so the connection stays aligned, then refuse.
                self._drain_chunked_body()
                raise BodyTooLarge(total)
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(remaining, 65536))
                if not data:
                    raise BadJSON()
                chunks.append(data)
                remaining -= len(data)
            self.rfile.read(2)  # CRLF after the chunk data
        self._body_consumed = True
        return b"".join(chunks)
    def _read_payload(self, max_bytes=MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list).
        Raises BodyTooLarge / BadJSON so every caller handles both cases the
        same way instead of each remembering to check for None.
        """
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            if "chunked" in transfer_encoding:
                raw_bytes = self._read_chunked_body(max_bytes=max_bytes)
                data = json.loads(raw_bytes.decode("utf-8", "replace") or "{}")
                if isinstance(data, dict):
                    return data
                if allow_list and isinstance(data, list):
                    return data
                return {}
            length = int(self.headers.get("Content-Length") or 0)
        except (BodyTooLarge, BadJSON):
            raise
        except Exception:
            raise BadJSON()
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            # Mark the body as taken so a later error reply does not try to
            # drain the same bytes again (that read would block forever).
            self._body_consumed = True
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            # The account-import endpoint accepts a bare array of accounts,
            # which is the most natural shape for a hand-written file.
            return data
        return {}
    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            message = ("payload too large (%d bytes > %d limit)"
                       % (exc.length, MAX_PAYLOAD_BYTES))
            if self._anthropic_route():
                self._anthropic_error(413, message, "invalid_request_error")
            else:
                self._error(413, message, "invalid_request_error")
            return None
        except BadJSON:
            if self._anthropic_route():
                self._anthropic_error(400, "invalid JSON body", "invalid_request_error")
            else:
                self._error(400, "invalid JSON body", "invalid_request_error")
            return None
    def _anthropic_route(self):
        """True when the request path is one of the Anthropic Messages routes.

        Errors raised before the route dispatcher runs (body read, size cap)
        have to answer in the Anthropic envelope, or a Claude Code client sees
        an OpenAI-shaped error it cannot parse.
        """
        return self.path.split("?")[0] in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )
    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list", "invalid_request_error")
            # The panel only ever shows a masked key, so a blank value means
            # "keep what is stored" for that row rather than "clear it".
            existing = {entry.get("id"): entry for entry in configured_keys()}
            # A submission that carries an explicit delete list is an upsert:
            # only the ids it names are retired. That is what stops a stale or
            # incomplete list (a second tab, a save racing the post-save
            # reload) from wiping a key the user never removed. An older panel
            # sends no such field and keeps the replace-by-omission contract.
            delete_ids = payload.get("deleted_api_key_ids")
            upsert = isinstance(delete_ids, list)
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                stored = existing.get(entry_id) or {}
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                # A new row keeps an empty id here; wb_settings mints a random
                # one on write. Deriving it from the row's position reused ids
                # of rows deleted earlier, and two rows sharing an id made
                # /settings/reveal answer with the wrong key.
                if value and len(value) < 4:
                    return self._error(400, "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400, "a key entry is empty - fill it in or remove the row",
                                       "invalid_request_error")
                # A field the submission omits keeps whatever is stored, the
                # same rule `models` already follows: an older or partial
                # client must not silently clear a key's exit binding, its
                # name, or its disabled state.
                if "realm" in item:
                    realm = str(item.get("realm") or "").strip().lower()
                else:
                    realm = str(stored.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                # An older cached panel does not know this field at all, so a
                # row that omits it keeps whatever is stored instead of
                # silently dropping the restriction.
                if "models" in item:
                    models = item.get("models")
                else:
                    models = stored.get("models")
                if "name" in item:
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(stored.get("name") or "").strip()
                if "enabled" in item:
                    enabled = item.get("enabled", True) is not False
                else:
                    enabled = stored.get("enabled", True) is not False
                created_at = item.get("created_at") or (stored.get("created_at") if entry_id in existing else None) or time.strftime("%Y/%m/%d %H:%M")
                cleaned.append({
                    "id": entry_id,
                    "name": name,
                    "key": value,
                    "realm": realm,
                    "models": models,
                    "enabled": enabled,
                    "created_at": created_at,
                })
            wb_settings.set_api_keys(
                ACCOUNTS_DIR, cleaned,
                delete_ids=(delete_ids if upsert else None),
            )
            reply["api_keys_saved"] = len(cleaned)
        if "auth_disabled" in payload:
            wb_settings.set_auth_disabled(ACCOUNTS_DIR, payload.get("auth_disabled"))
            reply["auth_disabled"] = bool(payload.get("auth_disabled"))

        # The four guards can arrive grouped under "limits" (the panel's own
        # shape) or as the flat top-level keys older clients still send, which
        # are read as the global default. Both land in the same grouped store,
        # so there is one source of truth no matter who writes it.
        limits_payload = payload.get("limits")
        if limits_payload is not None and not isinstance(limits_payload, dict):
            return self._error(400, "limits must be an object",
                               "invalid_request_error")
        touched = set()
        for key in wb_settings.LIMIT_KEYS:
            grouped = (limits_payload or {}).get(key)
            if isinstance(grouped, dict):
                scopes = {scope: grouped[scope]
                          for scope in wb_settings.LIMIT_SCOPES
                          if scope in grouped}
            elif key in payload:
                scopes = {"global": payload.get(key)}
            else:
                continue
            for scope, raw in scopes.items():
                if scope == "global":
                    if isinstance(raw, bool) or raw is None:
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    try:
                        number = int(raw)
                    except (TypeError, ValueError):
                        return self._error(400, "%s must be a whole number" % key,
                                           "invalid_request_error")
                    if number < 0:
                        return self._error(400, "%s cannot be negative" % key,
                                           "invalid_request_error")
                else:
                    # Blank clears the override back to "inherit the global".
                    if raw is None or raw == "":
                        number = None
                    else:
                        if isinstance(raw, bool):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        try:
                            number = int(raw)
                        except (TypeError, ValueError):
                            return self._error(
                                400, "%s must be a whole number" % key,
                                "invalid_request_error")
                        if number < 0:
                            return self._error(
                                400, "%s cannot be negative" % key,
                                "invalid_request_error")
                wb_settings.set_limit(ACCOUNTS_DIR, key, scope, number)
                touched.add(key)
            reply[key] = wb_settings.limit_value(ACCOUNTS_DIR, key)
        if limits_payload is not None:
            reply["limits"] = wb_settings.limits_snapshot(ACCOUNTS_DIR)
        if touched:
            if POOL and "reserve_credits" in touched:
                POOL.apply_reserve_credits()
            if "daily_token_limit" in touched:
                apply_daily_token_limit(refresh=True)
            if "daily_credit_limit" in touched:
                apply_daily_credit_limit(refresh=True)
            if "model_daily_token_limit" in touched:
                apply_model_daily_token_limit(refresh=True)
            if "expiring_window_days" in touched and POOL:
                POOL.apply_expiring_window()

        if "pricing_enabled" in payload:
            raw = payload.get("pricing_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_pricing_enabled(ACCOUNTS_DIR, raw)
            reply["pricing_enabled"] = raw
            if PRICING:
                PRICING.wake()
        if "pricing_refresh_minutes" in payload or "pricing_refresh_hours" in payload:
            # The interval is in minutes. The old field name is still accepted
            # (x60) so a panel page cached from the previous build cannot set
            # the wrong unit; it is answered under the new name.
            field = "pricing_refresh_minutes" \
                if "pricing_refresh_minutes" in payload else "pricing_refresh_hours"
            raw = payload.get(field)
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            try:
                minutes = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "%s must be a number" % field,
                                   "invalid_request_error")
            if field == "pricing_refresh_hours":
                minutes *= 60.0
            if minutes < 0:
                return self._error(400, "pricing_refresh_minutes cannot be negative",
                                   "invalid_request_error")
            stored = wb_settings.set_pricing_refresh_minutes(ACCOUNTS_DIR, minutes)
            if PRICING:
                # A running wait picks the new interval up on the spot.
                PRICING.set_interval(stored)
            reply["pricing_refresh_minutes"] = stored
        if "credits_refresh_hours" in payload:
            # How stale a credit balance may get before the background refresher
            # updates it. Zero turns the refresher off.
            raw = payload.get("credits_refresh_hours")
            if isinstance(raw, bool) or raw is None:
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            try:
                hours = float(raw)
            except (TypeError, ValueError):
                return self._error(400, "credits_refresh_hours must be a number",
                                   "invalid_request_error")
            if hours < 0:
                return self._error(400, "credits_refresh_hours cannot be negative",
                                   "invalid_request_error")
            reply["credits_refresh_hours"] = \
                wb_settings.set_credits_refresh_hours(ACCOUNTS_DIR, hours)
            if CREDITS_REFRESHER:
                CREDITS_REFRESHER.wake()
        if "ui_language" in payload:
            # Instance-wide default language. The dashboard overrides this per
            # browser with localStorage; this value is the fallback when no
            # browser-local preference exists.
            raw = payload.get("ui_language")
            if not isinstance(raw, str) or raw not in ("zh", "zh-Hant", "en"):
                return self._error(400, "ui_language must be zh, zh-Hant or en",
                                   "invalid_request_error")
            reply["ui_language"] = wb_settings.set_ui_language(ACCOUNTS_DIR, raw)
        if "pricing_variant_inherit" in payload:
            # Strictly a JSON boolean, like the other switches: "false" as a
            # string would be truthy and silently keep the feature on.
            raw = payload.get("pricing_variant_inherit")
            if not isinstance(raw, bool):
                return self._error(400, "pricing_variant_inherit must be true or false",
                                   "invalid_request_error")
            previous = wb_settings.pricing_variant_inherit(ACCOUNTS_DIR)
            wb_settings.set_pricing_variant_inherit(ACCOUNTS_DIR, raw)
            reply["pricing_variant_inherit"] = raw
            if PRICING and previous != raw:
                # The switch only takes effect on the next fetch - a refresh
                # drops (off) or adds (on) the suffix-inherited assignments -
                # so kick one off rather than waiting out the interval.
                threading.Thread(target=PRICING.run_once, daemon=True,
                                 name="price-refresh-inherit").start()
                reply["pricing_refresh_started"] = True
        if "auto_switch_product" in payload:
            # Strictly a JSON boolean: a string like "false" would be truthy and
            # silently switch the feature on, which is the one thing an operator
            # turning it off must not get.
            raw = payload.get("auto_switch_product")
            if not isinstance(raw, bool):
                return self._error(400, "auto_switch_product must be true or false",
                                   "invalid_request_error")
            wb_settings.set_auto_switch_product(ACCOUNTS_DIR, raw)
            reply["auto_switch_product"] = raw
        if "daily_chat_web" in payload:
            raw = payload.get("daily_chat_web")
            if not isinstance(raw, bool):
                return self._error(400, "daily_chat_web must be true or false",
                                   "invalid_request_error")
            wb_settings.set_daily_chat_web(ACCOUNTS_DIR, raw)
            reply["daily_chat_web"] = raw
        if "local_web_tools" in payload:
            raw = payload.get("local_web_tools")
            if not isinstance(raw, bool):
                return self._error(400, "local_web_tools must be true or false",
                                   "invalid_request_error")
            wb_settings.set_local_web_tools(ACCOUNTS_DIR, raw)
            reply["local_web_tools"] = raw
        if "accounts_collapsed" in payload:
            # A disclosure state, and the only thing this branch may touch: the
            # submission carries just this key, so the settings it does not name
            # survive the write.
            raw = payload.get("accounts_collapsed")
            if not isinstance(raw, bool):
                return self._error(400, "accounts_collapsed must be true or false",
                                   "invalid_request_error")
            wb_settings.set_accounts_collapsed(ACCOUNTS_DIR, raw)
            reply["accounts_collapsed"] = raw
        if "update_check_enabled" in payload:
            # Strictly a JSON boolean, like the switches above: "false" as a
            # string would be truthy and silently start the daily GitHub call.
            raw = payload.get("update_check_enabled")
            if not isinstance(raw, bool):
                return self._error(400, "update_check_enabled must be true or false",
                                   "invalid_request_error")
            wb_settings.set_update_check_enabled(ACCOUNTS_DIR, raw)
            reply["update_check_enabled"] = raw
            if UPDATES and raw:
                # Turning it on should not wait out the rest of the poll sleep.
                UPDATES.wake()
        if "upstream" in payload:
            raw = payload.get("upstream")
            if not isinstance(raw, dict):
                return self._error(400, "upstream must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_upstream_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_upstream_config(ACCOUNTS_DIR, patch)
            reply["upstream"] = wb_settings.upstream_config(ACCOUNTS_DIR)
        if "prompt" in payload:
            raw = payload.get("prompt")
            if not isinstance(raw, dict):
                return self._error(400, "prompt must be an object",
                                   "invalid_request_error")
            try:
                patch = wb_settings.validate_prompt_patch(raw)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            wb_settings.set_prompt_config(ACCOUNTS_DIR, patch)
            reply["prompt"] = wb_settings.prompt_config(ACCOUNTS_DIR)
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            global API_KEY, API_KEY_FILE_SET
            wb_settings.set_api_key(ACCOUNTS_DIR, new_key)
            API_KEY = new_key
            API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if payload.get("restart_scheduler"):
            if SCHEDULER:
                SCHEDULER.stop()
                SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(runtime_settings_view())
        return self._json(200, reply)

    def _handle_proxy_slots(self, path, payload):
        """Proxy-slot management (panel-authenticated)."""
        if path == "/proxy/slots":
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/save":
            raw = payload.get("slots")
            if not isinstance(raw, list):
                return self._error(400, "slots must be a list", "invalid_request_error")
            cleaned = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                # Normalized by the same helper the store uses, so the probed
                # exit fields the panel echoes back survive a save instead of
                # being dropped by a second, hand-written field list here.
                entry = wb_settings._clean_slot_entry(item)
                if entry is not None:
                    cleaned.append(entry)
            saved = wb_settings.set_proxy_slots(ACCOUNTS_DIR, cleaned)
            if POOL:
                # A slot may have been removed: unbind anyone still naming it
                # before recomputing, so a stale id cannot survive.
                dropped = wb_settings.drop_missing_bindings(POOL, saved)
                POOL.apply_proxy_slots(saved)
                if dropped:
                    log("proxy slots: unbound %d account(s) from removed slots"
                        % dropped)
            log("proxy slots saved: %d slot(s)" % len(saved))
            return self._json(200, {"slots": proxy_slots_view()})
        if path == "/proxy/slots/test":
            slot_id = str(payload.get("id") or "").strip()
            slot = wb_settings.find_proxy_slot(ACCOUNTS_DIR, slot_id)
            if slot is None:
                return self._error(404, "no such proxy slot")
            probe = probe_proxy_intel(slot["url"])
            reply = dict(probe)
            reply["id"] = slot_id
            reply["slot"] = None
            if probe["ok"]:
                # Remember what the exit turned out to be, so the panel shows it
                # without probing again. `ok` covers the IP probe only: when the
                # geo lookup came back with nothing, the exit info already stored
                # is kept rather than overwritten with blanks - a blip at the
                # lookup must not lose what the last good probe learned.
                updated_fields = {
                    "ip": probe["exit_ip"],
                    "probed_at": int(time.time()),
                }
                if probe.get("intel_ok"):
                    updated_fields.update({
                        "country": probe["country"],
                        "country_code": probe["country_code"],
                        "ip_type": probe["ip_type"],
                        "isp": probe["isp"],
                        "asn": probe["asn"],
                    })
                else:
                    log("proxy slots: %s probed %s but the geo lookup returned "
                        "nothing; keeping the exit info already stored"
                        % (slot_id, probe["exit_ip"] or "-"))
                # The name is deliberately left alone. An empty name is how the
                # store marks a slot as auto-named ("label this slot by its
                # exit"), and slot_label() derives that label from the country
                # and kind it shows - writing the derived text into the name
                # would freeze it to the exit it happened to have at the time.
                updated = wb_settings.update_proxy_slot(
                    ACCOUNTS_DIR,
                    slot_id,
                    updated_fields,
                )
                reply["slot"] = updated
                reply["name"] = (updated or {}).get("name", "")
            return self._json(200, reply)
        if path == "/proxy/discover":
            return self._json(200, {"candidates": discover_proxy_slots()})
        return self._error(404, "not found", "invalid_request_error")

    # ---- one-click agent integration (wb_agents) ----

    def _agents_base_url_hint(self):
        """Best guess at the URL a local client should point at.

        Derived from the bound socket; a 0.0.0.0 bind is unreachable for a
        client, so it is reported as 127.0.0.1 instead.
        """
        host, port = "", 0
        try:
            host, port = self.server.server_address[:2]
        except Exception:
            pass
        host = str(host or "").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return "http://%s:%d/v1" % (host, int(port or 0))

    def _agents_models(self):
        """Flat model list for the picker; bundled intl+cn catalog, deduped.

        Live sources stay out of this path on purpose: the panel must answer
        even when the upstream is down, so a failure here is simply a shorter
        list, never an error.
        """
        models = []
        seen = set()
        try:
            try:
                entries = fetch_models()
            except Exception:
                entries = []
            if entries:
                sources = ((mid, meta) for mid, meta in entries)
            else:
                sources = (
                    (item.get("id"), item)
                    for item in (list(getattr(wb_catalog, "STATIC_INTL_MODELS", []))
                                 + list(getattr(wb_catalog, "STATIC_CN_MODELS", [])))
                    if isinstance(item, dict)
                )
            for mid, meta in sources:
                mid = str(mid or "").strip()
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                meta = meta if isinstance(meta, dict) else {}
                entry = {"id": mid}
                ctx = meta.get("maxInputTokens")
                window = meta.get("contextWindow")
                if isinstance(window, dict) and window.get("defaultLength"):
                    ctx = window.get("defaultLength")
                if not ctx and isinstance(window, dict):
                    lengths = window.get("supportedLengths") or []
                    ctx = lengths[-1] if lengths else None
                if ctx:
                    try:
                        entry["context_window"] = int(ctx)
                    except (TypeError, ValueError):
                        pass
                out = meta.get("maxOutputTokens")
                if out:
                    try:
                        entry["max_output"] = int(out)
                    except (TypeError, ValueError):
                        pass
                name = meta.get("name")
                if name:
                    entry["name"] = name
                models.append(entry)
        except Exception:
            pass
        return models

    def _get_agents(self):
        keys = []
        try:
            for entry in configured_keys():
                if entry.get("enabled"):
                    keys.append({
                        "id": entry.get("id"),
                        "name": entry.get("name") or "",
                        "enabled": True,
                    })
        except Exception:
            keys = []
        return self._json(200, {
            "clients": list(wb_agents.overview(ACCOUNTS_DIR).values()),
            "models": self._agents_models(),
            "keys": keys,
            "global_key_set": bool(API_KEY),
            "auth_required": auth_required(),
            "gateway": {"base_url": self._agents_base_url_hint()},
        })

    def _agents_resolve_key(self, payload, warnings):
        """Pick the gateway key to hand to the client, per the payload.

        Returns the key string, or None when the request should fail. The
        failure message is appended to `warnings` only for the soft-fallback
        case; hard failures raise via the caller's 400 mapping.
        """
        key_id = str(payload.get("key_id") or "").strip()
        if key_id == "__global":
            return API_KEY or None
        keys = configured_keys()
        if key_id:
            for entry in keys:
                if entry.get("id") == key_id:
                    return entry.get("key") or None
            raise wb_agents.AgentConfigError(
                "no configured key with id %r" % key_id)
        # Default: the global key when set, else the first enabled panel key.
        if API_KEY:
            return API_KEY
        for entry in keys:
            if entry.get("enabled"):
                return entry.get("key") or None
        return None

    def _handle_agents_apply(self, payload):
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        base_url = str(payload.get("base_url") or "").strip()
        if not re.match(r"^https?://", base_url):
            return self._error(
                400, "base_url must start with http:// or https://",
                "invalid_request_error")
        model = str(payload.get("model") or "").strip() or None
        models = payload.get("models")
        if not models:
            # Fall back to the gateway's discovered catalog so clients that embed
            # model definitions (OpenCode, DSH, Crush) receive the full model list
            # even when the front-end omitted the field.
            models = self._agents_models()
        if not isinstance(models, list):
            return self._error(400, "models must be a list",
                               "invalid_request_error")
        # A runaway picker must not turn into a megabyte config file.
        models = models[:80]
        cleaned_models = []
        for item in models:
            if isinstance(item, dict) and item.get("id"):
                cleaned_models.append(item)
            elif isinstance(item, str) and item.strip():
                cleaned_models.append({"id": item.strip()})
        warnings = []
        try:
            api_key = self._agents_resolve_key(payload, warnings)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        if not api_key:
            if auth_required():
                return self._error(
                    400, "no gateway key available - 请先配置 API Key",
                    "invalid_request_error")
            api_key = "wb-local"
            warnings.append("gateway has no key configured; wrote placeholder "
                            "'wb-local' (auth is off, so any value works)")
        try:
            result = wb_agents.integrate(
                ACCOUNTS_DIR, client_id, base_url, api_key,
                model=model, models=cleaned_models)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents apply failed: %s" % exc)
        result["warnings"] = warnings
        log("agents apply: client=%s base_url=%s model=%s files=%d"
            % (client_id, base_url, model or "-", len(result.get("files") or [])),
            tag="agents")
        return self._json(200, result)

    def _handle_agents_restore(self, payload):
        client_id = str(payload.get("client") or payload.get("client_id") or "").strip()
        if not client_id:
            return self._error(400, "client is required", "invalid_request_error")
        try:
            result = wb_agents.restore(ACCOUNTS_DIR, client_id)
        except wb_agents.AgentConfigError as exc:
            return self._error(400, str(exc), "invalid_request_error")
        except Exception as exc:
            return self._error(500, "agents restore failed: %s" % exc)
        log("agents restore: client=%s files=%d"
            % (client_id, len(result.get("restored") or [])), tag="agents")
        return self._json(200, result)

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password + API key)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            client_ip = self.client_address[0] if hasattr(self, "client_address") and self.client_address else "127.0.0.1"
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429, f"too many login attempts, please wait {max(1, wait_sec)}s", "rate_limit_error")
            password = str(payload.get("password") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                # Small backoff delay to mitigate automated brute force
                time.sleep(0.5)
                return self._error(401, "invalid panel password", "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password": wb_settings.panel_password_is_default(ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # Everything past this point requires an authenticated panel session.
        if not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not wb_settings.verify_panel_password(ACCOUNTS_DIR, current):
                return self._error(401, "current password is wrong", "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters", "invalid_request_error")
            wb_settings.set_panel_password(ACCOUNTS_DIR, new)
            if new != wb_settings.DEFAULT_PANEL_PASSWORD:
                # Rotating the password invalidates every other browser session.
                PANEL.revoke_all()
            token = PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._error(404, "not found", "invalid_request_error")
    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            # A bare array is only meaningful for import; wrap it so the rest
            # of this handler can keep assuming a dict.
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object", "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            return self._route_accounts_credits_fetch(payload)
        if path == "/accounts/credits/detail":
            return self._route_account_credits_detail(payload)
        if path == "/tasks/run":
            return self._route_tasks_run(payload)
        if path == "/tasks/travel":
            return self._route_tasks_travel(payload)
        if path == "/scheduler/trigger":
            return self._route_scheduler_trigger(payload)
        if path == "/scheduler/toggle":
            return self._route_scheduler_toggle(payload)
        if path == "/pricing/refresh":
            return self._route_pricing_refresh(payload)
        if path == "/logs/clear":
            return self._route_logs_clear(payload)
        if path == "/realm":
            return self._route_realm(payload)
        if path == "/accounts/checkin":
            return self._route_accounts_checkin(payload)
        if path == "/accounts/daily-chat":
            return self._route_accounts_daily_chat(payload)
        if path == "/accounts/daily-chat-web":
            return self._route_accounts_daily_chat_web(payload)
        if path == "/accounts/login/start":
            return self._route_accounts_login_start(payload)
        if path == "/accounts/login/cancel":
            return self._route_accounts_login_cancel(payload)
        if path == "/accounts/import/desktop":
            return self._route_accounts_import_desktop(payload)
        if path == "/accounts/refresh":
            return self._route_accounts_refresh(payload)
        if path == "/accounts/sync-profile":
            return self._route_accounts_sync_profile(payload)
        if path == "/accounts/test":
            return self._route_accounts_test(payload)
        if path == "/accounts/set":
            return self._route_accounts_set(payload)
        if path == "/accounts/product":
            return self._route_accounts_product(payload)
        if path == "/accounts/set-all":
            return self._route_accounts_set_all(payload)
        if path == "/accounts/delete":
            return self._route_accounts_delete(payload)
        if path == "/accounts/import":
            return self._route_accounts_import(payload)
        return self._error(404, "unknown account endpoint", "invalid_request_error")
    def _route_accounts_product(self, payload):
        """切换出站身分（cli <-> workbuddy），并即时回传结果。

        官方有两套身分、两条配额线。某条满了可以切到另一条继续用。
        """
        target = str(payload.get("product") or "").strip().lower()
        uid = payload.get("uid")
        realm = payload.get("realm")

        if target not in wb_identity.VALID_PRODUCTS:
            return self._error(400, "product must be 'workbuddy', 'vscode', or 'cli'",
                               "invalid_request_error")

        if uid:
            targets = [POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in POOL.accounts if a.realm == realm]
        else:
            targets = list(POOL.accounts)

        changed = []
        for account in targets:
            if account is None:
                continue
            try:
                if account.set_product(target):
                    # 立刻落盘：set_product() 只改记忆体，而面板上这一下是操作者
                    # 的明确选择，不能等到别的路径（refresh / 签到 / 查积分）刚好
                    # 存档才生效——切完就重启容器的人会白白丢掉这次切换。
                    try:
                        account.save(ACCOUNTS_DIR)
                    except Exception as exc:
                        log("product save failed for %s: %s" % (account.uid[:8], exc),
                            level="WARN")
                    changed.append(account.uid[:8])
                    log("account %s: 面板手動切換身分 -> %s"
                        % (account.uid[:8], target), level="INFO")
            except Exception as exc:
                log("product switch failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")

        return self._json(200, {
            "ok": True,
            "product": target,
            "changed": changed,
            "accounts": account_views(),
        })

    def _route_accounts_credits_fetch(self, payload):
        uid = payload.get("uid")
        realm = payload.get("realm")
        if uid:
            targets = [POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in POOL.accounts if a.realm == realm]
        else:
            targets = list(POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.fetch_credits()
            results.append({"uid": account.uid, "ok": res.get("ok", False),
                            "credits": account.credits, "error": res.get("error", "")})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_account_credits_detail(self, payload):
        if not self._authorized():
            return
        uid = payload.get("uid")
        refresh = payload.get("refresh", True)
        if not POOL:
            return self._json(200, {"ok": False, "error": "账号池未初始化"})
        account = POOL.get(uid) if uid else None
        if not account:
            return self._json(200, {"ok": False, "error": "未找到指定账号"})

        if refresh or not account.credits or not account.credits.get("packages"):
            res = account.fetch_credits()
            if not res.get("ok"):
                return self._json(200, {
                    "ok": False,
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "realm": account.realm,
                    "credits": account.credits,
                    "error": res.get("error", "获取积分明细失败")
                })

        return self._json(200, {
            "ok": True,
            "uid": account.uid,
            "nickname": account.nickname,
            "realm": account.realm,
            "credits": account.credits
        })

    def _route_tasks_run(self, payload):
        if not POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        invalidate_tasks_cache()          # 跑完任务状态就变了，别再端旧快照
        uid = payload.get("uid")
        if uid and uid != "all":
            target = POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import run_growth_tasks
        combined_logs = []
        total_credit = 0
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            combined_logs.append(f"====== 正在为账号 [{nick} ({acc.uid})] 执行全自动成长任务 ({i+1}/{len(targets)}) ======")
            res = run_growth_tasks(acc, gap=1.0)
            # run_growth_tasks() reports its total as "earned_credit";
            # reading the old "credit_added" name silently summed zeros
            # and the dashboard always showed "+0 积分".
            total_credit += res.get("earned_credit") or 0
            for l in res.get("logs") or []:
                combined_logs.append(f"  {l}")
            if i < len(targets) - 1:
                time.sleep(1.5)
        combined_logs.append(f"====== 全部 {len(targets)} 个账号任务执行完毕，累计新增积分: +{total_credit} ======")
        return self._json(200, {
            "ok": True,
            "credit_added": total_credit,
            "logs": combined_logs,
            "accounts_count": len(targets)
        })

    def _route_tasks_travel(self, payload):
        if not POOL:
            return self._json(200, {"ok": False, "msg": "账号池不可用"})
        invalidate_tasks_cache()          # 同上：旅行会改任务/体力状态
        uid = payload.get("uid")
        if uid and uid != "all":
            target = POOL.get(uid)
            if not target or target.realm != "cn":
                return self._json(200, {"ok": False, "msg": "未找到指定的国内版账号"})
            targets = [target]
        else:
            targets = [a for a in POOL.accounts if a.realm == "cn" and a.enabled]
        if not targets:
            return self._json(200, {"ok": False, "msg": "未找到已启用的国内版账号"})
        from wb_tasks import do_cat_travel
        results = []
        for i, acc in enumerate(targets):
            uid_str = acc.uid[:8] if acc.uid else "?"
            nick = acc.nickname or uid_str
            res = do_cat_travel(acc)
            results.append({
                "uid": acc.uid,
                "nickname": nick,
                "action": res.get("action"),
                "msg": res.get("msg") or "",
                # do_cat_travel() returns the amount as "credit".
                "reward_credit": res.get("credit", 0)
            })
            if i < len(targets) - 1:
                time.sleep(1.0)
        summary_msg = chr(10).join([f"{r['nickname']}: {r['msg']}" for r in results])
        return self._json(200, {
            "ok": True,
            "results": results,
            "msg": summary_msg,
            "accounts_count": len(targets)
        })

    def _route_update_check(self):
        """Run one release check now, on the operator's explicit request.

        Inline rather than "started, poll /updates": it is a single bounded
        GitHub request, and the answer is what the button is for. `manual=True`
        ignores both the daily switch and the 24h window.
        """
        if not UPDATES:
            return self._json(200, {"ok": False, "msg": "更新检查未运行"})
        status = UPDATES.check(manual=True)
        status["ok"] = True
        return self._json(200, status)

    def _route_scheduler_trigger(self, payload):
        if SCHEDULER:
            return self._json(200, SCHEDULER.trigger_now())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_scheduler_toggle(self, payload):
        if SCHEDULER:
            SCHEDULER.enabled = not SCHEDULER.enabled
            SCHEDULER.log(f"用户切换调度器状态为: {'启用' if SCHEDULER.enabled else '暂停'}")
            return self._json(200, SCHEDULER.status())
        return self._json(200, {"ok": False, "msg": "调度器未初始化"})

    def _route_pricing_refresh(self, payload):
        # Fetching takes tens of seconds, so it runs on its own thread and the
        # panel polls /pricing for the outcome.
        if not PRICING:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        threading.Thread(target=PRICING.run_once, daemon=True,
                         name="price-refresh-manual").start()
        out = PRICING.status()
        out["ok"] = True
        out["msg"] = "已开始抓取，稍候刷新查看结果"
        return self._json(200, out)

    def _route_logs_clear(self, payload):
        clear_logs()
        return self._json(200, {"ok": True})

    def _route_pricing_mapping(self, payload):
        """面板手填一条「hub 模型 → OpenRouter id」映射。

        写进 usage/pricing-overrides.json（运行期覆盖，不改源码里的
        OVERRIDES，也不随镜像升级丢失），随后立刻触发一次取价，让这条映射
        在几秒内生效。or_id 为空表示删除该映射。写入只认形状像 OpenRouter
        id 的值，并在快照里有该条目时予以确认（没有也接受，但要如实说明，
        因为上游随时可能刚上架而本地清单还没刷新）。
        """
        if PRICING is None:
            return self._json(200, {"ok": False, "msg": "价格刷新未运行"})
        if not wb_settings.pricing_enabled(ACCOUNTS_DIR):
            return self._json(200, {"ok": False, "msg": "价估算已关闭，请先启用"})
        model = str(payload.get("model") or "").strip()
        or_id = str(payload.get("or_id") or "").strip()
        if not model:
            return self._error(400, "model is required", "invalid_request_error")
        if or_id and not re.match(r"^[A-Za-z0-9._\-]+/[A-Za-z0-9._\-:]+$", or_id):
            return self._error(400, "or_id must look like 'vendor/model'",
                               "invalid_request_error")
        or_models, _by_norm = wb_pricing.live_index()
        known = bool(or_id) and or_id in (or_models or {})
        wb_pricing.save_runtime_override(model, or_id)
        PRICING.invalidate_gaps()
        threading.Thread(target=PRICING.run_once, daemon=True,
                         name="price-refresh-mapping").start()
        if not or_id:
            msg = "已删除 %s 的手填映射，下次取价起按自动匹配" % model
        elif known:
            msg = "已记录 %s → %s，正在重新取价" % (model, or_id)
        else:
            msg = ("已记录 %s → %s；本次快照里还没看到该条目，"
                   "取价后仍可能显示未定价" % (model, or_id))
        add_log_entry("[定价] 面板手填映射：%s → %s%s"
                      % (model, or_id or "(清除)",
                         "" if (known or not or_id) else "（快照暂无此条目）"),
                      tag="pricing")
        return self._json(200, {"ok": True, "model": model, "or_id": or_id,
                                "known": known, "msg": msg})

    def _route_realm(self, payload):
        # Changing the exit affects every key that is not realm-bound, so
        # it is an admin action: the panel session is required. GET /realm
        # stays open to API keys because it only reports the current exit.
        if not self._panel_ok():
            return self._error(403, "changing the upstream exit requires the "
                                    "panel session, not an API key",
                               "invalid_request_error")
        new_realm = payload.get("realm")
        if new_realm in ("intl", "cn"):
            save_persisted_realm(new_realm)
        return self._json(200, {"ok": True, "current": CURRENT_REALM, "persisted": True})

    def _route_accounts_checkin(self, payload):
        uid = payload.get("uid")
        invalidate_tasks_cache()          # 签到会改连续打卡状态，成长任务快照随之作废
        targets = [POOL.get(uid)] if uid else [a for a in (POOL.accounts if POOL else []) if a.realm == "cn"]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.checkin(trigger="manual")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_daily_chat(self, payload):
        uid = payload.get("uid")
        if uid:
            targets = [POOL.get(uid)]
        else:
            targets = [a for a in POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat(trigger="manual")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_daily_chat_web(self, payload):
        """网页通道打卡：只建网页端会话，不发桌面端那条轻量对话。

        手动触发用。刻意不写 lastDailyChat——那是「今天已经打过卡」的闸门，
        手动补一次不该让定时巡检跳过当天的正常流程。
        """
        uid = payload.get("uid")
        if uid:
            targets = [POOL.get(uid)]
        else:
            targets = [a for a in POOL.accounts if a.realm == "intl" and a.enabled]
        results = []
        for account in targets:
            if account is None:
                continue
            res = account.daily_chat_web(trigger="manual")
            log("account %s: 网页通道打卡 -> %s"
                % (account.uid[:8], res.get("conversation") if res.get("ok") else res.get("error")),
                level="INFO" if res.get("ok") else "WARN")
            results.append({"uid": account.uid, "nickname": account.nickname, **res})
        return self._json(200, {"results": results, "accounts": account_views()})

    def _route_accounts_login_start(self, payload):
        platform = payload.get("platform") or "CLI"
        target_realm = payload.get("realm") or CURRENT_REALM
        try:
            started = POOL.start_login(realm=target_realm, platform=platform)
        except Exception as exc:
            return self._error(502, "could not start login: %s" % exc)
        log("oauth login started (realm=%s, platform=%s, state=%s)" % (target_realm, platform, started["state"][:8]))
        return self._json(200, started)

    def _route_accounts_login_cancel(self, payload):
        state = payload.get("state") or ""
        return self._json(200, {"cancelled": POOL.cancel_login(state)})

    def _route_accounts_import_desktop(self, payload):
        # Two ways to call this:
        #   {}                     -> scan only (read-only, nothing imported)
        #   {"path": "..."}        -> import that credential
        #   {"all": true}          -> import everything the scan found
        #   {"recoverKey": true}   -> recover the at-rest key from the desktop client
        target_path = payload.get("path")
        if payload.get("recoverKey"):
            try:
                key = wb_accounts.desktop_atrest_key(
                    force=bool(payload.get("force")), log=lambda m: log(m, tag="accounts"))
            except Exception as exc:
                log("at-rest key recovery failed: %s" % exc, level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "msg": str(exc),
                                        "atrest": wb_atrest.status()})
            return self._json(200, {
                "ok": True,
                "keyId": wb_atrest.derive_key_id(key),
                "msg": "已从桌面客户端进程回收密钥（只留在内存里）",
                "atrest": wb_atrest.status(),
            })
        if payload.get("forgetKey"):
            wb_atrest.forget_key()
            return self._json(200, {"ok": True, "atrest": wb_atrest.status()})
        if target_path:
            realm = payload.get("realm")
            try:
                account = POOL.import_desktop_credential(
                    path=target_path, realm=realm, source="desktop-app")
            except Exception as exc:
                return self._error(400, "import failed: %s" % exc)
            log("imported %s from %s (user confirmed)" % (account.uid[:8], os.path.basename(target_path)))
            return self._json(200, {
                "imported": [account.public()],
                "accounts": account_views(),
            })
        if payload.get("all"):
            imported = import_desktop_accounts(payload.get("realm"))
            return self._json(200, {
                "imported": [a.public() for a in imported],
                "accounts": account_views(),
            })
        return self._json(200, {
            "detected": desktop_credential_scan(),
            "accounts": account_views(),
            "pool_uids": [a.uid for a in POOL.accounts],
            "atrest": wb_atrest.status(),
        })

    def _route_accounts_refresh(self, payload):
        uid = payload.get("uid")
        targets = [POOL.get(uid)] if uid else list(POOL.accounts)
        results = []
        for account in targets:
            if account is None:
                continue
            ok = account.refresh()
            account.save(ACCOUNTS_DIR)
            results.append({"uid": account.uid, "ok": ok, "error": account.last_error})
        return self._json(200, {"results": results})

    def _route_accounts_sync_profile(self, payload):
        """Manual nickname refresh from the web console.

        Opt-in by nature: only runs when the operator asks for it. The
        upstream response carries phone numbers and other personal fields;
        wb_accounts.fetch_account_profile parses only uid/nickname.
        """
        uid = str(payload.get("uid") or "").strip()
        realm = payload.get("realm")
        if uid:
            targets = [POOL.get(uid)]
        elif realm and realm != "all":
            targets = [a for a in POOL.accounts if a.realm == realm]
        else:
            targets = list(POOL.accounts)
        updated = []
        failed = []
        for account in targets:
            if account is None:
                continue
            try:
                nickname = account.sync_nickname()
            except Exception as exc:
                failed.append({"uid": account.uid, "error": str(exc)[:200]})
                log("profile sync failed for %s: %s" % (account.uid[:8], exc),
                    level="WARN")
                continue
            updated.append({"uid": account.uid, "nickname": nickname})
            log("account %s: nickname synced from the web console"
                % account.uid[:8], level="INFO")
        return self._json(200, {"updated": updated, "failed": failed,
                                "accounts": account_views()})

    def _route_accounts_test(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        account = POOL.get(uid)
        if not account:
            return self._error(404, "no such account")
        test_model = payload.get("model") or "deepseek-v4.1-flash"
        cfg = wb_accounts.get_realm_config(account.realm)
        chat_url = account.chat_base_url() + CHAT_PATH
        test_body = {
            "model": test_model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        forwarded = build_upstream_body(test_body)
        body = json.dumps(forwarded, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            chat_url, data=body, method="POST",
            headers=account.headers(purpose="chat")
        )
        t0 = time.time()
        try:
            with wb_accounts.urlopen(req, timeout=30, proxy=account.proxy) as resp:
                chat_obj = aggregate_stream(resp, test_model, None)
                wall_ms = int((time.time() - t0) * 1000)
                choices = chat_obj.get("choices") or []
                msg = (choices[0].get("message") or {}) if choices else {}
                reply_text = (msg.get("content") or msg.get("reasoning_content") or "OK").strip()
                if len(reply_text) > 80:
                    reply_text = reply_text[:77] + "..."
                # A served request, so it takes the semantic success path. That
                # clears the account-level streak/breaker/degrade counters, which
                # clear_error() never touched: a green panel test used to leave
                # soft_streak standing, and the next 429 resumed the ladder from
                # it. Only the tested model's cooldown is dropped here - sibling
                # models keep theirs, and a 402 balance park keeps its own lift
                # path (revive_balance_cooldown on a credits refresh).
                account.note_success(model=test_model)
                log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms ok=True", tag="accounts")
                return self._json(200, {
                    "ok": True,
                    "uid": account.uid,
                    "model": test_model,
                    "elapsed_ms": wall_ms,
                    "reply": reply_text,
                })
        except urllib.error.HTTPError as exc:
            wall_ms = int((time.time() - t0) * 1000)
            detail = exc.read(400).decode("utf-8", "replace")
            # The label keeps its 80-char cut; `detail` rides along untouched so the
            # panel can show the whole upstream body on hover.
            account.note_error(f"HTTP {exc.code}: {detail[:80]}", cooldown=60, detail=detail)
            log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms error={exc.code}", level="WARN", tag="accounts")
            return self._json(200, {
                "ok": False,
                "uid": account.uid,
                "status": exc.code,
                "error": f"HTTP {exc.code}: {detail[:150]}",
                "elapsed_ms": wall_ms,
            })
        except Exception as exc:
            wall_ms = int((time.time() - t0) * 1000)
            account.note_error(str(exc)[:80], cooldown=60)
            log(f"account test: uid={account.uid[:8]} model={test_model} wall={wall_ms}ms exc={exc}", level="WARN", tag="accounts")
            return self._json(200, {
                "ok": False,
                "uid": account.uid,
                "status": 500,
                "error": str(exc),
                "elapsed_ms": wall_ms,
            })

    def _route_accounts_set(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        # Each field is applied on its own so a caller can change one thing
        # without restating the others; at least one must be present.
        updated = None
        if "proxySlot" in payload:
            updated = POOL.set_proxy_slot(uid, payload.get("proxySlot"))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s proxy slot set to %s"
                % (uid[:8], updated.get("proxySlot") or "(direct)"))
        if "proxy" in payload:
            updated = POOL.set_proxy(uid, payload.get("proxy"))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s proxy set to %s"
                % (uid[:8], updated.get("proxy") or "(direct)"))
        if "enabled" in payload:
            updated = POOL.set_enabled(uid, bool(payload.get("enabled")))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s %s"
                % (uid[:8], "enabled" if payload.get("enabled") else "disabled"))
        if updated is None:
            return self._error(
                400, "nothing to update: pass 'enabled', 'proxy' or 'proxySlot'"
            )
        return self._json(200, {"account": updated})

    def _route_accounts_set_all(self, payload):
        POOL.set_all_enabled(bool(payload.get("enabled")))
        return self._json(200, {"accounts": account_views()})

    def _route_accounts_delete(self, payload):
        uid = payload.get("uid")
        if not uid:
            return self._error(400, "uid required")
        removed = POOL.remove(uid)
        log("account %s deleted" % uid[:8])
        return self._json(200, {"deleted": removed, "accounts": account_views()})

    def _route_accounts_import(self, payload):
        # Import a previously exported document (or any hand-written list
        # of accounts). Body shapes accepted, see wb_accounts._coerce_account_rows:
        #   {"format":"workbuddy-accounts","accounts":[...]}   <- our export
        #   [...]                                              <- bare list
        #   {"accessToken": ...}                               <- single account
        #   {"account":{...},"auth":{...}}                     <- desktop credential
        #
        # Options:
        #   dryRun    (bool) - validate and report, write nothing
        #   overwrite (bool) - replace accounts whose uid already exists
        #   realm     ("intl"|"cn") - force a realm instead of detecting it
        #
        # `data` carries the document. It is preferred over the bare body so
        # the body can also hold the options above.
        blob = payload.get("data") if "data" in payload else payload
        if not isinstance(blob, (dict, list)):
            return self._error(400, "the document must be a JSON object or array",
                               "invalid_request_error")
        rows, problem = wb_accounts._coerce_account_rows(blob)
        if problem:
            return self._error(400, "cannot read the document: %s" % problem,
                               "invalid_request_error")
        dry_run = bool(payload.get("dryRun"))
        overwrite = bool(payload.get("overwrite"))
        forced_realm = (payload.get("realm") or "").strip().lower() or None
        if forced_realm and forced_realm not in ("intl", "cn"):
            return self._error(400, "realm must be intl or cn", "invalid_request_error")
        if dry_run:
            # Validate every row without touching the pool so the caller can
            # see exactly what an import would do before committing to it.
            # Shares its rules with the real import, so the preview cannot
            # disagree with what would actually happen.
            return self._json(200, {
                "dryRun": True,
                "count": len(rows),
                "result": POOL.preview_import_rows(rows, realm=forced_realm, overwrite=overwrite),
                "accounts": account_views(),
            })
        report = POOL.import_rows(rows, realm=forced_realm, overwrite=overwrite)
        log("account import: %d added, %d updated, %d skipped, %d invalid"
            % (len(report["added"]), len(report["updated"]),
               len(report["skipped"]), len(report["invalid"])))
        return self._json(200, {
            "count": len(rows),
            "result": report,
            "accounts": account_views(),
        })

    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        # The gateway is stateless: it keeps no store of previous responses,
        # so it cannot replay a prior turn. Silently ignoring the field would
        # answer a follow-up as if it were a fresh conversation - the client
        # gets a normal-looking reply with the context missing. Say so instead.
        # 拒绝 namespace 工具，逼 Codex fallback 成 flat 工具清单。
        # 不这样做的话，MCP／外挂工具全部会被 app 判定为不可执行。
        if payload.get("previous_response_id"):
            return self._error(
                400,
                "previous_response_id is not supported: this gateway does not "
                "store response state. Send the full conversation in 'input' "
                "instead, or use a stateless client.",
                "invalid_request_error")
        session_key = extract_session_key(self.headers, payload)
        custom_names = custom_tool_names(payload.get("tools"))
        chat_req = responses_to_chat(payload)
        ns_map = chat_req.pop("_namespace_map", None)
        # Echo these back on the response object; see chat_to_response.
        request_meta = {
            "tools": payload.get("tools") or [],
            "tool_choice": payload.get("tool_choice", "auto"),
            "parallel_tool_calls": payload.get("parallel_tool_calls", True),
        }
        model = payload.get("model") or "deepseek-v4.1-flash"
        want_stream = bool(payload.get("stream"))
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log(
            "responses: model=%s stream=%s msgs=%d effort=%r custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               sorted(custom_names) or "-")
        )
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            deadline_at = time.monotonic() + FIRST_FRAME_DEADLINE
            upstream, account, effort = open_upstream(chat_req, session_key=session_key,
                                              target_realm=req_realm, deadline_at=deadline_at)
            upstream, replacement = probe_first_frame(
                upstream, retry_call=_make_retry_call(chat_req, session_key, req_realm),
                tried=[account.uid], deadline_at=deadline_at)
            account = replacement or account
            if upstream is None:
                raise RuntimeError("upstream first-frame probe failed")
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200], elapsed_ms=int(t * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._responses_stream_response(
                    upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                    base_body=chat_req, session_key=session_key, realm=req_realm,
                    effort=effort)
            return self._responses_nonstream_response(
                upstream, model, custom_names, request_meta, fp, account, t_start, ns_map,
                base_body=chat_req, session_key=session_key, realm=req_realm,
                effort=effort)

    def _responses_stream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None):
        retry_call = _make_retry_call(base_body, session_key, realm) if base_body else None
        try:
            upstream, replacement = probe_first_frame(upstream, retry_call=retry_call,
                                                      tried=[account.uid])
            account = replacement or account
            if upstream is None:
                return self._error(502, "upstream first-frame probe failed")
        except Exception as exc:
            _close_upstream(upstream)
            record_error(model, 502, "first-frame probe failed: %s" % type(exc).__name__,
                         elapsed_ms=int((time.time() - t_start) * 1000), account=account.uid,
                         key=self._key_id(), effort=effort)
            return self._error(502, "upstream first-frame probe failed")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None, "custom_names": custom_names,
                  "request_meta": request_meta,
                  "namespace_map": namespace_map,
                  "base_body": base_body,
                  "base_messages": (base_body or {}).get("messages"),
                  "session_key": session_key,
                  "retry_owner": _retry_scope(self),
                  "realm": realm,
                  "retry_call": retry_call,
                  "account_uid": account.uid,
                  "account_usage": {}}
        holder.update(key_id=self._key_id(), effort=effort)
        holder["record_attempt"] = _attempt_recorder(holder, model, fp, t_start)
        first_ms = None
        wall = None
        outcome = None
        try:
            # 一轮跑完如果模型要的是 web_search / web_fetch，就由反代
            # 执行、把结果喂回去再跑一轮。客户端从头到尾只看到一则连续的回应。
            rounds = 0
            iteration = 0
            total_usage = None
            while True:
                holder.pop("internal_calls", None)
                holder.pop("suppress_completion", None)
                holder.pop("will_continue", None)
                holder["usage"] = None
                holder["round_accounted"] = False
                holder["usage_recorded"] = False
                holder["suppress_lifecycle"] = iteration > 0
                holder["account_uid"] = account.uid
                for frame in stream_responses_events(upstream, model, holder):
                    if first_ms is None:
                        first_ms = int((time.time() - t_start) * 1000)
                    _write_stream_frame(self, clean_responses_frame(frame))
                # 每一轮的 token 都是真的花掉的，记帐要加总
                total_usage = sum_usage(total_usage, holder.get("usage"))
                ledger = holder["account_usage"]
                ledger[account.uid] = sum_usage(ledger.get(account.uid), holder.get("usage"))
                holder["round_accounted"] = True
                _close_upstream(upstream)
                iteration += 1
                if holder.get("will_continue"):
                    upstream = holder.pop("continue_upstream")
                    account = holder.pop("continue_account")
                    holder["retried"] = True
                    continue
                internal = holder.get("internal_calls") or []
                if not internal:
                    break
                holder["record_attempt"]("web_tool_followup")
                rounds += 1
                # 用完就收回工具，让模型自己收尾；这里不合成任何事件。
                give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
                try:
                    upstream.close()
                except Exception:
                    pass
                upstream, account, _ = follow_up_with_tool_results(
                    internal, holder, model, session_key, t_start, drop_tools=give_up)
                upstream, replacement = probe_first_frame(
                    upstream, retry_call=holder.get("retry_call"), tried=[account.uid])
                account = replacement or account
                if upstream is None:
                    raise RuntimeError("web follow-up failed its first-frame probe")
            if total_usage:
                holder["total_usage"] = total_usage
        except _ClientStreamClosed:
            outcome = "client_aborted"
        except Exception as exc:
            holder["incomplete_reason"] = "upstream_stream_error"
            outcome = "upstream_aborted"
            try:
                if not holder.get("terminal_sent"):
                    self.wfile.write(_response_failed_frame(holder, model, "upstream_stream_error"))
                self.wfile.flush()
            except Exception:
                pass
        finally:
            # 代跑多轮时 upstream 会被换掉，外层的 with 只认得最开始那一条，
            # 最后一条要在这里收掉。
            _close_upstream(upstream)
            # A prepared continuation may exist when the downstream disconnects
            # while its final reasoning/output-item events are being flushed.
            pending = holder.pop("continue_upstream", None)
            if pending is not None:
                _close_upstream(pending)
            _finish_loop_retry(holder, False)
        wall = int((time.time() - t_start) * 1000)
        failed = holder.get("incomplete_reason")
        details = dict(elapsed_ms=wall, stream=True, ttft_ms=first_ms,
                       gen_ms=(wall - first_ms) if first_ms is not None else None,
                       fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        usage = None if holder.get("usage_recorded") else holder.get("usage")
        if failed:
            record_error(model, 502, failed, usage=usage, outcome="upstream_aborted", **details)
        elif not holder.get("usage_recorded"):
                record_usage(model, usage, outcome=outcome or "completed", **details)
        return

    def _responses_nonstream_response(self, upstream, model, custom_names, request_meta, fp, account, t_start, namespace_map=None, base_body=None, session_key=None, realm=None, effort=None):
        # 跟串流那条一样：客户端宣告 web_search / web_fetch 时由反代代跑。
        # 中间那几轮对客户端不可见，最后才组成一个 Responses 物件回传；不这样
        # 做的话 web_search 的 function_call 会直接漏给客户端，客户端只会回
        # 一句 unsupported call。
        sources = []
        rounds = 0
        holder = {"base_messages": (base_body or {}).get("messages"),
                  "base_body": base_body, "realm": realm, "account": account,
                  "session_key": session_key, "retry_owner": _retry_scope(self),
                  "web_sources": sources,
                  "retry_call": _make_retry_call(base_body, session_key, realm) if base_body else None}
        holder.update(key_id=self._key_id(), effort=effort)
        holder["record_attempt"] = _attempt_recorder(holder, model, fp, t_start, stream=False)
        # 开关关闭时不拦同名呼叫：那是客户端自己的工具。
        web_tools = web_tools_active(base_body)
        while True:
            try:
                chat_obj, account = _aggregate_with_recovery(upstream, model, account, holder)
            except Exception as exc:
                account = holder.get("account") or account
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id(), effort=effort)
                return self._error(502, f"upstream stream error: {exc}")
            if chat_obj.get("_ops_failure"):
                break
            calls = internal_calls_from_chat(chat_obj, web_tools=web_tools)
            if not calls:
                break
            if holder.get("web_tools_withdrawn"):
                chat_obj["_ops_failure"] = "web_tool_budget_exhausted"
                break
            holder["record_attempt"]("web_tool_followup")
            rounds += 1
            give_up = rounds > wb_webtools.MAX_WEB_ROUNDS
            try:
                upstream, account, _ = follow_up_with_tool_results(
                    calls, holder, model, session_key, t_start, drop_tools=give_up)
                upstream, replacement = probe_first_frame(
                    upstream, retry_call=holder.get("retry_call"), tried=[account.uid])
                account = replacement or account
                if upstream is None:
                    raise RuntimeError("web follow-up failed its first-frame probe")
            except Exception as exc:
                record_error(model, 502, "web tool follow-up failed: %s" % exc,
                             elapsed_ms=int((time.time() - t_start) * 1000),
                             account=account.uid, key=self._key_id(), effort=effort)
                return self._error(502, "web tool follow-up failed: %s" % exc)
            sources = holder.get("web_sources") or sources
        wall = int((time.time() - t_start) * 1000)
        local_usage = None if holder.get("usage_recorded") else holder.get("usage")
        chat_obj["usage"] = holder.get("total_usage") or chat_obj.get("usage")
        result = chat_to_response(chat_obj, model, custom_names, request_meta, namespace_map,
                                  sources=sources)
        if chat_obj.get("_ops_failure"):
            record_error(model, 502, chat_obj["_ops_failure"], usage=local_usage,
                         stream=False, elapsed_ms=wall, fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        elif not holder.get("usage_recorded"):
            record_usage(model, local_usage, stream=False, elapsed_ms=wall, fp=fp,
                         account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)

    def _handle_messages_count_tokens(self, payload):
        """Best-effort Anthropic count_tokens endpoint.

        The WorkBuddy upstream has no Anthropic tokenizer. This estimate uses
        the same CJK-aware estimator as the gateway's own accounting and keeps
        the native response shape; it is intentionally not presented as an
        official model-token count.
        """
        try:
            chat = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        return self._json(200, {"input_tokens": _anthropic_estimate_chat_tokens(chat)})

    def _handle_messages(self, payload):
        """Serve an Anthropic Messages request through the chat pipeline."""
        try:
            chat_req = messages_to_chat(payload)
        except ValueError as exc:
            return self._anthropic_error(400, str(exc), "invalid_request_error")
        model = chat_req.get("model") or "unknown"
        want_stream = bool(chat_req.get("stream"))
        session_key = extract_session_key(self.headers, chat_req)
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        try:
            client_ip = self.client_address[0] if self.client_address else ""
        except Exception:
            client_ip = ""
        log("messages: model=%s stream=%s msgs=%d effort=%r tools=%d"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"), len(chat_req.get("tools") or [])))
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._anthropic_error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(chat_req.get("model"))
            if banned:
                return self._anthropic_error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(chat_req.get("model"))
            if key_blocked:
                return self._anthropic_error(400, key_blocked, "invalid_request_error")
            upstream, account, effort = open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm)
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(403, "upstream 403: %s" %
                                          (exc.detail or "content rejected"),
                                          "permission_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_rate_limited(exc)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(600).decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._anthropic_error(exc.code,
                                         "upstream %s: %s" % (exc.code, detail))
        except Exception as exc:
            message = str(exc)
            low = message.lower()
            status = 503 if ("no account" in low or "no enabled account" in low) else 502
            record_error(model, status, message,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if status == 503:
                message += " - add or enable one at the dashboard (/)"
            return self._anthropic_error(status, message)
        with upstream:
            if want_stream:
                return self._messages_stream_response(
                    upstream, model, fp, account, t_start,
                    base_body=chat_req, session_key=session_key,
                    realm=req_realm, effort=effort)
            return self._messages_nonstream_response(
                upstream, model, fp, account, t_start,
                base_body=chat_req, session_key=session_key,
                realm=req_realm, effort=effort)

    def _messages_nonstream_response(self, upstream, model, fp, account, t_start,
                                     base_body=None, session_key=None, realm=None,
                                     effort=None):
        holder = {"base_body": base_body, "account": account,
                  "session_key": session_key, "realm": realm,
                  "retry_owner": _retry_scope(self), "key_id": self._key_id(), "effort": effort,
                  "retry_call": _make_retry_call(base_body, session_key, realm) if base_body else None}
        holder["record_attempt"] = _attempt_recorder(holder, model, fp, t_start, stream=False)
        try:
            chat_obj, account = _aggregate_with_recovery(upstream, model, account, holder)
            failure = chat_obj.get("_ops_failure")
            if failure:
                record_error(model, 502, failure,
                             usage=None if holder.get("usage_recorded") else holder.get("usage"),
                             stream=False, account=account.uid, key=self._key_id(), effort=effort)
                return self._anthropic_error(502, "upstream safeguards: " + failure)
            local_usage = holder.get("usage")
            chat_obj["usage"] = holder.get("total_usage") or chat_obj.get("usage")
            result = chat_to_messages(chat_obj)
            wall = int((time.time() - t_start) * 1000)
            record_usage(model, local_usage, stream=False, elapsed_ms=wall,
                         fp=fp, account=account.uid, key=self._key_id(), effort=effort)
            return self._json(200, result)
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            record_error(model, 502, "messages upstream error: %s" % exc,
                         elapsed_ms=wall, account=account.uid, key=self._key_id(), effort=effort)
            return self._anthropic_error(502, "upstream stream error: %s" % exc)
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def _messages_stream_response(self, upstream, model, fp, account, t_start,
                                  base_body=None, session_key=None, realm=None,
                                  session_meta=None, effort=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"usage": None, "account": account, "base_body": base_body,
                  "session_key": session_key, "realm": realm,
                  "retry_owner": _retry_scope(self), "key_id": self._key_id(), "effort": effort,
                  "retry_call": _make_retry_call(base_body, session_key, realm) if base_body else None}
        holder["record_attempt"] = _attempt_recorder(holder, model, fp, t_start,
                                                    usage_field="usage_this_round")
        protected = _stream_chat_events(upstream, model, holder)
        first_ms = None
        try:
            for frame in stream_messages_events(protected, model):
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                _write_stream_frame(self, frame)
        except (_ClientStreamClosed, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            wall = int((time.time() - t_start) * 1000)
            account = holder.get("account") or account
            record_usage(model, None if holder.get("usage_recorded") else holder.get("usage_this_round"), stream=True, elapsed_ms=wall,
                         ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid,
                         outcome="client_aborted", key=self._key_id(), effort=effort)
            return
        except Exception as exc:
            wall = int((time.time() - t_start) * 1000)
            account = holder.get("account") or account
            record_error(model, 502, "messages stream aborted: %s" % exc,
                         elapsed_ms=wall, account=account.uid,
                         usage=None if holder.get("usage_recorded") else holder.get("usage_this_round"), stream=True,
                         ttft_ms=first_ms, fp=fp, outcome="upstream_aborted",
                         key=self._key_id(), effort=effort)
            try:
                self.wfile.write(anthropic_sse_frame("error", {
                    "error": {"type": "api_error", "message": str(exc)}}))
                self.wfile.flush()
            except Exception:
                pass
            return
        finally:
            protected.close()
            _close_upstream(holder.pop("continue_upstream", None))
            _finish_loop_retry(holder, False)
            try:
                upstream.close()
            except Exception:
                pass
        wall = int((time.time() - t_start) * 1000)
        account = holder.get("account") or account
        if holder.get("incomplete_reason"):
            record_error(model, 502, holder["incomplete_reason"],
                         usage=None if holder.get("usage_recorded") else holder.get("usage_this_round"), stream=True, elapsed_ms=wall,
                         account=account.uid, key=self._key_id(), effort=effort)
            return
        record_usage(model, holder.get("usage_this_round"), stream=True, elapsed_ms=wall,
                     ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._handle_settings_save()
        if path == "/updates/check":
            # Panel-only management write, like the pricing ones. No body to
            # read: the action takes no parameters, so a button that posts
            # nothing must not be answered with "invalid JSON body".
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            return self._route_update_check()
        if path in ("/pricing/refresh", "/pricing/mapping"):
            # Panel-only management writes (the pricing table is the panel's
            # own view of the estimate). Registered here because the generic
            # dispatcher below only routes chat and account paths - POST
            # /pricing/refresh used to 404, which made the panel's "立即取价"
            # button fail silently behind its toast.
            if not self._panel_ok():
                return self._error(401, "panel password required", "invalid_request_error")
            payload = self._payload_or_error()
            if payload is None:
                return
            if path == "/pricing/mapping":
                return self._route_pricing_mapping(payload)
            return self._route_pricing_refresh(payload)
        if path.startswith("/proxy/"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            return self._handle_proxy_slots(path, payload)
        if path in ("/agents/apply", "/agents/restore"):
            if not self._panel_ok():
                return self._error(
                    401, "panel password required", "invalid_request_error"
                )
            payload = self._payload_or_error()
            if payload is None:
                return
            if path == "/agents/apply":
                return self._handle_agents_apply(payload)
            return self._handle_agents_restore(payload)
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required", "invalid_request_error")
        is_messages_route = path in (
            "/v1/messages", "/messages",
            "/v1/messages/count_tokens", "/messages/count_tokens",
        )
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and not is_messages_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/completions", "/completions",
                "/v1/responses", "/responses"):
            return self._error(404, "not found", "invalid_request_error")
        if is_messages_route:
            if not self._key_ok():
                return self._anthropic_error(
                    401, "missing or invalid API key", "authentication_error")
        elif not self._authorized():
            return
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        if is_messages_route:
            if path.endswith("/count_tokens"):
                return self._handle_messages_count_tokens(payload)
            if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
                message = ("gateway is at its concurrent chat limit "
                           "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
                record_error(payload.get("model") or "unknown", 503, message,
                             stream=payload.get("stream"), key=self._key_id())
                return self._anthropic_error(503, message, "overloaded_error")
            try:
                return self._handle_messages(payload)
            finally:
                _chat_slots.release()
        # Both OpenAI-shaped routes below can hold a thread for up to 600s.
        # Take a slot for the duration; release it in finally so every early
        # return (including client disconnects) gives the slot back.
        if not _chat_slots.acquire(timeout=CHAT_SLOT_WAIT_SECONDS):
            return self._error(503, "gateway is at its concurrent chat limit "
                                    "(%d in flight); retry shortly" % MAX_CONCURRENT_CHAT)
        try:
            return self._dispatch_chat_post(path, payload)
        finally:
            _chat_slots.release()

    def _dispatch_chat_post(self, path, payload):
        # 先挡背景请求：Codex 自己发的（记忆整理／环境建议／自动复核）
        # 不算「使用者实际使用」，一律本地拒绝，不碰上游。
        if BLOCK_BACKGROUND_REQUESTS:
            reason = background_request_reason(payload)
            if reason:
                try:
                    log("background request blocked: model=%s trigger=(%s)"
                        % (payload.get("model"), reason), level="INFO")
                except Exception:
                    pass
                return self._error(400, background_request_message(reason),
                                   "invalid_request_error")
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)
        # Diagnostics: what the client actually asked for, and what we forward.
        # Only the knobs that change behaviour are logged - never message text.
        forwarded = build_upstream_body(payload)
        given = client_effort_of(payload) or payload.get("reasoning") \
            or payload.get("thinking") or payload.get("enable_thinking")
        log(
            "chat: model=%s client_effort=%r -> upstream_effort=%r stream=%s msgs=%d"
            % (
                payload.get("model"),
                given,
                upstream_effort_of(forwarded, payload.get("model")),
                bool(payload.get("stream")),
                len(forwarded.get("messages") or []),
            )
        )
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(forwarded.get("messages"))
        want_stream = bool(payload.get("stream"))
        model = payload.get("model") or "hy4-preview"
        t_start = time.time()
        try:
            req_realm = self._request_realm() or CURRENT_REALM
            blocked = self._cross_realm_error(payload.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            banned = self._banned_model_error(payload.get("model"))
            if banned:
                return self._error(400, banned, "invalid_request_error")
            key_blocked = self._key_model_error(payload.get("model"))
            if key_blocked:
                return self._error(400, key_blocked, "invalid_request_error")
            deadline_at = time.monotonic() + FIRST_FRAME_DEADLINE
            upstream, account, effort = open_upstream(payload, session_key=session_key,
                                              target_realm=req_realm, deadline_at=deadline_at)
            upstream, replacement = probe_first_frame(
                upstream, retry_call=_make_retry_call(payload, session_key, req_realm),
                tried=[account.uid], deadline_at=deadline_at)
            account = replacement or account
            if upstream is None:
                raise RuntimeError("upstream first-frame probe failed")
        except ContentRejected as exc:
            record_error(model, 403, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(403, "upstream 403: %s" % (exc.detail or "content rejected"),
                               "invalid_request_error")
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            return self._error(exc.code, f"upstream {exc.code}: {detail}")
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message, elapsed_ms=int((time.time() - t_start) * 1000),
                         account=getattr(exc, "account_uid", None),
                         key=self._key_id())
            if message.startswith("no usable account"):
                # Only a genuinely empty/cooling pool is a 503. A throttled model
                # is reported as 429 by _rate_limited above instead.
                return self._error(503, message +
                                   " - add or enable one at the dashboard (/)")
            return self._error(502, f"upstream unreachable: {exc}")
        with upstream:
            if want_stream:
                return self._chat_stream_response(
                    upstream, model, fp, account, t_start,
                    base_body=payload, session_key=session_key, realm=req_realm, effort=effort)
            return self._chat_nonstream_response(
                upstream, model, fp, account, t_start,
                base_body=payload, session_key=session_key, realm=req_realm, effort=effort)

    @staticmethod
    def _scrub_stream_line(data):
        return clean_chunk(data)

    def _chat_stream_response(self, upstream, model, fp, account, t_start,
                              base_body=None, session_key=None, realm=None, effort=None):
        callback = _make_retry_call(base_body, session_key, realm) if base_body else None
        try:
            upstream, replacement = probe_first_frame(upstream, retry_call=callback,
                                                      tried=[account.uid])
            account = replacement or account
            if upstream is None:
                return self._error(502, "upstream first-frame probe failed")
        except Exception as exc:
            _close_upstream(upstream)
            record_error(model, 502, "first-frame probe failed: %s" % type(exc).__name__,
                         elapsed_ms=int((time.time() - t_start) * 1000), account=account.uid,
                         key=self._key_id(), effort=effort)
            return self._error(502, "upstream first-frame probe failed")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        holder = {"account": account, "base_body": base_body,
                  "session_key": session_key, "realm": realm, "retry_owner": _retry_scope(self),
                  "retry_call": callback, "account_usage": {}}
        holder.update(key_id=self._key_id(), effort=effort)
        holder["record_attempt"] = _attempt_recorder(
            holder, model, fp, t_start, usage_field="usage_this_round")
        first_ms = None
        outcome = None
        try:
            for frame in _stream_chat_events(upstream, model, holder):
                if first_ms is None:
                    first_ms = int((time.time() - t_start) * 1000)
                _write_stream_frame(self, frame)
        except _ClientStreamClosed:
            outcome = "client_aborted"
        except Exception:
            holder["incomplete_reason"] = "upstream_stream_error"
            try:
                for frame in _chat_error_frames(holder, "upstream_stream_error"):
                    _write_stream_frame(self, frame)
                _write_stream_frame(self, b"data: [DONE]\n\n")
            except _ClientStreamClosed:
                pass
        finally:
            _close_upstream(upstream)
            pending = holder.pop("continue_upstream", None)
            if pending is not None:
                _close_upstream(pending)
        wall = int((time.time() - t_start) * 1000)
        account = holder.get("account") or account
        failed = holder.get("incomplete_reason")
        details = dict(stream=True, elapsed_ms=wall, ttft_ms=first_ms,
                       gen_ms=(wall - first_ms) if first_ms is not None else None,
                       fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        usage = None if holder.get("usage_recorded") else holder.get("usage_this_round")
        if failed:
            record_error(model, 502, failed, usage=usage, outcome="upstream_aborted", **details)
        elif not holder.get("usage_recorded"):
            record_usage(model, usage, outcome=outcome or "completed", **details)
        return

    def _chat_nonstream_response(self, upstream, model, fp, account, t_start,
                                 base_body=None, session_key=None, realm=None, effort=None):
        holder = {"base_body": base_body, "account": account,
                  "session_key": session_key, "realm": realm, "retry_owner": _retry_scope(self),
                  "retry_call": (_make_retry_call(base_body, session_key, realm) if base_body else None)}
        holder.update(key_id=self._key_id(), effort=effort)
        holder["record_attempt"] = _attempt_recorder(holder, model, fp, t_start, stream=False)
        try:
            result, account = _aggregate_with_recovery(upstream, model, account, holder)
        except Exception as exc:
            record_error(model, 502, str(exc), elapsed_ms=int((time.time() - t_start) * 1000),
                         account=account.uid, key=self._key_id(), effort=effort)
            return self._error(502, f"upstream stream error: {exc}")
        wall = int((time.time() - t_start) * 1000)
        if result.get("_ops_failure"):
            record_error(model, 502, result["_ops_failure"],
                         usage=None if holder.get("usage_recorded") else holder.get("usage"),
                         stream=False, elapsed_ms=wall, fp=fp, account=account.uid,
                         key=self._key_id(), effort=effort)
            return self._error(502, "upstream safeguards: " + result["_ops_failure"])
        result["usage"] = holder.get("total_usage") or result.get("usage")
        first_at = result.get("first_chunk_at")
        # Measured from request arrival so streaming and non-streaming are comparable.
        first_ms = int((first_at - t_start) * 1000) if first_at else None
        record_usage(model, holder.get("usage"), stream=False,
                     elapsed_ms=wall, ttft_ms=first_ms,
                     gen_ms=(wall - first_ms) if first_ms is not None else None,
                     fp=fp, account=account.uid, key=self._key_id(), effort=effort)
        return self._json(200, result)

def running_version():
    """The version this process reports, e.g. "1.6.19".

    tests/_test_release_engineering.py pins the two version literals and asserts
    they agree, so the update checker reads the running one from the handler
    instead of becoming a third copy that could drift away from both.
    """
    return Handler.server_version.split("/", 1)[-1]

def main():
    args = _parse_cli_args()
    _apply_cli_overrides(args)
    if _probe_running_instance(args):
        return
    api_key_generated = _bootstrap_runtime(args)
    if _report_first_run(args):
        return
    _log_startup_summary(args, api_key_generated)
    _serve_forever(args)

def _parse_cli_args():
    ap = argparse.ArgumentParser(description="WorkBuddy (workbuddy.ai) -> OpenAI-compatible proxy")
    ap.add_argument("--info", help="path to the WorkBuddy *.info credential file")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or "8788"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the LAN can "
                         "reach it (implies --host 0.0.0.0 and forces an api key)")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY") or os.environ.get("WB_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT,
                    help="system message injected when the request has none (required upstream)")
    ap.add_argument("--user-agent", default=None,
                    help="override the upstream User-Agent (default: mirror the official "
                         "WorkBuddy AI client)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl (default: ./usage)")
    ap.add_argument("--accounts-dir", default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live (default: ./accounts)")
    ap.add_argument("--import-desktop", action="store_true",
                    help="import the desktop app credential as an account, then exit")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: admin)")
    args = ap.parse_args()
    return args

def _apply_cli_overrides(args):
    global USAGE_DIR, USAGE_LOG
    # LAN mode binds every interface. The key is generated below, once
    # ACCOUNTS_DIR is resolved, so it can be persisted and reused.
    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.user_agent:
        wb_accounts.USER_AGENT = args.user_agent.strip()
        log("user-agent : %s (override)" % wb_accounts.USER_AGENT)
    if args.usage_dir:
        USAGE_DIR = os.path.abspath(args.usage_dir)
        USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
    # 账号活动历史与用量日志同目录，并且要在任何后台线程起来之前定下来：调度器
    # 的第一轮巡检不能落在 --usage-dir 生效之前，否则记录会写进默认目录。
    wb_activity.set_data_dir(USAGE_DIR)

def _probe_running_instance(args):
    # Refuse to start a second copy. On Windows SO_REUSEADDR lets two sockets
    # bind the same port, which silently splits incoming connections between
    # them - confusing and hard to diagnose.
    try:
        probe = urllib.request.urlopen(
            f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/health",
            timeout=2,
        )
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None  # nothing answering /health - let the bind below decide
    if isinstance(existing, dict):
        # Only OUR /health carries the account-pool fields ("accounts"). Other
        # services can occupy the same port and also answer /health with JSON
        # (a dev proxy, another gateway); treating that as "already running" made this
        # launcher exit silently while the port belonged to someone else - the
        # dashboard then showed a foreign UI and API calls failed with 401/404.
        foreign = existing.get("service") or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print(f"  [ERROR] port {args.port} is already taken by another program: {who}")
            print("          wb-proxy itself is NOT running - nothing was started.")
            print()
            print("  Fix: start wb-proxy on a different port, e.g.")
            print("          %s" % launcher_hint(args.port + 1))
            print(f"          python3 wb_proxy.py --port {args.port + 1}")
            print()
            print("  Check who owns the port:  %s" % port_owner_hint(args.port))
            print()
            raise SystemExit(1)
        print()
        print(f"  [已有一个反代在 {args.port} 端口运行，无需重复启动]")
        print(f"  账号: {existing.get('uid', '?')} @ {existing.get('domain', '?')}")
        print(f"  看板: http://127.0.0.1:{args.port}/")
        print()
        print("  如果要重启: 先把原来那个窗口关掉（或结束 python 进程），再运行本程序。")
        print()
        # Return True so main() stops here. A bare return gives None, which
        # main() reads as "no running copy" and it would carry on to bind the
        # port that is already taken.
        return True
    return False

def _bootstrap_runtime(args):
    global POOL, ACCOUNTS_DIR, API_KEY, SYSTEM_PROMPT
    global API_KEY_FILE_SET, SCHEDULER
    api_key_generated = False
    API_KEY = args.api_key
    SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # LAN mode must not ship a known key: the gateway spends the account's own
    # upstream quota, so a guessable default lets anyone on the network drain
    # it. Generate one on first use, persist it, and reuse it afterwards.
    if args.lan and not API_KEY:
        API_KEY, api_key_generated = wb_settings.ensure_launcher_key(ACCOUNTS_DIR)
    # A key saved from the panel wins over an auto-generated LAN key so a
    # change made in the browser survives a restart of the .bat file. An
    # explicit --api-key on the command line still takes precedence.
    global API_KEY_FILE_SET
    saved_key, key_from_panel = wb_settings.api_key_override(ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        API_KEY = saved_key
        API_KEY_FILE_SET = True
    if args.panel_password:
        wb_settings.set_panel_password(ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif wb_settings.panel_password_is_default(ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it in the panel")
    POOL = wb_accounts.AccountPool(ACCOUNTS_DIR, log=log)
    POOL.load()
    POOL.apply_proxy_slots()
    POOL.apply_reserve_credits()
    # WB_MAX_CONCURRENT_CHAT=auto: size the chat ceiling from the pool that was
    # just loaded. Runs before the listener exists, so no request can hold a
    # slot yet and the semaphore swap is safe.
    resize_chat_slots(POOL.count_ready())
    apply_daily_token_limit()
    apply_daily_credit_limit()
    apply_model_daily_token_limit()
    load_persisted_realm()
    global SCHEDULER
    from wb_scheduler import Scheduler
    SCHEDULER = Scheduler(POOL)
    SCHEDULER.start()
    global PRICING
    # The policy table and its timeline live beside the usage log, so one
    # volume carries both and the request references resolve locally.
    wb_pricing.set_data_dir(USAGE_DIR)
    # The panel's pricing switches (master switch, variant inheritance) live in
    # settings.json; point the pricing side at the same file the panel writes.
    wb_pricing.set_settings_dir(ACCOUNTS_DIR)
    # The refresher always exists so /pricing can report its state; with the
    # master switch off its loop simply parks without fetching.
    PRICING = wb_pricing.PriceRefresher(
        wb_settings.pricing_refresh_minutes(ACCOUNTS_DIR))
    PRICING.start()
    # Credit balances back the expiring-credits dispatch preference, and only
    # the sign-in / daily-activity tasks used to refresh them. The refresher
    # keeps them current in the background so no request pays for a lookup.
    global CREDITS_REFRESHER
    CREDITS_REFRESHER = wb_accounts.CreditsRefresher(POOL)
    CREDITS_REFRESHER.start()
    global UPDATES
    # Release discovery only: the checker asks GitHub what the newest stable
    # release is and reports it. It never downloads, replaces or restarts
    # anything, and with the daily switch off (the default) it sends nothing.
    UPDATES = wb_updates.UpdateChecker(
        current_version=running_version(),
        settings_dir=ACCOUNTS_DIR,
        log=lambda msg: add_log_entry("[更新] %s" % msg, tag="update"),
    )
    UPDATES.start()
    return api_key_generated

def _report_first_run(args):
    if args.info:
        account = POOL.import_desktop_credential(args.info, source="file")
        log("imported account %s from %s" % (account.uid[:8], args.info))
    first_run = not POOL.accounts
    if first_run:
        # Never adopt the desktop client's login silently: just report what is
        # available and let the user import it from the dashboard.
        detected = desktop_credential_scan()
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d desktop credential(s), NOT importing" % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("open the dashboard and click [Scan desktop app] to import")
        else:
            log("no accounts yet - no desktop credentials found on this machine")
    if first_run and not POOL.accounts:
        # Do NOT exit here: the dashboard has to stay reachable so a new
        # account can be added through the browser login flow.
        log("still no accounts - starting anyway so you can log in via the dashboard")
    if args.import_desktop:
        for account in POOL.accounts:
            print("  %s  %s  %s" % (account.uid[:8], account.nickname, account.domain))
        return

def _log_startup_summary(args, api_key_generated):
    rep = current_account()
    log("accounts   : %d total, %d usable" % (len(POOL.accounts), POOL.count_ready()))
    for account in POOL.accounts:
        log("  - %s  %s  %s  %s" % (account.uid[:8], account.nickname or "(no name)",
                                    account.domain, wb_accounts._human_delta(
                                        (account.expires_at or 0) - time.time()) or "?"))
    log("store      : %s" % ACCOUNTS_DIR)
    log(f"credential : {rep.path if rep else chr(45)}")
    if os.path.exists(PRODUCT_CONFIG_CACHE):
        log(f"catalog    : {PRODUCT_CONFIG_CACHE}")
    else:
        log("catalog    : app cache not found - will use the model API instead")
    log(f"account    : {rep.uid if rep else chr(45)} @ {rep.domain if rep else chr(45)}")
    log(f"issuer     : {wb_accounts.jwt_issuer(rep.access_token) if rep else chr(45)}")
    log("realm      : %s (%s)" % (
        CURRENT_REALM,
        "www.workbuddy.ai" if CURRENT_REALM == "intl" else "copilot.tencent.com"))
    log("user-agent : %s" % wb_accounts.USER_AGENT)
    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % API_KEY)
        if api_key_generated:
            print("                (newly generated & saved to accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste the key.")
        print()
        if IS_WINDOWS:
            print("    If nothing can connect, allow python through the")
            print("    firewall: run allow-firewall.bat once as administrator.")
        elif sys.platform == "darwin":
            print("    If other devices cannot connect, allow incoming")
            print("    connections for Python (macOS asks automatically the")
            print("    first time it listens; on macOS 15+ also allow Local")
            print("    Network access for your terminal). Helper script:")
            print("    ./allow-firewall.command")
        else:
            print("    If other devices cannot connect, open the port in")
            print("    your firewall (ufw / firewalld) for the LAN subnet.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()
    if not POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [Login new account]:")
        print("      http://127.0.0.1:%s/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

def _serve_forever(args):
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        # Port stolen between the probe above and this bind, or held by
        # something that does not answer /health: report it in plain words
        # instead of dumping a raw socketserver traceback.
        print()
        print(f"  [ERROR] failed to listen on {args.host}:{args.port} - {exc}")
        print("          the port is reserved or held by another program;")
        print("          wb-proxy did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another port:")
        print("          %s" % port_owner_hint(args.port))
        print("          %s" % launcher_hint(args.port + 1))
        print()
        raise SystemExit(1)
    reset_retry_counters_file()
    # Only claim the address once the socket really exists, so a failed bind
    # never prints a "listening" line that contradicts the error below.
    # Report the state the request path actually enforces: the panel can turn
    # key checking on after startup, so reading API_KEY alone printed "off"
    # while every /v1 call was still being rejected with 401.
    if auth_required():
        _panel_keys = [k for k in configured_keys() if k.get("enabled")]
        _key_state = ("on (%d key(s) from the panel)" % len(_panel_keys)) if _panel_keys else "on (--api-key)"
    else:
        _key_state = "off"
    log(f"listening  : http://{args.host}:{args.port}/v1  (api key: {_key_state})")
    log(f"dashboard  : http://{args.host}:{args.port}/")
    # Keep the handler referenced for the process lifetime: SetConsoleCtrlHandler
    # stores a raw pointer, so a collected callback would crash on close.
    _ctrl_handler = install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        # Keep console output readable regardless of the active code page.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
