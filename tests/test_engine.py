import unittest

from dice_astrbot.dice_engine import DiceEngine, DiceError


class SequenceRng:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        if not self.values:
            raise AssertionError(f"No deterministic value left for {low}..{high}")
        value = self.values.pop(0)
        if not low <= value <= high:
            raise AssertionError(f"{value} is outside {low}..{high}")
        return value


class DiceEngineTests(unittest.TestCase):
    def test_expression_and_keep_highest(self):
        engine = DiceEngine(SequenceRng([1, 3, 6, 1, 2, 4, 6]))
        self.assertEqual(engine.roll("3d6+2").total, 12)
        result = engine.roll("4d6k3")
        self.assertEqual(result.total, 12)
        self.assertIn("保留6,4,2", result.format())

    def test_normal_dice_details_sort_only_above_twenty(self):
        values = [6, 1, 5, 2, 4, 3] * 3 + [6, 1, 5]
        twenty = DiceEngine(SequenceRng(values[:20])).roll("20d6")
        self.assertEqual(twenty.total, sum(values[:20]))
        self.assertEqual(twenty.terms[0].detail, f"20d6[{','.join(map(str, values[:20]))}]")

        expected = f"21d6[{','.join(map(str, sorted(values)))}]"
        explicit = DiceEngine(SequenceRng(values)).roll("21d6")
        self.assertEqual(explicit.total, sum(values))
        self.assertEqual(explicit.terms[0].detail, expected)

        defaulted = DiceEngine(SequenceRng(values)).roll("21D", default_die=6)
        self.assertEqual(defaulted.total, sum(values))
        self.assertEqual(defaulted.terms[0].detail, expected)

    def test_long_result_format_uses_upstream_compact_fallback(self):
        result = DiceEngine(SequenceRng([1] * 1998)).roll("999d6+999d6")
        self.assertEqual(result.total, 1998)
        self.assertEqual(result.format(), "999D6+999D6=999+999=1998")

    def test_call_scoped_rng_is_used_and_restored_for_rolls_and_checks(self):
        engine = DiceEngine(SequenceRng([2, 55]))
        override = SequenceRng([5, 11])
        self.assertEqual(engine.roll("D6", rng=override).total, 5)
        self.assertEqual(engine.roll("D6").total, 2)
        self.assertEqual(engine.check(50, rule=0, rng=override).roll, 11)
        self.assertEqual(engine.check(50, rule=0).roll, 55)

    def test_fudge_shorthand_and_default_die(self):
        engine = DiceEngine(SequenceRng([1, 0, -1, 1, 83]))
        self.assertEqual(engine.roll("F").total, 1)
        self.assertEqual(engine.roll("D", default_die=100).total, 83)

    def test_bonus_and_penalty_choose_correct_tens(self):
        engine = DiceEngine(SequenceRng([83, 2, 9, 83, 2, 9]))
        self.assertEqual(engine.roll("B2").total, 13)
        self.assertEqual(engine.roll("P2").total, 83)

    def test_bonus_and_penalty_default_to_one_extra_die(self):
        engine = DiceEngine(SequenceRng([83, 2, 83, 2]))
        self.assertEqual(engine.roll("B").total, 13)
        self.assertEqual(engine.roll("P").total, 83)

    def test_bonus_and_penalty_keep_100_units_convention(self):
        engine = DiceEngine(SequenceRng([100, 1, 2, 100, 1, 10]))
        self.assertEqual(engine.roll("B2").total, 10)
        self.assertEqual(engine.roll("P2").total, 100)

    def test_all_coc_room_rules_match_upstream_boundaries(self):
        cases = [
            (0, 1, 0, "critical"), (0, 100, 100, "fumble"),
            (0, 96, 49, "fumble"), (0, 96, 50, "failure"), (0, 20, 100, "extreme"),
            (1, 5, 50, "critical"), (1, 5, 49, "extreme"),
            (1, 96, 49, "fumble"), (1, 99, 50, "failure"),
            (2, 5, 5, "critical"), (2, 5, 4, "failure"),
            (2, 96, 95, "fumble"), (2, 100, 100, "fumble"),
            (3, 5, 0, "critical"), (3, 96, 100, "fumble"),
            (4, 5, 50, "critical"), (4, 5, 49, "extreme"),
            (4, 96, 10, "failure"), (4, 97, 10, "fumble"),
            (5, 1, 20, "critical"), (5, 2, 20, "extreme"),
            (5, 98, 49, "fumble"), (5, 98, 50, "failure"), (5, 99, 100, "fumble"),
            (6, 11, 11, "critical"), (6, 22, 21, "fumble"),
            (6, 22, 22, "critical"), (6, 12, 11, "failure"), (6, 100, 100, "success"),
            (7, 1, 0, "extreme"), (7, 13, 65, "extreme"), (7, 14, 65, "success"),
            (7, 96, 10, "fumble"), (7, 96, 30, "failure"),
            (7, 99, 70, "fumble"), (7, 99, 100, "failure"), (7, 100, 100, "fumble"),
        ]
        for rule, roll, skill, expected in cases:
            with self.subTest(rule=rule, roll=roll, skill=skill):
                result = DiceEngine(SequenceRng([roll])).check(skill, rule=rule)
                self.assertEqual(result.outcome, expected)
        self.assertEqual(DiceEngine(SequenceRng([2])).check(100, rule=5).threshold, 2)

    def test_coc_rule_and_skill_bounds(self):
        for rule in (-1, 8, True):
            with self.subTest(rule=rule), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).check(50, rule=rule)
        for skill in (-1, 10_001, True):
            with self.subTest(skill=skill), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).check(skill, rule=0)
        self.assertEqual(DiceEngine(SequenceRng([1])).check(10_000, rule=0).outcome, "critical")

    def test_fudge_dice_count_respects_upstream_two_digit_limit(self):
        self.assertEqual(DiceEngine(SequenceRng([0] * 99)).roll("99DF").total, 0)
        for expression in ("100DF", "100F"):
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).roll(expression)

    def test_non_keep_dice_count_matches_upstream_three_digit_limit(self):
        self.assertEqual(DiceEngine(SequenceRng([1] * 999)).roll("999d6").total, 999)
        self.assertEqual(DiceEngine(SequenceRng([6] * 100)).roll("100d6k1").total, 6)
        for expression in ("1000d6", "101d6k1", "099d6k1", "999d6k1"):
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).roll(expression)

    def test_upstream_only_accepts_ascii_numeric_tokens(self):
        for expression in ("١", "١D٦", "1D٦", "١F", "B٢"):
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).roll(expression)
        self.assertEqual(DiceEngine(SequenceRng([6])).roll("1D6").total, 6)
        self.assertEqual(DiceEngine(SequenceRng([1])).roll("1DF").total, 1)
        self.assertEqual(DiceEngine(SequenceRng([100, 1])).roll("B1").total, 10)

    def test_invalid_keep_does_not_consume_random_values(self):
        for expression in ("2d6k0", "2d6k3"):
            rng = SequenceRng([2, 5, 4])
            engine = DiceEngine(rng)
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                engine.roll(expression)
            self.assertEqual(engine.roll("D6").total, 2)

    def test_upstream_numeric_token_length_bounds(self):
        invalid = ("100000", "0001d6", "1d10000", "1d6k0001", "0001D", "B01", "P01")
        for expression in invalid:
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).roll(expression)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("99999").total, 99_999)
        self.assertEqual(DiceEngine(SequenceRng([9_999])).roll("1d9999").total, 9_999)
        self.assertEqual(DiceEngine(SequenceRng([5, 2])).roll("2d6k01").total, 5)

    def test_default_die_side_token_respects_upstream_four_digit_limit(self):
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([])).roll("2D", default_die=100_000)
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([])).roll("D", default_die=100_000)
        self.assertEqual(DiceEngine(SequenceRng([9_999, 9_999])).roll("2D", default_die=9_999).total, 19_998)
        self.assertEqual(DiceEngine(SequenceRng([6])).roll("D6", default_die=100_000).total, 6)

    def test_invalid_and_bounded_expressions_fail_closed(self):
        for expression in ("0d6", "1d100001", "1d6+"):
            with self.subTest(expression=expression), self.assertRaises(DiceError):
                DiceEngine(SequenceRng([])).roll(expression)
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([3])).roll("1d6/0")
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([1])).roll("1d6k2")
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([])).roll("1" * 161)
        with self.assertRaises(DiceError):
            DiceEngine(SequenceRng([])).roll("B10")

    def test_integer_division_truncates_toward_zero(self):
        engine = DiceEngine(SequenceRng([]))
        self.assertEqual(engine.roll("5/2").total, 2)
        self.assertEqual(engine.roll("-5/2").total, -2)
        self.assertEqual(DiceEngine(SequenceRng([-1, -1])).roll("6/2DF").total, -3)

    def test_multiplication_after_division_belongs_to_denominator(self):
        engine = DiceEngine(SequenceRng([]))
        self.assertEqual(engine.roll("5/2X2").total, 1)
        self.assertEqual(engine.roll("5X2/2X2").total, 2)

    def test_divisor_factors_roll_right_to_left_before_numerator(self):
        engine = DiceEngine(SequenceRng([2, 3, 6]))
        self.assertEqual(engine.roll("1d6/1d6X1d10").total, 1)

    def test_fudge_and_percentile_terms_ignore_arithmetic_modifiers(self):
        self.assertEqual(DiceEngine(SequenceRng([-1] * 4)).roll("4DFX2").total, -4)
        self.assertEqual(DiceEngine(SequenceRng([-1] * 4)).roll("4DF/2").total, -4)
        self.assertEqual(DiceEngine(SequenceRng([83, 2, 9])).roll("B2X2").total, 13)
        self.assertEqual(DiceEngine(SequenceRng([-1, -1])).roll("6/2DFX2").total, -3)


    def test_supported_expression_shorthand_normalization(self):
        self.assertEqual(DiceEngine(SequenceRng([1, -1])).roll("2F").total, 0)
        self.assertEqual(DiceEngine(SequenceRng([2, 5])).roll("2d6K").total, 5)
        self.assertEqual(DiceEngine(SequenceRng([2, 5])).roll("2d6K+1").total, 6)
        self.assertEqual(DiceEngine(SequenceRng([2, 5])).roll("2d6K-1").total, 4)
        # Local extension: upstream prefixes 1 but retains `a`, then rejects it.
        self.assertEqual(DiceEngine(SequenceRng([])).roll("A+2").total, 3)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("-A+2").total, 1)
        self.assertEqual(DiceEngine(SequenceRng([5])).roll("DK").total, 5)

    def test_upstream_constructor_truncates_invalid_operator_pairs(self):
        cases = (
            ("5//2", "5", 5),
            ("2XX3", "2", 2),
            ("2X/3", "2", 2),
            ("2+*3", "2", 2),
            ("2/*3", "2", 2),
            ("1X2XX3", "1X2", 2),
        )
        for expression, normalized, total in cases:
            with self.subTest(expression=expression):
                result = DiceEngine(SequenceRng([])).roll(expression)
                self.assertEqual(result.expression, normalized)
                self.assertEqual(result.total, total)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("+2").expression, "2")

    def test_upstream_ignores_unary_plus_after_multiplication_and_division(self):
        self.assertEqual(DiceEngine(SequenceRng([])).roll("2X+3").total, 6)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("2*+3").total, 6)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("1/+2").total, 0)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("2X++3").total, 6)

    def test_upstream_truncates_minus_after_multiplication_or_division(self):
        cases = (("2X-3", 2), ("2/-3", 2), ("2*-3", 2), ("1+2X-3", 3), ("2X+-3", 2), ("2X--3", 2))
        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(DiceEngine(SequenceRng([])).roll(expression).total, expected)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("2X+3").total, 6)
        self.assertEqual(DiceEngine(SequenceRng([])).roll("1-2").total, -1)

    def test_adjacent_signs_are_normalized_like_upstream(self):
        engine = DiceEngine(SequenceRng([]))
        self.assertEqual(engine.roll("1++2").total, 3)
        self.assertEqual(engine.roll("1+-2").total, -1)
        self.assertEqual(engine.roll("1-+2").total, -1)
        self.assertEqual(engine.roll("1--2").total, 3)

if __name__ == "__main__":
    unittest.main()
