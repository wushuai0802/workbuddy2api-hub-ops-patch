"""Detect-only leading-thinking mixed stalls, without gateway/account imports."""

import os
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("WB_TEST_SOURCE", str(ROOT / "files")))
from wb_ops import LeakedThinkingGuard, ReasoningLoopGuard, REASON_LOOP_TAIL
from wb_stall import MAX_LINE_CHARS, MAX_SENTENCES, MAX_WINDOW_CHARS, MixedStallGuard


# This fixture independently misses the old guard and satisfies the narrowly
# matched mixed rule. Changing its channel must not remove that protection.
MIXED = ("Hmm.\nHmm.\nLet me go.\n"
         "The candidate needs another comparison before a concrete result.\n") * 80
LEGACY = "Let me run the actual tool call.\n" * 30
FINAL_MIXED = "".join("Hmm. Hmm.\nEvidence at position %d.\n" % i for i in range(32)) + "Let me go.\nI will run"


class LeadingMixedTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"WB_OPS_LOOP_GUARD": "1"}))

    @staticmethod
    def run_guard(text, widths, *, mixed=True, finish=True):
        guard = LeakedThinkingGuard(mixed_stall=MixedStallGuard() if mixed else None)
        offset = 0
        for width in widths:
            if offset >= len(text) or guard.feed(text[offset:offset + width]):
                break
            offset += width
        if finish:
            guard.finish()
        return guard

    @staticmethod
    def result(guard):
        return guard.hit, guard.rule, guard.total_chars, guard.mixed_metrics

    def test_confirmed_leading_view_must_detect_same_mixed_fixture(self):
        self.assertFalse(ReasoningLoopGuard().feed(MIXED))
        expected = MixedStallGuard().feed(MIXED, final=True)
        self.assertIsNotNone(expected)
        # Assigning an optional standalone detector also reproduces the old
        # parser's failure to feed it, without requiring the new constructor.
        guard = LeakedThinkingGuard()
        guard.mixed_stall = MixedStallGuard()
        self.assertTrue(guard.feed("<thinking>" + MIXED))
        self.assertTrue(guard.detected)
        self.assertFalse(guard.guard.hit)
        self.assertEqual(guard.rule, "mixed-stall")
        self.assertEqual(guard.mixed_metrics, expected)

    def test_hit_and_exact_position_are_partition_independent(self):
        for opening in ("<thinking>", "  <THINKING>", "\n<THINK>"):
            text = opening + MIXED
            expected = self.result(self.run_guard(text, [1] * len(text)))
            self.assertTrue(expected[0])
            self.assertEqual(expected[1], "mixed-stall")
            for width in (2, 3, 7, 17, 64, 128, 1024, len(text)):
                with self.subTest(opening=opening, width=width):
                    actual = self.run_guard(text, [width] * (len(text) // width + 1))
                    self.assertEqual(self.result(actual), expected)
            randomizer = random.Random(421)
            actual = self.run_guard(text, [randomizer.randint(1, 137) for _ in text])
            self.assertEqual(self.result(actual), expected)

    def test_default_has_no_mixed_detector_and_legacy_still_works(self):
        guard = LeakedThinkingGuard()
        self.assertFalse(guard.feed("<thinking>" + MIXED))
        self.assertFalse(guard.finish())
        self.assertIsNone(guard.mixed_metrics)
        guard = LeakedThinkingGuard()
        self.assertTrue(guard.feed("<think>" + LEGACY))
        self.assertTrue(guard.guard.hit)
        self.assertEqual(guard.rule, guard.guard.rule)
        self.assertEqual(guard.total_chars, guard.guard.total_chars)

    def test_latched_hit_keeps_original_diagnostics_on_later_calls(self):
        guard = LeakedThinkingGuard(mixed_stall=MixedStallGuard())
        self.assertTrue(guard.feed("<thinking>" + MIXED))
        result = self.result(guard)
        old_guard_chars = guard.guard.total_chars
        self.assertTrue(guard.feed(LEGACY))
        self.assertTrue(guard.finish())
        self.assertTrue(guard.finish())
        self.assertEqual(self.result(guard), result)
        self.assertEqual(guard.guard.total_chars, old_guard_chars)

    def test_arbitrary_content_and_quoted_or_code_tags_never_activate(self):
        for prefix in ("", "Ordinary answer.\n<thinking>", "Example: <thinking>",
                       '"<thinking>', "'<think>", "“<thinking>",
                       "```text\n<thinking>", "> <thinking>", "<thinking attr='x'>",
                       " " * 65 + "<thinking>"):
            with self.subTest(prefix=prefix):
                guard = self.run_guard(prefix + MIXED, [3] * (len(prefix + MIXED) // 3 + 1))
                self.assertFalse(guard.detected)
                self.assertFalse(guard.hit)
                self.assertEqual(guard.mixed_stall.total_chars, 0)

    def test_complete_boundary_stops_special_view_in_all_partitions(self):
        for opening, closing in (("<thinking>", "</thinking>"), ("<THINK>", "</ThInK>")):
            text = opening + "A distinct useful result.\n" + closing + MIXED
            for width in (1, 2, 7, 17, 64, len(text)):
                with self.subTest(opening=opening, width=width):
                    guard = self.run_guard(text, [width] * (len(text) // width + 1))
                    self.assertTrue(guard.detected)
                    self.assertEqual(guard.state, "done")
                    self.assertFalse(guard.hit)
                    self.assertIsNone(guard.mixed_metrics)
                    self.assertEqual(guard.mixed_stall.total_chars, len("A distinct useful result.\n"))

    def test_closing_marker_at_every_split_position(self):
        prefix, marker = "<thinking>A distinct useful result.\n", "</thinking>"
        for split in range(len(marker) + 1):
            with self.subTest(split=split):
                guard = LeakedThinkingGuard(mixed_stall=MixedStallGuard())
                self.assertFalse(guard.feed(prefix + marker[:split]))
                self.assertFalse(guard.feed(marker[split:] + MIXED))
                self.assertFalse(guard.finish())
                self.assertEqual(guard.state, "done")

    def test_code_fence_newline_before_boundary_does_not_expose_later_answer(self):
        text = "<thinking>```text\nA literal code example.\n```\n</thinking>" + MIXED
        for width in (1, 2, 7, 17, len(text)):
            with self.subTest(width=width):
                guard = self.run_guard(text, [width] * (len(text) // width + 1))
                self.assertFalse(guard.hit)
                self.assertEqual(guard.state, "done")
                self.assertIsNone(guard.mixed_metrics)

    def test_protected_thinking_body_remains_excluded(self):
        cases = (
            "```text\n" + MIXED + "</thinking>\n```\n",
            "~~~\n" + MIXED + "~~~\n",
            '"' + MIXED + '"\n',
            "“" + MIXED + "”\n",
            "\n".join("> " + line for line in MIXED.splitlines()) + "\n",
            "\n".join("    " + line for line in MIXED.splitlines()) + "\n",
            "\n".join("`" + line + "`" for line in MIXED.splitlines()) + "\n",
            "\n".join("Example: " + line for line in MIXED.splitlines()) + "\n",
        )
        for body in cases:
            for width in (1, 17, len(body)):
                with self.subTest(body_prefix=body[:15], width=width):
                    text = "<thinking>" + body + "</thinking>"
                    guard = self.run_guard(text, [width] * (len(text) // width + 1))
                    self.assertFalse(guard.hit)
                    self.assertEqual(guard.state, "done")

    def test_final_unpunctuated_sentence_can_complete_evidence(self):
        body = FINAL_MIXED
        for width in (1, 2, 17, len(body)):
            text = "<thinking>" + body
            guard = self.run_guard(text, [width] * (len(text) // width + 1), finish=False)
            self.assertFalse(guard.hit)
            self.assertTrue(guard.finish())
            self.assertEqual(guard.rule, "mixed-stall")
            result = self.result(guard)
            self.assertTrue(guard.finish())
            self.assertEqual(self.result(guard), result)

    def test_closing_tag_finalizes_only_the_preceding_thinking_body(self):
        body = FINAL_MIXED
        text = "<thinking>" + body + "</thinking>A concrete answer."
        expected = self.result(self.run_guard(text, [1] * len(text)))
        self.assertTrue(expected[0])
        self.assertEqual(expected[1], "mixed-stall")
        self.assertEqual(expected[2], len(body))
        for width in (2, 7, 64, len(text)):
            guard = self.run_guard(text, [width] * (len(text) // width + 1))
            self.assertEqual(self.result(guard), expected)

    def test_short_habits_and_normal_analysis_remain_unchanged(self):
        for body in ("Hmm. Let me go.\n" * 3,
                     "The task needs verification. Let me go. A new fact.\n" * 100,
                     "Hmm.\n" * 100,
                     "OK.\n" * 100 + "Let me go. I will run."):
            text = "<thinking>" + body + "</thinking>The answer follows."
            guard = self.run_guard(text, [17] * (len(text) // 17 + 1))
            legacy = self.run_guard(text, [17] * (len(text) // 17 + 1), mixed=False)
            # Some intentionally very repetitive cases already trigger the
            # legacy rule. The optional mixed detector must not broaden them.
            self.assertIsNone(guard.mixed_metrics)
            self.assertEqual((guard.hit, guard.rule, guard.state), (legacy.hit, legacy.rule, legacy.state))

    def test_independent_instances_do_not_join_evidence(self):
        body = "Let me go. Hmm. Hmm. Hmm. A brief fact.\n" * 4
        self.assertIsNotNone(MixedStallGuard().feed(body * 2, final=True))
        for _ in range(2):
            text = "<thinking>" + body + "</thinking>"
            guard = self.run_guard(text, [1] * len(text))
            self.assertFalse(guard.hit)
            self.assertIsNone(guard.mixed_metrics)

    def test_state_remains_bounded_and_evidence_contains_no_text(self):
        text = "<thinking>" + "".join("Distinct observation at position %d.\n" % i for i in range(1500))
        guard = self.run_guard(text, [len(text)])
        self.assertFalse(guard.hit)
        self.assertLessEqual(len(guard.probe), 65)
        self.assertLessEqual(len(guard.pending), len("</thinking>"))
        self.assertLessEqual(len(guard.guard.line), REASON_LOOP_TAIL)
        self.assertLessEqual(len(guard.mixed_stall.line), MAX_LINE_CHARS)
        self.assertLessEqual(len(guard.mixed_stall.window.records), MAX_SENTENCES)
        self.assertLessEqual(guard.mixed_stall.window.chars, MAX_WINDOW_CHARS)
        hit = self.run_guard("<thinking>" + MIXED, [len(MIXED) + 10])
        self.assertTrue(hit.hit)
        self.assertTrue(all(isinstance(value, int) for value in hit.mixed_metrics.values()))

    def test_oversized_structured_line_is_still_excluded(self):
        body = '{"value": "' + "x" * 10000 + MIXED.replace("\n", " ") + '"}\n'
        text = "<thinking>" + body + "</thinking>"
        guard = self.run_guard(text, [17] * (len(text) // 17 + 1))
        self.assertFalse(guard.hit)
        self.assertEqual(guard.state, "done")
        self.assertLessEqual(len(guard.mixed_stall.line), MAX_LINE_CHARS)

    def test_master_switch_disables_both_detectors(self):
        for value in ("0", "false", "off"):
            with self.subTest(value=value), patch.dict(os.environ, {"WB_OPS_LOOP_GUARD": value}):
                guard = self.run_guard("<thinking>" + MIXED + LEGACY, [len(MIXED + LEGACY) + 10])
                self.assertFalse(guard.hit)
                self.assertIsNone(guard.mixed_metrics)


if __name__ == "__main__":
    unittest.main()
