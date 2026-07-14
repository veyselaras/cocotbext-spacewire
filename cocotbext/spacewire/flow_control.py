"""SpaceWire data-link-layer flow control (ECSS-E-ST-50-12C §5.5.4 / §5.5.5).

Pure Python.  The class is named ``CreditCounter`` because §5.5.4 e/l describe
the mechanism as a pair of arithmetic counters, not a decision policy — the
higher-level TX loop asks it questions (``can_send`` / ``should_send_fct``)
and reports events back (``on_fct_received`` / ``on_nchar_sent`` / ...).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .exceptions import CreditError

__all__ = [
    "CreditCounter",
    "FCT_CREDIT_UNITS",
    "MAX_CREDIT",
    "MAX_OUTSTANDING_FCTS",
]


FCT_CREDIT_UNITS = 8  # §5.5.4b: each FCT is exchanged for eight N-Chars.
MAX_CREDIT = 56  # §5.5.4h: transmit credit max = 56.
MAX_OUTSTANDING_FCTS = 7  # §5.5.4i: max seven FCTs outstanding.


@dataclass
class CreditCounter:
    """Track TX and RX credit per ECSS §5.5.4 e/l.

    ``max_rx_credit`` defaults to 56 (§5.5.4n) but may be lowered when the
    receive FIFO cannot hold 56 N-Chars (§5.5.4o).
    """

    max_rx_credit: int = MAX_CREDIT  # §5.5.4n / §5.5.4o
    tx_credit: int = 0  # §5.5.4e (starts at 0; §5.5.4g clears on ErrorReset)
    rx_credit: int = 0  # §5.5.4l (starts at 0; §5.5.4m clears on ErrorReset)
    _fcts_received: int = field(default=0, init=False, repr=False)
    _nchars_sent: int = field(default=0, init=False, repr=False)
    _fcts_sent: int = field(default=0, init=False, repr=False)
    _nchars_received: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_rx_credit < 0:
            raise ValueError("max_rx_credit must be non-negative")
        if self.max_rx_credit > MAX_CREDIT:
            raise ValueError(
                f"max_rx_credit {self.max_rx_credit} exceeds ECSS §5.5.4h/n cap of {MAX_CREDIT}"
            )

    # -- TX side --------------------------------------------------------------

    def on_fct_received(self) -> None:
        """Register an incoming FCT: +8 tx_credit (§5.5.4e.1)."""

        new_credit = self.tx_credit + FCT_CREDIT_UNITS  # §5.5.4e.1
        if new_credit > MAX_CREDIT:  # §5.5.4j
            # §5.5.5a.2: leave the counter at its pre-violation value so the
            # link reset path can inspect it and so the object is not
            # corrupted by a raised exception mid-mutation.
            raise CreditError(reason="fct_overflow")
        self.tx_credit = new_credit
        self._fcts_received += 1

    def on_nchar_sent(self) -> None:
        """Register an outgoing N-Char (DATA / EOP / EEP): −1 tx_credit (§5.5.4e.2)."""

        if self.tx_credit <= 0:
            # §5.5.4d / §5.5.4f: the TX loop must gate on can_send();
            # arriving here means the caller violated the contract.
            raise RuntimeError(
                "on_nchar_sent called with tx_credit == 0 "
                "(caller must gate on can_send)"
            )
        self.tx_credit -= 1  # §5.5.4e.2
        self._nchars_sent += 1

    def can_send(self) -> bool:
        """§5.5.4d / §5.5.4f: N-Chars may only be sent while credit > 0."""

        return self.tx_credit > 0  # §5.5.4f

    # -- RX side --------------------------------------------------------------

    def on_fct_sent(self) -> None:
        """Register an outgoing FCT: +8 rx_credit (§5.5.4l.1)."""

        new_credit = self.rx_credit + FCT_CREDIT_UNITS  # §5.5.4l.1
        if new_credit > self.max_rx_credit:
            # Caller must gate on should_send_fct().  Belt-and-braces guard
            # against the mirror image of §5.5.4j on our own RX side.
            raise RuntimeError(
                "on_fct_sent would push rx_credit past max_rx_credit "
                "(caller must gate on should_send_fct)"
            )
        self.rx_credit = new_credit
        self._fcts_sent += 1

    def on_nchar_received(self) -> None:
        """Register an incoming N-Char: −1 rx_credit (§5.5.4l.2)."""

        if self.rx_credit <= 0:  # §5.5.5a.1
            raise CreditError(reason="nchar_with_zero_rx_credit")
        self.rx_credit -= 1  # §5.5.4l.2
        self._nchars_received += 1

    def should_send_fct(self, rx_buffer_headroom_nchars: int) -> bool:
        """§5.5.4p: send an FCT only when both conditions hold.

        1. Room for at least 8 more N-Chars in the RX buffer.
        2. rx_credit is at least 8 below its max, so the +8 won't overflow.
        """

        if rx_buffer_headroom_nchars < FCT_CREDIT_UNITS:  # §5.5.4p condition A
            return False
        return self.rx_credit <= self.max_rx_credit - FCT_CREDIT_UNITS  # §5.5.4p condition B

    # -- lifecycle ------------------------------------------------------------

    def reset(self) -> None:
        """§5.5.4g / §5.5.4m: zero both counters on entry to ErrorReset."""

        self.tx_credit = 0  # §5.5.4g
        self.rx_credit = 0  # §5.5.4m

    @staticmethod
    def initial_fct_count(rx_fifo_capacity_nchars: int) -> int:
        """§5.5.4k: at link init/re-init, send one FCT per 8 RX slots, cap 7."""

        if rx_fifo_capacity_nchars < 0:
            raise ValueError("rx_fifo_capacity_nchars must be non-negative")
        return min(
            rx_fifo_capacity_nchars // FCT_CREDIT_UNITS,  # §5.5.4k
            MAX_OUTSTANDING_FCTS,  # §5.5.4i / §5.5.4k cap
        )
