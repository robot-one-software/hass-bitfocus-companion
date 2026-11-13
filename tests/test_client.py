"""The satellite client against the fake server (no Home Assistant involved)."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.bitfocus_companion.satellite import (
    ButtonState,
    CompanionConnectionError,
    CompanionSatelliteClient,
    CompanionSubscriptionsDisabled,
    Location,
    async_probe,
)

from .conftest import FakeCompanion


async def test_probe_reads_handshake(fake_companion: FakeCompanion) -> None:
    info = await async_probe("127.0.0.1", fake_companion.port)
    assert info.companion_version.startswith("5.1.1")
    assert info.api_version == "1.14.0"
    assert info.subscriptions_enabled
    assert info.rotary_amount


async def test_probe_refused_port() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(CompanionConnectionError):
        await async_probe("127.0.0.1", port)


async def test_subscribe_press_and_state(fake_companion: FakeCompanion) -> None:
    states: list[tuple[ButtonState, bool]] = []
    connections: list[bool] = []
    client = CompanionSatelliteClient(
        "127.0.0.1",
        fake_companion.port,
        state_callback=lambda state, changed: states.append((state, changed)),
        connection_callback=connections.append,
    )
    fake_companion.texts["1/0/2"] = "CAM 1"
    await client.async_start()
    try:
        await client.async_wait_connected(5)
        assert connections == [True]
        assert client.handshake is not None and client.subscriptions_supported

        location = Location(1, 0, 2)
        await client.async_subscribe(location)
        await asyncio.sleep(0.05)
        assert client.states[location].text == "CAM 1"
        assert client.states[location].color == "#ff0000"
        assert states[-1][1] is False

        await client.async_press(location)
        assert fake_companion.presses == [("1/0/2", True), ("1/0/2", False)]

        await fake_companion.push_state("1/0/2", "CAM 1", pressed=True)
        await asyncio.sleep(0.05)
        assert client.states[location].pressed is True
        assert states[-1][1] is True

        await client.async_rotate(location, -2)
        assert fake_companion.rotations == [("1/0/2", -2)]

        # Pressing a location that was never subscribed subscribes on the fly.
        await client.async_press(Location(1, 1, 1))
        assert fake_companion.presses[-2:] == [("1/1/1", True), ("1/1/1", False)]

        await client.async_unsubscribe(location)
        assert location not in client.states
    finally:
        await client.async_stop()
    assert connections[-1] is False


async def test_subscriptions_disabled_is_reported() -> None:
    server = FakeCompanion(subscriptions=False)
    await server.start()
    client = CompanionSatelliteClient("127.0.0.1", server.port)
    try:
        await client.async_start()
        await client.async_wait_connected(5)
        assert not client.subscriptions_supported
        with pytest.raises(CompanionSubscriptionsDisabled):
            await client.async_press(Location(1, 0, 0))
    finally:
        await client.async_stop()
        await server.stop()


async def test_reconnects_and_resubscribes(fake_companion: FakeCompanion) -> None:
    connections: list[bool] = []
    client = CompanionSatelliteClient("127.0.0.1", fake_companion.port, connection_callback=connections.append)
    await client.async_start()
    try:
        await client.async_wait_connected(5)
        await client.async_subscribe(Location(1, 0, 0))
        port = fake_companion.port
        await fake_companion.stop()
        async with asyncio.timeout(5):
            while client.connected:
                await asyncio.sleep(0.01)
        fake_companion.port = port
        await fake_companion.start()
        await client.async_wait_connected(10)
        await asyncio.sleep(0.1)
        # The subscription came back on its own, so a press works without resubscribing.
        await client.async_press(Location(1, 0, 0))
        assert fake_companion.presses == [("1/0/0", True), ("1/0/0", False)]
        assert connections == [True, False, True]
    finally:
        await client.async_stop()


async def test_scan_page_reports_only_filled_cells(fake_companion: FakeCompanion) -> None:
    fake_companion.texts["1/0/0"] = "CAM 1\nwide"
    fake_companion.texts["1/1/5"] = "REC"
    client = CompanionSatelliteClient("127.0.0.1", fake_companion.port)
    await client.async_start()
    try:
        await client.async_wait_connected(5)
        await client.async_subscribe(Location(1, 0, 0))
        found = await client.async_scan_page(1, max_row=1, max_column=7, settle=0.1)
        assert len(found) == 16
        filled = {str(location): state for location, state in found.items() if not state.is_empty}
        assert set(filled) == {"1/0/0", "1/1/5"}
        assert filled["1/0/0"].label == "CAM 1"
        assert found[Location(1, 0, 3)].is_empty
        # Only the pre-existing subscription survives the scan.
        await asyncio.sleep(0.05)
        assert fake_companion.subscription_count() == 1
        assert Location(1, 0, 0) in client.states and Location(1, 1, 5) not in client.states
    finally:
        await client.async_stop()


async def test_concurrent_subscribe_sends_one_add_sub(fake_companion: FakeCompanion) -> None:
    client = CompanionSatelliteClient("127.0.0.1", fake_companion.port)
    await client.async_start()
    try:
        await client.async_wait_connected(5)
        location = Location(1, 0, 0)
        await asyncio.gather(
            client.async_subscribe(location), client.async_subscribe(location), client.async_press(location)
        )
        assert fake_companion.add_sub_count == 1
        assert fake_companion.presses == [("1/0/0", True), ("1/0/0", False)]
    finally:
        await client.async_stop()
