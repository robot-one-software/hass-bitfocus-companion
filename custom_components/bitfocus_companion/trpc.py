"""Read Companion's variable catalogue through the API its own admin page uses.

Companion has no public endpoint that lists variables, but its web UI gets them over
a tRPC WebSocket at ``/trpc`` on the admin port. Two procedures are used here:

- ``variables.definitions.watch`` (subscription): the first message carries every
  variable definition, grouped by connection label.
- ``variables.values.connection`` (query, ``{"label": ...}``): current values for one label.

This is Companion's internal API, so it may change between versions; it is only used
while the user is picking variables, never at runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TRPC_TIMEOUT = 10.0
CATALOG_TIMEOUT = 60.0
MAX_MESSAGE_SIZE = 64 * 1024 * 1024
CUSTOM_LABEL = "custom"


class CompanionTrpcError(Exception):
    """The internal API could not be reached or did not answer as expected."""


def _ws_url(host: str, port: int) -> str:
    host = f"[{host}]" if ":" in host else host
    return f"ws://{host}:{port}/trpc"


async def _with_socket(
    session: aiohttp.ClientSession,
    host: str,
    port: int,
    work: Callable[[aiohttp.ClientWebSocketResponse], Any],
    timeout: float = TRPC_TIMEOUT,
) -> Any:
    try:
        async with asyncio.timeout(timeout):
            async with session.ws_connect(_ws_url(host, port), max_msg_size=MAX_MESSAGE_SIZE) as ws:
                return await work(ws)
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        raise CompanionTrpcError(f"Companion's admin API at {_ws_url(host, port)} did not answer: {err}") from err


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    """Next JSON message, answering tRPC's plain-text keepalive on the way.

    Companion's server sends the text frame ``PING`` every 30 s and drops the connection
    if ``PONG`` does not come back, so that exchange is handled here for every caller.
    """
    while True:
        message = await ws.receive()
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            raise CompanionTrpcError("Companion closed the connection")
        if message.type == aiohttp.WSMsgType.ERROR:
            raise CompanionTrpcError(f"WebSocket error: {ws.exception()}")
        if message.type != aiohttp.WSMsgType.TEXT:
            continue
        if message.data == "PING":
            await ws.send_str("PONG")
            continue
        if message.data == "PONG":
            continue
        try:
            data = json.loads(message.data)
        except ValueError as err:
            raise CompanionTrpcError("Companion sent a message that is not JSON") from err
        if "error" in data:
            raise CompanionTrpcError(str(data["error"].get("message", data["error"])))
        return data


async def async_fetch_definitions(session: aiohttp.ClientSession, host: str, port: int) -> dict[str, dict[str, str]]:
    """Return ``{label: {variable name: description}}`` for every connection."""

    async def work(ws: aiohttp.ClientWebSocketResponse) -> dict[str, dict[str, str]]:
        await ws.send_json({"id": 1, "method": "subscription", "params": {"path": "variables.definitions.watch"}})
        try:
            while True:
                data = await _receive(ws)
                result = data.get("result") or {}
                payload = result.get("data")
                if result.get("type") == "data" and isinstance(payload, dict) and payload.get("type") == "init":
                    raw = payload.get("variables") or {}
                    break
        finally:
            await ws.send_json({"id": 1, "method": "subscription.stop"})
        definitions: dict[str, dict[str, str]] = {}
        for label, variables in raw.items():
            if not isinstance(variables, dict):
                continue
            definitions[str(label)] = {
                str(name): str((info or {}).get("description") or "") if isinstance(info, dict) else ""
                for name, info in variables.items()
            }
        return definitions

    return await _with_socket(session, host, port, work)


async def async_fetch_values(session: aiohttp.ClientSession, host: str, port: int, label: str) -> dict[str, Any]:
    """Return ``{variable name: value}`` for one connection label."""

    async def work(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        await ws.send_json(
            {"id": 2, "method": "query", "params": {"path": "variables.values.connection", "input": {"label": label}}}
        )
        while True:
            data = await _receive(ws)
            if data.get("id") != 2:
                continue
            result = data.get("result") or {}
            if result.get("type") == "data":
                values = result.get("data")
                return {str(k): v for k, v in values.items()} if isinstance(values, dict) else {}

    return await _with_socket(session, host, port, work)


def format_value(value: Any, limit: int = 40) -> str:
    """Short, single-line rendering of a variable value for a picker label."""
    if value is None:
        return "—"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Page map: which control sits at which position
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field  # noqa: E402

from .satellite import Location  # noqa: E402

WATCH_RECONNECT_MIN = 2.0
WATCH_RECONNECT_MAX = 60.0
WATCH_SETTLE = 0.3


@dataclass
class PageMap:
    """Companion's grid: every position that holds a control, and the reverse."""

    locations: dict[Location, str] = field(default_factory=dict)
    controls: dict[str, Location] = field(default_factory=dict)
    page_names: dict[int, str] = field(default_factory=dict)

    @classmethod
    def from_init(cls, payload: dict[str, Any]) -> PageMap:
        page_map = cls()
        order = payload.get("order") or []
        pages = payload.get("pages") or {}
        for index, page_id in enumerate(order):
            page = pages.get(page_id) or {}
            page_number = index + 1
            page_map.page_names[page_number] = str(page.get("name") or "")
            for row, columns in (page.get("controls") or {}).items():
                if not isinstance(columns, dict):
                    continue
                for column, control_id in columns.items():
                    if not control_id:
                        continue
                    try:
                        location = Location(page_number, int(row), int(column))
                    except ValueError:
                        continue
                    page_map.locations[location] = str(control_id)
                    page_map.controls[str(control_id)] = location
        return page_map


async def async_fetch_page_map(session: aiohttp.ClientSession, host: str, port: int) -> PageMap:
    """One-shot read of the page map."""

    async def work(ws: aiohttp.ClientWebSocketResponse) -> PageMap:
        await ws.send_json({"id": 3, "method": "subscription", "params": {"path": "pages.watch"}})
        try:
            return await _read_page_init(ws, 3)
        finally:
            await ws.send_json({"id": 3, "method": "subscription.stop"})

    return await _with_socket(session, host, port, work)


async def _read_page_init(ws: aiohttp.ClientWebSocketResponse, msg_id: int) -> PageMap:
    while True:
        data = await _receive(ws)
        if data.get("id") != msg_id:
            continue
        result = data.get("result") or {}
        payload = result.get("data")
        if result.get("type") == "data" and isinstance(payload, dict) and payload.get("type") == "init":
            return PageMap.from_init(payload)


class PageMapWatcher:
    """Keeps a page-map subscription open and reports every change as a fresh full map.

    Change messages carry partial diffs whose shape may evolve, so rather than
    interpreting them the watcher restarts the subscription and reads a new init.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        on_map: Callable[[PageMap], None],
        on_available: Callable[[bool], None] | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._on_map = on_map
        self._on_available = on_available
        self._task: asyncio.Task[None] | None = None
        self.available = False
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name=f"companion-pages-{self.host}")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._set_available(False)

    def _set_available(self, value: bool) -> None:
        if self.available == value:
            return
        self.available = value
        if self._on_available is not None:
            try:
                self._on_available(value)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Page map availability callback failed")

    async def _run(self) -> None:
        backoff = WATCH_RECONNECT_MIN
        while not self._stopping:
            try:
                await self._session_loop()
                backoff = WATCH_RECONNECT_MIN
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, OSError, TimeoutError, CompanionTrpcError) as err:
                _LOGGER.debug("Page map watch on %s:%s failed: %s", self.host, self.port, err)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error watching Companion pages")
            self._set_available(False)
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, WATCH_RECONNECT_MAX)

    async def _session_loop(self) -> None:
        async with self._session.ws_connect(
            _ws_url(self.host, self.port), max_msg_size=MAX_MESSAGE_SIZE, heartbeat=20
        ) as ws:
            msg_id = 100
            while True:
                msg_id += 1
                await ws.send_json({"id": msg_id, "method": "subscription", "params": {"path": "pages.watch"}})
                page_map = await asyncio.wait_for(_read_page_init(ws, msg_id), TRPC_TIMEOUT)
                self._set_available(True)
                try:
                    self._on_map(page_map)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Page map callback failed")
                # Wait for any change notification, then settle briefly so a burst becomes one reread.
                while True:
                    data = await _receive(ws)
                    if data.get("id") != msg_id:
                        continue
                    result = data.get("result") or {}
                    if result.get("type") == "data":
                        break
                    if result.get("type") == "stopped":
                        raise CompanionTrpcError("Page subscription stopped by Companion")
                await asyncio.sleep(WATCH_SETTLE)
                await ws.send_json({"id": msg_id, "method": "subscription.stop"})


# ---------------------------------------------------------------------------
# Control details: type, text and rotary flag, read from each control's config
# ---------------------------------------------------------------------------

NAV_CONTROL_TYPES = {"pageup", "pagedown", "pagenum"}


@dataclass(frozen=True)
class ControlInfo:
    """What a control is, from Companion's point of view."""

    control_id: str
    type: str
    text: str
    rotary: bool

    @property
    def is_navigation(self) -> bool:
        return self.type in NAV_CONTROL_TYPES

    @property
    def label(self) -> str:
        return self.text.strip().splitlines()[0].strip() if self.text.strip() else ""


def _text_from_config(config: dict[str, Any]) -> str:
    """Find the button text in either the layered (5.x) or classic style model."""
    style = config.get("style")
    if isinstance(style, dict):
        layers = style.get("layers")
        if isinstance(layers, list):
            for layer in layers:
                if isinstance(layer, dict) and layer.get("type") == "text":
                    text = layer.get("text")
                    if isinstance(text, dict):
                        # An expression renders differently at runtime; don't name things after the formula.
                        if text.get("isExpression"):
                            return ""
                        value = text.get("value")
                    else:
                        value = text
                    if isinstance(value, str) and value.strip():
                        return value
            return ""
        text = style.get("text")
        if isinstance(text, str):
            return text
    return ""


def control_info_from_config(control_id: str, config: dict[str, Any]) -> ControlInfo:
    options = config.get("options") if isinstance(config.get("options"), dict) else {}
    return ControlInfo(
        control_id=control_id,
        type=str(config.get("type") or ""),
        text=_text_from_config(config),
        rotary=bool(options.get("rotaryActions", False)),
    )


async def async_fetch_control_infos(
    session: aiohttp.ClientSession, host: str, port: int, control_ids: list[str]
) -> dict[str, ControlInfo]:
    """Read type, text and rotary flag for the given controls, one subscription each."""
    if not control_ids:
        return {}

    async def work(ws: aiohttp.ClientWebSocketResponse) -> dict[str, ControlInfo]:
        infos: dict[str, ControlInfo] = {}
        msg_id = 1000
        for control_id in control_ids:
            msg_id += 1
            await ws.send_json(
                {
                    "id": msg_id,
                    "method": "subscription",
                    "params": {"path": "controls.watchControl", "input": {"controlId": control_id}},
                }
            )
            try:
                while True:
                    data = await _receive(ws)
                    if data.get("id") != msg_id:
                        continue
                    result = data.get("result") or {}
                    payload = result.get("data")
                    if result.get("type") == "data" and isinstance(payload, dict) and payload.get("type") == "init":
                        config = payload.get("config")
                        if isinstance(config, dict):
                            infos[control_id] = control_info_from_config(control_id, config)
                        break
            except CompanionTrpcError as err:
                # One missing control (deleted meanwhile) should not spoil the rest.
                _LOGGER.debug("Control %s could not be read: %s", control_id, err)
                continue
            finally:
                await ws.send_json({"id": msg_id, "method": "subscription.stop"})
        return infos

    return await _with_socket(session, host, port, work, timeout=CATALOG_TIMEOUT)
