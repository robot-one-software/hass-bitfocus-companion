"""Bitfocus Companion integration for Home Assistant.

Connects to a Companion server over its satellite protocol so that Companion
buttons can be pressed from Home Assistant and their state can be seen here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_PRESSED,
    ATTR_SERVER,
    ATTR_TEXT,
    CONF_COLUMN,
    CONF_HTTP_HOST,
    CONF_HTTP_PORT,
    CONF_PAGE,
    CONF_POLL_INTERVAL,
    CONF_ROW,
    CONNECT_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_BUTTON,
    ISSUE_HTTP_UNREACHABLE,
    ISSUE_SUBSCRIPTIONS,
    ISSUE_TOO_OLD,
    MIN_SUBSCRIPTION_API,
    signal_connection,
    signal_state,
)
from .coordinator import VariablesCoordinator
from .helpers import ButtonConfig, VariableConfig, buttons_from_options, variables_from_options
from .http_api import DEFAULT_HTTP_PORT, CompanionHttpClient
from .satellite import (
    DEFAULT_PORT,
    ButtonState,
    CompanionError,
    CompanionSatelliteClient,
    CompanionSubscriptionsDisabled,
)
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

# A reconnect usually lands within a second or two; only mark entities unavailable
# once a drop has lasted this long, so brief blips don't litter their history.
AVAILABILITY_GRACE = 5.0

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class CompanionRuntime:
    """Per-entry runtime state."""

    client: CompanionSatelliteClient
    buttons: list[ButtonConfig]
    http: CompanionHttpClient
    variables: list[VariableConfig]
    coordinator: VariablesCoordinator | None = None
    available: bool = True


type CompanionConfigEntry = ConfigEntry[CompanionRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services; entries are set up separately."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CompanionConfigEntry) -> bool:
    """Connect to one Companion server."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)

    @callback
    def _on_state(state: ButtonState, pressed_changed: bool) -> None:
        async_dispatcher_send(hass, signal_state(entry.entry_id, state.location), state)
        if pressed_changed:
            hass.bus.async_fire(
                EVENT_BUTTON,
                {
                    "entry_id": entry.entry_id,
                    ATTR_SERVER: entry.title,
                    CONF_PAGE: state.location.page,
                    CONF_ROW: state.location.row,
                    CONF_COLUMN: state.location.column,
                    ATTR_PRESSED: state.pressed,
                    ATTR_TEXT: state.text,
                },
            )

    grace_handle: asyncio.TimerHandle | None = None

    @callback
    def _set_available(value: bool) -> None:
        runtime: CompanionRuntime | None = getattr(entry, "runtime_data", None)
        if runtime is not None:
            runtime.available = value
        async_dispatcher_send(hass, signal_connection(entry.entry_id), value)

    @callback
    def _grace_expired() -> None:
        nonlocal grace_handle
        grace_handle = None
        if not client.connected:
            _set_available(False)

    @callback
    def _on_connection(connected: bool) -> None:
        nonlocal grace_handle
        if grace_handle is not None:
            grace_handle.cancel()
            grace_handle = None
        if connected:
            _update_issues(hass, entry, client)
            _set_available(True)
        else:
            # The Connected sensor reflects this at once; entities wait out the grace period.
            async_dispatcher_send(hass, signal_connection(entry.entry_id), False)
            grace_handle = hass.loop.call_later(AVAILABILITY_GRACE, _grace_expired)

    @callback
    def _cancel_grace() -> None:
        if grace_handle is not None:
            grace_handle.cancel()

    entry.async_on_unload(_cancel_grace)

    client = CompanionSatelliteClient(host, port, state_callback=_on_state, connection_callback=_on_connection)
    await client.async_start()
    try:
        await client.async_wait_connected(CONNECT_TIMEOUT)
    except TimeoutError as err:
        await client.async_stop()
        raise ConfigEntryNotReady(f"Cannot connect to Companion at {host}:{port}") from err

    buttons = buttons_from_options(entry.options)
    variables = variables_from_options(entry.options)
    http = CompanionHttpClient(
        async_get_clientsession(hass),
        str(entry.options.get(CONF_HTTP_HOST) or host),
        int(entry.options.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)),
    )
    coordinator: VariablesCoordinator | None = None
    if variables:
        coordinator = VariablesCoordinator(
            hass, entry, http, variables, float(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        )
        # A first failure must not block setup: sensors start unavailable and a Repairs
        # issue explains that the HTTP API could not be reached.
        await coordinator.async_refresh()
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_HTTP_UNREACHABLE}_{entry.entry_id}")
    entry.runtime_data = CompanionRuntime(
        client=client, buttons=buttons, http=http, variables=variables, coordinator=coordinator
    )
    _prune_removed_entities(hass, entry, buttons, variables)

    for button in buttons:
        try:
            await client.async_subscribe(button.location)
        except CompanionSubscriptionsDisabled:
            break
        except CompanionError as err:
            _LOGGER.warning("Could not subscribe to Companion button %s: %s", button.location, err)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CompanionConfigEntry) -> bool:
    """Disconnect from a Companion server."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.client.async_stop()
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_SUBSCRIPTIONS}_{entry.entry_id}")
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_TOO_OLD}_{entry.entry_id}")
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_HTTP_UNREACHABLE}_{entry.entry_id}")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: CompanionConfigEntry) -> None:
    """Options changed: reload so the entity list matches."""
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _prune_removed_entities(
    hass: HomeAssistant, entry: CompanionConfigEntry, buttons: list[ButtonConfig], variables: list[VariableConfig]
) -> None:
    """Drop registry entries for buttons and variables the user removed from the options."""
    keep = {f"{entry.entry_id}_connected"}
    for button in buttons:
        location = button.location
        prefix = f"{entry.entry_id}_{location.page}_{location.row}_{location.column}"
        keep.update({f"{prefix}_press", f"{prefix}_state"})
    for variable in variables:
        keep.add(variable_unique_id(entry, variable))
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id not in keep:
            registry.async_remove(registry_entry.entity_id)


@callback
def _update_issues(hass: HomeAssistant, entry: CompanionConfigEntry, client: CompanionSatelliteClient) -> None:
    """Raise or clear repair issues based on what the server told us on connect."""
    handshake = client.handshake
    if handshake is None:
        return
    placeholders = {"name": entry.title, "host": entry.data[CONF_HOST]}

    too_old_id = f"{ISSUE_TOO_OLD}_{entry.entry_id}"
    if handshake.api_version and handshake.api_version_tuple < MIN_SUBSCRIPTION_API:
        ir.async_create_issue(
            hass,
            DOMAIN,
            too_old_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_TOO_OLD,
            translation_placeholders={**placeholders, "version": handshake.companion_version},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, too_old_id)

    subs_id = f"{ISSUE_SUBSCRIPTIONS}_{entry.entry_id}"
    if not handshake.subscriptions_enabled:
        ir.async_create_issue(
            hass,
            DOMAIN,
            subs_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SUBSCRIPTIONS,
            translation_placeholders=placeholders,
            learn_more_url="https://companion.free/for-developers/Satellite-API",
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, subs_id)


def variable_unique_id(entry: ConfigEntry, variable: VariableConfig) -> str:
    return f"{entry.entry_id}_var_{variable.ref.label}_{variable.ref.name}"
