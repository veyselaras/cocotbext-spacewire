"""Data-Strobe (DS) encoder and decoder.

In DS encoding the recovered clock is ``D XOR S``.  For every transmitted
bit exactly one signal changes:

* If the new data bit differs from the previous D level, D changes and S
  stays unchanged.
* If the new data bit equals the previous D level, D stays unchanged and S
  toggles.

The initial line levels are configurable; both default to zero.
"""

from __future__ import annotations

from collections.abc import Iterable


BitInput = str | bytes | bytearray | Iterable[int | bool]


def _bit(value: object, name: str) -> int:
    """Return *value* as 0 or 1, rejecting non-binary values."""
    if value is True or value == 1:
        return 1
    if value is False or value == 0:
        return 0
    raise ValueError(f"{name} must contain only binary values (0 or 1); got {value!r}")


def _bits(values: BitInput, name: str) -> list[int]:
    """Normalize a supported bit input to a list of integer bits."""
    if isinstance(values, str):
        compact = "".join(values.split())
        invalid = next((char for char in compact if char not in "01"), None)
        if invalid is not None:
            raise ValueError(
                f"{name} must be a binary string; found character {invalid!r}"
            )
        return [int(char) for char in compact]

    # Bytes are treated as packed data, most-significant bit first.
    if isinstance(values, (bytes, bytearray)):
        return [
            (byte >> shift) & 1
            for byte in values
            for shift in range(7, -1, -1)
        ]

    try:
        return [_bit(value, name) for value in values]
    except TypeError as exc:
        raise TypeError(
            f"{name} must be a bit string, bytes, or an iterable of bits"
        ) from exc


def ds_encode(
    data: BitInput,
    initial_d: int | bool = 0,
    initial_s: int | bool = 0,
) -> tuple[list[int], list[int]]:
    """Encode data bits into Data and Strobe line-level samples.

    Args:
        data: Bits to encode. Accepts a string such as ``"1011"``, packed
            ``bytes`` (MSB first), or an iterable containing 0/1 values.
        initial_d: D line level immediately before the first bit (default 0).
        initial_s: S line level immediately before the first bit (default 0).

    Returns:
        ``(d_levels, s_levels)``. Each list contains the line level after the
        transition for the corresponding input bit.

    Example:
        >>> ds_encode("0011")
        ([0, 0, 1, 1], [1, 0, 0, 1])
    """
    bits = _bits(data, "data")
    previous_d = _bit(initial_d, "initial_d")
    strobe = _bit(initial_s, "initial_s")
    d_levels: list[int] = []
    s_levels: list[int] = []

    for data_bit in bits:
        if data_bit == previous_d:
            strobe ^= 1

        d_levels.append(data_bit)
        s_levels.append(strobe)
        previous_d = data_bit

    return d_levels, s_levels


def ds_decode(
    d_levels: BitInput,
    s_levels: BitInput,
    initial_d: int | bool = 0,
    initial_s: int | bool = 0,
    *,
    validate: bool = True,
) -> list[int]:
    """Decode sampled Data and Strobe levels into data bits.

    The decoder expects one D/S sample per encoded bit, taken after that
    bit's transition. Since D carries the data directly, decoding returns the
    D samples. By default the DS transition rule is also checked, making
    malformed or misaligned signals visible instead of silently accepting
    them.

    Args:
        d_levels: Sampled D line levels.
        s_levels: Corresponding sampled S line levels.
        initial_d: D line level immediately before the first sample.
        initial_s: S line level immediately before the first sample.
        validate: If true (default), require exactly one line to change for
            every sample. Set false only when raw D extraction is desired.

    Returns:
        The decoded bits as a list of integers.

    Raises:
        ValueError: If input lengths differ, a value is not binary, or DS
            transition validation fails.
    """
    data = _bits(d_levels, "d_levels")
    strobe = _bits(s_levels, "s_levels")

    if len(data) != len(strobe):
        raise ValueError(
            "d_levels and s_levels must have the same length "
            f"(got {len(data)} and {len(strobe)})"
        )

    previous_d = _bit(initial_d, "initial_d")
    previous_s = _bit(initial_s, "initial_s")

    if validate:
        for index, (current_d, current_s) in enumerate(zip(data, strobe)):
            d_changed = current_d != previous_d
            s_changed = current_s != previous_s
            if d_changed == s_changed:  # Both changed, or neither changed.
                change = "both changed" if d_changed else "neither changed"
                raise ValueError(
                    f"invalid DS transition at sample {index}: {change}; "
                    "exactly one of D and S must change"
                )
            previous_d = current_d
            previous_s = current_s

    return data


__all__ = ["ds_encode", "ds_decode"]
