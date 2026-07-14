"""Unit tests for SpaceWirePacket (ECSS-E-ST-50-12C §5.4)."""

from __future__ import annotations

import dataclasses

import pytest

from cocotbext.spacewire.packet import SpaceWirePacket


def test_construction_normalizes_list_to_tuple():
    pkt = SpaceWirePacket(data=[1, 2, 3], terminator="EOP")
    assert pkt.data == (1, 2, 3)
    assert pkt.terminator == "EOP"


def test_equality_uses_content():
    a = SpaceWirePacket(data=(1, 2, 3), terminator="EOP")
    b = SpaceWirePacket(data=[1, 2, 3], terminator="EOP")
    c = SpaceWirePacket(data=(1, 2, 3), terminator="EEP")
    assert a == b
    assert a != c


def test_frozen_dataclass_rejects_mutation():
    pkt = SpaceWirePacket.eop([0xAA, 0xBB])
    with pytest.raises(dataclasses.FrozenInstanceError):
        pkt.terminator = "EEP"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        pkt.data = ()  # type: ignore[misc]


def test_len_includes_terminator():
    # §5.5.4e NOTE: EOP/EEP is credit-counted as an N-Char.
    assert len(SpaceWirePacket([1, 2, 3], "EOP")) == 4
    assert len(SpaceWirePacket([], "EEP")) == 1


def test_empty_packet_is_legal():
    # §5.5.4e NOTE: empty packet -> terminator alone still counts.
    pkt = SpaceWirePacket(data=(), terminator="EOP")
    assert pkt.data == ()
    assert len(pkt) == 1


@pytest.mark.parametrize("bad", [-1, 256, 1000, 0x1_0000])
def test_byte_out_of_range_raises_value_error(bad):
    with pytest.raises(ValueError):
        SpaceWirePacket(data=(0x00, bad, 0x01), terminator="EOP")


def test_non_int_byte_raises_value_error():
    with pytest.raises(ValueError):
        SpaceWirePacket(data=(1, "2", 3), terminator="EOP")  # type: ignore[arg-type]


def test_bool_bytes_rejected_even_though_int():
    # bool is a subclass of int; SpaceWire data bytes should be actual ints.
    with pytest.raises(ValueError):
        SpaceWirePacket(data=(True, False), terminator="EOP")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["EOF", "eop", "", "EOP ", "END"])
def test_invalid_terminator_raises_value_error(bad):
    with pytest.raises(ValueError):
        SpaceWirePacket(data=(1,), terminator=bad)  # type: ignore[arg-type]


def test_eop_factory():
    pkt = SpaceWirePacket.eop([0xDE, 0xAD])
    assert pkt == SpaceWirePacket(data=(0xDE, 0xAD), terminator="EOP")


def test_eep_factory():
    pkt = SpaceWirePacket.eep([0xBE, 0xEF])
    assert pkt == SpaceWirePacket(data=(0xBE, 0xEF), terminator="EEP")
