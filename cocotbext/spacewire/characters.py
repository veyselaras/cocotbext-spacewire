"""SpaceWire character-level codec and cocotb-facing BFM helpers.

The pure ``Encoder`` and ``Decoder`` classes convert SpaceWire characters to and
from the bit stream carried by the Data-Strobe layer.  Bits are transmitted as
``parity, data-control flag, payload LSB first``.  SpaceWire odd parity spans
two characters: a character's parity bit covers the previous character content
bits, itself, and the current character's data-control flag.

The async ``CharDriver`` and ``CharMonitor`` are deliberately thin wrappers over
a minimal DS bit interface:

* ``async put_bit(bit: int) -> None``
* ``async get_bit() -> int``

The repository currently provides only the pure DS codec, so tests can pass any
adapter object or callable implementing those two methods.
"""

from __future__ import annotations

import inspect
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Protocol

from .exceptions import (
    EscapeError,
    ParityError,
    ProtocolError,
    SpaceWireEscapeError,
    SpaceWireParityError,
    SpaceWireProtocolError,
)

try:  # pragma: no cover - exercised only when cocotb is installed.
    from cocotb.queue import Queue
except ImportError:  # pragma: no cover - keeps pure unit tests cocotb-free.
    from asyncio import Queue  # type: ignore[assignment]


LOGGER_NAME = "cocotbext.spacewire.characters"
LOG_PREFIX = "[spacewire.char]"

CONTROL_CONTENT_BITS: dict["CharType", tuple[int, int]] = {}
CONTROL_TYPE_BY_BITS: dict[tuple[int, int], "CharType"] = {}


class CharType(Enum):
    """SpaceWire character and escape-code classifications."""

    FCT = auto()
    EOP = auto()
    EEP = auto()
    ESC = auto()
    DATA = auto()
    NULL = auto()
    TIMECODE = auto()
    BROADCAST = auto()


CONTROL_CONTENT_BITS = {
    CharType.FCT: (0, 0),
    CharType.EOP: (0, 1),
    CharType.EEP: (1, 0),
    CharType.ESC: (1, 1),
}
CONTROL_TYPE_BY_BITS = {value: key for key, value in CONTROL_CONTENT_BITS.items()}


@dataclass(frozen=True)
class ParityCheck:
    """Details for one parity check in a primitive character."""

    bit_index: int
    expected: int
    got: int
    prev_content_bits: tuple[int, ...]
    dc_flag: int


@dataclass
class Character:
    """Decoded or requested SpaceWire character.

    ``value`` is the data byte for ``DATA``, ``TIMECODE``, and ``BROADCAST``.
    For escaped data codes, ``type_field`` contains ``B7:B6`` and
    ``code_value`` contains ``B5:B0``.
    """

    type: CharType
    value: int | None = None
    raw_bits: list[int] = field(default_factory=list)
    parity_ok: bool = True
    error: str | None = None
    sim_time: int | float | None = None
    type_field: int | None = None
    code_value: int | None = None
    expected_parity: int | None = None
    got_parity: int | None = None
    parity_checks: tuple[ParityCheck, ...] = ()

    @property
    def is_l_char(self) -> bool:
        """Return true for link characters: NULL and FCT."""

        return is_l_char_type(self.type)

    @property
    def is_n_char(self) -> bool:
        """Return true for normal characters: DATA, EOP, and EEP."""

        return is_n_char_type(self.type)


class BitSink(Protocol):
    """Minimal async interface consumed by ``CharDriver``."""

    async def put_bit(self, bit: int) -> None:
        """Send one bit to the DS layer."""


class BitSource(Protocol):
    """Minimal async interface consumed by ``CharMonitor``."""

    async def get_bit(self) -> int:
        """Receive one bit from the DS layer."""


def _bit(value: object, name: str = "bit") -> int:
    """Return ``value`` as 0 or 1, rejecting non-binary values."""

    if value is True or value == 1:
        return 1
    if value is False or value == 0:
        return 0
    raise ValueError(f"{name} must be 0 or 1; got {value!r}")


def _bits(values: Iterable[int | bool], name: str = "bits") -> list[int]:
    """Normalize an iterable of bit-like values to integer bits."""

    return [_bit(value, name) for value in values]


def _byte(value: int, name: str = "value") -> int:
    """Validate and return an 8-bit integer."""

    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be in range 0..255; got {value!r}")
    return value


def _six_bit(value: int, name: str = "value") -> int:
    """Validate and return a 6-bit integer."""

    if not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= 0x3F:
        raise ValueError(f"{name} must be in range 0..63; got {value!r}")
    return value


def _byte_to_lsb_bits(value: int) -> list[int]:
    """Return an 8-bit value as LSB-first bits."""

    byte = _byte(value)
    return [(byte >> index) & 1 for index in range(8)]


def _lsb_bits_to_int(bits: Iterable[int | bool]) -> int:
    """Return LSB-first bits as an integer."""

    value = 0
    for index, bit in enumerate(_bits(bits)):
        value |= bit << index
    return value


def compute_parity(prev_content_bits: Iterable[int | bool], dc_flag: int | bool) -> int:
    """Compute SpaceWire odd parity for the next character.

    The returned parity bit makes the total number of ones in
    ``previous-content-bits + parity + current-dc-flag`` odd.  For the first
    character, callers pass an empty previous-content list.
    """

    previous_ones = sum(_bits(prev_content_bits, "prev_content_bits"))
    flag = _bit(dc_flag, "dc_flag")
    return 1 if (previous_ones + flag) % 2 == 0 else 0


def is_l_char_type(char_type: CharType) -> bool:
    """Return true for SpaceWire L-Chars: NULL and FCT."""

    return char_type in {CharType.NULL, CharType.FCT}


def is_n_char_type(char_type: CharType) -> bool:
    """Return true for SpaceWire N-Chars: DATA, EOP, and EEP."""

    return char_type in {CharType.DATA, CharType.EOP, CharType.EEP}


def error_types(error: str | None) -> list[str]:
    """Split a combined character error string into individual error names."""

    if error is None:
        return []
    return [part.strip() for part in error.split("+") if part.strip()]


def _combine_errors(*errors: str | None) -> str | None:
    """Combine unique error labels in first-seen order."""

    combined: list[str] = []
    for error in errors:
        for item in error_types(error):
            if item not in combined:
                combined.append(item)
    return "+".join(combined) if combined else None


def _coverage_dict(counter: Counter[Any]) -> dict[str, int]:
    """Return a string-keyed copy suitable for logs or assertions."""

    result: dict[str, int] = {}
    for key, count in counter.items():
        result[key.name if isinstance(key, CharType) else str(key)] = count
    return result


class Encoder:
    """Stateful SpaceWire character encoder.

    ``encode`` updates the previous-content state after each primitive
    character.  High-level escaped codes such as ``NULL`` encode as two
    primitive characters, and parity for the second primitive therefore uses the
    ESC content bits as required by SpaceWire.
    """

    def __init__(self, initial_prev_content_bits: Iterable[int | bool] = ()) -> None:
        self._prev_content_bits = _bits(
            initial_prev_content_bits,
            "initial_prev_content_bits",
        )
        self._bad_parity_offsets: list[int] = []

    @property
    def prev_content_bits(self) -> tuple[int, ...]:
        """Content bits that will be used by the next parity calculation."""

        return tuple(self._prev_content_bits)

    def reset(self, initial_prev_content_bits: Iterable[int | bool] = ()) -> None:
        """Reset encoder parity state and pending error-injection requests."""

        self._prev_content_bits = _bits(
            initial_prev_content_bits,
            "initial_prev_content_bits",
        )
        self._bad_parity_offsets.clear()

    def force_bad_parity(self, offset: int = 0) -> None:
        """Flip parity on the next/Nth primitive character encoded.

        ``offset=0`` corrupts the next primitive character, ``offset=1`` the one
        after that, and so on.
        """

        if offset < 0:
            raise ValueError("offset must be non-negative")
        self._bad_parity_offsets.append(offset)

    @staticmethod
    def data(byte: int) -> Character:
        """Build a data character request."""

        return Character(CharType.DATA, value=_byte(byte, "byte"))

    @staticmethod
    def null() -> Character:
        """Build a NULL request, encoded as ESC + FCT."""

        return Character(CharType.NULL)

    @staticmethod
    def time_code(value: int) -> Character:
        """Build a time-code request from a 6-bit value."""

        code_value = _six_bit(value)
        return Character(
            CharType.TIMECODE,
            value=code_value,
            type_field=0,
            code_value=code_value,
        )

    @staticmethod
    def broadcast(value: int, type_field: int = 0b10) -> Character:
        """Build a broadcast/distributed-interrupt request."""

        if type_field not in (0, 1, 2, 3):
            raise ValueError("type_field must be in range 0..3")
        code_value = _six_bit(value)
        byte = (type_field << 6) | code_value
        return Character(
            CharType.BROADCAST,
            value=byte,
            type_field=type_field,
            code_value=code_value,
        )

    def encode(self, char: Character | CharType | int) -> list[int]:
        """Encode one high-level character request to SpaceWire bits."""

        request = self._coerce_character(char)

        if request.type in CONTROL_CONTENT_BITS:
            return self._encode_control(request.type)
        if request.type is CharType.DATA:
            if request.value is None:
                raise ValueError("DATA character requires value")
            return self._encode_data(request.value)
        if request.type is CharType.NULL:
            return self._encode_control(CharType.ESC) + self._encode_control(CharType.FCT)
        if request.type is CharType.TIMECODE:
            code_value = request.code_value
            if code_value is None:
                if request.value is None:
                    raise ValueError("TIMECODE character requires value")
                code_value = request.value & 0x3F
            return self._encode_control(CharType.ESC) + self._encode_data(code_value)
        if request.type is CharType.BROADCAST:
            if request.value is None:
                type_field = 0b10 if request.type_field is None else request.type_field
                code_value = 0 if request.code_value is None else request.code_value
                value = (type_field << 6) | _six_bit(code_value)
            else:
                value = request.value
            return self._encode_control(CharType.ESC) + self._encode_data(value)

        raise ValueError(f"unsupported character type {request.type!r}")

    def _coerce_character(self, char: Character | CharType | int) -> Character:
        """Normalize accepted encode input forms."""

        if isinstance(char, Character):
            return char
        if isinstance(char, CharType):
            return Character(char)
        if isinstance(char, int):
            return self.data(char)
        raise TypeError("char must be a Character, CharType, or data byte integer")

    def _encode_control(self, char_type: CharType) -> list[int]:
        """Encode one primitive control character."""

        content_bits = list(CONTROL_CONTENT_BITS[char_type])
        bits = self._encode_primitive(dc_flag=1, content_bits=content_bits)
        return bits

    def _encode_data(self, value: int) -> list[int]:
        """Encode one primitive data character."""

        content_bits = _byte_to_lsb_bits(value)
        return self._encode_primitive(dc_flag=0, content_bits=content_bits)

    def _encode_primitive(self, dc_flag: int, content_bits: list[int]) -> list[int]:
        """Encode one primitive character and update parity state."""

        parity = compute_parity(self._prev_content_bits, dc_flag)
        if self._consume_bad_parity_request():
            parity ^= 1

        bits = [parity, dc_flag, *content_bits]
        self._prev_content_bits = list(content_bits)
        self._advance_bad_parity_requests()
        return bits

    def _consume_bad_parity_request(self) -> bool:
        """Return true if the current primitive parity should be corrupted."""

        return any(offset == 0 for offset in self._bad_parity_offsets)

    def _advance_bad_parity_requests(self) -> None:
        """Advance pending bad-parity offsets after one primitive encode."""

        self._bad_parity_offsets = [
            offset - 1 for offset in self._bad_parity_offsets if offset > 0
        ]


@dataclass
class _Primitive:
    """Internal primitive character plus content bits for parity state."""

    character: Character
    content_bits: list[int]


class Decoder:
    """Stateful SpaceWire character decoder.

    ``feed`` accepts one bit at a time and returns zero or more decoded
    high-level ``Character`` objects.  ESC is held until the next primitive
    character resolves it to NULL, time-code, broadcast, or escape error.
    """

    def __init__(self, initial_prev_content_bits: Iterable[int | bool] = ()) -> None:
        self._initial_prev_content_bits = _bits(
            initial_prev_content_bits,
            "initial_prev_content_bits",
        )
        self._prev_content_bits = list(self._initial_prev_content_bits)
        self._buffer: list[int] = []
        self._expected_len: int | None = None
        self._pending_escape: Character | None = None
        self._type_counts: Counter[CharType] = Counter()
        self._error_counts: Counter[str] = Counter()

    @property
    def pending_bits(self) -> tuple[int, ...]:
        """Bits collected for the current incomplete primitive character."""

        return tuple(self._buffer)

    @property
    def prev_content_bits(self) -> tuple[int, ...]:
        """Content bits that will be used by the next parity check."""

        return tuple(self._prev_content_bits)

    def reset(self) -> None:
        """Reset decoder framing, escape, parity, and coverage state."""

        self._prev_content_bits = list(self._initial_prev_content_bits)
        self._buffer.clear()
        self._expected_len = None
        self._pending_escape = None
        self._type_counts.clear()
        self._error_counts.clear()

    def feed(self, bit: int | bool, sim_time: int | float | None = None) -> list[Character]:
        """Feed one bit and return decoded characters, if any."""

        self._buffer.append(_bit(bit))

        if len(self._buffer) == 2:
            self._expected_len = 4 if self._buffer[1] == 1 else 10

        if self._expected_len is None or len(self._buffer) < self._expected_len:
            return []

        raw_bits = self._buffer
        self._buffer = []
        self._expected_len = None

        primitive = self._parse_primitive(raw_bits, sim_time)
        characters = self._resolve_primitive(primitive, sim_time)
        for character in characters:
            self._record(character)
        return characters

    def push(
        self,
        bits: Iterable[int | bool],
        sim_time: int | float | None = None,
    ) -> list[Character]:
        """Feed multiple bits and return all decoded characters."""

        characters: list[Character] = []
        for bit in bits:
            characters.extend(self.feed(bit, sim_time=sim_time))
        return characters

    def stats(self) -> dict[str, dict[str, int]]:
        """Return functional coverage counters."""

        return {
            "characters": _coverage_dict(self._type_counts),
            "errors": _coverage_dict(self._error_counts),
        }

    def dump_coverage(self) -> str:
        """Return a compact coverage summary string."""

        stats = self.stats()
        chars = ", ".join(f"{key}={value}" for key, value in stats["characters"].items())
        errors = ", ".join(f"{key}={value}" for key, value in stats["errors"].items())
        return f"characters: {chars or 'none'}; errors: {errors or 'none'}"

    def _parse_primitive(
        self,
        raw_bits: list[int],
        sim_time: int | float | None,
    ) -> _Primitive:
        """Parse a complete primitive data/control character."""

        parity = raw_bits[0]
        dc_flag = raw_bits[1]
        expected_parity = compute_parity(self._prev_content_bits, dc_flag)
        parity_ok = parity == expected_parity
        parity_check = ParityCheck(
            bit_index=0,
            expected=expected_parity,
            got=parity,
            prev_content_bits=tuple(self._prev_content_bits),
            dc_flag=dc_flag,
        )

        if dc_flag == 0:
            content_bits = raw_bits[2:10]
            value = _lsb_bits_to_int(content_bits)
            char_type = CharType.DATA
        else:
            content_bits = raw_bits[2:4]
            char_type = CONTROL_TYPE_BY_BITS[tuple(content_bits)]
            value = None

        self._prev_content_bits = list(content_bits)
        return _Primitive(
            character=Character(
                type=char_type,
                value=value,
                raw_bits=list(raw_bits),
                parity_ok=parity_ok,
                error=None if parity_ok else "parity_error",
                sim_time=sim_time,
                expected_parity=expected_parity,
                got_parity=parity,
                parity_checks=(parity_check,),
            ),
            content_bits=list(content_bits),
        )

    def _resolve_primitive(
        self,
        primitive: _Primitive,
        sim_time: int | float | None,
    ) -> list[Character]:
        """Resolve ESC state and return high-level character events."""

        character = primitive.character

        if self._pending_escape is None:
            if character.type is CharType.ESC:
                self._pending_escape = character
                return []
            return [character]

        escaped = self._pending_escape
        self._pending_escape = None

        if character.type is CharType.FCT:
            return [
                self._combine_escaped(
                    escaped,
                    character,
                    CharType.NULL,
                    value=None,
                    sim_time=sim_time,
                )
            ]

        if character.type is CharType.DATA:
            return [self._decode_escaped_data(escaped, character, sim_time)]

        return [
            self._combine_escaped(
                escaped,
                character,
                character.type,
                value=character.value,
                sim_time=sim_time,
                extra_error="escape_error",
            )
        ]

    def _decode_escaped_data(
        self,
        escaped: Character,
        data_character: Character,
        sim_time: int | float | None,
    ) -> Character:
        """Decode ESC + data as time-code or broadcast-style code."""

        if data_character.value is None:
            raise ValueError("escaped data character is missing value")

        type_field = (data_character.value >> 6) & 0b11
        code_value = data_character.value & 0x3F

        if type_field == 0b00:
            char_type = CharType.TIMECODE
            extra_error = None
        elif type_field == 0b10:
            char_type = CharType.BROADCAST
            extra_error = None
        else:
            char_type = CharType.BROADCAST
            extra_error = "reserved_escape_data_type"

        return self._combine_escaped(
            escaped,
            data_character,
            char_type,
            value=data_character.value,
            sim_time=sim_time,
            type_field=type_field,
            code_value=code_value,
            extra_error=extra_error,
        )

    def _combine_escaped(
        self,
        escaped: Character,
        character: Character,
        char_type: CharType,
        *,
        value: int | None,
        sim_time: int | float | None,
        type_field: int | None = None,
        code_value: int | None = None,
        extra_error: str | None = None,
    ) -> Character:
        """Combine ESC and following primitive into one high-level event."""

        raw_bits = [*escaped.raw_bits, *character.raw_bits]
        checks = _offset_checks(escaped.parity_checks, 0) + _offset_checks(
            character.parity_checks,
            len(escaped.raw_bits),
        )
        parity_ok = escaped.parity_ok and character.parity_ok
        error = _combine_errors(escaped.error, character.error, extra_error)
        return Character(
            type=char_type,
            value=value,
            raw_bits=raw_bits,
            parity_ok=parity_ok,
            error=error,
            sim_time=sim_time,
            type_field=type_field,
            code_value=code_value,
            expected_parity=character.expected_parity,
            got_parity=character.got_parity,
            parity_checks=checks,
        )

    def _record(self, character: Character) -> None:
        """Record decoded character and error counters."""

        self._type_counts[character.type] += 1
        for error in error_types(character.error):
            self._error_counts[error] += 1


def _offset_checks(checks: tuple[ParityCheck, ...], offset: int) -> tuple[ParityCheck, ...]:
    """Return parity checks with bit indices shifted by ``offset``."""

    return tuple(
        ParityCheck(
            bit_index=check.bit_index + offset,
            expected=check.expected,
            got=check.got,
            prev_content_bits=check.prev_content_bits,
            dc_flag=check.dc_flag,
        )
        for check in checks
    )


async def _call_async(callable_obj: Any, *args: Any) -> Any:
    """Call a sync or async callable and await awaitable results."""

    result = callable_obj(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _resolve_method(obj: Any, method_name: str) -> Any:
    """Return a required method from an adapter object."""

    method = getattr(obj, method_name, None)
    if method is None:
        raise TypeError(f"DS bit adapter must provide {method_name}()")
    return method


def _get_sim_time() -> int | float | None:
    """Return cocotb simulation time when available."""

    try:  # pragma: no cover - exercised only under cocotb.
        from cocotb.utils import get_sim_time
    except ImportError:
        return None
    return get_sim_time()


class CharDriver:
    """Async character driver over a DS-layer ``put_bit`` interface."""

    def __init__(
        self,
        bit_sink: BitSink | Any,
        *,
        encoder: Encoder | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.encoder = encoder if encoder is not None else Encoder()
        self._put_bit = bit_sink if callable(bit_sink) else _resolve_method(bit_sink, "put_bit")
        self.log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        self._type_counts: Counter[CharType] = Counter()
        self._error_counts: Counter[str] = Counter()

    def force_bad_parity(self, offset: int = 0) -> None:
        """Flip parity on the next/Nth primitive encoded character."""

        self.encoder.force_bad_parity(offset)
        self._error_counts["injected_bad_parity"] += 1

    async def send(self, char: Character | CharType | int) -> None:
        """Encode and send one high-level character request."""

        request = self.encoder._coerce_character(char)
        bits = self.encoder.encode(request)
        self.log.debug("%s send %s bits=%s", LOG_PREFIX, request.type.name, bits)
        await self.send_raw_bits(bits)
        self._type_counts[request.type] += 1

    async def send_data(self, byte: int) -> None:
        """Send one data character."""

        await self.send(Encoder.data(byte))

    async def send_null(self) -> None:
        """Send NULL as ESC + FCT."""

        await self.send(Encoder.null())

    async def send_time_code(self, value: int) -> None:
        """Send one time-code with a 6-bit value."""

        await self.send(Encoder.time_code(value))

    async def send_broadcast(self, value: int, type_field: int = 0b10) -> None:
        """Send one broadcast/distributed-interrupt code."""

        await self.send(Encoder.broadcast(value, type_field=type_field))

    async def send_fct(self) -> None:
        """Send one FCT control character."""

        await self.send(CharType.FCT)

    async def send_eop(self) -> None:
        """Send one EOP control character."""

        await self.send(CharType.EOP)

    async def send_eep(self) -> None:
        """Send one EEP control character."""

        await self.send(CharType.EEP)

    async def send_escape(self) -> None:
        """Send one raw ESC control character."""

        await self.send(CharType.ESC)

    async def send_illegal_escape(self, following: CharType = CharType.ESC) -> None:
        """Emit an illegal ESC sequence: ESC+ESC, ESC+EOP, or ESC+EEP."""

        if following not in {CharType.ESC, CharType.EOP, CharType.EEP}:
            raise ValueError("following must be ESC, EOP, or EEP")
        await self.send(CharType.ESC)
        await self.send(following)
        self._error_counts["injected_escape_error"] += 1

    async def send_raw_bits(self, bits: Iterable[int | bool]) -> None:
        """Send arbitrary raw bits directly to the DS layer."""

        raw_bits = _bits(bits)
        self.log.debug("%s send raw bits=%s", LOG_PREFIX, raw_bits)
        for bit in raw_bits:
            await _call_async(self._put_bit, bit)

    async def send_truncated(
        self,
        char: Character | CharType | int,
        keep_bits: int,
    ) -> None:
        """Encode a character and send only the first ``keep_bits`` bits."""

        if keep_bits < 0:
            raise ValueError("keep_bits must be non-negative")
        request = self.encoder._coerce_character(char)
        bits = self.encoder.encode(request)
        await self.send_raw_bits(bits[:keep_bits])
        self._type_counts[request.type] += 1
        self._error_counts["injected_truncation"] += 1

    def stats(self) -> dict[str, dict[str, int]]:
        """Return driver functional counters."""

        return {
            "characters": _coverage_dict(self._type_counts),
            "errors": _coverage_dict(self._error_counts),
        }

    def dump_coverage(self) -> str:
        """Return a compact driver coverage summary string."""

        stats = self.stats()
        chars = ", ".join(f"{key}={value}" for key, value in stats["characters"].items())
        errors = ", ".join(f"{key}={value}" for key, value in stats["errors"].items())
        return f"characters: {chars or 'none'}; errors: {errors or 'none'}"


class CharMonitor:
    """Async character monitor over a DS-layer ``get_bit`` interface."""

    def __init__(
        self,
        bit_source: BitSource | Any,
        *,
        decoder: Decoder | None = None,
        strict: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self.decoder = decoder if decoder is not None else Decoder()
        self.strict = strict
        self.queue: Queue[Character] = Queue()
        self._get_bit = (
            bit_source if callable(bit_source) else _resolve_method(bit_source, "get_bit")
        )
        self.log = logger if logger is not None else logging.getLogger(LOGGER_NAME)

    async def run(self) -> None:
        """Continuously read bits, decode characters, and publish events."""

        while True:
            bit = await _call_async(self._get_bit)
            sim_time = _get_sim_time()
            for character in self.decoder.feed(bit, sim_time=sim_time):
                await self._publish(character)

    async def recv(self) -> Character:
        """Return the next decoded character event."""

        return await self.queue.get()

    async def _publish(self, character: Character) -> None:
        """Log/check one decoded character and put it on the event queue."""

        if character.error is not None:
            message = self._format_error(character)
            self.log.error("%s %s", LOG_PREFIX, message)
            if self.strict:
                if "escape_error" in error_types(character.error):
                    raise SpaceWireEscapeError(character, message)
                if "parity_error" in error_types(character.error):
                    raise SpaceWireParityError(character, message)
                raise SpaceWireProtocolError(character, message)
        else:
            self.log.debug(
                "%s decoded %s value=%r raw=%s",
                LOG_PREFIX,
                character.type.name,
                character.value,
                character.raw_bits,
            )
        await self.queue.put(character)

    def _format_error(self, character: Character) -> str:
        """Build a self-checking error message with parity context."""

        checks = ", ".join(
            f"bit{check.bit_index}:expected={check.expected}:got={check.got}"
            for check in character.parity_checks
            if check.expected != check.got
        )
        parity_detail = f" parity=({checks})" if checks else ""
        return (
            f"{character.error} on {character.type.name} raw={character.raw_bits}"
            f"{parity_detail} sim_time={character.sim_time}"
        )

    def stats(self) -> dict[str, dict[str, int]]:
        """Return decoder functional coverage counters."""

        return self.decoder.stats()

    def dump_coverage(self) -> str:
        """Return a compact monitor coverage summary string."""

        return self.decoder.dump_coverage()


__all__ = [
    "CharDriver",
    "CharMonitor",
    "CharType",
    "Character",
    "Decoder",
    "Encoder",
    "EscapeError",
    "ParityCheck",
    "ParityError",
    "ProtocolError",
    "SpaceWireEscapeError",
    "SpaceWireParityError",
    "SpaceWireProtocolError",
    "compute_parity",
    "error_types",
    "is_l_char_type",
    "is_n_char_type",
]
