"""Unit tests for ds_codec.py.

Run from the project directory with:

    python -m unittest discover -s outputs -p "test_*.py" -v
"""

from __future__ import annotations

import itertools
import unittest

from cocotbext.spacewire.ds_codec import ds_encode, ds_decode


class DSEncodeTests(unittest.TestCase):
    def assert_valid_ds(
        self,
        d_levels: list[int],
        s_levels: list[int],
        initial_d: int = 0,
        initial_s: int = 0,
    ) -> None:
        """Assert that exactly one DS line changes for every bit."""
        previous_d = initial_d
        previous_s = initial_s
        previous_clock = initial_d ^ initial_s

        for index, (current_d, current_s) in enumerate(zip(d_levels, s_levels)):
            changes = (current_d != previous_d) + (current_s != previous_s)
            self.assertEqual(changes, 1, f"invalid transition at sample {index}")

            current_clock = current_d ^ current_s
            self.assertNotEqual(
                current_clock,
                previous_clock,
                f"recovered clock did not toggle at sample {index}",
            )

            previous_d = current_d
            previous_s = current_s
            previous_clock = current_clock

    def test_known_vector_with_default_initial_levels(self) -> None:
        d_levels, s_levels = ds_encode("0011")

        self.assertEqual(d_levels, [0, 0, 1, 1])
        self.assertEqual(s_levels, [1, 0, 0, 1])
        self.assert_valid_ds(d_levels, s_levels)

    def test_custom_initial_levels(self) -> None:
        d_levels, s_levels = ds_encode("0011", initial_d=1, initial_s=0)

        self.assertEqual(d_levels, [0, 0, 1, 1])
        self.assertEqual(s_levels, [0, 1, 1, 0])
        self.assert_valid_ds(d_levels, s_levels, initial_d=1, initial_s=0)

    def test_string_whitespace_is_ignored(self) -> None:
        self.assertEqual(ds_encode("10 01\n"), ds_encode([1, 0, 0, 1]))

    def test_bytes_are_read_most_significant_bit_first(self) -> None:
        d_levels, _ = ds_encode(bytes([0xA5]))
        self.assertEqual(d_levels, [1, 0, 1, 0, 0, 1, 0, 1])

    def test_bytearray_is_supported(self) -> None:
        self.assertEqual(ds_encode(bytearray([0xA5])), ds_encode(bytes([0xA5])))

    def test_generator_is_supported(self) -> None:
        bits = (bit for bit in [1, 1, 0, 1])
        d_levels, _ = ds_encode(bits)
        self.assertEqual(d_levels, [1, 1, 0, 1])

    def test_empty_input(self) -> None:
        self.assertEqual(ds_encode(""), ([], []))

    def test_invalid_binary_string_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "binary string"):
            ds_encode("1021")

    def test_invalid_iterable_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "only binary values"):
            ds_encode([0, 1, 2])

    def test_invalid_initial_level_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "initial_d"):
            ds_encode([0], initial_d=2)


class DSDecodeTests(unittest.TestCase):
    def test_known_vector(self) -> None:
        decoded = ds_decode("0011", "1001")
        self.assertEqual(decoded, [0, 0, 1, 1])

    def test_round_trip_for_all_short_sequences_and_initial_levels(self) -> None:
        for length in range(7):
            for bits in itertools.product((0, 1), repeat=length):
                for initial_d in (0, 1):
                    for initial_s in (0, 1):
                        with self.subTest(
                            bits=bits, initial_d=initial_d, initial_s=initial_s
                        ):
                            d_levels, s_levels = ds_encode(
                                bits,
                                initial_d=initial_d,
                                initial_s=initial_s,
                            )
                            decoded = ds_decode(
                                d_levels,
                                s_levels,
                                initial_d=initial_d,
                                initial_s=initial_s,
                            )
                            self.assertEqual(decoded, list(bits))

    def test_different_lengths_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "same length"):
            ds_decode([1, 0], [0])

    def test_neither_line_changing_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "neither changed"):
            ds_decode([0], [0], initial_d=0, initial_s=0)

    def test_both_lines_changing_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "both changed"):
            ds_decode([1], [1], initial_d=0, initial_s=0)

    def test_error_reports_sample_index(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample 1"):
            ds_decode([1, 1], [0, 0], initial_d=0, initial_s=0)

    def test_validation_can_be_disabled_for_raw_data_extraction(self) -> None:
        decoded = ds_decode([1, 1], [1, 1], validate=False)
        self.assertEqual(decoded, [1, 1])

    def test_empty_input(self) -> None:
        self.assertEqual(ds_decode([], []), [])


if __name__ == "__main__":
    unittest.main()
