"""SpaceWire link integration layer.

``SpaceWireLink`` binds the pure-Python :class:`~.link_fsm.LinkFSM` to real
signals: it feeds character events from the monitor into the FSM, drives the
TX encoder according to the FSM's current ``tx_mode``, and manages the 6.4
microsecond / 12.8 microsecond initialization timers.

The FSM stays untouched; this module is pure orchestration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

import cocotb
from cocotb.handle import SimHandleBase
from cocotb.queue import Queue as CocoQueue, QueueEmpty as CocoQueueEmpty
from cocotb.task import Task
from cocotb.triggers import (
    Event as CocoEvent,
    SimTimeoutError,
    Timer,
    with_timeout,
)

from .bus import SpaceWireBus
from .characters import Character, CharType, Decoder, Encoder, error_types
from .driver import SpaceWireDriver
from .exceptions import CreditError
from .flow_control import CreditCounter, MAX_CREDIT
from .link_fsm import Event as FsmEvent, LinkFSM, Output, State, TxMode
from .monitor import SpaceWireMonitor
from .packet import SpaceWirePacket, Terminator

__all__ = ["SpaceWireLink"]

_LOG = logging.getLogger(__name__)

_TICK_6_4_NS = 6400
_TICK_12_8_NS = 12800
_DISCONNECT_TIMEOUT_NS = 850.0


class _CharacterParser:
    """Bit-stream to FSM-event translator.

    Wraps :class:`~.characters.Decoder` and maps each decoded high-level
    character to the matching :class:`~.link_fsm.Event`.  Parity errors,
    illegal escape sequences, and other decoded errors are translated to a
    single ``RX_ERR`` event.
    """

    def __init__(
        self,
        on_event: Callable[[FsmEvent], None],
        on_character: Callable[[Character], None] | None = None,
    ) -> None:
        self._on_event = on_event
        self._on_character = on_character
        self._decoder = Decoder()

    def reset(self) -> None:
        """Drop any half-received character and clear parity state."""

        self._decoder.reset()

    def feed_bit(self, bit: int) -> None:
        """Consume one line bit; emit FSM events for each completed character."""

        for character in self._decoder.feed(bit):
            self._dispatch(character)

    def feed_bits(self, bits: list[int]) -> None:
        """Convenience batch entry point (used by unit tests)."""

        for bit in bits:
            self.feed_bit(bit)

    def _dispatch(self, character: Character) -> None:
        if character.error is not None and _is_hard_error(character.error):
            self._on_event(FsmEvent.RX_ERR)
            return

        char_type = character.type
        if char_type is CharType.NULL:
            self._on_event(FsmEvent.GOT_NULL)
        elif char_type is CharType.FCT:
            self._on_event(FsmEvent.GOT_FCT)
        elif char_type in (CharType.DATA, CharType.EOP, CharType.EEP):
            self._on_event(FsmEvent.GOT_N_CHAR)
        elif char_type in (CharType.TIMECODE, CharType.BROADCAST):
            self._on_event(FsmEvent.GOT_TIME_CODE)
        elif char_type is CharType.ESC:
            # A bare ESC without a following primitive should never surface;
            # the decoder holds it pending.  Treat any such leak as noise.
            _LOG.debug("parser saw bare ESC; ignoring")
            return

        # Also expose the raw character to the link layer so it can run
        # credit accounting (§5.5.4l / §5.5.5) and feed the RX packet queue.
        if self._on_character is not None:
            self._on_character(character)


def _is_hard_error(error: str) -> bool:
    """Return True for character errors that must reset the link."""

    return any(kind in error_types(error) for kind in ("parity_error", "escape_error"))


class SpaceWireLink:
    """Bind ``SpaceWireDriver`` + ``SpaceWireMonitor`` + ``LinkFSM`` together."""

    def __init__(
        self,
        entity: SimHandleBase,
        *,
        prefix: str = "spw",
        bit_rate: int = 200_000_000,
        link_start: bool = False,
        auto_start: bool = False,
        only_nulls: bool = False,
        rx_fifo_capacity: int = MAX_CREDIT,
        max_rx_credit: int | None = None,
    ) -> None:
        self._bus = SpaceWireBus(entity, prefix=prefix)
        self._driver = SpaceWireDriver(self._bus, bitrate_mbps=10.0)
        self._monitor = SpaceWireMonitor(
            self._bus,
            disconnect_timeout_ns=_DISCONNECT_TIMEOUT_NS,
        )
        self._fsm = LinkFSM(target_bit_rate_bps=bit_rate)
        self._parser = _CharacterParser(
            on_event=self._feed_fsm,
            on_character=self._on_rx_character,
        )
        self._encoder = Encoder()

        # §5.5.4o: cap rx_credit at min(fifo_capacity, MAX_CREDIT).  A user
        # may lower this via ``max_rx_credit`` for tests that want a small
        # window; otherwise default to the FIFO capacity.
        capped_max = min(rx_fifo_capacity, MAX_CREDIT)
        self._credit = CreditCounter(
            max_rx_credit=capped_max if max_rx_credit is None else min(max_rx_credit, capped_max)
        )
        self.rx_fifo_capacity_nchars = rx_fifo_capacity
        self._tx_nchar_queue: CocoQueue[Character] = CocoQueue()
        self._rx_nchar_queue: CocoQueue[Character] = CocoQueue()
        # Outstanding TX N-Chars: bumped on enqueue, dropped on emit.  The
        # drained event flips off while any are in flight and back on when
        # the last one has been driven — that's how send_packet blocks on
        # a §5.5.4d credit-limited stall.
        self._tx_nchar_outstanding = 0
        self._tx_drained_evt = CocoEvent("spw_tx_drained")
        self._tx_drained_evt.set()

        self._run_reached = CocoEvent("spw_link_run_reached")
        self._error_reset_entered = CocoEvent("spw_link_error_reset")
        self._tx_wakeup = CocoEvent("spw_link_tx_wakeup")

        self._timer_task: Task[None] | None = None
        self._timer_gen = 0
        self._tx_task: Task[None] | None = None
        self._disconnect_task: Task[None] | None = None
        self._error_reset_count = 0
        self._only_nulls = only_nulls
        self._fct_sent_this_connecting = False
        self._started = False
        self._pending_link_start = link_start
        self._pending_auto_start = auto_start
        self._pending_initial_fct_burst = 0  # §5.5.4k; recomputed on Run entry

    # --- introspection helpers ------------------------------------------

    @property
    def state(self) -> State:
        """Current FSM state."""

        return self._fsm.state

    @property
    def bit_rate_bps(self) -> int:
        """Current line bit rate in bits per second."""

        return int(self._driver._bitrate_mbps * 1_000_000)

    @property
    def error_reset_count(self) -> int:
        """Number of times ``ErrorReset`` has been (re-)entered since start."""

        return self._error_reset_count

    @property
    def tx_credit(self) -> int:
        """Current transmit credit (§5.5.4e)."""

        return self._credit.tx_credit

    @property
    def rx_credit(self) -> int:
        """Current receive credit (§5.5.4l)."""

        return self._credit.rx_credit

    @property
    def tx_queue_depth(self) -> int:
        """Number of N-Chars queued for transmission but not yet emitted."""

        return self._tx_nchar_queue.qsize()

    @property
    def rx_queue_depth(self) -> int:
        """Number of received N-Chars not yet consumed by ``recv_packet``."""

        return self._rx_nchar_queue.qsize()

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Bring the driver / monitor / FSM online (does not wait for Run)."""

        if self._started:
            raise RuntimeError("SpaceWireLink is already started")
        self._started = True

        self._driver.start()
        # Let the driver's initial D=0/S=0 drive propagate before the RX
        # monitor arms; otherwise a stale line level left over from an
        # earlier link (or from an X->0 transient at t=0) can register as a
        # spurious first bit and desynchronise the parity chain.
        await Timer(1, unit="ns")
        self._monitor.on_bit(self._parser.feed_bit)
        self._monitor.start()

        # Immediately arm the ErrorReset 6.4us timer for the initial state.
        self._start_timer(_TICK_6_4_NS, FsmEvent.TICK_6_4US)

        self._tx_task = cocotb.start_soon(self._tx_loop())
        self._disconnect_task = cocotb.start_soon(self._disconnect_watchdog())

        if self._pending_auto_start:
            self._feed_fsm(FsmEvent.AUTO_START)
        if self._pending_link_start:
            self._feed_fsm(FsmEvent.LINK_START)

    async def connect(self, timeout_us: int = 100) -> None:
        """Return when the FSM reaches Run, or raise ``TimeoutError``."""

        if not self._started:
            await self.start()

        if self._fsm.state is State.RUN:
            return

        try:
            await with_timeout(self._run_reached.wait(), timeout_us, "us")
        except SimTimeoutError as exc:
            raise TimeoutError(
                f"SpaceWire link did not reach Run within {timeout_us} us "
                f"(state={self._fsm.state.name})"
            ) from exc

    async def disconnect(self) -> None:
        """Drop the link into ErrorReset and stop TX from generating traffic."""

        self._feed_fsm(FsmEvent.LINK_DISABLE)
        # Give the peer time to observe the disconnect timeout before the
        # caller tears the test down.
        await Timer(1, unit="ns")

    def stop(self) -> None:
        """Stop background tasks; safe to call multiple times."""

        self._cancel_timer()
        for task_attr in ("_tx_task", "_disconnect_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.kill()
                setattr(self, task_attr, None)
        self._driver.stop()
        self._monitor.stop()
        self._started = False

    async def wait_for_error_reset(self, count: int = 1, timeout_us: int = 200) -> None:
        """Wait until ``ErrorReset`` has been re-entered ``count`` times."""

        deadline_left = count
        while deadline_left > 0:
            self._error_reset_entered.clear()
            try:
                await with_timeout(
                    self._error_reset_entered.wait(), timeout_us, "us"
                )
            except SimTimeoutError as exc:
                raise TimeoutError(
                    f"only observed {count - deadline_left} ErrorReset "
                    f"re-entries within {timeout_us} us"
                ) from exc
            deadline_left -= 1

    # --- FSM plumbing ---------------------------------------------------

    def _feed_fsm(self, event: FsmEvent) -> None:
        prev_state = self._fsm.state
        out = self._fsm.feed(event)
        if out.state is not prev_state:
            self._on_state_change(prev_state, out)

    def _on_state_change(self, prev: State, out: Output) -> None:
        _LOG.debug("link state %s -> %s (tx=%s)", prev.name, out.state.name, out.tx_mode.name)
        new = out.state

        # Timer schedule per Rev.1 sec. 8.5.
        self._cancel_timer()
        if new is State.ERROR_RESET:
            self._start_timer(_TICK_6_4_NS, FsmEvent.TICK_6_4US)
        elif new in (State.ERROR_WAIT, State.STARTED, State.CONNECTING):
            self._start_timer(_TICK_12_8_NS, FsmEvent.TICK_12_8US)

        # Update raw bit rate (10 Mbit before Run, target in Run).
        self._driver._bitrate_mbps = out.bit_rate_bps / 1_000_000.0

        # Reset codecs on any drop back to ErrorReset so a fresh handshake
        # starts with a clean parity chain and no half-parsed character.
        if new is State.ERROR_RESET:
            self._encoder.reset()
            self._parser.reset()
            # §5.5.4g / §5.5.4m: entry to ErrorReset zeros both credit counts.
            self._credit.reset()
            self._error_reset_count += 1
            self._error_reset_entered.set()

        if new is State.CONNECTING:
            self._fct_sent_this_connecting = False

        if new is State.RUN:
            # §5.5.4k: at link init / re-init, send one FCT per 8 RX slots
            # (capped at 7).  The Run-mode _next_char() implements this
            # implicitly via should_send_fct() (§5.5.4p): with rx_credit
            # already at 8 from the Connecting-phase handshake FCT and
            # full RX headroom, the loop emits (initial_fct_count - 1)
            # additional FCTs, so the peer receives exactly
            # initial_fct_count in total.
            self._pending_initial_fct_burst = CreditCounter.initial_fct_count(
                self.rx_fifo_capacity_nchars
            )
            self._run_reached.set()

        # Wake the TX loop so it can react to the new tx_mode.
        self._tx_wakeup.set()

    # --- TX side --------------------------------------------------------

    async def _tx_loop(self) -> None:
        """Feed the driver with characters selected by the FSM's tx_mode."""

        while True:
            mode = self._fsm.output().tx_mode

            if mode is TxMode.RESET:
                # Drain wake-up event and wait for a state change.
                self._tx_wakeup.clear()
                await self._tx_wakeup.wait()
                continue

            char, was_nchar_from_queue = self._next_char(mode)
            bits = self._encoder.encode(char)
            await self._driver.send_bits_and_wait(bits)

            if char.type is CharType.NULL:
                self._feed_fsm(FsmEvent.SENT_NULL)
            elif char.type is CharType.FCT:
                self._feed_fsm(FsmEvent.SENT_FCT)
            if was_nchar_from_queue:
                self._tx_nchar_outstanding -= 1
                if self._tx_nchar_outstanding == 0:
                    self._tx_drained_evt.set()

    def _next_char(self, mode: TxMode) -> tuple[Character, bool]:
        """Pick the next primitive to transmit; second tuple element is True
        iff the char was popped from the user's TX N-Char queue (so the
        outer loop knows to mark it done)."""

        if mode is TxMode.SEND_NULLS:
            return Encoder.null(), False

        if mode is TxMode.SEND_NULLS_FCTS:
            # Connecting handshake (Rev.1 §8.5.3.5): emit exactly one FCT,
            # then NULLs.  The handshake FCT is a real FCT on the wire so
            # §5.5.4l.1 credits it toward rx_credit — the receiver will
            # decrement it on the first N-Char they send us in Run.
            if self._only_nulls:
                return Encoder.null(), False
            if not self._fct_sent_this_connecting:
                self._fct_sent_this_connecting = True
                try:
                    self._credit.on_fct_sent()  # §5.5.4l.1 for handshake FCT
                except RuntimeError:  # pragma: no cover - capacity < 8
                    pass
                return Character(CharType.FCT), False
            return Encoder.null(), False

        # SEND_NULLS_FCTS_NCHARS_TIMECODES (Run): credit-gated priority order:
        #   1. Pending time-code       (Phase 8 — TODO hook, not implemented)
        #   2. Pending FCT (§5.5.4p)   → +8 rx_credit (§5.5.4l.1)
        #   3. Pending N-Char with tx_credit > 0 (§5.5.4d/f) → −1 tx_credit
        #                                                     (§5.5.4e.2)
        #   4. Otherwise NULL (§5.5.4f: NULLs keep flowing when credit is 0)

        # TODO(Phase 8): time-code TX hook goes here.
        # if self._pending_time_code is not None:
        #     return self._pop_time_code(), False

        headroom = self.rx_fifo_capacity_nchars - self._rx_nchar_queue.qsize()
        if self._credit.should_send_fct(headroom):  # §5.5.4p
            self._credit.on_fct_sent()  # §5.5.4l.1
            return Character(CharType.FCT), False

        if not self._tx_nchar_queue.empty() and self._credit.can_send():  # §5.5.4d
            try:
                char = self._tx_nchar_queue.get_nowait()
            except CocoQueueEmpty:  # pragma: no cover - guarded above
                return Encoder.null(), False
            self._credit.on_nchar_sent()  # §5.5.4e.2 (EOP/EEP counted per NOTE)
            return char, True

        # §5.5.4f: FCTs, NULLs and broadcast codes continue to flow when
        # tx_credit is zero.
        return Encoder.null(), False

    # --- RX character handler (feeds credit accounting and rx queue) ----

    def _on_rx_character(self, character: Character) -> None:
        """Route a decoded high-level character to credit accounting.

        FSM events are already fired by the parser; this hook adds §5.5.4l
        / §5.5.5a bookkeeping and enqueues N-Chars for ``recv_packet``.

        FCTs and N-Chars are only ever legal in Connecting/Run (the FSM
        would have thrown us to ErrorReset otherwise), so we count them
        unconditionally.  This is important for the handshake FCT: if it
        arrives at the parser BEFORE our own ``SENT_FCT`` fires from the
        TX loop, the FSM is still in Connecting when ``_on_character``
        runs — but the FCT is real and must count toward tx_credit.  Any
        stale characters after an unexpected reset are already gated by
        ``credit.reset()`` in ``_on_state_change``.
        """

        char_type = character.type
        if char_type is CharType.FCT:
            try:
                self._credit.on_fct_received()  # §5.5.4e.1
            except CreditError:  # §5.5.5a.2
                self._feed_fsm(FsmEvent.CREDIT_ERR)
                return
            # A new FCT means tx_credit went up; wake TX loop in case it
            # was idling on NULLs while blocked at credit==0.
            self._tx_wakeup.set()
            return

        if char_type in (CharType.DATA, CharType.EOP, CharType.EEP):
            # N-Chars are illegal in Connecting (FSM already handled the
            # error path).  Guard defensively — if we're not in Run yet,
            # discard silently rather than raising a spurious CreditError.
            if self._fsm.state is not State.RUN:
                return
            try:
                self._credit.on_nchar_received()  # §5.5.4l.2 (+ §5.5.5a.1)
            except CreditError:
                self._feed_fsm(FsmEvent.CREDIT_ERR)
                return
            # Deliver to the user-visible packet queue.
            self._rx_nchar_queue.put_nowait(character)
            return

    # --- Packet-layer public API ---------------------------------------

    def _enqueue_nchar(self, char: Character) -> None:
        """Push one N-Char onto the TX queue and bump the drain counter."""

        self._tx_nchar_queue.put_nowait(char)
        self._tx_nchar_outstanding += 1
        self._tx_drained_evt.clear()

    async def send_char(self, char: Character) -> None:
        """Enqueue a single N-Char for transmission and wait until it is
        driven onto the wire (credit permitting)."""

        if char.type not in (CharType.DATA, CharType.EOP, CharType.EEP):
            raise ValueError(
                f"send_char accepts N-Chars only (DATA/EOP/EEP), got {char.type.name}"
            )
        self._enqueue_nchar(char)
        await self._tx_drained_evt.wait()

    async def send_packet(
        self,
        data: Iterable[int],
        terminator: Terminator = "EOP",
    ) -> SpaceWirePacket:
        """Send a full packet: data N-Chars followed by EOP or EEP.

        Blocks naturally when the far end has not granted enough credit
        (§5.5.4d): the TX queue drains only as FCTs arrive.  Returns the
        constructed :class:`SpaceWirePacket` once every N-Char has been
        driven.
        """

        pkt = SpaceWirePacket(data=tuple(data), terminator=terminator)
        for byte in pkt.data:
            self._enqueue_nchar(Character(CharType.DATA, value=byte))
        term_type = CharType.EOP if pkt.terminator == "EOP" else CharType.EEP
        self._enqueue_nchar(Character(term_type))
        await self._tx_drained_evt.wait()
        return pkt

    async def recv_packet(self) -> SpaceWirePacket:
        """Collect N-Chars from the RX queue until EOP or EEP, return packet."""

        data: list[int] = []
        while True:
            char = await self._rx_nchar_queue.get()
            if char.type is CharType.EOP:
                return SpaceWirePacket.eop(data)
            if char.type is CharType.EEP:
                return SpaceWirePacket.eep(data)
            # DATA: value is guaranteed non-None by the decoder.
            data.append(char.value if char.value is not None else 0)

    # --- RX side --------------------------------------------------------

    async def _disconnect_watchdog(self) -> None:
        """Feed ``RX_ERR`` if the peer goes silent AFTER the first NULL.

        Rev.1 (and SpaceWire Light's ``spwrecv.vhd``) only start the
        disconnect timer once the receiver has observed at least one bit
        from the peer.  Before then silence is expected — both endpoints
        are still bringing their transmitters up during ErrorReset /
        ErrorWait / Ready.
        """

        while True:
            await Timer(100, unit="ns")
            if not self._fsm.output().rx_enabled:
                continue
            if not self._fsm.got_null_latched:
                continue
            if self._monitor.disconnected:
                self._feed_fsm(FsmEvent.RX_ERR)

    # --- timer management ----------------------------------------------

    def _start_timer(self, duration_ns: int, event: FsmEvent) -> None:
        self._timer_gen += 1
        gen = self._timer_gen
        self._timer_task = cocotb.start_soon(self._run_timer(duration_ns, event, gen))

    async def _run_timer(self, duration_ns: int, event: FsmEvent, gen: int) -> None:
        await Timer(duration_ns, unit="ns")
        if gen == self._timer_gen:
            self._feed_fsm(event)

    def _cancel_timer(self) -> None:
        self._timer_gen += 1
        task = self._timer_task
        if task is not None:
            try:
                task.kill()
            except Exception:  # pragma: no cover - defensive
                pass
            self._timer_task = None


def _select_signal(entity: SimHandleBase, name: str) -> Any:  # pragma: no cover
    """Reserved for a future custom-signal-name constructor."""

    return getattr(entity, name)
