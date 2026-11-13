"""Config and options flows."""

from __future__ import annotations

from ipaddress import ip_address

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bitfocus_companion.const import CONF_BUTTONS, DOMAIN

from .conftest import FakeCompanion


def _zeroconf_info(port: int) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        hostname="studio-mac.local.",
        name="Companion (Studio-Mac)._companion-satellite-tcp._tcp.local.",
        port=port,
        properties={"id": "machine-123", "version": "5.1.1", "protocolVersion": "1.14.0"},
        type="_companion-satellite-tcp._tcp.local.",
    )


async def test_user_flow_creates_entry(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_companion.port}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Companion (127.0.0.1)"
    assert result["data"][CONF_HOST] == "127.0.0.1"
    assert result["data"][CONF_PORT] == fake_companion.port
    assert result["data"]["companion_version"] == "5.1.1"
    assert result["result"].unique_id == f"127.0.0.1:{fake_companion.port}"


async def test_user_flow_cannot_connect(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_flow(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf_info(fake_companion.port)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"]["name"] == "Companion (Studio-Mac)"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Companion (Studio-Mac)"
    assert result["result"].unique_id == "machine-123"
    assert result["data"]["machine_id"] == "machine-123"

    # Discovering the same server again is a no-op.
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf_info(fake_companion.port)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_add_and_remove(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_companion.port},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == [
        "scan_page",
        "add_button",
        "add_variables",
        "add_variables_by_name",
        "http_settings",
    ]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_button"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"page": 1, "row": 0, "column": 2, "name": "Camera 1"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BUTTONS] == [{"page": 1, "row": 0, "column": 2, "name": "Camera 1"}]
    assert hass.states.get("button.companion_test_camera_1") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_button"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"page": 1, "row": 0, "column": 2})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_added"}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_button"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_BUTTONS: ["1/0/2"]})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BUTTONS] == []
    assert hass.states.get("button.companion_test_camera_1") is None


async def test_options_flow_scan_page(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    fake_companion.texts["1/0/2"] = "CAM 1\nwide"
    fake_companion.texts["1/2/7"] = "REC"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_companion.port},
        options={CONF_BUTTONS: [{"page": 1, "row": 2, "column": 7, "name": "Record"}]},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "scan_page"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "scan_page"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bitfocus_companion.satellite.SCAN_SETTLE_SECONDS", 0.1)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"page": 1, "max_row": 3, "max_column": 7}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick_buttons"
    assert result["description_placeholders"] == {"count": "2"}
    labels = [option["label"] for option in result["data_schema"].schema[CONF_BUTTONS].config["options"]]
    assert labels == ["1/0/2  CAM 1", "1/2/7  REC (already added)"]

    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_BUTTONS: ["1/0/2", "1/2/7"]})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BUTTONS] == [
        {"page": 1, "row": 2, "column": 7, "name": "Record"},
        {"page": 1, "row": 0, "column": 2, "name": "CAM 1"},
    ]
    assert hass.states.get("button.companion_test_cam_1") is not None


async def test_options_flow_scan_empty_page(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_companion.port}, title="C", unique_id="m"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "scan_page"})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("custom_components.bitfocus_companion.satellite.SCAN_SETTLE_SECONDS", 0.05)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"page": 7, "max_row": 0, "max_column": 1}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "scan_page"
    assert result["errors"] == {"base": "nothing_found"}
