"""Simulator-free unit tests for the SpaceWire character codec."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Iterable
from dataclasses import dataclass

from hypothesis import given, settings, strategies as st

from cocotbext.spacewire.exceptions import EscapeError, ParityError
from cocotbext.spacewire.characters import (
    CharMonitor,
    CharType,
    Character,
    Decoder,
    Encoder,
    compute_parity,
    is_l_char_type,
    is_n_char_type,
)


def encode_sequence(items: list[object]) -> list[int]:
    """Encode a sequence with one shared encoder state."""

    encoder = Encoder()
    bits: list[int] = []
    for item in items:
        bits.extend(encoder.encode(item))  # type: ignore[arg-type]
    return bits


@dataclass(frozen=True)
class PrimitiveSpan:
    """Bit index span for one primitive data or control character."""

    start: int
    length: int
    dc_flag: int


@dataclass(frozen=True)
class CharSpec:
    """Compact randomized high-level character description."""

    kind: str
    value: int | None = None
    type_field: int | None = None


def character_from_spec(spec: CharSpec) -> Character | CharType:
    """Build an encoder input from a randomized character spec."""

    if spec.kind == "data":
        assert spec.value is not None
        return Encoder.data(spec.value)
    if spec.kind == "time_code":
        assert spec.value is not None
        return Encoder.time_code(spec.value)
    if spec.kind == "broadcast":
        assert spec.value is not None
        assert spec.type_field is not None
        return Encoder.broadcast(spec.value, type_field=spec.type_field)
    if spec.kind == "null":
        return Encoder.null()
    return CharType[spec.kind.upper()]


def expected_from_spec(spec: CharSpec) -> tuple[CharType, int | None]:
    """Return the expected decoded ``(type, value)`` pair for a legal spec."""

    if spec.kind == "data":
        return (CharType.DATA, spec.value)
    if spec.kind == "time_code":
        return (CharType.TIMECODE, spec.value)
    if spec.kind == "broadcast":
        assert spec.value is not None
        assert spec.type_field is not None
        return (CharType.BROADCAST, (spec.type_field << 6) | spec.value)
    if spec.kind == "null":
        return (CharType.NULL, None)
    return (CharType[spec.kind.upper()], None)


def primitive_spans(bits: list[int]) -> list[PrimitiveSpan]:
    """Return primitive character spans from a well-formed encoded stream."""

    spans: list[PrimitiveSpan] = []
    index = 0
    while index < len(bits):
        dc_flag = bits[index + 1]
        length = 4 if dc_flag else 10
        spans.append(PrimitiveSpan(start=index, length=length, dc_flag=dc_flag))
        index += length
    return spans


def covered_indices(bits: list[int], original_bit_count: int) -> list[int]:
    """Return bit indices whose parity coverage can be observed in this stream."""

    covered: set[int] = set()
    spans = primitive_spans(bits)
    for primitive_index, span in enumerate(spans):
        if span.start >= original_bit_count:
            break

        covered.add(span.start)
        covered.add(span.start + 1)

        if primitive_index > 0:
            previous = spans[primitive_index - 1]
            payload_start = previous.start + 2
            payload_end = previous.start + previous.length
            covered.update(
                index
                for index in range(payload_start, payload_end)
                if index < original_bit_count
            )

    return sorted(covered)


def has_parity_error(bits: Iterable[int]) -> bool:
    """Return true if decoding reports a parity error."""

    return any(
        "parity_error" in (character.error or "")
        for character in Decoder().push(bits)
    )


LEGAL_CHAR_SPECS = st.one_of(
    st.builds(CharSpec, st.just("data"), st.integers(min_value=0, max_value=255)),
    st.builds(CharSpec, st.just("time_code"), st.integers(min_value=0, max_value=63)),
    st.builds(
        CharSpec,
        st.just("broadcast"),
        st.integers(min_value=0, max_value=63),
        st.just(0b10),
    ),
    st.builds(CharSpec, st.just("fct")),
    st.builds(CharSpec, st.just("eop")),
    st.builds(CharSpec, st.just("eep")),
    st.builds(CharSpec, st.just("null")),
)


class CharacterCodecTests(unittest.TestCase):
    def test_round_trip_reproduces_character_sequence(self) -> None:
        items = [
            CharType.FCT,
            Encoder.data(0xA5),
            CharType.EOP,
            CharType.EEP,
            Encoder.null(),
            Encoder.time_code(0x15),
            Encoder.broadcast(0x22),
            Encoder.data(0x00),
        ]

        decoded = Decoder().push(encode_sequence(items))

        self.assertEqual(
            [character.type for character in decoded],
            [
                CharType.FCT,
                CharType.DATA,
                CharType.EOP,
                CharType.EEP,
                CharType.NULL,
                CharType.TIMECODE,
                CharType.BROADCAST,
                CharType.DATA,
            ],
        )
        self.assertEqual(decoded[1].value, 0xA5)
        self.assertEqual(decoded[5].value, 0x15)
        self.assertEqual(decoded[5].type_field, 0)
        self.assertEqual(decoded[5].code_value, 0x15)
        self.assertEqual(decoded[6].value, 0xA2)
        self.assertEqual(decoded[6].type_field, 0b10)
        self.assertEqual(decoded[6].code_value, 0x22)
        self.assertEqual(decoded[7].value, 0x00)
        self.assertTrue(all(character.error is None for character in decoded))

    def test_parity_spans_previous_content_bits(self) -> None:
        encoder = Encoder()
        first = encoder.encode(Encoder.data(0x01))
        second = encoder.encode(CharType.FCT)

        self.assertEqual(first[0], compute_parity([], 0))
        self.assertEqual(second[0], compute_parity([1, 0, 0, 0, 0, 0, 0, 0], 1))
        self.assertNotEqual(second[0], compute_parity([], 1))

    def test_flipping_any_bit_covered_by_next_data_parity_is_detected(self) -> None:
        bits = encode_sequence([Encoder.data(0xA5), Encoder.data(0x5A)])
        covered_indices = [
            *range(2, 10),  # previous data content bits
            10,  # current parity bit
            11,  # current data-control flag
        ]

        for index in covered_indices:
            with self.subTest(index=index):
                corrupted = list(bits)
                corrupted[index] ^= 1
                decoded = Decoder().push(corrupted)
                self.assertTrue(
                    any(character.error == "parity_error" for character in decoded),
                    f"no parity error after flipping bit {index}",
                )

    def test_null_time_code_and_broadcast_are_decoded_from_escape_sequences(self) -> None:
        decoded = Decoder().push(
            encode_sequence(
                [
                    Encoder.null(),
                    Encoder.time_code(0x2A),
                    Encoder.broadcast(0x05),
                ]
            )
        )

        self.assertEqual([character.type for character in decoded], [
            CharType.NULL,
            CharType.TIMECODE,
            CharType.BROADCAST,
        ])
        self.assertIsNone(decoded[0].value)
        self.assertEqual(decoded[1].type_field, 0b00)
        self.assertEqual(decoded[1].code_value, 0x2A)
        self.assertEqual(decoded[2].value, 0x85)
        self.assertEqual(decoded[2].type_field, 0b10)
        self.assertEqual(decoded[2].code_value, 0x05)

    def test_illegal_escape_control_combinations_are_flagged(self) -> None:
        for illegal in (CharType.EOP, CharType.EEP, CharType.ESC):
            with self.subTest(illegal=illegal):
                decoded = Decoder().push(encode_sequence([CharType.ESC, illegal]))

                self.assertEqual(len(decoded), 1)
                self.assertEqual(decoded[0].type, illegal)
                self.assertIn("escape_error", decoded[0].error or "")

    def test_l_char_and_n_char_classification(self) -> None:
        self.assertTrue(is_l_char_type(CharType.NULL))
        self.assertTrue(is_l_char_type(CharType.FCT))
        self.assertFalse(is_l_char_type(CharType.DATA))

        for char_type in (CharType.DATA, CharType.EOP, CharType.EEP):
            self.assertTrue(is_n_char_type(char_type))

        for char_type in (CharType.NULL, CharType.FCT, CharType.TIMECODE):
            self.assertFalse(is_n_char_type(char_type))

    def test_forced_bad_parity_is_reported(self) -> None:
        encoder = Encoder()
        encoder.force_bad_parity()
        bits = encoder.encode(Encoder.data(0x33))

        decoded = Decoder().push(bits)

        self.assertEqual(len(decoded), 1)
        self.assertFalse(decoded[0].parity_ok)
        self.assertEqual(decoded[0].error, "parity_error")
        self.assertEqual(decoded[0].expected_parity, 1)
        self.assertEqual(decoded[0].got_parity, 0)

    def test_reserved_escaped_data_type_is_visible(self) -> None:
        decoded = Decoder().push(encode_sequence([CharType.ESC, Encoder.data(0x40)]))

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].type, CharType.BROADCAST)
        self.assertEqual(decoded[0].type_field, 0b01)
        self.assertEqual(decoded[0].code_value, 0)
        self.assertIn("reserved_escape_data_type", decoded[0].error or "")

    def test_decoder_stats_count_characters_and_errors(self) -> None:
        decoder = Decoder()
        decoder.push(encode_sequence([CharType.FCT, CharType.ESC, CharType.EOP]))

        stats = decoder.stats()

        self.assertEqual(stats["characters"]["FCT"], 1)
        self.assertEqual(stats["characters"]["EOP"], 1)
        self.assertEqual(stats["errors"]["escape_error"], 1)


class GoldenVectorTests(unittest.TestCase):
    """Known-answer vectors traced to ECSS figures and odd-parity rules."""

    PRIMARY_ORACLE = [
        1, 0, 0, 0, 1, 1, 1, 0, 1, 0,
        0, 1, 1, 1, 0, 1, 0, 0,
    ]

    def test_ecss_fig_5_15_data_then_null(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.data(0x5C), Encoder.null()]),
            self.PRIMARY_ORACLE,
        )

    def test_ecss_fig_5_15_standalone_data(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.data(0x5C)]),
            [1, 0, 0, 0, 1, 1, 1, 0, 1, 0],
        )

    def test_ecss_fig_5_13_standalone_null(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.null()]),
            [0, 1, 1, 1, 0, 1, 0, 0],
        )

    def test_ecss_fig_5_12_control_chars_as_first_char(self) -> None:
        self.assertEqual(encode_sequence([CharType.FCT]), [0, 1, 0, 0])
        self.assertEqual(encode_sequence([CharType.EOP]), [0, 1, 0, 1])
        self.assertEqual(encode_sequence([CharType.EEP]), [0, 1, 1, 0])
        self.assertEqual(encode_sequence([CharType.ESC]), [0, 1, 1, 1])

    def test_parity_spanning_known_answer(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.data(0x01), CharType.FCT]),
            [1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        )

    def test_idle_stream_repeats_null_pattern(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.null(), Encoder.null(), Encoder.null()]),
            [0, 1, 1, 1, 0, 1, 0, 0] * 3,
        )

    def test_time_code_known_answers(self) -> None:
        self.assertEqual(
            encode_sequence([Encoder.time_code(0)]),
            [0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        )
        self.assertEqual(
            encode_sequence([Encoder.time_code(1)]),
            [0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0],
        )

    def test_primary_oracle_decodes_to_data_then_null(self) -> None:
        decoded = Decoder().push(self.PRIMARY_ORACLE)

        self.assertEqual(
            [(character.type, character.value) for character in decoded],
            [(CharType.DATA, 0x5C), (CharType.NULL, None)],
        )
        self.assertTrue(all(character.error is None for character in decoded))
        self.assertTrue(all(character.parity_ok for character in decoded))


class InvariantTests(unittest.TestCase):
    """Standard-stated invariants across value ranges."""

    def test_esc_primitive_content_bits_are_ones(self) -> None:
        self.assertEqual(encode_sequence([CharType.ESC])[2:4], [1, 1])

    def test_time_code_middle_parity_is_always_one(self) -> None:
        for value in range(64):
            with self.subTest(value=value):
                self.assertEqual(encode_sequence([Encoder.time_code(value)])[4], 1)

    def test_broadcast_middle_parity_is_always_one(self) -> None:
        for type_field in range(4):
            for value in range(64):
                with self.subTest(type_field=type_field, value=value):
                    bits = encode_sequence([
                        Encoder.broadcast(value, type_field=type_field)
                    ])
                    self.assertEqual(bits[4], 1)

    def test_null_fct_parity_is_always_zero_in_any_position(self) -> None:
        prefixes: list[list[object]] = [
            [],
            [CharType.FCT],
            [Encoder.data(0x00)],
            [Encoder.data(0xFF), CharType.EOP],
            [Encoder.time_code(0x12), Encoder.broadcast(0x05)],
        ]
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                bits = encode_sequence([*prefix, Encoder.null()])
                self.assertEqual(bits[-4], 0)


class PropertyTests(unittest.TestCase):
    """Hypothesis properties for the pure character codec."""

    @settings(max_examples=200)
    @given(st.lists(LEGAL_CHAR_SPECS, max_size=50))
    def test_round_trip_random_legal_sequences(self, specs: list[CharSpec]) -> None:
        bits = encode_sequence([character_from_spec(spec) for spec in specs])
        decoded = Decoder().push(bits)

        self.assertEqual(
            [(character.type, character.value) for character in decoded],
            [expected_from_spec(spec) for spec in specs],
        )
        self.assertTrue(all(character.error is None for character in decoded))

    @settings(max_examples=200)
    @given(st.lists(LEGAL_CHAR_SPECS, min_size=1, max_size=50))
    def test_encoder_streams_decode_with_valid_odd_parity(
        self,
        specs: list[CharSpec],
    ) -> None:
        bits = encode_sequence([character_from_spec(spec) for spec in specs])
        decoded = Decoder().push(bits)

        self.assertTrue(all(character.parity_ok for character in decoded))

    @settings(max_examples=50)
    @given(st.lists(LEGAL_CHAR_SPECS, min_size=2, max_size=20))
    def test_single_covered_bit_flip_reports_parity_error(
        self,
        specs: list[CharSpec],
    ) -> None:
        original_items = [character_from_spec(spec) for spec in specs]
        original_bits = encode_sequence(original_items)
        stream_with_sentinel = encode_sequence([*original_items, Encoder.data(0x00)])

        for index in covered_indices(stream_with_sentinel, len(original_bits)):
            corrupted = list(stream_with_sentinel)
            corrupted[index] ^= 1
            self.assertTrue(
                has_parity_error(corrupted),
                f"no parity error after flipping covered bit {index}",
            )

    @settings(max_examples=100)
    @given(
        type_field=st.integers(min_value=0, max_value=3),
        value=st.integers(min_value=0, max_value=63),
    )
    def test_broadcast_type_field_semantics_are_explicit(
        self,
        type_field: int,
        value: int,
    ) -> None:
        decoded = Decoder().push(
            encode_sequence([Encoder.broadcast(value, type_field=type_field)])
        )

        self.assertEqual(len(decoded), 1)
        if type_field == 0b00:
            self.assertEqual(decoded[0].type, CharType.TIMECODE)
            self.assertIsNone(decoded[0].error)
        elif type_field == 0b10:
            self.assertEqual(decoded[0].type, CharType.BROADCAST)
            self.assertIsNone(decoded[0].error)
        else:
            self.assertEqual(decoded[0].type, CharType.BROADCAST)
            self.assertIn("reserved_escape_data_type", decoded[0].error or "")


class FakeBitSource:
    """Tiny async source for strict monitor tests."""

    def __init__(self, bits: Iterable[int]) -> None:
        self._bits = list(bits)

    async def get_bit(self) -> int:
        """Return the next bit or fail if the monitor reads past the test stream."""

        await asyncio.sleep(0)
        if not self._bits:
            raise AssertionError("monitor read past expected test stream")
        return self._bits.pop(0)


class StrictAndInjectionTests(unittest.IsolatedAsyncioTestCase):
    """Strict monitor and explicit error-injection coverage."""

    async def test_strict_monitor_raises_on_forced_bad_parity(self) -> None:
        encoder = Encoder()
        encoder.force_bad_parity()
        monitor = CharMonitor(FakeBitSource(encoder.encode(Encoder.data(0x33))), strict=True)

        with self.assertRaises(ParityError):
            await monitor.run()

    async def test_strict_monitor_raises_on_escape_error(self) -> None:
        for illegal in (CharType.EOP, CharType.ESC):
            with self.subTest(illegal=illegal):
                monitor = CharMonitor(
                    FakeBitSource(encode_sequence([CharType.ESC, illegal])),
                    strict=True,
                )

                with self.assertRaises(EscapeError):
                    await monitor.run()

    def test_force_bad_parity_null_corrupts_first_primitive(self) -> None:
        encoder = Encoder()
        encoder.force_bad_parity(offset=0)
        decoded = Decoder().push(encoder.encode(Encoder.null()))

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].type, CharType.NULL)
        self.assertIn("parity_error", decoded[0].error or "")
        self.assertEqual(decoded[0].parity_checks[0].bit_index, 0)

    def test_force_bad_parity_time_code_corrupts_first_primitive(self) -> None:
        encoder = Encoder()
        encoder.force_bad_parity(offset=0)
        decoded = Decoder().push(encoder.encode(Encoder.time_code(0x12)))

        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].type, CharType.TIMECODE)
        self.assertIn("parity_error", decoded[0].error or "")
        self.assertEqual(decoded[0].parity_checks[0].bit_index, 0)


if __name__ == "__main__":
    unittest.main()
