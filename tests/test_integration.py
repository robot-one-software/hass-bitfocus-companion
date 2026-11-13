"""Entities, services and events against the fake server."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.bitfocus_companion.const import CONF_BUTTONS, DOMAIN, EVENT_BUTTON

from .conftest import FakeCompanion


async def _wait_until(condition, timeout: float = 5.0) -> None:
    """Socket delivery is outside Home Assistant's task tracking, so poll briefly."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _setup(
    hass: HomeAssistant, port: int, *, title: str = "Companion (test)", unique_id: str = "test-machine"
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={
            CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1"}, {"page": 2, "row": 1, "column": 0}],
            "button_sensors": True,
        },
        title=title,
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_entities_and_button_press(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    fake_companion.texts["1/0/2"] = "CAM\n1"
    await _setup(hass, fake_companion.port)

    assert hass.states.get("binary_sensor.companion_test_connected").state == STATE_ON

    button = hass.states.get("button.companion_test_camera_1")
    assert button is not None and button.state != STATE_UNAVAILABLE
    assert hass.states.get("button.companion_test_button_2_1_0") is not None

    text = hass.states.get("sensor.companion_test_camera_1_text")
    assert text.state == "CAM\n1"
    assert text.attributes["background_color"] == "#ff0000"
    assert text.attributes["pressed"] is False
    assert text.attributes["page"] == 1

    await hass.services.async_call("button", "press", {"entity_id": "button.companion_test_camera_1"}, blocking=True)
    assert fake_companion.presses == [("1/0/2", True), ("1/0/2", False)]

    events = async_capture_events(hass, EVENT_BUTTON)
    await fake_companion.push_state("1/0/2", "CAM 1", pressed=True)
    await _wait_until(lambda: hass.states.get("sensor.companion_test_camera_1_text").attributes["pressed"] is True)
    await hass.async_block_till_done()
    assert len(events) == 1
    assert (
        events[0].data["page"] == 1
        and events[0].data["pressed"] is True
        and events[0].data["server"] == "Companion (test)"
    )


async def test_services_by_location(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    await _setup(hass, fake_companion.port)

    await hass.services.async_call(DOMAIN, "press", {"page": 3, "row": 1, "column": 4}, blocking=True)
    assert fake_companion.presses == [("3/1/4", True), ("3/1/4", False)]

    await hass.services.async_call(
        DOMAIN, "down", {"page": 3, "row": 1, "column": 4, "server": "Companion (test)"}, blocking=True
    )
    await hass.services.async_call(DOMAIN, "up", {"page": 3, "row": 1, "column": 4}, blocking=True)
    assert fake_companion.presses[-2:] == [("3/1/4", True), ("3/1/4", False)]

    await hass.services.async_call(
        DOMAIN, "rotate", {"page": 3, "row": 1, "column": 4, "direction": "left", "amount": 3}, blocking=True
    )
    assert fake_companion.rotations == [("3/1/4", -3)]

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "press", {"page": 1, "row": 0, "column": 0, "server": "nope"}, blocking=True
        )


async def test_two_servers_need_a_name(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    second = FakeCompanion()
    await second.start()
    try:
        await _setup(hass, fake_companion.port)
        await _setup(hass, second.port, title="Companion (two)", unique_id="other-machine")
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "press", {"page": 1, "row": 0, "column": 0}, blocking=True)
        await hass.services.async_call(
            DOMAIN, "press", {"page": 1, "row": 0, "column": 0, "server": "companion (two)"}, blocking=True
        )
        assert second.presses == [("1/0/0", True), ("1/0/0", False)]
        assert fake_companion.presses == []
    finally:
        await second.stop()


async def test_subscriptions_disabled_raises_issue(hass: HomeAssistant) -> None:
    server = FakeCompanion(subscriptions=False)
    await server.start()
    try:
        entry = await _setup(hass, server.port)
        registry = ir.async_get(hass)
        assert registry.async_get_issue(DOMAIN, f"subscriptions_disabled_{entry.entry_id}") is not None
        assert hass.states.get("button.companion_test_camera_1").state == STATE_UNAVAILABLE
        assert hass.states.get("binary_sensor.companion_test_connected").state == STATE_ON
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "press", {"page": 1, "row": 0, "column": 2}, blocking=True)
    finally:
        await server.stop()


async def test_setup_fails_when_unreachable(hass: HomeAssistant) -> None:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "127.0.0.1", CONF_PORT: port}, unique_id="x")
    entry.add_to_hass(hass)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bitfocus_companion.CONNECT_TIMEOUT", 0.3)
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_connection_loss_marks_unavailable_after_grace(
    hass: HomeAssistant, fake_companion: FakeCompanion
) -> None:
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bitfocus_companion.AVAILABILITY_GRACE", 0.3)
        await _setup(hass, fake_companion.port)
        await fake_companion.stop()
        await _wait_until(lambda: hass.states.get("binary_sensor.companion_test_connected").state == STATE_OFF)
        # Entities ride out a short drop...
        assert hass.states.get("button.companion_test_camera_1").state != STATE_UNAVAILABLE
        # ...and only give up once the grace period has passed.
        await _wait_until(lambda: hass.states.get("button.companion_test_camera_1").state == STATE_UNAVAILABLE)


async def test_brief_drop_keeps_entities_available(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    await _setup(hass, fake_companion.port)
    port = fake_companion.port
    await fake_companion.stop()
    await _wait_until(lambda: hass.states.get("binary_sensor.companion_test_connected").state == STATE_OFF)
    fake_companion.port = port
    await fake_companion.start()
    await _wait_until(lambda: hass.states.get("binary_sensor.companion_test_connected").state == STATE_ON, timeout=10)
    await hass.async_block_till_done()
    assert hass.states.get("button.companion_test_camera_1").state != STATE_UNAVAILABLE
