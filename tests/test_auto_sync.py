"""Opt-in automatic sync of the button list with Companion."""

from __future__ import annotations

import asyncio

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bitfocus_companion import const
from custom_components.bitfocus_companion.const import (
    CONF_AUTO_ADD,
    CONF_AUTO_REMOVE,
    CONF_BUTTONS,
    CONF_HTTP_PORT,
    DOMAIN,
)

from .conftest import FakeCompanion, FakeCompanionHttp, _layered


async def _wait_until(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


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


@pytest.fixture(autouse=True)
def quick_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    import custom_components.bitfocus_companion as integration

    monkeypatch.setattr(const, "AUTO_SYNC_DEBOUNCE", 0.05)
    monkeypatch.setattr(integration, "AUTO_SYNC_DEBOUNCE", 0.05)


async def test_auto_add_creates_every_button_at_setup(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    fake_companion.texts["3/0/0"] = "Pergola Lights"
    entry = await _setup(hass, fake_companion.port, fake_http.port, {CONF_AUTO_ADD: True})

    # Every real control, named after its text; the page-up key is skipped; the dial is rotary.
    assert hass.states.get("button.companion_test_cam_1") is not None
    assert hass.states.get("button.companion_test_cameras") is not None
    assert hass.states.get("button.companion_test_rec") is not None
    assert hass.states.get("button.companion_test_vol_rotate_left") is not None
    assert hass.states.get("button.companion_test_pergola_lights") is not None
    assert not [s for s in hass.states.async_entity_ids("button") if "pageup" in s or "1_0_0" in s]
    stored = {b["control_id"]: b for b in entry.options[CONF_BUTTONS]}
    assert stored["bank:pergola"]["name"] == "Pergola Lights"
    assert stored["bank:dial"]["rotary"] is True
    assert "bank:pageup" not in stored


async def test_auto_add_follows_buttons_created_later(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {
            CONF_AUTO_ADD: True,
            CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}],
        },
    )
    before = len(entry.options[CONF_BUTTONS])
    await _wait_until(lambda: fake_http._page_subs)  # noqa: SLF001

    # A brand-new button drawn in Companion.
    fake_companion.texts["3/2/2"] = "Lights Off"
    await fake_http.add_control("3/2/2", "bank:new", _layered("Lights Off"))
    await _wait_until(lambda: hass.states.get("button.companion_test_lights_off") is not None, timeout=10)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert len(entry.options[CONF_BUTTONS]) == before + 1
    new = [b for b in entry.options[CONF_BUTTONS] if b["control_id"] == "bank:new"][0]
    assert new["page"] == 3 and new["row"] == 2 and new["column"] == 2 and new["name"] == "Lights Off"


async def test_auto_remove_drops_deleted_buttons(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {
            CONF_AUTO_REMOVE: True,
            CONF_BUTTONS: [
                {"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"},
                {"page": 1, "row": 1, "column": 1, "name": "Cameras", "control_id": "bank:cameras"},
            ],
        },
    )
    await _wait_until(lambda: fake_http._page_subs)  # noqa: SLF001
    assert hass.states.get("button.companion_test_camera_1") is not None

    await fake_http.remove_control("1/0/2")
    await _wait_until(lambda: er.async_get(hass).async_get("button.companion_test_camera_1") is None, timeout=10)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert [b["control_id"] for b in entry.options[CONF_BUTTONS]] == ["bank:cameras"]
    assert hass.states.get("button.companion_test_cameras").state != STATE_UNAVAILABLE


async def test_sync_is_off_by_default(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        {CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}]},
    )
    await _wait_until(lambda: fake_http._page_subs)  # noqa: SLF001
    await fake_http.add_control("3/2/2", "bank:new", _layered("Lights Off"))
    await fake_http.remove_control("1/0/2")
    await _wait_until(lambda: hass.states.get("button.companion_test_camera_1").state == STATE_UNAVAILABLE)
    await asyncio.sleep(0.3)
    assert hass.states.get("button.companion_test_lights_off") is None
    assert len(entry.options[CONF_BUTTONS]) == 1
