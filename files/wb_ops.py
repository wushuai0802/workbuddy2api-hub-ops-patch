"""Pure stream safeguards shared by the WorkBuddy protocol adapters."""
import json
import hashlib
import os
import re
import unicodedata
import threading
import time
import struct
from collections import Counter, deque

UPSTREAM_IDLE_TIMEOUT = 120
FIRST_FRAME_MAX_ATTEMPTS = 3
FIRST_FRAME_DEADLINE = 45
CONTINUATION_MAX_ATTEMPTS = 1
_FINISH_FALSY = (None, "", False)

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


TAG_START = r"<[^>]*DSML[^>]*"


DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>", re.DOTALL)


def finish_reason_of(data):
    """Return the first truthy finish_reason in a chat chunk, else None.

    `data` may be raw SSE bytes, a decoded string, or an already-parsed dict.
    Any parse failure returns None rather than raising: a malformed chunk is
    not an end-of-stream marker.
    """
    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", "replace")
        except Exception:
            return None
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("data:"):
            text = text[5:].strip()
        if not text or text == "[DONE]":
            return None
        try:
            data = json.loads(text)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    for choice in data.get("choices") or ():
        if not isinstance(choice, dict):
            continue
        value = choice.get("finish_reason")
        if value not in _FINISH_FALSY:
            return value
    return None


REASON_LOOP_MIN_UNIT = 6


REASON_LOOP_MAX_UNIT = 200


REASON_LOOP_LONG_AT = 48


REASON_LOOP_SHORT_REPS = 8


REASON_LOOP_LONG_REPS = 4


REASON_LOOP_SHORT_SPAN = 200


REASON_LOOP_LONG_SPAN = 600


REASON_LOOP_ENTROPY_AT = 12


REASON_LOOP_TAIL = 2400


REASON_CHURN_MIN_LINES = 24


REASON_CHURN_MAX_LINE = 60


_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_THINK_TAG_RE = re.compile(r"</?(?:thinking|think)>", re.IGNORECASE)
_STRUCTURED_RE = re.compile(r"(?:^\s*(?:[-*+#>] |\d+[.)] |```|~~~)|\b[A-Za-z_]\w*\(|[\d{}\[\]<>|`=;/\\])")
_STALL_RE = re.compile(
    r"^(?:(?:let me|i will|i'll|i’m going to|i'm going to)\b.*|"
    r"(?:go|write|writing|output|produce|producing|execute|executing|"
    r"now|ok|okay|alright|final|done|stop|enough)(?:\s+(?:it|now|the|tool|call|output|writing|write))*|"
    r"done (?:thinking|deliberating))$", re.IGNORECASE)
_ACTION_RE = re.compile(
    r"^(?:(?:ok|okay|alright)[, ]+)?"
    r"(?:(?:let me|i will|i'll|i need to|i'm going to)\s+)?"
    r"(go|write|writing|output|produce|producing|execute|executing)"
    r"(?:\s+(?:it|out|now|the|tool|call|output))*$")


def _phrase_key(phrase):
    action = _ACTION_RE.fullmatch(phrase)
    if action:
        verb = action.group(1)
        return {"writing": "write", "producing": "produce", "executing": "execute"}.get(verb, verb)
    return phrase


def _prose_line(line):
    line = _THINK_TAG_RE.sub("", _ANSI_RE.sub("", line))
    return line if not _STRUCTURED_RE.search(line) else ""


def detect_reasoning_loop(tail, conservative=False):
    """Return a short description when the tail looks like a degenerate loop.

    Two detectors, because the observed failure has two shapes:

      D1 exact unit repetition -- one unit repeated back to back.
      D2 short-line churn -- a small rotation of very short phrases repeated
         many times ("Let me write. / Go. / Final. / Writing. / OK. / ...").
         This is the shape the user actually hit; D1 alone misses it because
         the rotation is not byte-identical each cycle.

    The streaming guard excludes structured/fenced lines before calling this.
    Content uses stricter thresholds and requires stalling language. Detection
    is heuristic; None means no matching pattern in this window.
    """
    if not tail:
        return None
    n = len(tail)

    # --- D1: exact unit repetition -----------------------------------------
    # False-positive guards, in order of what they exclude:
    #   * fewer than two DISTINCT alphanumerics  -> "AAAA...", "0000...", "----"
    #   * a long unit with high character variety -> base64 blobs, source code
    #     (only applied to LONG units; a short high-variety unit repeated many
    #     times is exactly the degenerate pattern being hunted)
    #   * a repeated span shorter than the floor -> a handful of repeated
    #     markup rows, which is ordinary formatting, not a loop
    hi = min(REASON_LOOP_MAX_UNIT, n // 3)
    for ulen in range(REASON_LOOP_MIN_UNIT, hi + 1):
        unit = tail[n - ulen:]
        alnum = _ALNUM_RE.findall(unit)
        distinct = len(set(alnum))
        if len(alnum) < 2 or distinct < 2:
            continue
        is_long = ulen > REASON_LOOP_LONG_AT
        if is_long and distinct >= REASON_LOOP_ENTROPY_AT:
            continue
        need = REASON_LOOP_LONG_REPS if is_long else REASON_LOOP_SHORT_REPS
        span_need = REASON_LOOP_LONG_SPAN if is_long else REASON_LOOP_SHORT_SPAN
        if conservative:
            words = re.findall(r"[a-z]+", unit.casefold())
            stall = _STALL_RE.fullmatch(" ".join(words)) or (
                words and set(words) <= {"go", "write", "writing", "now", "ok", "okay", "final", "done"})
            if not stall:
                continue
            need, span_need = max(16, need), max(400, span_need)
        if n < ulen * need or ulen * need < span_need:
            continue
        reps = 0
        pos = n
        while pos >= ulen and tail[pos - ulen:pos] == unit:
            reps += 1
            pos -= ulen
        if reps >= need and reps * ulen >= span_need:
            return "repeated unit %d times (%d bytes): %r" % (reps, reps * ulen, unit[:40])

    # --- D2: normalized short-phrase churn --------------------------------
    # Sentence boundaries also catch an inline rotation. Keep long phrases in
    # the denominator: a little intervening prose must not defeat a loop, but
    # real progress must not be erased when measuring repetition coverage.
    normalized = unicodedata.normalize("NFKC", _ANSI_RE.sub("", tail)).casefold()
    phrases = [" ".join(p.strip(" \t\r(),:，：\"'").split())
               for p in re.split(r"[\n.!?。！？]+", normalized)]
    phrases = [p for p in phrases if p]
    minimum = 40 if conservative else REASON_CHURN_MIN_LINES
    for width in (minimum, 48):
        window = phrases[-width:]
        if len(window) < minimum:
            continue
        short = [p for p in window if len(p) <= REASON_CHURN_MAX_LINE
                 and len(p.split()) <= 12 and _prose_line(p) and _ALNUM_RE.search(p)]
        counts = Counter(_phrase_key(p) for p in short)
        repeated = sum(n for n in counts.values() if n >= 2)
        excess = len(short) - len(counts)
        size = len(window)
        stalling = sum(bool(_STALL_RE.fullmatch(p)) for p in short)
        if (len(short) >= size * .85 and repeated >= size * .75
                and excess >= size * (.65 if conservative else .50)
                and max(counts.values(), default=0) >= (6 if conservative else 4)
                and sum(map(len, short)) >= (240 if conservative else 160)
                and (not conservative or stalling >= size * .50)):
            return ("short-phrase churn: %d phrases, %d distinct, %d repeated"
                    % (size, len(counts), repeated))
    return None


_FAST_ACTION = re.compile(
    r"^(?:(?:ok|okay|alright)\s+)?"
    r"(?:(?:let me|i will|i'll|i need to)\s+)?(?:(?:now|just|actually)\s+)?"
    r"(go|run|write|writing|read|do|emit|output|produce|producing|execute|executing)"
    r"(?:\s+(?:it|that|this|now|the|actual|tool|call|command|script|answer|out|both))*$", re.I)


class FastStallGuard:
    """Small high-confidence intent window, never spanning code or real prose."""

    def __init__(self, conservative=False):
        self.phrases = deque(maxlen=48)
        self.conservative = conservative

    def reset(self):
        self.phrases.clear()

    def feed_line(self, line):
        line = unicodedata.normalize("NFKC", line).casefold()
        for phrase in re.split(r"[.!?。！？\n]+", line):
            phrase = " ".join(phrase.strip(" \t\r,:，：").split())
            if not phrase:
                continue
            action = _FAST_ACTION.fullmatch(phrase)
            if action:
                key = {"writing": "write", "producing": "produce", "executing": "execute"}.get(action[1], action[1])
            elif phrase in ("ok", "okay", "alright", "now", "final", "done"):
                key = "ok" if phrase in ("okay", "alright") else phrase
            elif phrase in ("ok here goes", "here goes", "okay here goes"):
                key = "go"
            elif re.fullmatch(r"let me (?:now )?make (?:the |a )?tool call", phrase):
                key = "write"
            else:
                if len(phrase) > 120 or len(phrase.split()) > 20:
                    self.reset()
                    continue
                key = None
            self.phrases.append((key, len(phrase)))
            while sum(n for _, n in self.phrases) > 2048:
                self.phrases.popleft()
            keys = [k for k, _ in self.phrases]
            floor = 24 if self.conservative else 16
            span_floor = 240 if self.conservative else 160
            for period in range(1, 9):
                count, end = 0, len(keys)
                unit = keys[-period:]
                if not all(unit):
                    continue
                while end >= period and keys[end-period:end] == unit:
                    count += 1
                    end -= period
                matched = count * period
                chars = sum(n for _, n in list(self.phrases)[-matched:])
                single_short = period == 1 and count >= (48 if self.conservative else 32) and chars >= 64
                if single_short or (count >= 3 and matched >= floor and chars >= span_floor):
                    return "fast-period:%d:%d" % (period, count)
            # Allow a few short connective phrases inside an irregular cycle.
            # Still require >=75% narrowly matched intents (>=84% in content),
            # repeated occurrences and a dominant action; long substantive
            # prose and structured lines reset the entire window.
            width = 32 if self.conservative else 24
            if len(keys) >= width:
                counts = Counter(k for k in keys[-width:] if k is not None)
                if (sum(counts.values()) >= width - (6 if not self.conservative else 5) and
                        sum(n for n in counts.values() if n >= 2) >= (16 if not self.conservative else 24) and
                        len(counts) <= 8 and max(counts.values(), default=0) >= 6 and
                        sum(n for _, n in list(self.phrases)[-width:]) >= span_floor):
                    return "fast-intent:%d" % width
        return None


class FailedReasoningCache:
    """Hash-only, process-local quarantine of exactly observed failed prefixes.

    A cache miss leaves reasoning untouched. No text is retained. Only a known
    failed prefix is removed; the successful attempt's reasoning is preserved.
    """

    def __init__(self, limit=128, ttl=3600, clock=time.monotonic):
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self.entries = deque(maxlen=limit)
        self.lock = threading.Lock()

    def remember(self, text):
        if not isinstance(text, str) or not 160 <= len(text) <= 65536:
            return
        with self.lock:
            self.entries.append((self.clock(), len(text), hashlib.sha256(text.encode("utf-8")).digest()))

    def clean(self, text):
        if not isinstance(text, str) or not text:
            return text
        with self.lock:
            now = self.clock()
            while self.entries and now - self.entries[0][0] >= self.ttl:
                self.entries.popleft()
            candidates = sorted({(n, digest) for _, n, digest in self.entries if n <= len(text)}, reverse=True)
        for n, digest in candidates:
            if hashlib.sha256(text[:n].encode("utf-8")).digest() == digest:
                return text[n:]
        return text


class CompactSSEBuffer:
    """Lossless bounded front/suffix coding against the first raw item.

    Never parse or reserialize SSE frames. Storage is a single base
    plus packed records; decoding yields one original bytes/str item at a time.
    Match scans are bounded, and no external dictionary or decompressor is used.
    The 64 KiB budget counts base + record headers + literal bytes, not expanded
    SSE. Object/allocator overhead and one-item encode/decode workspace are
    additional but bounded by the single-item, 512-record and 1024-item limits.
    One explicitly supplied LF/CRLF separator can attach to its preceding
    record; iteration still yields each original item and original type.
    """
    LIMIT = 65536
    MAX_ITEMS = 1024
    MAX_RECORDS = 512
    MATCH_LIMIT = 4096
    HEADER = struct.Struct("<BHHI")

    def __init__(self):
        self.clear()

    def clear(self):
        self.base = None
        self.records = bytearray()
        self.count = 0
        self.record_count = 0
        self.last_record = None
        self.last_attachable = False

    def __len__(self):
        return self.count

    @property
    def storage_bytes(self):
        return len(self.base or b"") + len(self.records)

    def prepare(self, raw, attach_separator=False):
        data = raw.encode("utf8") if isinstance(raw, str) else raw
        if not isinstance(data, bytes) or len(data) > self.LIMIT:
            return None
        flag = int(isinstance(raw, str))
        if (attach_separator and data in (b'\n', b'\r\n') and self.last_attachable and self.last_record is not None
                and not self.records[self.last_record] & 4):
            return (data, 32 | (8 if data == b'\r\n' else 0) | (16 if flag else 0), 0, 0, b'', 0)
        if self.base is None:
            return (data, flag | 2, 0, 0, b"", len(data) + self.HEADER.size)
        prefix = 0
        bound = min(len(data), len(self.base), self.MATCH_LIMIT)
        while prefix < bound and data[prefix] == self.base[prefix]:
            prefix += 1
        suffix = 0
        bound = min(len(data)-prefix, len(self.base)-prefix, self.MATCH_LIMIT)
        while suffix < bound and data[len(data)-1-suffix] == self.base[len(self.base)-1-suffix]:
            suffix += 1
        middle = data[prefix:len(data)-suffix if suffix else len(data)]
        return (data, flag, prefix, suffix, middle, self.HEADER.size + len(middle))

    def fits(self, prepared):
        return prepared is not None and self.storage_bytes + prepared[-1] <= self.LIMIT

    def has_room(self, prepared):
        return (prepared is not None and self.count < self.MAX_ITEMS
                and (bool(prepared[1] & 32) or self.record_count < self.MAX_RECORDS))

    def append_prepared(self, prepared):
        if not self.fits(prepared) or not self.has_room(prepared):
            raise BufferError("compact SSE storage limit")
        data, flag, prefix, suffix, middle, _ = prepared
        if flag & 32:
            self.records[self.last_record] |= 4 | (flag & 24)
            self.count += 1
            return
        if flag & 2:
            self.base = data
        self.last_record = len(self.records)
        self.last_attachable = bool(data.strip())
        self.records.extend(self.HEADER.pack(flag, prefix, suffix, len(middle)))
        self.records.extend(middle)
        self.count += 1
        self.record_count += 1

    def __iter__(self):
        pos = 0
        while pos < len(self.records):
            flag, prefix, suffix, size = self.HEADER.unpack_from(self.records, pos)
            pos += self.HEADER.size
            if flag & 2:
                data = self.base
            else:
                middle = bytes(self.records[pos:pos+size])
                data = self.base[:prefix] + middle + (self.base[-suffix:] if suffix else b"")
            pos += size
            yield data.decode("utf8") if flag & 1 else data
            if flag & 4:
                separator = b'\r\n' if flag & 8 else b'\n'
                yield separator.decode('ascii') if flag & 16 else separator


class StallObserver:
    """Observe mixed prose/planning only; never a gate for output or retries.

    Keep at most 96 nonempty lines / 8192 characters, with a 2048-character
    line buffer. Store only intent categories and lengths in the window.
    Unlike FastStallGuard, ordinary prose does not clear the window. Fenced
    code and quoted blocks do. A candidate is NOT a confirmed generation loop.
    """
    def __init__(self):
        self.window = deque(maxlen=96)
        self.line = ""
        self.line_chars = 0
        self.total_chars = 0
        self.fence = None
        self.reported = False

    def _record(self):
        line = self.line
        length = self.line_chars
        self.line, self.line_chars = "", 0
        plain = _ANSI_RE.sub("", line).strip()
        marker = re.match(r"(`{3,}|~{3,})", plain)
        if marker:
            token = marker.group(1)
            if self.fence is None:
                self.fence = token
            elif token[0] == self.fence[0] and len(token) >= len(self.fence) and not plain[len(token):].strip():
                self.fence = None
            self.window.clear()
            return None
        if self.fence or plain.startswith(">"):
            self.window.clear()
            return None
        if not plain and length <= 2048:
            return None
        key = None
        if length <= 2048:
            phrase = unicodedata.normalize("NFKC", _prose_line(line)).casefold()
            phrase = " ".join(phrase.strip(" \t\r.,!?:;，。！？：；").split())
            action = _FAST_ACTION.fullmatch(phrase) if len(phrase) <= 60 else None
            if action:
                key = {"writing": "write", "producing": "produce", "executing": "execute"}.get(action[1], action[1])
        self.window.append((key, length))
        while sum(n for _, n in self.window) > 8192:
            self.window.popleft()
        counts = Counter(k for k, _ in self.window if k is not None)
        intents = sum(counts.values())
        repeated = sum(n for n in counts.values() if n >= 2)
        span = sum(n for _, n in self.window)
        if (span >= 256 and repeated >= 12 and intents * 4 >= len(self.window)
                and max(counts.values(), default=0) >= 6):
            self.reported = True
            result = {"window_chars": span, "window_lines": len(self.window),
                      "intent_lines": intents, "dominant_count": max(counts.values()),
                      "distinct_intents": len(counts), "received_chars": self.total_chars}
            self.window.clear()
            return result
        return None

    def feed(self, text, final=False):
        if self.reported:
            return None
        for ch in text:
            self.total_chars += 1
            if ch == "\n":
                result = self._record()
                if result:
                    return result
            else:
                self.line_chars = min(8193, self.line_chars + 1)
                if len(self.line) < 2048:
                    self.line += ch
        if final and self.line_chars:
            return self._record()
        return None


class ReasoningLoopGuard:
    """Bounded tail tracker that latches once a repetition loop is seen."""

    def __init__(self, conservative=False):
        self.tail = ""
        self.hit = False
        self.detail = ""
        self.conservative = conservative
        self.line = ""
        self.line_blocked = False
        self.fence = None
        self.scanned = 0
        self.fast = FastStallGuard(conservative)
        self.total_chars = 0
        self.rule = ""

    def _check(self, candidate):
        detail = detect_reasoning_loop(candidate[-REASON_LOOP_TAIL:], self.conservative)
        if detail:
            self.hit, self.detail = True, detail
            self.rule = "exact" if detail.startswith("repeated unit") else "churn"
        return self.hit

    def _line_state(self, complete=False):
        stripped = self.line.lstrip()
        match = re.match(r"(`{3,}|~{3,})", stripped)
        marker = match.group(1) if match else None
        safe = _prose_line(self.line)
        # Latch structured lines at deterministic checkpoints so a long code
        # prefix cannot fall out of the bounded window and become prose.
        if self.line.strip() and not safe:
            self.line_blocked = True
        if self.line_blocked:
            safe = ""
        if complete:
            if marker and self.fence is None:
                self.fence = marker
                self.tail = ""
                self.fast.reset()
            elif (marker and self.fence and marker[0] == self.fence[0]
                  and len(marker) >= len(self.fence) and not stripped[len(marker):].strip()):
                self.fence = None
                self.tail = ""
                self.fast.reset()
            elif self.fence is None:
                self.tail = (self.tail + safe + "\n")[-REASON_LOOP_TAIL:] if safe or not self.line.strip() else ""
                if safe:
                    hit = self.fast.feed_line(safe)
                    if hit:
                        self.hit, self.detail, self.rule = True, hit, hit.split(":")[0]
                elif self.line.strip():
                    self.fast.reset()
            self.line = ""
            self.line_blocked = False
        if self.hit or self.fence or marker:
            return self.hit
        if safe or complete:
            if not complete:
                end = max((safe.rfind(mark) for mark in ".!?。！？"), default=-1)
                if end >= 0:
                    preview = FastStallGuard(self.conservative)
                    preview.phrases = self.fast.phrases.copy()
                    hit = preview.feed_line(safe[:end+1])
                    if hit:
                        self.hit, self.detail, self.rule = True, hit, hit.split(":")[0]
            if not self.hit:
                self._check(self.tail + ("" if complete else safe))
        return self.hit

    def feed(self, text):
        """Schedule checks at absolute positions, independent of SSE partitions."""
        if os.environ.get("WB_OPS_LOOP_GUARD", "1").lower() in ("0", "false", "off"):
            return False
        if self.hit or not text:
            return self.hit
        for char in text:
            self.total_chars += 1
            self.scanned += 1
            if char == "\n":
                if self._line_state(complete=True):
                    return True
            else:
                self.line = (self.line + char)[-REASON_LOOP_TAIL:]
            if self.scanned >= 64:
                self.scanned = 0
                if self._line_state():
                    return True
        return self.hit

    def describe(self):
        if self.rule.startswith("fast"):
            return "short-intent repetition churn (" + self.detail + ")"
        return self.detail or "reasoning repetition loop"


class LeakedThinkingGuard:
    """Detect-only leading-tag view. Never changes content or permits replay."""
    def __init__(self):
        self.state = "probe"
        self.probe = ""
        self.pending = ""
        self.tag = None
        self.detected = False
        self.guard = ReasoningLoopGuard()

    def feed(self, text):
        for ch in text:
            if self.state == "done":
                break
            if self.state == "probe":
                self.probe += ch
                lead = self.probe.lstrip().lower()
                tags = ("<thinking>", "<think>")
                if lead in tags:
                    self.state, self.detected = "active", True
                    self.tag = "</thinking>" if lead == tags[0] else "</think>"
                elif len(self.probe) > 64 or (lead and not any(t.startswith(lead) for t in tags)):
                    self.state = "done"
                continue
            self.pending += ch
            if self.pending.lower().endswith(self.tag) and self.guard.fence is None:
                self.guard.feed(self.pending[:-len(self.tag)])
                self.pending, self.state = "", "done"
                return self.guard.hit
            if len(self.pending) > len(self.tag):
                part, self.pending = self.pending[0], self.pending[1:]
                if self.guard.feed(part):
                    return True
        return self.guard.hit

    def finish(self):
        if self.state == "active":
            self.guard.feed(self.pending)
            self.pending = ""
        return self.guard.hit


class LoopRetryCircuit:
    """Bounded TTL budget for identical request contexts; retains hashes only."""
    def __init__(self, ttl=120, limit=256, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.entries = {}
        self.lock = threading.Lock()

    def allow(self, key):
        if not key:
            return True
        with self.lock:
            now = self.clock()
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < self.ttl}
            start, count = self.entries.get(key, (now, 0))
            if key not in self.entries and len(self.entries) >= self.limit:
                self.entries.pop(next(iter(self.entries)))
            self.entries[key] = (start, min(count + 1, 2))
            return count == 0

    def success(self, key):
        with self.lock:
            self.entries.pop(key, None)


REASON_DSML_MAX_HOLD = 8192


_DSML_BAR = "\uff5c"


def quarantine_dsml_text(text):
    """Split trailing unparsable DSML text into (keep, dropped).

    Returns (keep, dropped). `keep` is the text before the first DSML marker;
    `dropped` is the marker and everything after it, i.e. an envelope that was
    cut off before it closed. A string with no DSML marker is returned whole,
    so ordinary prose -- including text that merely contains '<' -- is never
    touched.
    """
    if not text or "DSML" not in text:
        return text, ""
    m = _DSML_OPEN_CALLS_RE.search(text)
    if m:
        return text[:m.start()], text[m.start():]
    m = _DSML_STRAY_TAG_RE.search(text)
    if m:
        return text[:m.start()], text[m.start():]
    m = re.search("DSML", text)
    if m:
        return text[:m.start()], text[m.start():]
    return text, ""


_DSML_OPEN_CALLS_RE = re.compile(TAG_START + r"calls\s*>", re.IGNORECASE)


_DSML_STRAY_TAG_RE = re.compile(
    TAG_START + r"(?:calls|invoke|parameter)\b[^>]*>?", re.IGNORECASE
)


def strip_dsml_from_text(text):
    """Remove DSML envelopes and stray DSML markers from a complete string.

    Only ever deletes; never returns a tool call.
    """
    if not text or "DSML" not in text:
        return text
    out = text
    while True:
        m = DSML_CALLS_RE.search(out)
        if not m:
            break
        out = out[:m.start()] + out[m.end():]
    if "DSML" in out:
        # An envelope that never closed: the opening marker and everything
        # after it belong to the broken block, so drop the remainder.
        m = _DSML_OPEN_CALLS_RE.search(out)
        if m:
            out = out[:m.start()]
        else:
            out = _DSML_STRAY_TAG_RE.sub("", out)
    return out


def is_dsml_fragment(tail):
    """True when a held-back tail can only be a truncated DSML marker.

    Used at stream end, where the choice is to emit or drop the remainder.
    Deliberately narrow: plain text fragments such as "<y" or "<div" must
    survive, so a fragment only qualifies when it carries the fullwidth bars
    the DSML marker is written with, or spells out the DSML prefix itself.
    """
    if not tail.startswith("<") or ">" in tail:
        return False
    body = tail[1:]
    if _DSML_BAR in body:
        return True
    compact = body.replace("|", "").strip().upper()
    if not compact:
        return True
    return "DSML".startswith(compact[:4]) or compact.startswith("DSML")


def dsml_safe_prefix(buf, final=False):
    """Split a reasoning buffer into (emit, keep).

    emit is DSML-free text safe to forward now. keep is the tail withheld
    because it may still grow into a DSML tag: a tag always starts with '<',
    so anything from a possibly-tag-forming '<' onward is held until it can be
    classified or the stream ends.

    Mirrors the hold-back rules already proven on the content channel, minus
    the tool-call recovery, which must not happen for reasoning.
    """
    if not buf:
        return "", ""
    emit_parts = []
    while buf:
        idx = buf.find("<")
        if idx == -1:
            emit_parts.append(buf)
            buf = ""
            break
        m = DSML_CALLS_RE.search(buf)
        if m and m.start() == idx:
            emit_parts.append(buf[:idx])
            buf = buf[m.end():]
            continue
        cand = buf[idx:idx + 30]
        is_cand = ("DSML" in cand) or (
            len(cand) < 10 and not any(c in cand for c in (" ", "\t", "\n", ">"))
        )
        if is_cand:
            emit_parts.append(buf[:idx])
            buf = buf[idx:]
            break
        next_lt = buf[idx + 1:].find("<")
        if next_lt != -1:
            flush_len = idx + 1 + next_lt
            emit_parts.append(buf[:flush_len])
            buf = buf[flush_len:]
        else:
            emit_parts.append(buf)
            buf = ""
            break
    if final and buf:
        if is_dsml_fragment(buf):
            buf = ""
        else:
            emit_parts.append(strip_dsml_from_text(buf))
            buf = ""
    return "".join(emit_parts), buf


def _scrub_dsml_reasoning_messages(messages):
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for field in ("reasoning_content", "reasoning"):
            text = message.get(field)
            if isinstance(text, str):
                message[field] = dsml_safe_prefix(text, final=True)[0]
    return messages


def reasoning_only_turn(text_parts, tool_calls, finish, failed_reason=None):
    if failed_reason or finish != "stop" or tool_calls:
        return False
    text = "".join(text_parts) if isinstance(text_parts, list) else str(text_parts or "")
    return not text.strip()


class ReasoningAnswerRecovery:
    """Bounded, terminal-only repair of a DeepSeek answer in reasoning_content.

    Never infer an answer from arbitrary reasoning. Require one unquoted close
    tag on its own line or after a small, observed answer-transition phrase.
    Already streamed reasoning is not retracted; the recovered content is sent
    once, before the original normal finish. Structured calls are never made.
    """

    MAX_CHARS = 65536
    _CLOSE = re.compile(r"</(?:thinking|think)>", re.IGNORECASE)
    _OPEN = re.compile(r"<(?:thinking|think)>", re.IGNORECASE)
    _TRANSITION = re.compile(
        r"(?:go|now write the answer|let me (?:write|report|answer|summarize))\s*[.!]?",
        re.IGNORECASE)
    _EXAMPLE = re.compile(r"example|literal|quot(?:e|ed)|tag|marker|示例|标签|字符串", re.IGNORECASE)

    def __init__(self, model):
        self.blocked = (not str(model or "").lower().startswith("deepseek-") or
                        os.environ.get("WB_OPS_REASONING_RECOVERY", "1").lower() in ("0", "false", "off"))
        self.parts = []
        self.size = 0

    def block(self):
        self.blocked = True
        self.parts.clear()

    def feed(self, text):
        if self.blocked or not text:
            return
        self.size += len(text)
        if self.size > self.MAX_CHARS:
            self.block()
        else:
            self.parts.append(text)

    def answer(self):
        if self.blocked:
            return ""
        text = _ANSI_RE.sub("", "".join(self.parts))
        # DSML is a separate tool protocol. Do not turn it into a text answer.
        if re.search(TAG_START, text) or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            return ""
        fence, previous, offset = None, "", 0
        boundaries, openings = [], []
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            # Unfenced multiline string examples are ambiguous, too.
            if stripped.startswith(('"""', "'''")):
                return ""
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if marker:
                token = marker.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end():].strip():
                    fence = None
            elif fence is None and not line.startswith(("    ", "\t")) and not stripped.startswith(">"):
                if self._OPEN.fullmatch(stripped):
                    openings.append(offset)
                for match in self._CLOSE.finditer(line):
                    prefix = line[:match.start()].strip()
                    if (not prefix or self._TRANSITION.fullmatch(prefix)) and not self._EXAMPLE.search(previous):
                        boundaries.append(offset + match.end())
            if stripped:
                previous = stripped
            offset += len(line)
        if len(boundaries) != 1 or len(openings) > 1 or any(p >= boundaries[0] for p in openings):
            return ""
        answer = text[boundaries[0]:].strip()
        # A dangling boundary or another small stalling phrase is not an answer.
        if sum(c.isalnum() for c in answer) < 12 or _STALL_RE.fullmatch(answer.rstrip(".!")):
            return ""
        return answer


class ReasoningScrubber:
    """Bounded reasoning look-ahead, including oversized unclosed envelopes."""

    def __init__(self):
        self.hold = ""
        self.discard_end = None
        self.discard_tail = ""

    def feed(self, text="", final=False):
        if self.discard_end:
            combined = self.discard_tail + text
            match = self.discard_end.search(combined)
            if not match:
                self.discard_tail = "" if final else combined[-512:]
                if final:
                    self.discard_end = None
                return ""
            text = combined[match.end():]
            self.discard_end = None
            self.discard_tail = ""
        safe, self.hold = dsml_safe_prefix(self.hold + text, final=final)
        if len(self.hold) > REASON_DSML_MAX_HOLD:
            match = re.search(r"DSML[^>]*?\b(calls|invoke|parameter)\b", self.hold)
            tag = match.group(1) if match else "calls"
            self.discard_end = re.compile(r"</[^>]*DSML[^>]*" + tag + r"\s*>")
            self.discard_tail = self.hold[-512:]
            self.hold = ""
        return safe
