"""Asyncio client for the Bitfocus Companion satellite protocol.

Only what Home Assistant needs is implemented: the handshake, keepalive, button
subscriptions (ADD-SUB / SUB-STATE / SUB-PRESS / SUB-ROTATE) and reconnects. No
surface is registered and no bitmaps are requested, so each update is a few bytes.

Protocol reference: https://companion.free/for-developers/Satellite-API
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 16622
LINE_LIMIT = 4 * 1024 * 1024
HANDSHAKE_TIMEOUT = 5.0
CAPS_TIMEOUT = 1.0
RESPONSE_TIMEOUT = 5.0
# Companion drops a satellite socket that is silent for 5 s, so ping well inside that.
PING_INTERVAL = 2.0
PING_TIMEOUT = 15.0
SCAN_SETTLE_SECONDS = 1.0
PRESS_HOLD_SECONDS = 0.05
RECONNECT_MIN = 1.0
RECONNECT_MAX = 30.0
STABLE_SESSION_SECONDS = 30.0


class CompanionError(Exception):
    """Base class for Companion client errors."""


class CompanionConnectionError(CompanionError):
    """Companion could not be reached, or the connection dropped."""


class CompanionResponseError(CompanionError):
    """Companion answered a command with ERROR."""


class CompanionSubscriptionsDisabled(CompanionResponseError):
    """Button subscriptions are switched off in Companion's settings."""


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def tokenize(line: str) -> list[str]:
    """Split a protocol line on unquoted spaces, honouring quotes and backslashes."""
    fragments = [""]
    in_quotes = False
    i = 0
    length = len(line)
    while i < length:
        char = line[i]
        if char == "\\":
            if i + 1 < length:
                fragments[-1] += line[i + 1]
            i += 2
            continue
        if char == '"':
            in_quotes = not in_quotes
        elif char == " " and not in_quotes:
            fragments.append("")
        else:
            fragments[-1] += char
        i += 1
    return [fragment for fragment in fragments if fragment != ""]


def parse_line(line: str) -> tuple[str, str | None, dict[str, str | bool]]:
    """Parse ``COMMAND [OK|ERROR] KEY=VALUE ...`` into its parts."""
    fragments = tokenize(line)
    if not fragments:
        raise ValueError("Empty line")
    command = fragments[0].upper()
    rest = fragments[1:]
    status: str | None = None
    if rest and rest[0] in ("OK", "ERROR"):
        status = rest[0]
        rest = rest[1:]
    params: dict[str, str | bool] = {}
    for fragment in rest:
        key, separator, value = fragment.partition("=")
        params[key] = value if separator else True
    return command, status, params


def quote_value(value: str) -> str:
    """Quote a value the way Companion's own formatter does."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    if value == "" or any(char in value for char in ' "\\'):
        return f'"{escaped}"'
    return escaped


def format_message(command: str, **args: str | int | float | bool) -> str:
    """Build a protocol line, newline included."""
    chunks = [command]
    for key, value in args.items():
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, (int, float)):
            text = str(value)
        else:
            text = quote_value(str(value))
        chunks.append(f"{key}={text}")
    return " ".join(chunks) + "\n"


def truthy(value: str | bool | None) -> bool:
    """Interpret a protocol boolean (``1``/``0``/``true``/``false``)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false")


def decode_text(value: str | bool | None) -> str | None:
    """Decode a base64 TEXT field."""
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=False).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def decode_bitmap(value: str | bool | None) -> tuple[bytes, str] | None:
    """Decode a BITMAP field into ``(bytes, format)``.

    Encoded images arrive as a data URL (``data:image/png;base64,...``); anything else is
    base64 of raw rgb pixels, reported with the format ``rgb``.
    """
    if not isinstance(value, str) or not value:
        return None
    fmt = "rgb"
    payload = value
    if value.startswith("data:"):
        header, separator, payload = value.partition(",")
        if not separator:
            return None
        fmt = header[5:].split(";", 1)[0].strip().lower() or "application/octet-stream"
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None
    return (data, fmt) if data else None


def parse_version(value: str) -> tuple[int, ...]:
    """Turn ``1.14.0`` into ``(1, 14, 0)``; unknown parts become 0."""
    parts: list[int] = []
    for piece in value.split("."):
        digits = "".join(char for char in piece if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Location:
    """A button position in Companion: page, row, column (page is 1-based)."""

    page: int
    row: int
    column: int

    @property
    def sub_id(self) -> str:
        """Subscription id used for this location (letters, digits, - and / only)."""
        return f"ha-{self.page}-{self.row}-{self.column}"

    @classmethod
    def from_sub_id(cls, sub_id: str) -> Location | None:
        """Recover a location from a subscription id produced by ``sub_id``."""
        parts = sub_id.split("-")
        if len(parts) != 4 or parts[0] != "ha":
            return None
        try:
            return cls(int(parts[1]), int(parts[2]), int(parts[3]))
        except ValueError:
            return None

    def __str__(self) -> str:
        return f"{self.page}/{self.row}/{self.column}"


@dataclass
class ButtonState:
    """What Companion last told us about a subscribed button."""

    location: Location
    type: str = "BUTTON"
    pressed: bool = False
    text: str | None = None
    color: str | None = None
    text_color: str | None = None
    font_size: str | None = None
    # The rendered face of the button, when the subscription asked for one.
    image: bytes | None = None
    # ``image/png``, ``image/webp`` or ``rgb`` (raw pixels, ``image_size`` wide and high).
    image_format: str | None = None
    image_size: int | None = None

    @property
    def is_empty(self) -> bool:
        """True for a location with nothing on it: no text and a black background."""
        has_text = bool((self.text or "").strip())
        black = self.color in (None, "#000000", "rgb(0,0,0)")
        return self.type == "BUTTON" and not has_text and black

    @property
    def label(self) -> str:
        """First line of the button text, for naming things after it."""
        return (self.text or "").strip().splitlines()[0].strip() if (self.text or "").strip() else ""


@dataclass(frozen=True)
class HandshakeInfo:
    """The BEGIN and CAPS lines Companion sends on connect."""

    companion_version: str
    api_version: str
    caps: dict[str, str | bool] = field(default_factory=dict)

    @property
    def api_version_tuple(self) -> tuple[int, ...]:
        return parse_version(self.api_version)

    @property
    def subscriptions_enabled(self) -> bool:
        """True when CAPS says subscriptions are on, or when CAPS was not sent."""
        if "SUBSCRIPTIONS" not in self.caps:
            return True
        return truthy(self.caps["SUBSCRIPTIONS"])

    @property
    def rotary_amount(self) -> bool:
        return truthy(self.caps.get("ROTARY_AMOUNT", False))

    @property
    def bitmap_formats(self) -> tuple[str, ...]:
        """Encodings Companion offers for button images; empty before API 1.12 (raw rgb only)."""
        value = self.caps.get("BITMAP_FORMATS")
        if not isinstance(value, str):
            return ()
        return tuple(part.strip().lower() for part in value.split(",") if part.strip())


StateCallback = Callable[[ButtonState, bool], None]
ConnectionCallback = Callable[[bool], None]


async def _read_handshake(reader: asyncio.StreamReader) -> tuple[HandshakeInfo, str | None]:
    """Read BEGIN and, when sent, CAPS. Returns any extra line that followed."""
    raw = await asyncio.wait_for(reader.readline(), HANDSHAKE_TIMEOUT)
    if not raw:
        raise CompanionConnectionError("Connection closed before handshake")
    command, _, params = parse_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    if command != "BEGIN":
        raise CompanionConnectionError(f"Unexpected handshake message: {command}")
    caps: dict[str, str | bool] = {}
    leftover: str | None = None
    try:
        raw2 = await asyncio.wait_for(reader.readline(), CAPS_TIMEOUT)
    except TimeoutError:
        raw2 = b""
    if raw2:
        line2 = raw2.decode("utf-8", errors="replace").rstrip("\r\n")
        command2, _, params2 = parse_line(line2)
        if command2 == "CAPS":
            caps = params2
        else:
            leftover = line2
    info = HandshakeInfo(
        companion_version=str(params.get("CompanionVersion", "")),
        api_version=str(params.get("ApiVersion", "")),
        caps=caps,
    )
    return info, leftover


async def async_probe(host: str, port: int = DEFAULT_PORT) -> HandshakeInfo:
    """Connect once, read the handshake and disconnect. Used by the config flow."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=LINE_LIMIT), HANDSHAKE_TIMEOUT
        )
    except (OSError, TimeoutError) as err:
        raise CompanionConnectionError(str(err) or "Connection timed out") from err
    try:
        info, _ = await _read_handshake(reader)
        writer.write(b"QUIT\n")
        with suppress(OSError):
            await writer.drain()
    except (OSError, TimeoutError, ValueError) as err:
        raise CompanionConnectionError(str(err) or "Handshake failed") from err
    finally:
        writer.close()
        with suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 1.0)
    return info


class CompanionSatelliteClient:
    """A long-lived satellite connection with button subscriptions."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        state_callback: StateCallback | None = None,
        connection_callback: ConnectionCallback | None = None,
        bitmap_size: int | None = None,
    ) -> None:
        self.host = host
        self.port = port
        # Ask Companion to stream each subscribed button's rendered face at this size (pixels).
        self.bitmap_size = bitmap_size
        self._state_callback = state_callback
        self._connection_callback = connection_callback

        self.connected = False
        self.handshake: HandshakeInfo | None = None
        self.states: dict[Location, ButtonState] = {}

        self._task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, deque[asyncio.Future[dict[str, str | bool]]]] = defaultdict(deque)
        self._desired: dict[str, Location] = {}
        self._active: set[str] = set()
        self._inflight: set[str] = set()
        self._connected_event = asyncio.Event()
        self._stopping = False
        self._last_pong = 0.0

    # -- lifecycle ---------------------------------------------------------

    @property
    def subscriptions_supported(self) -> bool:
        """False only when Companion has told us subscriptions are off."""
        return self.handshake is None or self.handshake.subscriptions_enabled

    async def async_start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name=f"companion-{self.host}:{self.port}")

    async def async_stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._close_writer(send_quit=True)
        self._set_connected(False)

    async def async_wait_connected(self, timeout: float) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

    async def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stopping:
            started = monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, CompanionError, ValueError) as err:
                _LOGGER.debug("Companion %s:%s: %s", self.host, self.port, err)
            except Exception:  # noqa: BLE001 - keep the reconnect loop alive
                _LOGGER.exception("Unexpected error talking to Companion %s:%s", self.host, self.port)
            was_connected = self.connected
            self._set_connected(False)
            if self._stopping:
                break
            if was_connected and monotonic() - started > STABLE_SESSION_SECONDS:
                backoff = RECONNECT_MIN
            _LOGGER.debug("Reconnecting to Companion %s:%s in %.0fs", self.host, self.port, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=LINE_LIMIT), HANDSHAKE_TIMEOUT
        )
        self._writer = writer
        self._active.clear()
        self._inflight.clear()
        resubscribe: asyncio.Task[None] | None = None
        try:
            self.handshake, leftover = await _read_handshake(reader)
            _LOGGER.info(
                "Connected to Companion %s (API %s) at %s:%s",
                self.handshake.companion_version,
                self.handshake.api_version,
                self.host,
                self.port,
            )
            self._last_pong = monotonic()
            self._set_connected(True)
            self._ping_task = asyncio.create_task(self._ping_loop())
            resubscribe = asyncio.create_task(self._resubscribe())
            if leftover:
                self._handle_line(leftover)
            while True:
                raw = await reader.readline()
                if not raw:
                    raise CompanionConnectionError("Companion closed the connection")
                self._handle_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        finally:
            for task in (self._ping_task, resubscribe):
                if task is not None and not task.done():
                    task.cancel()
            self._ping_task = None
            self._fail_pending(CompanionConnectionError("Connection to Companion lost"))
            self._active.clear()
            self._inflight.clear()
            await self._close_writer(send_quit=False)

    async def _close_writer(self, *, send_quit: bool) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        with suppress(OSError, RuntimeError):
            if send_quit and not writer.is_closing():
                writer.write(b"QUIT\n")
                await asyncio.wait_for(writer.drain(), 1.0)
        with suppress(OSError, RuntimeError, TimeoutError):
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 1.0)

    def _set_connected(self, value: bool) -> None:
        if self.connected == value:
            return
        self.connected = value
        if value:
            self._connected_event.set()
        else:
            self._connected_event.clear()
        if self._connection_callback is not None:
            try:
                self._connection_callback(value)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Connection callback failed")

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if monotonic() - self._last_pong > PING_TIMEOUT:
                _LOGGER.debug("Companion %s:%s stopped answering pings", self.host, self.port)
                if self._writer is not None:
                    self._writer.transport.abort()
                return
            self._write_nowait("PING ha\n")

    async def _resubscribe(self) -> None:
        for location in list(self._desired.values()):
            try:
                await self._add_sub(location)
            except CompanionSubscriptionsDisabled:
                _LOGGER.warning(
                    "Button subscriptions are not enabled in Companion at %s:%s; "
                    "enable them under Settings > Protocols",
                    self.host,
                    self.port,
                )
                return
            except CompanionError as err:
                _LOGGER.warning("Could not subscribe to Companion button %s: %s", location, err)

    # -- inbound -----------------------------------------------------------

    def _handle_line(self, line: str) -> None:
        if not line.strip():
            return
        if line.startswith("PING"):
            self._write_nowait(f"PONG{line[4:]}\n")
            return
        try:
            command, status, params = parse_line(line)
        except ValueError:
            return
        if command == "PONG":
            self._last_pong = monotonic()
            return
        if command == "SUB-STATE":
            self._handle_sub_state(params)
            return
        if command == "CAPS" and self.handshake is not None:
            self.handshake = HandshakeInfo(
                self.handshake.companion_version, self.handshake.api_version, {**self.handshake.caps, **params}
            )
            return
        if command == "ERROR":
            _LOGGER.warning("Companion %s:%s reported: %s", self.host, self.port, params.get("MESSAGE"))
            return
        if status is not None:
            queue = self._pending.get(command)
            if queue:
                future = queue.popleft()
                if not future.done():
                    if status == "OK":
                        future.set_result(params)
                    else:
                        future.set_exception(self._error_for(params.get("MESSAGE")))
            else:
                _LOGGER.debug("Unmatched reply from Companion: %s", line)
            return
        _LOGGER.debug("Ignoring Companion message: %s", command)

    @staticmethod
    def _error_for(message: str | bool | None) -> CompanionResponseError:
        text = str(message) if message not in (None, True) else "Companion rejected the command"
        if "not enabled" in text.lower():
            return CompanionSubscriptionsDisabled(text)
        return CompanionResponseError(text)

    def _handle_sub_state(self, params: dict[str, str | bool]) -> None:
        sub_id = params.get("SUBID")
        if not isinstance(sub_id, str):
            return
        location = self._desired.get(sub_id) or Location.from_sub_id(sub_id)
        if location is None:
            return
        previous = self.states.get(location)
        state = ButtonState(
            location=location,
            type=str(params.get("TYPE", "BUTTON")),
            pressed=truthy(params.get("PRESSED", False)),
            text=decode_text(params.get("TEXT")),
            color=params.get("COLOR") if isinstance(params.get("COLOR"), str) else None,
            text_color=params.get("TEXTCOLOR") if isinstance(params.get("TEXTCOLOR"), str) else None,
            font_size=str(params["FONT_SIZE"]) if "FONT_SIZE" in params else None,
        )
        if (bitmap := decode_bitmap(params.get("BITMAP"))) is not None:
            state.image, state.image_format = bitmap
            state.image_size = self.bitmap_size
        self.states[location] = state
        pressed_changed = previous is not None and previous.pressed != state.pressed
        if self._state_callback is not None:
            try:
                self._state_callback(state, pressed_changed)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("State callback failed")

    # -- outbound ----------------------------------------------------------

    def _write_nowait(self, text: str) -> None:
        if self._writer is None or self._writer.is_closing():
            return
        with suppress(OSError, RuntimeError):
            self._writer.write(text.encode())

    def _fail_pending(self, error: Exception) -> None:
        for queue in self._pending.values():
            while queue:
                future = queue.popleft()
                if not future.done():
                    future.set_exception(error)
        self._pending.clear()

    async def _request(self, command: str, **args: str | int | float | bool) -> dict[str, str | bool]:
        if not self.connected or self._writer is None:
            raise CompanionConnectionError("Not connected to Companion")
        future: asyncio.Future[dict[str, str | bool]] = asyncio.get_running_loop().create_future()
        self._pending[command].append(future)
        try:
            self._writer.write(format_message(command, **args).encode())
            await self._writer.drain()
            return await asyncio.wait_for(future, RESPONSE_TIMEOUT)
        except TimeoutError as err:
            with suppress(ValueError):
                self._pending[command].remove(future)
            raise CompanionConnectionError(f"Companion did not answer {command}") from err
        except OSError as err:
            with suppress(ValueError):
                self._pending[command].remove(future)
            raise CompanionConnectionError(str(err)) from err

    async def _add_sub(self, location: Location) -> None:
        if not self.subscriptions_supported:
            raise CompanionSubscriptionsDisabled("Button subscriptions are not enabled in Companion")
        sub_id = location.sub_id
        if sub_id in self._active or sub_id in self._inflight:
            return
        self._inflight.add(sub_id)
        try:
            args: dict[str, str | int | bool] = {
                "SUBID": sub_id,
                "LOCATION": str(location),
                "TEXT": True,
                "COLORS": "hex",
                "TEXT_STYLE": True,
            }
            if self.bitmap_size:
                args["BITMAP"] = int(self.bitmap_size)
                # PNG is compact and ready to serve; Companion before API 1.12 ignores the
                # format and sends raw rgb pixels, which the image entity converts itself.
                if self.handshake is not None and "png" in self.handshake.bitmap_formats:
                    args["BITMAP_FORMAT"] = "png"
            await self._request("ADD-SUB", **args)
            self._active.add(sub_id)
        finally:
            self._inflight.discard(sub_id)

    async def async_subscribe(self, location: Location) -> None:
        """Start receiving state for a button. Survives reconnects."""
        self._desired[location.sub_id] = location
        if self.connected and location.sub_id not in self._active:
            await self._add_sub(location)

    async def async_unsubscribe(self, location: Location) -> None:
        self._desired.pop(location.sub_id, None)
        self.states.pop(location, None)
        if self.connected and location.sub_id in self._active:
            self._active.discard(location.sub_id)
            with suppress(CompanionError):
                await self._request("REMOVE-SUB", SUBID=location.sub_id)

    async def _ensure_sub(self, location: Location) -> None:
        if not self.connected:
            raise CompanionConnectionError("Not connected to Companion")
        if location.sub_id not in self._active:
            await self.async_subscribe(location)
            if location.sub_id not in self._active:
                # Another caller was mid-subscribe; wait for it to land.
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    while location.sub_id in self._inflight:
                        await asyncio.sleep(0.01)

    async def async_scan_page(
        self, page: int, max_row: int = 3, max_column: int = 7, settle: float = SCAN_SETTLE_SECONDS
    ) -> dict[Location, ButtonState]:
        """Briefly subscribe to every cell of a page and report what Companion has there.

        Cells that were not already subscribed are released again afterwards.
        """
        if not self.connected:
            raise CompanionConnectionError("Not connected to Companion")
        locations = [Location(page, row, column) for row in range(max_row + 1) for column in range(max_column + 1)]
        temporary = [location for location in locations if location.sub_id not in self._desired]
        try:
            for location in locations:
                await self.async_subscribe(location)
            await asyncio.sleep(settle)
            return {location: self.states[location] for location in locations if location in self.states}
        finally:
            for location in temporary:
                await self.async_unsubscribe(location)

    async def async_set_pressed(self, location: Location, pressed: bool) -> None:
        """Hold down or release a button, as a surface would."""
        await self._ensure_sub(location)
        await self._request("SUB-PRESS", SUBID=location.sub_id, PRESSED=pressed)

    async def async_press(self, location: Location, hold: float = PRESS_HOLD_SECONDS) -> None:
        """Press and release a button."""
        await self.async_set_pressed(location, True)
        try:
            await asyncio.sleep(hold)
        finally:
            await self.async_set_pressed(location, False)

    async def async_rotate(self, location: Location, steps: int) -> None:
        """Rotate an encoder button: positive is clockwise (right)."""
        if steps == 0:
            return
        await self._ensure_sub(location)
        if self.handshake is None or not self.handshake.rotary_amount:
            steps = 1 if steps > 0 else -1
        await self._request("SUB-ROTATE", SUBID=location.sub_id, DIRECTION=steps)
