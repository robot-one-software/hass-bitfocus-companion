"""Shared fixtures: a fake Companion server speaking the satellite protocol."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

import pytest
from aiohttp import web

from custom_components.bitfocus_companion.satellite import format_message, parse_line


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let the test Home Assistant load custom_components."""


@pytest.fixture(autouse=True)
def allow_loopback_sockets(socket_enabled: None) -> None:
    """The fake Companion server listens on 127.0.0.1, which the harness blocks by default."""


class FakeCompanion:
    """Just enough of Companion's satellite server to exercise the client."""

    def __init__(
        self,
        *,
        subscriptions: bool = True,
        send_caps: bool = True,
        version: str = "5.1.1+10000-stable-abc",
        api_version: str = "1.14.0",
    ) -> None:
        self.subscriptions = subscriptions
        self.send_caps = send_caps
        self.version = version
        self.api_version = api_version
        self.port = 0
        self.connections = 0
        self.presses: list[tuple[str, bool]] = []
        self.rotations: list[tuple[str, int]] = []
        self.texts: dict[str, str] = {}
        self.add_sub_count = 0
        self.pressed: dict[str, bool] = {}
        self.press_event = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[tuple[asyncio.StreamWriter, dict[str, str]]] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for writer, _ in list(self._writers):
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def subscription_count(self) -> int:
        return sum(len(subs) for _, subs in self._writers)

    def _state_line(self, sub_id: str, location: str) -> bytes:
        text = base64.b64encode(self.texts.get(location, "").encode()).decode()
        # An empty location renders black with no text, like Companion does.
        return format_message(
            "SUB-STATE",
            SUBID=sub_id,
            TYPE="BUTTON",
            PRESSED=self.pressed.get(location, False),
            COLOR="#ff0000" if location in self.texts else "#000000",
            TEXTCOLOR="#ffffff",
            TEXT=text,
            FONT_SIZE="auto",
        ).encode()

    async def push_state(self, location: str, text: str, pressed: bool = False) -> None:
        """Pretend the button at ``location`` was redrawn."""
        self.texts[location] = text
        self.pressed[location] = pressed
        for writer, subs in self._writers:
            for sub_id, sub_location in subs.items():
                if sub_location == location:
                    writer.write(self._state_line(sub_id, location))
                    await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        subs: dict[str, str] = {}
        self._writers.append((writer, subs))
        writer.write(f'BEGIN CompanionVersion="{self.version}" ApiVersion="{self.api_version}" \n'.encode())
        if self.send_caps:
            writer.write(
                f"CAPS SUBSCRIPTIONS={1 if self.subscriptions else 0} NONSQUARE=1 "
                f'BITMAP_FORMATS="rgb,png,webp" ROTARY_AMOUNT=1 \n'.encode()
            )
        try:
            while raw := await reader.readline():
                line = raw.decode().rstrip("\r\n")
                if line.startswith("PING"):
                    writer.write(f"PONG{line[4:]}\n".encode())
                    continue
                command, _, params = parse_line(line)
                if command == "QUIT":
                    break
                if command in ("ADD-SUB", "SUB-PRESS", "SUB-ROTATE", "REMOVE-SUB") and not self.subscriptions:
                    writer.write(f'{command} ERROR MESSAGE="Subscriptions not enabled"\n'.encode())
                    continue
                sub_id = str(params.get("SUBID", ""))
                if command == "ADD-SUB":
                    self.add_sub_count += 1
                    if sub_id in subs:
                        writer.write(b'ADD-SUB ERROR MESSAGE="SUBID already in use"\n')
                        continue
                    location = str(params["LOCATION"])
                    subs[sub_id] = location
                    writer.write(f"ADD-SUB OK SUBID={sub_id}\n".encode())
                    writer.write(self._state_line(sub_id, location))
                elif command == "REMOVE-SUB":
                    subs.pop(sub_id, None)
                    writer.write(f"REMOVE-SUB OK SUBID={sub_id}\n".encode())
                elif command == "SUB-PRESS":
                    if sub_id not in subs:
                        writer.write(b'SUB-PRESS ERROR MESSAGE="Unknown SUBID"\n')
                        continue
                    pressed = str(params.get("PRESSED")) not in ("0", "false")
                    self.presses.append((subs[sub_id], pressed))
                    self.press_event.set()
                    writer.write(f"SUB-PRESS OK SUBID={sub_id}\n".encode())
                elif command == "SUB-ROTATE":
                    self.rotations.append((subs[sub_id], int(str(params["DIRECTION"]))))
                    writer.write(f"SUB-ROTATE OK SUBID={sub_id}\n".encode())
                else:
                    writer.write(f'ERROR MESSAGE="Unknown command: {command}"\n'.encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if (writer, subs) in self._writers:
                self._writers.remove((writer, subs))
            writer.close()

    async def wait_for_presses(self, count: int, timeout: float = 5.0) -> None:
        async with asyncio.timeout(timeout):
            while len(self.presses) < count:
                self.press_event.clear()
                await self.press_event.wait()


@pytest.fixture
async def fake_companion() -> AsyncIterator[FakeCompanion]:
    server = FakeCompanion()
    await server.start()
    yield server
    await server.stop()


class FakeCompanionHttp:
    """Companion's HTTP API: variables, custom variables, connections."""

    def __init__(self) -> None:
        self.port = 0
        self.variables: dict[str, str] = {"RCV:combo1_livelevelL": "-18.5", "atem:program_input": "2"}
        self.custom: dict[str, object] = {"fader": "0"}
        self.connections = [
            {"id": "a1", "label": "RCV", "moduleId": "rode-rcv", "enabled": True, "status": {"category": "good"}},
            {"id": "b2", "label": "atem", "moduleId": "bmd-atem", "enabled": True, "status": {"category": "good"}},
        ]
        self.requests = 0
        self.definitions: dict[str, dict[str, str]] = {
            "RCV": {"combo1_livelevelL": "Combo 1 live level left", "show_name": "Show name"},
            "atem": {"program_input": "Program input", "tbar_1": "T-bar position"},
            "internal": {"time_hms": "Time"},
        }
        self.trpc_enabled = True
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/connections", self._connections)
        app.router.add_get("/api/variable/{label}/{name}/value", self._variable)
        app.router.add_get("/api/custom-variable/{name}/value", self._custom_get)
        app.router.add_post("/api/custom-variable/{name}/value", self._custom_set)
        app.router.add_get("/trpc", self._trpc)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        # Also answer on IPv6 loopback, so tests can use a second address without DNS.
        await web.TCPSite(self._runner, "::1", self.port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _connections(self, request: web.Request) -> web.Response:
        self.requests += 1
        return web.json_response(self.connections)

    async def _variable(self, request: web.Request) -> web.Response:
        self.requests += 1
        key = f"{request.match_info['label']}:{request.match_info['name']}"
        if key not in self.variables:
            return web.Response(status=404, text="Not found")
        return web.Response(text=self.variables[key])

    async def _custom_get(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.custom:
            return web.Response(status=404, text="Not found")
        return web.Response(text=str(self.custom[name]))

    async def _custom_set(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in self.custom:
            return web.Response(status=404, text="Not found")
        if request.content_type == "application/json":
            self.custom[name] = await request.json()
        else:
            self.custom[name] = request.query.get("value", (await request.text()).strip())
        return web.Response(text="ok")

    async def _trpc(self, request: web.Request) -> web.WebSocketResponse:
        """Just enough of Companion's tRPC-over-WebSocket admin API."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if not self.trpc_enabled:
            await ws.close()
            return ws
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                break
            data = json.loads(message.data)
            msg_id = data.get("id")
            method = data.get("method")
            path = (data.get("params") or {}).get("path")
            if method == "subscription" and path == "variables.definitions.watch":
                await ws.send_json({"id": msg_id, "result": {"type": "started"}})
                variables = {
                    label: {name: {"name": name, "description": desc} for name, desc in names.items()}
                    for label, names in self.definitions.items()
                }
                await ws.send_json(
                    {"id": msg_id, "result": {"type": "data", "data": {"type": "init", "variables": variables}}}
                )
            elif method == "subscription.stop":
                await ws.send_json({"id": msg_id, "result": {"type": "stopped"}})
            elif method == "query" and path == "variables.values.connection":
                label = data["params"]["input"]["label"]
                values = {k.split(":", 1)[1]: v for k, v in self.variables.items() if k.startswith(f"{label}:")}
                if label == "custom":
                    values = dict(self.custom)
                await ws.send_json({"id": msg_id, "result": {"type": "data", "data": values}})
            else:
                await ws.send_json({"id": msg_id, "error": {"message": f"No procedure {path}"}})
        return ws


@pytest.fixture
async def fake_http() -> AsyncIterator[FakeCompanionHttp]:
    server = FakeCompanionHttp()
    await server.start()
    yield server
    await server.stop()
