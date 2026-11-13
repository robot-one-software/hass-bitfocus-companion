"""Buttons are followed by Companion's control id across moves; one entity per button."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bitfocus_companion.const import CONF_BUTTON_SENSORS, CONF_BUTTONS, CONF_HTTP_PORT, DOMAIN

from .conftest import FakeCompanion, FakeCompanionHttp


async def _wait_until(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


async def _setup(hass: HomeAssistant, sat_port: int, http_port: int, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: sat_port},
        options={CONF_HTTP_PORT: http_port, **options},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_single_entity_with_attributes_and_optional_sensor(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    fake_companion.texts["1/0/2"] = "CAM\n1"
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1"}]},
    )
    button = hass.states.get("button.companion_test_camera_1")
    assert button is not None and button.state != STATE_UNAVAILABLE
    assert button.attributes["text"] == "CAM\n1"
    assert button.attributes["label"] == "CAM"
    assert button.attributes["background_color"] == "#ff0000"
    assert button.attributes["pressed"] is False
    assert button.attributes["control_id"] == "bank:cam1"
    assert button.attributes["exists"] is True
    assert (button.attributes["page"], button.attributes["row"], button.attributes["column"]) == (1, 0, 2)
    # No text sensor unless asked for.
    assert hass.states.get("sensor.companion_test_camera_1_text") is None

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_BUTTON_SENSORS: True})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.companion_test_camera_1_text").state == "CAM\n1"


async def test_control_id_resolved_and_unique_ids_migrated(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    registry = er.async_get(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_companion.port},
        options={
            CONF_HTTP_PORT: fake_http.port,
            CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1"}],
        },
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    # An entity from a version that keyed buttons by position, renamed by the user.
    registry.async_get_or_create(
        "button", DOMAIN, f"{entry.entry_id}_1_0_2_press", config_entry=entry, suggested_object_id="my_cam_button"
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    registry_entry = registry.async_get("button.my_cam_button")
    assert registry_entry is not None
    assert registry_entry.unique_id == f"{entry.entry_id}_ctl_bank:cam1_press"
    assert entry.options[CONF_BUTTONS] == [
        {"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}
    ]
    assert hass.states.get("button.my_cam_button").attributes["control_id"] == "bank:cam1"


async def test_moved_button_is_followed(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    fake_companion.texts["1/0/2"] = "CAM 1"
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}]},
    )
    await _wait_until(lambda: fake_http._page_subs)  # noqa: SLF001

    # The user drags the button to page 3.
    fake_companion.texts["3/1/1"] = "CAM 1"
    await fake_http.move_control("1/0/2", "3/1/1")
    await _wait_until(lambda: hass.states.get("button.companion_test_camera_1").attributes["page"] == 3)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options[CONF_BUTTONS][0]["page"] == 3 and entry.options[CONF_BUTTONS][0]["row"] == 1
    assert entry.options[CONF_BUTTONS][0]["control_id"] == "bank:cam1"

    await hass.services.async_call("button", "press", {"entity_id": "button.companion_test_camera_1"}, blocking=True)
    assert fake_companion.presses[-2:] == [("3/1/1", True), ("3/1/1", False)]
    await asyncio.sleep(0.05)
    assert fake_companion.subscription_count() == 1

    # Deleting the control makes the entity unavailable instead of reading an empty cell.
    await fake_http.remove_control("3/1/1")
    await _wait_until(lambda: hass.states.get("button.companion_test_camera_1").state == STATE_UNAVAILABLE)


async def test_watcher_answers_keepalive_pings(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}]},
    )
    await _wait_until(lambda: fake_http._page_subs)  # noqa: SLF001
    clients_before = len(fake_http._ws_clients)  # noqa: SLF001
    await fake_http.ping_clients()
    await _wait_until(lambda: fake_http.pongs >= 1)
    await asyncio.sleep(0.1)
    # Still the same connection and subscription; nothing reconnected.
    assert len(fake_http._ws_clients) == clients_before  # noqa: SLF001
    assert fake_http._page_subs  # noqa: SLF001
    # A move afterwards is still followed.
    await fake_http.move_control("1/0/2", "2/0/0")
    await _wait_until(lambda: hass.states.get("button.companion_test_camera_1").attributes["page"] == 2)
