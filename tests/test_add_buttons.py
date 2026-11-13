"""Picking missing buttons from Companion's catalogue, and rotary buttons."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bitfocus_companion.const import CONF_BUTTONS, CONF_HTTP_PORT, DOMAIN
from custom_components.bitfocus_companion.trpc import async_fetch_control_infos, control_info_from_config

from .conftest import FakeCompanion, FakeCompanionHttp


def test_control_info_from_config() -> None:
    layered = {
        "type": "button-layered",
        "options": {"rotaryActions": True},
        "style": {"layers": [{"type": "box"}, {"type": "text", "text": {"value": "CAM\n1", "isExpression": False}}]},
    }
    info = control_info_from_config("bank:x", layered)
    assert (info.type, info.text, info.label, info.rotary, info.is_navigation) == (
        "button-layered",
        "CAM\n1",
        "CAM",
        True,
        False,
    )
    dynamic = {
        "type": "button-layered",
        "style": {"layers": [{"type": "text", "text": {"value": "$(x)", "isExpression": True}}]},
    }
    assert control_info_from_config("bank:d", dynamic).label == ""
    classic = {"type": "button", "style": {"text": "REC"}, "options": {}}
    assert control_info_from_config("bank:y", classic).text == "REC"
    nav = control_info_from_config("bank:z", {"type": "pagenum"})
    assert nav.is_navigation and nav.label == "" and nav.rotary is False


async def test_fetch_control_infos(fake_http: FakeCompanionHttp) -> None:
    async with aiohttp.ClientSession() as session:
        infos = await async_fetch_control_infos(
            session, "127.0.0.1", fake_http.port, ["bank:cam1", "bank:nope", "bank:dial"]
        )
    assert set(infos) == {"bank:cam1", "bank:dial"}
    assert infos["bank:cam1"].label == "CAM 1" and infos["bank:dial"].rotary is True


async def _setup(hass: HomeAssistant, sat_port: int, http_port: int, buttons: list[dict]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: sat_port},
        options={CONF_HTTP_PORT: http_port, CONF_BUTTONS: buttons},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_add_missing_buttons(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        [{"page": 1, "row": 0, "column": 2, "name": "Camera 1", "control_id": "bank:cam1"}],
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_buttons"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "add_buttons"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {
        "url": f"http://127.0.0.1:{fake_http.port}",
        "count": "3",
        "total": "4",
    }
    options = {o["value"]: o["label"] for o in result["data_schema"].schema[CONF_BUTTONS].config["options"]}
    # Page-up is left out; the already-added camera is left out; the dial is flagged.
    assert options == {
        "bank:cameras": "Page 1 · 1/1  CAMERAS",
        "bank:rec": "Page 2 · 1/0  REC",
        "bank:dial": "Page 2 · 1/1  VOL  ⟳ rotary",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_BUTTONS: ["bank:rec", "bank:dial"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BUTTONS][1:] == [
        {"page": 2, "row": 1, "column": 0, "control_id": "bank:rec", "rotary": False, "name": "REC"},
        {"page": 2, "row": 1, "column": 1, "control_id": "bank:dial", "rotary": True, "name": "VOL"},
    ]
    assert hass.states.get("button.companion_test_rec") is not None
    vol = hass.states.get("button.companion_test_vol")
    assert vol is not None and vol.attributes["rotary"] is True
    assert hass.states.get("button.companion_test_vol_rotate_left") is not None
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.companion_test_vol_rotate_right"}, blocking=True
    )
    assert fake_companion.rotations == [("2/1/1", 1)]
    assert hass.states.get("button.companion_test_rec_rotate_left") is None

    # Only the CAMERAS button is left to add; adding it empties the list.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_buttons"})
    assert result["description_placeholders"]["count"] == "1"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_BUTTONS: ["bank:cameras"]})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_buttons"})
    assert result["errors"] == {"base": "nothing_missing"}


async def test_rotary_learned_for_existing_buttons(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        [{"page": 2, "row": 1, "column": 1, "name": "Volume", "control_id": "bank:dial"}],
    )
    assert entry.options[CONF_BUTTONS][0]["rotary"] is True
    assert hass.states.get("button.companion_test_volume_rotate_right") is not None


async def test_add_buttons_without_admin_api(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    entry = await _setup(hass, fake_companion.port, 1, [])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_buttons"})
    assert result["errors"] == {"base": "catalog_unavailable"}
