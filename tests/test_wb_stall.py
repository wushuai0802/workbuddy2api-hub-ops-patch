"""Offline mixed-stall detector and history preservation checks."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wb_stall import MixedStallGuard, clean_stalled_reasoning


def mixed():
    return ("Let me go. Hmm. Hmm. Hmm. A brief fact.\n" * 8)


class StallTests(unittest.TestCase):
    def test_identical_action_anchor(self):
        metrics = MixedStallGuard().feed(mixed(), final=True)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["path"], 1)
        self.assertGreaterEqual(metrics["dominant_count"], 8)
        self.assertTrue(all(isinstance(value, int) for value in metrics.values()))

    def test_extreme_hmm_density_independent_path(self):
        text = "Hmm.\n" * 64 + "Let me go.\nI will run.\nAn observed detail."
        metrics = MixedStallGuard().feed(text, final=True)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["path"], 2)
        self.assertLess(metrics["dominant_count"], 8)

    def test_extreme_hmm_with_other_marker_and_no_action(self):
        text = "Hmm.\n" * 64 + "OK.\nOK.\nAn observed detail."
        metrics = MixedStallGuard().feed(text, final=True)
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics["path"], 2)
        self.assertEqual(metrics["intent_lines"], 0)
        cleaned = clean_stalled_reasoning(text)
        self.assertEqual(cleaned.count("OK."), 2)
        self.assertIn("An observed detail.", cleaned)

    def test_plural_and_identifier_possessives_do_not_open_quote(self):
        text = "The tools' state and `name`'s value agree.\n" + mixed()
        self.assertIsNotNone(MixedStallGuard().feed(text, final=True))
        self.assertTrue(clean_stalled_reasoning(text).startswith(text.splitlines()[0] + "\n"))

    def test_short_habits_and_ordinary_progress_pass(self):
        for text in ("Hmm. Let me go.\n" * 3,
                     "The task needs verification. Let me go. A new fact.\n" * 100,
                     "Hmm.\n" * 100,
                     "OK.\n" * 100 + "Let me go. I will run."):
            self.assertIsNone(MixedStallGuard().feed(text, final=True))
            self.assertEqual(clean_stalled_reasoning(text), text)

    def test_same_action_means_same_complete_sentence(self):
        actions = ("Let me go", "I will go", "Go", "Let me run", "Run", "I will run", "Let me write", "Write")
        text = "\n".join(action + ". Hmm. Hmm. Hmm." for action in actions)
        self.assertIsNone(MixedStallGuard().feed(text, final=True))
        self.assertEqual(clean_stalled_reasoning(text), text)

    def test_substantive_character_budget(self):
        text = ("A" * 400 + ".\n") * 8 + ("Let me go. Hmm. Hmm. Hmm.\n" * 8)
        self.assertIsNone(MixedStallGuard().feed(text, final=True))
        self.assertEqual(clean_stalled_reasoning(text), text)

    def test_protected_regions_are_unchanged(self):
        body = mixed()
        cases = (
            "```text\n" + body + "```\n",
            "~~~\n" + body + "~~~\n",
            '"' + body + '"\n',
            "“" + body + "”\n",
            "\n".join("> " + line for line in body.splitlines()),
            "\n".join("    " + line for line in body.splitlines()),
            "\n".join("`" + line + "`" for line in body.splitlines()),
            "\n".join("Example: " + line for line in body.splitlines()),
            "\n".join("1 " + line for line in body.splitlines()),
        )
        for text in cases:
            with self.subTest(text_size=len(text)):
                self.assertIsNone(MixedStallGuard().feed(text, final=True))
                self.assertEqual(clean_stalled_reasoning(text), text)

    def test_partition_invariance_and_final_flush(self):
        # Deliberately no newline or final punctuation after the last phrase.
        text = mixed().replace("\n", " ").rstrip() + " Hmm"
        expected = MixedStallGuard().feed(text, final=True)
        self.assertIsNotNone(expected)
        for width in (1, 2, 3, 7, 31, 64, 101):
            guard = MixedStallGuard()
            actual = None
            for offset in range(0, len(text), width):
                actual = guard.feed(text[offset:offset + width])
                if actual:
                    break
            actual = actual or guard.feed(final=True)
            self.assertEqual(actual, expected)

    def test_final_word_without_punctuation(self):
        text = "Hmm.\n" * 64 + "Let me go.\nI will run"
        guard = MixedStallGuard()
        self.assertIsNone(guard.feed(text))
        self.assertIsNotNone(guard.feed(final=True))

    def test_bounded_state_and_old_evidence_expires(self):
        text = ("An ordinary fact with unique progress.\n" * 10000)
        guard = MixedStallGuard()
        self.assertIsNone(guard.feed(text, final=True))
        self.assertLessEqual(len(guard.window.records), 128)
        self.assertLessEqual(guard.window.chars, 8192)
        self.assertLessEqual(len(guard.line), 2048)
        self.assertTrue(all(isinstance(item.digest, bytes) for item in guard.window.records))
        self.assertIsNone(guard.feed("Hmm. Let me go. " * 3, final=True))

    def test_oversized_line_is_protected(self):
        text = "x" * 10000 + mixed().replace("\n", " ")
        guard = MixedStallGuard()
        self.assertIsNone(guard.feed(text, final=True))
        self.assertLessEqual(len(guard.line), 2048)
        self.assertEqual(clean_stalled_reasoning(text), text)

    def test_history_cleanup_preserves_substantive_actions_and_whitespace(self):
        text = "Important exact prefix.\n" + mixed() + "\nImportant exact suffix."
        result = clean_stalled_reasoning(text)
        self.assertTrue(result.startswith("Important exact prefix.\n"))
        self.assertTrue(result.endswith("\nImportant exact suffix."))
        self.assertEqual(result.count("A brief fact."), text.count("A brief fact."))
        self.assertEqual(result.count("Let me go."), text.count("Let me go."))
        self.assertLess(result.count("Hmm."), text.count("Hmm."))
        self.assertEqual(result.replace(" ", "").count("\n"), text.count("\n"))
        self.assertEqual(clean_stalled_reasoning(result), result)

    def test_history_decision_words_and_actions_are_not_removed(self):
        text = mixed() + "OK.\nDone.\nWait.\nNow.\nFinal.\n"
        result = clean_stalled_reasoning(text)
        for phrase in ("OK.", "Done.", "Wait.", "Now.", "Final.", "Let me go."):
            self.assertEqual(result.count(phrase), text.count(phrase))

    def test_cleanup_only_confirmed_window_and_protected_text(self):
        protected = '```\nHmm. Let me go.\n```\n"Hmm."\n'
        prefix = "Hmm. Let me go.\n" + "An unrelated substantive observation.\n" * 128
        text = prefix + protected + mixed() + protected
        result = clean_stalled_reasoning(text)
        self.assertTrue(result.startswith(prefix + protected))
        self.assertTrue(result.endswith(protected))

    def test_master_guard_switch(self):
        for value in ("0", "false", "off"):
            with patch.dict(os.environ, {"WB_OPS_LOOP_GUARD": value}):
                self.assertIsNone(MixedStallGuard().feed(mixed(), final=True))
                self.assertEqual(clean_stalled_reasoning(mixed()), mixed())


if __name__ == "__main__":
    unittest.main()
