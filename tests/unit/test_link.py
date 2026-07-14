"""Unit tests for the character parser inside ``SpaceWireLink``.

The parser is the only Link piece that doesn't need a simulator: it is a
pure bit-in / event-out translator wrapping the character decoder.
"""

from __future__ import annotations

import pytest

from cocotbext.spacewire.characters import CharType, Encoder
from cocotbext.spacewire.link import _CharacterParser
from cocotbext.spacewire.link_fsm import Event as FsmEvent


def _capture():
    events: list[FsmEvent] = []
    return events, events.append


def test_null_two_primitives_maps_to_single_got_null():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    bits = enc.encode(Encoder.null())
    parser.feed_bits(bits)
    assert events == [FsmEvent.GOT_NULL]


def test_bare_fct_maps_to_got_fct():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    # A stand-alone FCT after a NULL (need to establish parity chain first).
    parser.feed_bits(enc.encode(Encoder.null()))
    parser.feed_bits(enc.encode(CharType.FCT))
    assert events == [FsmEvent.GOT_NULL, FsmEvent.GOT_FCT]


def test_data_char_maps_to_got_n_char():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    parser.feed_bits(enc.encode(Encoder.null()))
    parser.feed_bits(enc.encode(Encoder.data(0x5A)))
    assert events == [FsmEvent.GOT_NULL, FsmEvent.GOT_N_CHAR]


def test_eop_and_eep_map_to_got_n_char():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    parser.feed_bits(enc.encode(Encoder.null()))
    parser.feed_bits(enc.encode(CharType.EOP))
    parser.feed_bits(enc.encode(CharType.EEP))
    assert events == [
        FsmEvent.GOT_NULL,
        FsmEvent.GOT_N_CHAR,
        FsmEvent.GOT_N_CHAR,
    ]


def test_time_code_maps_to_got_time_code():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    parser.feed_bits(enc.encode(Encoder.null()))
    parser.feed_bits(enc.encode(Encoder.time_code(0x2A)))
    assert events == [FsmEvent.GOT_NULL, FsmEvent.GOT_TIME_CODE]


@pytest.mark.parametrize("bad_follow", [CharType.ESC, CharType.EOP, CharType.EEP])
def test_illegal_escape_sequence_emits_rx_err(bad_follow):
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    parser.feed_bits(enc.encode(Encoder.null()))
    # ESC + illegal follower: escape_error is a hard link error.
    parser.feed_bits(enc.encode(CharType.ESC))
    parser.feed_bits(enc.encode(bad_follow))
    assert FsmEvent.RX_ERR in events


def test_parity_error_emits_rx_err():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    parser.feed_bits(enc.encode(Encoder.null()))
    enc.force_bad_parity()  # corrupt parity on the next primitive
    parser.feed_bits(enc.encode(Encoder.data(0x00)))
    assert FsmEvent.RX_ERR in events


def test_null_stream_produces_stream_of_got_null():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    enc = Encoder()
    for _ in range(4):
        parser.feed_bits(enc.encode(Encoder.null()))
    assert events == [FsmEvent.GOT_NULL] * 4


def test_reset_clears_half_received_character():
    events, sink = _capture()
    parser = _CharacterParser(sink)
    # feed only 2 bits then reset — no event yet, and after reset feeding a
    # full NULL from a fresh encoder must decode cleanly.
    parser.feed_bits([0, 1])
    parser.reset()
    fresh = Encoder()
    parser.feed_bits(fresh.encode(Encoder.null()))
    assert events == [FsmEvent.GOT_NULL]
