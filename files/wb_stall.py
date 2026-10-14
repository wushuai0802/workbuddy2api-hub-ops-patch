"""Bounded, narrowly matched mixed planning-stall evidence for DeepSeek.

The caller selects the model. This module never changes tool calls, content,
client history, or an SSE frame. A live guard reports numeric evidence only.
The history helper removes repeated *Hmm* sentences only inside windows
that independently satisfy the same conservative evidence rules.
"""

from collections import Counter, deque
from dataclasses import dataclass
import hashlib
import os
import re
import unicodedata


MAX_SENTENCES = 128
MAX_WINDOW_CHARS = 8192
MAX_LINE_CHARS = 2048
CHECKPOINT_CHARS = 64
MAX_QUOTE_CHARS = 65536

_SUBSTANTIVE, _ACTION, _HMM, _NEUTRAL = range(4)
_BOUNDARY = re.compile(r"[.!?。！？]+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_STRUCTURED = re.compile(
    r"(?:^\s*(?:[-*+#>] |\d+[.)] )|\b[A-Za-z_]\w*\(|[\d{}\[\]<>|`=;/\\])"
)
_LINE_STRUCTURED = re.compile(r"^\s*(?:[-*+#>] |\d+[.)] )")
_EXAMPLE = re.compile(
    r"\b(?:example|literal|quoted?|quotation|verbatim|test\s+(?:text|case)|"
    r"repeat\s+(?:the\s+)?(?:phrase|sentence))\b|示例|字面|字符串|引用", re.I
)
_ACTION_SENTENCE = re.compile(
    r"^(?:(?:ok|okay|alright)\s+)?"
    r"(?:(?:let me|i will|i'll|i need to)\s+)?"
    r"(?:(?:now|just|actually)\s+)?"
    r"(?:go|run|write|writing|read|do|emit|output|produce|producing|execute|executing|call)"
    r"(?:\s+(?:it|that|this|now|the|actual|tool|call|command|script|answer|out|both))*$",
    re.I,
)
_HMM_WORDS = frozenset(("hm", "hmm", "hmmm"))
_NEUTRAL_WORDS = frozenset((
    "ok", "okay", "alright", "now", "final", "done",
))


def _enabled():
    return os.environ.get("WB_OPS_LOOP_GUARD", "1").lower() not in ("0", "false", "off")


@dataclass(frozen=True)
class _Record:
    kind: int
    digest: bytes
    chars: int
    start: int
    end: int


def _record(raw, start, end, protected=False):
    stripped = raw.strip()
    if not stripped:
        return None
    # Offsets exclude whitespace. Removing a neutral sentence never removes
    # whitespace that separates two substantive sentences.
    start += len(raw) - len(raw.lstrip())
    phrase = " ".join(unicodedata.normalize("NFKC", stripped).casefold()
                      .strip(" \t\r,:，：").split())
    if protected or _STRUCTURED.search(stripped):
        kind = _SUBSTANTIVE
    elif phrase in _HMM_WORDS:
        kind = _HMM
    elif phrase in _NEUTRAL_WORDS:
        kind = _NEUTRAL
    elif len(phrase) <= 60 and len(phrase.split()) <= 12 and _ACTION_SENTENCE.fullmatch(phrase):
        kind = _ACTION
    else:
        kind = _SUBSTANTIVE
    return _Record(kind, hashlib.sha256(phrase.encode("utf-8")).digest(),
                   len(stripped), start, end)


def _records(line, offset, final=True, protected=False):
    begin = 0
    for match in _BOUNDARY.finditer(line):
        item = _record(line[begin:match.start()], offset + begin, offset + match.end(), protected)
        if item is not None:
            yield item
        begin = match.end()
    if final and line[begin:].strip():
        item = _record(line[begin:], offset + begin, offset + len(line.rstrip()), protected)
        if item is not None:
            yield item


class _Safety:
    """Fences/quotes span lines; raw text does not."""

    def __init__(self):
        self.fence = None
        self.quote = None
        self.quote_chars = 0

    def copy(self):
        result = _Safety()
        result.fence, result.quote = self.fence, self.quote
        result.quote_chars = self.quote_chars
        return result

    def protected(self, line, structured=False):
        marker = _FENCE.match(line)
        if marker and self.quote is None:
            token = marker.group(1)
            if self.fence is None:
                self.fence = token
            elif (token[0] == self.fence[0] and len(token) >= len(self.fence)
                  and not line[marker.end():].strip()):
                self.fence = None
            return True
        if self.fence is not None:
            return True
        if (structured and self.quote is None and not line.lstrip().startswith(('"', "'", "“", "‘"))
                and _STRUCTURED.search(line)):
            # Quotes embedded in code/data are not prose quotation boundaries.
            return True
        quoted = self.quote is not None
        escaped = False
        for index, char in enumerate(line):
            if self.quote is not None:
                self.quote_chars += 1
                if self.quote_chars > MAX_QUOTE_CHARS:
                    # An unmatched quote cannot exempt the rest of a generation.
                    self.quote, self.quote_chars = None, 0
                    quoted = False
            if escaped:
                escaped = False
                continue
            if char == '\\':
                escaped = True
                continue
            if char not in ('"', "'", "“", "”", "‘", "’"):
                continue
            before = line[index - 1] if index else ""
            after = line[index + 1] if index + 1 < len(line) else ""
            # Apostrophes in I'll/it's/I'm are not quotation boundaries.
            if char in ("'", "’") and before.isalpha() and after.isalpha():
                continue
            # A possessive plural's trailing apostrophe is not an opening
            # multiline quotation. Unpaired smart closing quotes do not open
            # a quotation either. Keep those lines protected if ambiguous.
            if self.quote is None:
                if char in ("'", "’") and before.isalnum():
                    continue
                if char == "'" and before and not (before.isspace() or before in "([{:,"):
                    # Possessive suffixes can follow a quoted/code identifier
                    # rather than a letter. Only a plausible opening context
                    # starts an ASCII single-quoted multiline quotation.
                    continue
                if char in ("”", "’"):
                    quoted = True
                    continue
                if char == '"' and before.isdigit():
                    continue
            quoted = True
            if self.quote is None:
                self.quote = {"“": "”", "‘": "’"}.get(char, char)
                self.quote_chars = 0
            elif char == self.quote:
                self.quote = None
                self.quote_chars = 0
        return (quoted or line.startswith(("    ", "\t")) or
                bool(_LINE_STRUCTURED.search(line)) or bool(_EXAMPLE.search(line)))


class _Window:
    def __init__(self):
        self.records = deque()
        self.chars = 0

    def reset(self):
        self.records.clear()
        self.chars = 0

    def copy(self):
        result = _Window()
        result.records = self.records.copy()
        result.chars = self.chars
        return result

    def add(self, item):
        self.records.append(item)
        self.chars += item.chars
        while len(self.records) > MAX_SENTENCES or self.chars > MAX_WINDOW_CHARS:
            self.chars -= self.records.popleft().chars

    def evidence(self, received_chars):
        records = self.records
        if not records:
            return None
        actions = Counter(item.digest for item in records if item.kind == _ACTION)
        hmm = Counter(item.digest for item in records if item.kind == _HMM)
        action_count = sum(actions.values())
        action_dominant = max(actions.values(), default=0)
        hmm_dominant = max(hmm.values(), default=0)
        neutral_count = sum(item.kind in (_HMM, _NEUTRAL) for item in records)
        other_neutral_count = sum(item.kind == _NEUTRAL for item in records)
        substantive = [item for item in records if item.kind == _SUBSTANTIVE]
        substantive_chars = sum(item.chars for item in substantive)
        stall_count = action_count + neutral_count
        size = len(records)
        # Short neutral sentences contribute few characters, so a character
        # fraction alone hides even hundreds of interleaved "Hmm" sentences.
        # Keep an absolute substantive budget and an average sentence budget.
        # A: repeated *identical complete* action, with many identical Hmm's.
        path_a = (action_dominant >= 8 and hmm_dominant >= 24 and
                  stall_count * 5 >= size * 3 and substantive_chars <= 3072 and
                  substantive_chars <= max(1, len(substantive)) * 64)
        # B: extreme density of one narrowly defined hesitation word, plus
        # at least two complete action/known neutral marker sentences. The
        # 64-repeat requirement is never generalized to OK or other words.
        path_b = (hmm_dominant >= 64 and action_count + other_neutral_count >= 2 and
                  stall_count * 20 >= size * 11 and substantive_chars <= 3072 and
                  substantive_chars <= max(1, len(substantive)) * 64)
        if not (path_a or path_b):
            return None
        return {
            "path": 1 if path_a else 2,
            "window_chars": self.chars,
            "window_lines": size,
            "intent_lines": action_count,
            "dominant_count": action_dominant,
            "distinct_intents": len(actions),
            "neutral_lines": neutral_count,
            "neutral_dominant_count": hmm_dominant,
            "substantive_lines": len(substantive),
            "substantive_chars": substantive_chars,
            "received_chars": received_chars,
        }

    def repeated_neutral_spans(self):
        # Decision words such as Done/Wait/OK are never removed, even when
        # useful as additional evidence. Only repeated hesitation is cleaned.
        counts = Counter(item.digest for item in self.records if item.kind == _HMM)
        return [(item.start, item.end) for item in self.records
                if item.kind == _HMM and counts[item.digest] >= 2]


class MixedStallGuard:
    """Streaming latch; fixed checkpoints make SSE partitioning irrelevant.

    State retains at most 128 category/hash/size records and one 2048-character
    line. The returned dictionary contains integers only, never text or IDs.
    Use one instance per upstream attempt, only for the intended model.
    """

    def __init__(self):
        self.window = _Window()
        self.safety = _Safety()
        self.line = ""
        self.line_chars = 0
        self.line_start = 0
        self.total_chars = 0
        self.hit = False
        self.metrics = None

    def _latch(self, metrics):
        if metrics:
            self.hit, self.metrics = True, metrics
        return dict(self.metrics) if self.metrics else None

    def _line(self, final):
        if self.line_chars > MAX_LINE_CHARS:
            if final:
                self.safety.protected(self.line)
                self.window.reset()
            return None
        safety = self.safety if final else self.safety.copy()
        window = self.window if final else self.window.copy()
        protected = safety.protected(self.line)
        # Protected text consumes the substantive budget and remains intact.
        # A quoted identifier or a numbered observation must not erase all
        # evidence of hundreds of surrounding unquoted hesitation sentences.
        for item in _records(self.line, self.line_start, final=final, protected=protected):
            window.add(item)
            metrics = window.evidence(self.total_chars)
            if metrics:
                return self._latch(metrics)
        return None

    def feed(self, text="", final=False):
        if not _enabled():
            return None
        if self.hit:
            return dict(self.metrics)
        for char in text or "":
            self.total_chars += 1
            if char == "\n":
                result = self._line(final=True)
                self.line, self.line_chars = "", 0
                self.line_start = self.total_chars
                if result:
                    return result
            else:
                self.line_chars += 1
                if len(self.line) < MAX_LINE_CHARS:
                    self.line += char
                elif self.line_chars == MAX_LINE_CHARS + 1:
                    self.window.reset()
            if self.total_chars % CHECKPOINT_CHARS == 0:
                result = self._line(final=False)
                if result:
                    return result
        if final and self.line_chars:
            result = self._line(final=True)
            self.line, self.line_chars = "", 0
            self.line_start = self.total_chars
            return result
        return None


def clean_stalled_reasoning(text):
    """Remove complete repeated Hmm sentences from confirmed windows.

    All substantive text, action sentences, protected lines, and separating
    whitespace remain byte-for-byte. Window/mark state is bounded; committed
    output is emitted as soon as it is too old to join a future window.
    The caller handles the upstream's nonempty reasoning-field placeholder.
    """
    if not isinstance(text, str) or not text or not _enabled():
        return text
    window, safety = _Window(), _Safety()
    pieces, marks, cursor = [], set(), 0

    def commit(before):
        nonlocal cursor
        for start, end in sorted(span for span in marks if span[1] <= before):
            if start >= cursor:
                pieces.append(text[cursor:start])
                cursor = end
            marks.discard((start, end))
        if before > cursor:
            pieces.append(text[cursor:before])
            cursor = before

    for match in re.finditer(r"[^\n]*(?:\n|$)", text):
        if match.start() == match.end():
            continue
        line = match.group().rstrip("\n")
        protected = safety.protected(line[:MAX_LINE_CHARS])
        if len(line) > MAX_LINE_CHARS:
            window.reset()
            commit(match.end())
            continue
        for item in _records(line, match.start(), final=True, protected=protected):
            window.add(item)
            if window.evidence(item.end):
                marks.update(window.repeated_neutral_spans())
            if window.records:
                commit(window.records[0].start)
    commit(len(text))
    return "".join(pieces)
