"""Unit tests for the SpaceWire link initialization FSM (Rev.1)."""

from __future__ import annotations

import pytest

from cocotbext.spacewire.link_fsm import (
    INITIAL_BIT_RATE_BPS,
    Event,
    LinkFSM,
    State,
    TxMode,
)

TARGET_BPS = 200_000_000


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _new(**kwargs) -> LinkFSM:
    return LinkFSM(target_bit_rate_bps=kwargs.pop("target", TARGET_BPS), **kwargs)


def _feed(fsm: LinkFSM, *events: Event) -> None:
    for event in events:
        fsm.feed(event)


def _to_error_wait(fsm: LinkFSM) -> None:
    fsm.feed(Event.TICK_6_4US)
    assert fsm.state is State.ERROR_WAIT


def _to_ready(fsm: LinkFSM) -> None:
    _to_error_wait(fsm)
    fsm.feed(Event.TICK_12_8US)
    # If ``link_start`` was latched by an earlier handshake the FSM
    # legitimately skips Ready and lands directly in Started.
    assert fsm.state in (State.READY, State.STARTED)


def _to_started(fsm: LinkFSM) -> None:
    _to_ready(fsm)
    if fsm.state is State.READY:
        fsm.feed(Event.LINK_START)
    assert fsm.state is State.STARTED


def _to_connecting(fsm: LinkFSM) -> None:
    _to_started(fsm)
    fsm.feed(Event.SENT_NULL)
    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.CONNECTING


def _to_run(fsm: LinkFSM) -> None:
    _to_connecting(fsm)
    fsm.feed(Event.SENT_FCT)
    fsm.feed(Event.GOT_FCT)
    assert fsm.state is State.RUN


def _fsm_in_state(state: State) -> LinkFSM:
    fsm = _new()
    if state is State.ERROR_RESET:
        return fsm
    if state is State.ERROR_WAIT:
        _to_error_wait(fsm)
    elif state is State.READY:
        _to_ready(fsm)
    elif state is State.STARTED:
        _to_started(fsm)
    elif state is State.CONNECTING:
        _to_connecting(fsm)
    elif state is State.RUN:
        _to_run(fsm)
    else:  # pragma: no cover
        raise ValueError(state)
    return fsm


# --------------------------------------------------------------------------- #
# 1. Initial state
# --------------------------------------------------------------------------- #


def test_initial_state():
    fsm = _new()
    out = fsm.output()
    assert out.state is State.ERROR_RESET
    assert out.tx_mode is TxMode.RESET
    assert out.rx_enabled is False
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS


# --------------------------------------------------------------------------- #
# 2. ErrorReset -> ErrorWait
# --------------------------------------------------------------------------- #


def test_error_reset_to_error_wait_on_6_4us():
    fsm = _new()
    out = fsm.feed(Event.TICK_6_4US)
    assert out.state is State.ERROR_WAIT
    assert out.tx_mode is TxMode.RESET  # still silent on the wire
    assert out.rx_enabled is True
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS


# --------------------------------------------------------------------------- #
# 3. ErrorReset with link_disable
# --------------------------------------------------------------------------- #


def test_error_reset_stays_when_link_disabled():
    fsm = _new()
    fsm.feed(Event.LINK_DISABLE)
    fsm.feed(Event.TICK_6_4US)
    assert fsm.state is State.ERROR_RESET
    # Clearing the disable flag re-enables the exit path.
    fsm.feed(Event.LINK_DISABLE_CLEAR)
    fsm.feed(Event.TICK_6_4US)
    assert fsm.state is State.ERROR_WAIT


# --------------------------------------------------------------------------- #
# 4. ErrorWait -> Ready
# --------------------------------------------------------------------------- #


def test_error_wait_to_ready_on_12_8us():
    fsm = _fsm_in_state(State.ERROR_WAIT)
    out = fsm.feed(Event.TICK_12_8US)
    assert out.state is State.READY
    assert out.tx_mode is TxMode.RESET
    assert out.rx_enabled is True


# --------------------------------------------------------------------------- #
# 5. ErrorWait error conditions -> ErrorReset (incl. gotNULL gate)
# --------------------------------------------------------------------------- #


def test_error_wait_rx_err_to_error_reset():
    fsm = _fsm_in_state(State.ERROR_WAIT)
    fsm.feed(Event.RX_ERR)
    assert fsm.state is State.ERROR_RESET


@pytest.mark.parametrize(
    "trigger", [Event.GOT_FCT, Event.GOT_N_CHAR, Event.GOT_TIME_CODE]
)
def test_error_wait_after_got_null_triggers_error(trigger):
    fsm = _fsm_in_state(State.ERROR_WAIT)
    fsm.feed(Event.GOT_NULL)
    fsm.feed(trigger)
    assert fsm.state is State.ERROR_RESET


@pytest.mark.parametrize(
    "trigger", [Event.GOT_FCT, Event.GOT_N_CHAR, Event.GOT_TIME_CODE]
)
def test_error_wait_without_got_null_ignores_char_events(trigger):
    fsm = _fsm_in_state(State.ERROR_WAIT)
    fsm.feed(trigger)
    assert fsm.state is State.ERROR_WAIT


def test_got_null_latch_cleared_on_error_reset_reentry():
    fsm = _fsm_in_state(State.ERROR_WAIT)
    fsm.feed(Event.GOT_NULL)
    fsm.feed(Event.RX_ERR)  # drop to ErrorReset -> latch must clear
    assert fsm.state is State.ERROR_RESET

    # Come back to ErrorWait: an unqualified FCT must once again be ignored.
    fsm.feed(Event.TICK_6_4US)
    assert fsm.state is State.ERROR_WAIT
    fsm.feed(Event.GOT_FCT)
    assert fsm.state is State.ERROR_WAIT


# --------------------------------------------------------------------------- #
# 6. Ready -> Started via LinkEnabled combinations
# --------------------------------------------------------------------------- #


def test_ready_to_started_via_link_start():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.LINK_START)
    assert fsm.state is State.STARTED


def test_ready_to_started_via_auto_start_and_got_null():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.AUTO_START)
    assert fsm.state is State.READY  # still waiting for NULL from peer
    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.STARTED


def test_ready_auto_start_without_got_null_stays():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.AUTO_START)
    assert fsm.state is State.READY


def test_ready_link_disable_blocks_link_start():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.LINK_DISABLE)
    fsm.feed(Event.LINK_START)
    assert fsm.state is State.READY


def test_ready_link_disable_blocks_auto_start():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.LINK_DISABLE)
    fsm.feed(Event.AUTO_START)
    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.READY


def test_ready_stays_when_nothing_enables_link():
    fsm = _fsm_in_state(State.READY)
    # Idle events must not lift the link.
    _feed(fsm, Event.SENT_NULL, Event.SENT_FCT)
    assert fsm.state is State.READY


def test_ready_rx_err_to_error_reset():
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.RX_ERR)
    assert fsm.state is State.ERROR_RESET


# --------------------------------------------------------------------------- #
# 7. Started TX behaviour
# --------------------------------------------------------------------------- #


def test_started_output():
    fsm = _fsm_in_state(State.STARTED)
    out = fsm.output()
    assert out.tx_mode is TxMode.SEND_NULLS
    assert out.rx_enabled is True
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS


# --------------------------------------------------------------------------- #
# 8. Started -> Connecting requires sentNULL AND gotNULL (Rev.1 gate)
# --------------------------------------------------------------------------- #


def test_started_got_null_alone_stays():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.STARTED


def test_started_sent_null_alone_stays():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.SENT_NULL)
    assert fsm.state is State.STARTED


def test_started_sent_then_got_null_advances():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.SENT_NULL)
    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.CONNECTING


def test_started_got_then_sent_null_advances():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.GOT_NULL)
    fsm.feed(Event.SENT_NULL)
    assert fsm.state is State.CONNECTING


# --------------------------------------------------------------------------- #
# 9. Started timeout
# --------------------------------------------------------------------------- #


def test_started_timeout_to_error_reset():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.TICK_12_8US)
    assert fsm.state is State.ERROR_RESET


def test_started_rx_err_to_error_reset():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.RX_ERR)
    assert fsm.state is State.ERROR_RESET


# --------------------------------------------------------------------------- #
# 10. Connecting -> Run requires sentFCT AND gotFCT (Rev.1 gate)
# --------------------------------------------------------------------------- #


def test_connecting_sent_then_got_fct_advances():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.SENT_FCT)
    fsm.feed(Event.GOT_FCT)
    assert fsm.state is State.RUN


def test_connecting_got_then_sent_fct_advances():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.GOT_FCT)
    fsm.feed(Event.SENT_FCT)
    assert fsm.state is State.RUN


def test_connecting_sent_fct_alone_stays():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.SENT_FCT)
    assert fsm.state is State.CONNECTING


def test_connecting_got_fct_alone_stays():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.GOT_FCT)
    assert fsm.state is State.CONNECTING


@pytest.mark.parametrize("early", [Event.GOT_N_CHAR, Event.GOT_TIME_CODE])
def test_connecting_n_char_or_time_code_before_fct_errors(early):
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(early)
    assert fsm.state is State.ERROR_RESET


# --------------------------------------------------------------------------- #
# 11. Connecting timeout / errors
# --------------------------------------------------------------------------- #


def test_connecting_timeout_to_error_reset():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.TICK_12_8US)
    assert fsm.state is State.ERROR_RESET


def test_connecting_rx_err_to_error_reset():
    fsm = _fsm_in_state(State.CONNECTING)
    fsm.feed(Event.RX_ERR)
    assert fsm.state is State.ERROR_RESET


# --------------------------------------------------------------------------- #
# 12. Run TX behaviour
# --------------------------------------------------------------------------- #


def test_run_output_and_target_bit_rate():
    fsm = _fsm_in_state(State.RUN)
    out = fsm.output()
    assert out.tx_mode is TxMode.SEND_NULLS_FCTS_NCHARS_TIMECODES
    assert out.rx_enabled is True
    assert out.bit_rate_bps == TARGET_BPS


# --------------------------------------------------------------------------- #
# 13. Run -> ErrorReset triggers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "event", [Event.RX_ERR, Event.CREDIT_ERR, Event.LINK_DISABLE]
)
def test_run_error_paths(event):
    fsm = _fsm_in_state(State.RUN)
    fsm.feed(event)
    assert fsm.state is State.ERROR_RESET


def test_run_normal_traffic_stays():
    fsm = _fsm_in_state(State.RUN)
    _feed(
        fsm,
        Event.GOT_NULL,
        Event.GOT_FCT,
        Event.GOT_N_CHAR,
        Event.GOT_TIME_CODE,
        Event.SENT_NULL,
        Event.SENT_FCT,
    )
    assert fsm.state is State.RUN


# --------------------------------------------------------------------------- #
# 14. got_null latch reset when re-entering ErrorReset
# --------------------------------------------------------------------------- #


def test_second_handshake_requires_fresh_got_null():
    fsm = _fsm_in_state(State.RUN)
    fsm.feed(Event.RX_ERR)
    assert fsm.state is State.ERROR_RESET

    # Rev.1: gotNULL must be observed AGAIN before Started can advance.
    _to_started(fsm)
    fsm.feed(Event.SENT_NULL)
    # No fresh GOT_NULL -> must stay in Started, not sneak into Connecting
    # off the previous handshake's latched flag.
    assert fsm.state is State.STARTED

    fsm.feed(Event.GOT_NULL)
    assert fsm.state is State.CONNECTING


def test_sent_null_from_previous_visit_does_not_leak():
    # SENT_NULL is only latched while _in_ Started.  Feeding it in Ready
    # must not cause the next Started visit to auto-advance.
    fsm = _fsm_in_state(State.READY)
    fsm.feed(Event.SENT_NULL)
    fsm.feed(Event.LINK_START)
    assert fsm.state is State.STARTED
    fsm.feed(Event.GOT_NULL)
    # Would advance if SENT_NULL had leaked in from Ready.
    assert fsm.state is State.STARTED


def test_sent_null_across_reset_does_not_leak():
    fsm = _fsm_in_state(State.STARTED)
    fsm.feed(Event.SENT_NULL)
    fsm.feed(Event.TICK_12_8US)  # drop to ErrorReset
    assert fsm.state is State.ERROR_RESET

    _to_started(fsm)
    fsm.feed(Event.GOT_NULL)  # only got_null this time
    assert fsm.state is State.STARTED  # sent_null_in_started was cleared


# --------------------------------------------------------------------------- #
# 15. End-to-end handshake
# --------------------------------------------------------------------------- #


def test_full_handshake_walkthrough():
    fsm = _new()

    out = fsm.output()
    assert out.state is State.ERROR_RESET
    assert out.tx_mode is TxMode.RESET
    assert out.rx_enabled is False
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS

    out = fsm.feed(Event.TICK_6_4US)
    assert out.state is State.ERROR_WAIT
    assert out.tx_mode is TxMode.RESET
    assert out.rx_enabled is True

    out = fsm.feed(Event.TICK_12_8US)
    assert out.state is State.READY
    assert out.tx_mode is TxMode.RESET
    assert out.rx_enabled is True

    out = fsm.feed(Event.LINK_START)
    assert out.state is State.STARTED
    assert out.tx_mode is TxMode.SEND_NULLS
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS

    fsm.feed(Event.SENT_NULL)
    out = fsm.feed(Event.GOT_NULL)
    assert out.state is State.CONNECTING
    assert out.tx_mode is TxMode.SEND_NULLS_FCTS
    assert out.bit_rate_bps == INITIAL_BIT_RATE_BPS

    fsm.feed(Event.SENT_FCT)
    out = fsm.feed(Event.GOT_FCT)
    assert out.state is State.RUN
    assert out.tx_mode is TxMode.SEND_NULLS_FCTS_NCHARS_TIMECODES
    assert out.rx_enabled is True
    assert out.bit_rate_bps == TARGET_BPS


# --------------------------------------------------------------------------- #
# Parametrized transition-table smoke coverage
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "start_state, event, expected_state",
    [
        (State.ERROR_RESET, Event.TICK_6_4US, State.ERROR_WAIT),
        (State.ERROR_WAIT, Event.TICK_12_8US, State.READY),
        (State.ERROR_WAIT, Event.RX_ERR, State.ERROR_RESET),
        (State.READY, Event.LINK_START, State.STARTED),
        (State.READY, Event.RX_ERR, State.ERROR_RESET),
        (State.STARTED, Event.TICK_12_8US, State.ERROR_RESET),
        (State.STARTED, Event.RX_ERR, State.ERROR_RESET),
        (State.CONNECTING, Event.TICK_12_8US, State.ERROR_RESET),
        (State.CONNECTING, Event.RX_ERR, State.ERROR_RESET),
        (State.CONNECTING, Event.CREDIT_ERR, State.ERROR_RESET),
        (State.CONNECTING, Event.GOT_N_CHAR, State.ERROR_RESET),
        (State.CONNECTING, Event.GOT_TIME_CODE, State.ERROR_RESET),
        (State.RUN, Event.RX_ERR, State.ERROR_RESET),
        (State.RUN, Event.CREDIT_ERR, State.ERROR_RESET),
        (State.RUN, Event.LINK_DISABLE, State.ERROR_RESET),
    ],
)
def test_transition_table(start_state, event, expected_state):
    fsm = _fsm_in_state(start_state)
    fsm.feed(event)
    assert fsm.state is expected_state
