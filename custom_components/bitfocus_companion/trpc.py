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
) -> Any:
    try:
        async with asyncio.timeout(TRPC_TIMEOUT):
            async with session.ws_connect(_ws_url(host, port), max_msg_size=MAX_MESSAGE_SIZE) as ws:
                return await work(ws)
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        raise CompanionTrpcError(f"Companion's admin API at {_ws_url(host, port)} did not answer: {err}") from err


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
    message = await ws.receive()
    if message.type != aiohttp.WSMsgType.TEXT:
        raise CompanionTrpcError(f"Unexpected WebSocket frame {message.type}")
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
