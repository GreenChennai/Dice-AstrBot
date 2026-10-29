import unittest

from dice_astrbot.safe_regex import SafeRegexError, compile_safe_regex


class SafeRegexCaptureTests(unittest.TestCase):
    def test_nested_alternation_captures_original_text(self):
        regex = compile_safe_regex(r"^((ab)|(cd))$")
        self.assertEqual(regex.fullmatch_captures("CD"), ("CD", None, "CD"))
        self.assertTrue(regex.fullmatch("CD"))
        self.assertIsNone(regex.fullmatch_captures("other"))

    def test_empty_and_unmatched_groups(self):
        regex = compile_safe_regex(r"^()(a)?$")
        self.assertEqual(regex.fullmatch_captures(""), ("", None))
        self.assertEqual(regex.fullmatch_captures("a"), ("", "a"))

    def test_greedy_and_lazy_repetition_choose_capture_spans(self):
        greedy = compile_safe_regex(r"^(a+)(a*)$")
        lazy = compile_safe_regex(r"^(a+?)(a*)$")
        self.assertEqual(greedy.fullmatch_captures("aaa"), ("aaa", ""))
        self.assertEqual(lazy.fullmatch_captures("aaa"), ("a", "aa"))

    def test_two_digit_capture_numbering(self):
        regex = compile_safe_regex(r"^(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)$")
        captures = regex.fullmatch_captures("abcdefghij")
        self.assertEqual(len(captures), 10)
        self.assertEqual(captures[0], "a")
        self.assertEqual(captures[9], "j")

    def test_capture_match_respects_step_budget(self):
        regex = compile_safe_regex(r"^(a|aa)*$")
        with self.assertRaises(SafeRegexError):
            regex.fullmatch_captures("a" * 20, max_steps=1)

    def test_capture_acceptance_matches_boolean_fullmatch(self):
        cases = [
            (r"^(ab|cd)+$", "abcd"),
            (r"^a(b)?c$", "ac"),
            (r"^(a+?)(a*)$", "aaaa"),
            (r"^(x|y)$", "z"),
        ]
        for pattern, text in cases:
            with self.subTest(pattern=pattern, text=text):
                regex = compile_safe_regex(pattern)
                self.assertEqual(regex.fullmatch_captures(text) is not None, regex.fullmatch(text))


if __name__ == "__main__":
    unittest.main()
