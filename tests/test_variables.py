"""Variables over the HTTP API: client, sensors, service and options."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import aiohttp
import pytest
import pytest_socket
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.bitfocus_companion.const import (
    CONF_HTTP_HOST,
    CONF_HTTP_PORT,
    CONF_POLL_INTERVAL,
    CONF_VARIABLES,
    DOMAIN,
)
from custom_components.bitfocus_companion.http_api import (
    CompanionHttpClient,
    CompanionHttpError,
    CompanionHttpNotFound,
    VariableRef,
    parse_variable_ref,
)

from .conftest import FakeCompanion, FakeCompanionHttp


def test_parse_variable_ref() -> None:
    assert parse_variable_ref("$(RCV:combo1_livelevelL)") == VariableRef("RCV", "combo1_livelevelL")
    assert parse_variable_ref(" atem:program_input ") == VariableRef("atem", "program_input")
    assert parse_variable_ref("custom:fader").is_custom
    assert str(VariableRef("RCV", "x")) == "$(RCV:x)"
    assert parse_variable_ref("nocolon") is None
    assert parse_variable_ref("a b:c") is None
    assert parse_variable_ref(":name") is None


async def test_http_client(fake_http: FakeCompanionHttp) -> None:
    async with aiohttp.ClientSession() as session:
        client = CompanionHttpClient(session, "127.0.0.1", fake_http.port)
        connections = await client.async_get_connections()
        assert [c.label for c in connections] == ["RCV", "atem"]
        assert connections[0].module_id == "rode-rcv" and connections[0].status == "good"
        assert await client.async_get_variable(VariableRef("RCV", "combo1_livelevelL")) == "-18.5"
        assert await client.async_get_variable(VariableRef("custom", "fader")) == "0"
        with pytest.raises(CompanionHttpNotFound):
            await client.async_get_variable(VariableRef("RCV", "nope"))
        await client.async_set_custom_variable("fader", -12.5)
        assert fake_http.custom["fader"] == -12.5
        await client.async_set_custom_variable("fader", "intro")
        assert fake_http.custom["fader"] == "intro"
        with pytest.raises(CompanionHttpNotFound):
            await client.async_set_custom_variable("missing", 1)
        unreachable = CompanionHttpClient(session, "127.0.0.1", 1)
        with pytest.raises(CompanionHttpError):
            await unreachable.async_get_connections()


async def _setup(hass: HomeAssistant, sat_port: int, http_port: int, variables: list[dict]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: sat_port},
        options={CONF_HTTP_PORT: http_port, CONF_POLL_INTERVAL: 1.0, CONF_VARIABLES: variables},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_variable_sensors_and_polling(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    await _setup(
        hass,
        fake_companion.port,
        fake_http.port,
        [
            {"ref": "RCV:combo1_livelevelL", "unit": "dB"},
            {"ref": "atem:program_input", "name": "Program input"},
            {"ref": "RCV:not_there"},
        ],
    )
    level = hass.states.get("sensor.companion_test_rcv_combo1_livelevell")
    assert level.state == "-18.5"
    assert level.attributes["unit_of_measurement"] == "dB"
    assert level.attributes["state_class"] == "measurement"
    assert level.attributes["variable"] == "$(RCV:combo1_livelevelL)"

    program = hass.states.get("sensor.companion_test_program_input")
    assert program.state == "2"
    assert "unit_of_measurement" not in program.attributes

    missing = hass.states.get("sensor.companion_test_rcv_not_there")
    assert missing.state == STATE_UNKNOWN
    assert missing.attributes["defined"] is False

    fake_http.variables["RCV:combo1_livelevelL"] = "-6"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.5))
    async with asyncio.timeout(5):
        while hass.states.get("sensor.companion_test_rcv_combo1_livelevell").state != "-6":
            await asyncio.sleep(0.02)

    # Buttons still work alongside variables.
    assert hass.states.get("binary_sensor.companion_test_connected").state == "on"


async def test_http_unreachable_marks_sensors_unavailable_and_raises_issue(
    hass: HomeAssistant, fake_companion: FakeCompanion
) -> None:
    entry = await _setup(hass, fake_companion.port, 1, [{"ref": "RCV:combo1_livelevelL"}])
    assert hass.states.get("sensor.companion_test_rcv_combo1_livelevell").state == STATE_UNAVAILABLE
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"http_unreachable_{entry.entry_id}") is not None
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "fader", "value": 1}, blocking=True)


async def test_set_custom_variable_service(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    await _setup(hass, fake_companion.port, fake_http.port, [])
    await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "fader", "value": "-12.5"}, blocking=True)
    assert fake_http.custom["fader"] == -12.5
    await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "fader", "value": "7"}, blocking=True)
    assert fake_http.custom["fader"] == 7
    await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "fader", "value": "true"}, blocking=True)
    assert fake_http.custom["fader"] is True
    await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "fader", "value": "intro"}, blocking=True)
    assert fake_http.custom["fader"] == "intro"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "set_custom_variable", {"name": "nope", "value": 1}, blocking=True)


async def test_options_add_and_remove_variables(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(hass, fake_companion.port, fake_http.port, [])

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == [
        "scan_page",
        "add_button",
        "add_variables",
        "add_variables_by_name",
        "http_settings",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_variables_by_name"}
    )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {}
    labels = [o["value"] for o in result["data_schema"].schema["label"].config["options"]]
    assert labels == ["RCV", "atem", "custom"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"label": "RCV", "names": "combo1_livelevelL\n$(atem:program_input), custom:fader", "unit": "dB"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_VARIABLES] == [
        {"ref": "RCV:combo1_livelevelL", "unit": "dB"},
        {"ref": "atem:program_input", "unit": "dB"},
        {"ref": "custom:fader", "unit": "dB"},
    ]
    assert hass.states.get("sensor.companion_test_rcv_combo1_livelevell").state == "-18.5"
    assert hass.states.get("sensor.companion_test_custom_fader").state == "0"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_variables_by_name"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"label": "RCV", "names": "ghost"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "variables_not_found"}
    assert result["description_placeholders"]["missing"] == "RCV:ghost"

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"names": "nocolon"})
    assert result["errors"] == {"base": "invalid_variable"}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_variables"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_VARIABLES: ["atem:program_input", "custom:fader"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_VARIABLES] == [{"ref": "RCV:combo1_livelevelL", "unit": "dB"}]
    assert hass.states.get("sensor.companion_test_custom_fader") is None


async def test_options_http_settings(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    # The harness allows 127.0.0.1 only; this test also talks to the IPv6 loopback.
    pytest_socket.socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)
    entry = await _setup(hass, fake_companion.port, 1, [])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "http_settings"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HTTP_PORT: 1, CONF_POLL_INTERVAL: 2}
    )
    assert result["errors"] == {"base": "http_unreachable"}
    assert result["description_placeholders"]["url"] == "http://127.0.0.1:1"
    # A different host for the HTTP API than for the satellite socket is allowed.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HTTP_HOST: "::1", CONF_HTTP_PORT: fake_http.port, CONF_POLL_INTERVAL: 2}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HTTP_HOST] == "::1"
    assert entry.options[CONF_HTTP_PORT] == fake_http.port
    assert entry.options[CONF_POLL_INTERVAL] == 2.0
    assert entry.runtime_data.http.host == "::1"
    assert entry.runtime_data.http.base_url == f"http://[::1]:{fake_http.port}"

    # Leaving the host empty falls back to the satellite host and clears the override.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "http_settings"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HTTP_HOST: "", CONF_HTTP_PORT: fake_http.port, CONF_POLL_INTERVAL: 1}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_HTTP_HOST not in entry.options
    assert entry.runtime_data.http.host == "127.0.0.1"


async def test_catalog_fetchers(fake_http: FakeCompanionHttp) -> None:
    from custom_components.bitfocus_companion.trpc import (
        CompanionTrpcError,
        async_fetch_definitions,
        async_fetch_values,
        format_value,
    )

    async with aiohttp.ClientSession() as session:
        definitions = await async_fetch_definitions(session, "127.0.0.1", fake_http.port)
        assert definitions["RCV"] == {"combo1_livelevelL": "Combo 1 live level left", "show_name": "Show name"}
        assert set(definitions) == {"RCV", "atem", "internal"}
        values = await async_fetch_values(session, "127.0.0.1", fake_http.port, "RCV")
        assert values == {"combo1_livelevelL": "-18.5"}
        with pytest.raises(CompanionTrpcError):
            await async_fetch_definitions(session, "127.0.0.1", 1)
    assert format_value(None) == "—"
    assert format_value("CAM\n1") == "CAM 1"
    assert format_value({"a": 1}) == '{"a": 1}'
    assert format_value("x" * 50, limit=10) == "xxxxxxxxx…"


async def test_options_pick_variables_from_catalog(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    entry = await _setup(hass, fake_companion.port, fake_http.port, [{"ref": "RCV:show_name"}])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_variables"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "add_variables"
    assert result["errors"] == {}
    labels = {o["value"]: o["label"] for o in result["data_schema"].schema["label"].config["options"]}
    assert labels == {
        "atem": "atem (2 variables)",
        "custom": "custom (custom variables)",
        "internal": "internal (1 variables)",
        "RCV": "RCV (2 variables)",
    }

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"label": "RCV"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "pick_variables"
    assert result["description_placeholders"]["count"] == "2"
    options = {o["value"]: o["label"] for o in result["data_schema"].schema["names"].config["options"]}
    assert options == {
        "combo1_livelevelL": "combo1_livelevelL = -18.5  ·  Combo 1 live level left",
        "show_name": "show_name = —  ·  Show name  (already added)",
    }

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"names": ["combo1_livelevelL", "show_name"], "unit": "dB"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_VARIABLES] == [{"ref": "RCV:show_name"}, {"ref": "RCV:combo1_livelevelL", "unit": "dB"}]
    assert hass.states.get("sensor.companion_test_rcv_combo1_livelevell").state == "-18.5"

    # A filter narrows the list; an unmatched filter sends you back with an error.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_variables"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"label": "atem", "filter": "bar"})
    assert result["step_id"] == "pick_variables"
    assert [o["value"] for o in result["data_schema"].schema["names"].config["options"]] == ["tbar_1"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_variables"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"label": "atem", "filter": "zzz"})
    assert result["step_id"] == "add_variables"
    assert result["errors"] == {"base": "nothing_matched"}


async def test_options_catalog_unavailable_falls_back(
    hass: HomeAssistant, fake_companion: FakeCompanion, fake_http: FakeCompanionHttp
) -> None:
    fake_http.trpc_enabled = False
    entry = await _setup(hass, fake_companion.port, fake_http.port, [])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_variables"})
    assert result["step_id"] == "add_variables"
    assert result["errors"] == {"base": "catalog_unavailable"}
