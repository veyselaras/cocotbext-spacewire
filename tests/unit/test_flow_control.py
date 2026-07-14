"""Unit tests for CreditCounter (ECSS-E-ST-50-12C §5.5.4 / §5.5.5)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from cocotbext.spacewire.exceptions import CreditError
from cocotbext.spacewire.flow_control import (
    FCT_CREDIT_UNITS,
    MAX_CREDIT,
    MAX_OUTSTANDING_FCTS,
    CreditCounter,
)


# --------------------------------------------------------------------------- #
# TX credit
# --------------------------------------------------------------------------- #


def test_tx_credit_starts_at_zero():
    # §5.5.4e: transmit credit count starts at 0.
    cc = CreditCounter()
    assert cc.tx_credit == 0
    assert cc.can_send() is False  # §5.5.4d


def test_tx_credit_plus_eight_per_fct_received():
    # §5.5.4e.1
    cc = CreditCounter()
    cc.on_fct_received()
    assert cc.tx_credit == 8
    cc.on_fct_received()
    assert cc.tx_credit == 16


def test_tx_credit_minus_one_per_nchar_sent():
    # §5.5.4e.2
    cc = CreditCounter()
    cc.on_fct_received()  # tx_credit = 8
    for expected in range(7, -1, -1):
        cc.on_nchar_sent()
        assert cc.tx_credit == expected
    assert cc.can_send() is False


def test_tx_credit_blocks_at_zero():
    # §5.5.4d / §5.5.4f: cease sending N-Chars when credit reaches zero.
    cc = CreditCounter()
    assert cc.can_send() is False
    with pytest.raises(RuntimeError):
        # Caller violated the contract by not gating on can_send().
        cc.on_nchar_sent()


def test_tx_credit_reaches_max_after_seven_fcts():
    # §5.5.4h / §5.5.4i: 7 FCTs -> 56 credit.
    cc = CreditCounter()
    for _ in range(MAX_OUTSTANDING_FCTS):
        cc.on_fct_received()
    assert cc.tx_credit == MAX_CREDIT


def test_tx_credit_overflow_raises_and_preserves_state():
    # §5.5.4j / §5.5.5a.2: 8th FCT would push credit past 56 -> credit error.
    cc = CreditCounter()
    for _ in range(MAX_OUTSTANDING_FCTS):
        cc.on_fct_received()
    assert cc.tx_credit == MAX_CREDIT
    with pytest.raises(CreditError) as exc_info:
        cc.on_fct_received()
    assert exc_info.value.reason == "fct_overflow"
    # State must be preserved so the FSM's ErrorReset path can inspect it.
    assert cc.tx_credit == MAX_CREDIT


# --------------------------------------------------------------------------- #
# RX credit
# --------------------------------------------------------------------------- #


def test_rx_credit_starts_at_zero():
    # §5.5.4l: receive credit count starts at 0.
    cc = CreditCounter()
    assert cc.rx_credit == 0


def test_rx_credit_plus_eight_per_fct_sent():
    # §5.5.4l.1
    cc = CreditCounter()
    cc.on_fct_sent()
    assert cc.rx_credit == 8
    cc.on_fct_sent()
    assert cc.rx_credit == 16


def test_rx_credit_minus_one_per_nchar_received():
    # §5.5.4l.2
    cc = CreditCounter()
    cc.on_fct_sent()  # rx_credit = 8
    for expected in range(7, -1, -1):
        cc.on_nchar_received()
        assert cc.rx_credit == expected


def test_rx_credit_underflow_raises_credit_error():
    # §5.5.5a.1: N-Char received while rx_credit == 0 -> credit error.
    cc = CreditCounter()
    with pytest.raises(CreditError) as exc_info:
        cc.on_nchar_received()
    assert exc_info.value.reason == "nchar_with_zero_rx_credit"
    assert cc.rx_credit == 0


def test_rx_credit_caps_at_max():
    # §5.5.4n / §5.5.4o: max_rx_credit is a hard cap.
    cc = CreditCounter()
    for _ in range(MAX_OUTSTANDING_FCTS):
        cc.on_fct_sent()
    assert cc.rx_credit == MAX_CREDIT
    with pytest.raises(RuntimeError):
        cc.on_fct_sent()
    assert cc.rx_credit == MAX_CREDIT


def test_rx_credit_can_use_lowered_cap():
    # §5.5.4o: max_rx_credit may be lower than 56 for small RX FIFOs.
    cc = CreditCounter(max_rx_credit=16)
    cc.on_fct_sent()
    cc.on_fct_sent()
    assert cc.rx_credit == 16
    with pytest.raises(RuntimeError):
        cc.on_fct_sent()


# --------------------------------------------------------------------------- #
# reset()
# --------------------------------------------------------------------------- #


def test_reset_zeros_both_counters_from_various_states():
    # §5.5.4g / §5.5.4m: entry to ErrorReset zeros both counters.
    cc = CreditCounter()
    cc.reset()
    assert (cc.tx_credit, cc.rx_credit) == (0, 0)

    cc.on_fct_received()
    cc.on_fct_sent()
    cc.reset()
    assert (cc.tx_credit, cc.rx_credit) == (0, 0)

    for _ in range(MAX_OUTSTANDING_FCTS):
        cc.on_fct_received()
        cc.on_fct_sent()
    cc.on_nchar_sent()
    cc.on_nchar_received()
    cc.reset()
    assert (cc.tx_credit, cc.rx_credit) == (0, 0)


# --------------------------------------------------------------------------- #
# should_send_fct — §5.5.4p decision table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "headroom, rx_credit, expected",
    [
        (0, 0, False),  # no headroom, no credit         -> No
        (7, 0, False),  # sub-8 headroom                 -> No
        (8, 0, True),   # exactly meets both             -> Yes
        (16, 8, True),  # comfortable headroom + credit  -> Yes
        (8, 48, True),  # rx_credit == max-8 boundary    -> Yes
        (8, 49, False), # rx_credit > max-8              -> No
        (8, 56, False), # rx_credit at max               -> No
        (100, 0, True), # huge headroom                  -> Yes
    ],
)
def test_should_send_fct_decision_table(headroom, rx_credit, expected):
    cc = CreditCounter()
    cc.rx_credit = rx_credit
    assert cc.should_send_fct(headroom) is expected


def test_should_send_fct_honours_lowered_max():
    cc = CreditCounter(max_rx_credit=16)
    cc.rx_credit = 8  # exactly max - 8
    assert cc.should_send_fct(8) is True
    cc.rx_credit = 9
    assert cc.should_send_fct(8) is False


# --------------------------------------------------------------------------- #
# initial_fct_count — §5.5.4k
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "capacity, expected",
    [
        (0, 0),
        (7, 0),
        (8, 1),
        (16, 2),
        (56, 7),
        (57, 7),
        (1000, 7),
    ],
)
def test_initial_fct_count(capacity, expected):
    assert CreditCounter.initial_fct_count(capacity) == expected


def test_initial_fct_count_rejects_negative():
    with pytest.raises(ValueError):
        CreditCounter.initial_fct_count(-1)


# --------------------------------------------------------------------------- #
# Property-based test: invariants under random legal sequences
# --------------------------------------------------------------------------- #


_OPS = st.sampled_from(["fct_recv", "nchar_sent", "fct_sent", "nchar_recv"])


@settings(max_examples=200, deadline=None)
@given(
    ops=st.lists(_OPS, min_size=0, max_size=200),
    max_rx=st.integers(min_value=FCT_CREDIT_UNITS, max_value=MAX_CREDIT),
)
def test_counter_invariants_under_random_legal_sequences(ops, max_rx):
    # Ensure max_rx is a multiple of 8 so on_fct_sent guards align with the
    # cap; §5.5.4o allows any value but our on_fct_sent contract expects the
    # caller to gate on should_send_fct (headroom + rx_credit + 8 <= max).
    max_rx -= max_rx % FCT_CREDIT_UNITS
    if max_rx == 0:
        max_rx = FCT_CREDIT_UNITS
    cc = CreditCounter(max_rx_credit=max_rx)

    for op in ops:
        if op == "fct_recv":
            if cc.tx_credit + FCT_CREDIT_UNITS > MAX_CREDIT:
                continue  # guard: caller wouldn't push past §5.5.4h
            cc.on_fct_received()
        elif op == "nchar_sent":
            if not cc.can_send():
                continue  # guard: §5.5.4d / §5.5.4f
            cc.on_nchar_sent()
        elif op == "fct_sent":
            if not cc.should_send_fct(rx_buffer_headroom_nchars=FCT_CREDIT_UNITS):
                continue  # guard: §5.5.4p
            cc.on_fct_sent()
        else:  # nchar_recv
            if cc.rx_credit == 0:
                continue  # guard: §5.5.5a.1
            cc.on_nchar_received()

        assert 0 <= cc.tx_credit <= MAX_CREDIT  # §5.5.4h
        assert 0 <= cc.rx_credit <= cc.max_rx_credit  # §5.5.4n / §5.5.4o
