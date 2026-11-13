"""Services: press, down, up and rotate any Companion button by location."""

from __future__ import annotations

from functools import partial

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_AMOUNT,
    ATTR_DIRECTION,
    ATTR_SERVER,
    CONF_COLUMN,
    CONF_PAGE,
    CONF_ROW,
    DOMAIN,
    SERVICE_DOWN,
    SERVICE_PRESS,
    SERVICE_ROTATE,
    SERVICE_UP,
)
from .satellite import CompanionError, CompanionSubscriptionsDisabled, Location

_LOCATION_FIELDS = {
    vol.Required(CONF_PAGE): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Required(CONF_ROW): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Required(CONF_COLUMN): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional(ATTR_SERVER): cv.string,
}

PRESS_SCHEMA = vol.Schema(_LOCATION_FIELDS)
ROTATE_SCHEMA = vol.Schema(
    {
        **_LOCATION_FIELDS,
        vol.Required(ATTR_DIRECTION): vol.In(["left", "right"]),
        vol.Optional(ATTR_AMOUNT, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services once per Home Assistant instance."""
    if hass.services.has_service(DOMAIN, SERVICE_PRESS):
        return
    hass.services.async_register(DOMAIN, SERVICE_PRESS, partial(_async_handle, hass, SERVICE_PRESS), PRESS_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DOWN, partial(_async_handle, hass, SERVICE_DOWN), PRESS_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UP, partial(_async_handle, hass, SERVICE_UP), PRESS_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ROTATE, partial(_async_handle, hass, SERVICE_ROTATE), ROTATE_SCHEMA)


def _resolve_entry(hass: HomeAssistant, server: str | None):
    entries = [entry for entry in hass.config_entries.async_entries(DOMAIN) if entry.state is ConfigEntryState.LOADED]
    if server:
        wanted = server.strip().casefold()
        for entry in entries:
            candidates = {entry.entry_id, entry.unique_id or "", entry.title, entry.data.get("host", "")}
            if wanted in {candidate.casefold() for candidate in candidates if candidate}:
                return entry
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="server_not_found",
            translation_placeholders={"server": server},
        )
    if len(entries) == 1:
        return entries[0]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_server")
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="server_required")


async def _async_handle(hass: HomeAssistant, action: str, call: ServiceCall) -> None:
    entry = _resolve_entry(hass, call.data.get(ATTR_SERVER))
    client = entry.runtime_data.client
    location = Location(call.data[CONF_PAGE], call.data[CONF_ROW], call.data[CONF_COLUMN])
    try:
        if action == SERVICE_PRESS:
            await client.async_press(location)
        elif action == SERVICE_DOWN:
            await client.async_set_pressed(location, True)
        elif action == SERVICE_UP:
            await client.async_set_pressed(location, False)
        elif action == SERVICE_ROTATE:
            amount = call.data[ATTR_AMOUNT]
            await client.async_rotate(location, amount if call.data[ATTR_DIRECTION] == "right" else -amount)
    except CompanionSubscriptionsDisabled as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="subscriptions_disabled",
            translation_placeholders={"name": entry.title},
        ) from err
    except CompanionError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"name": entry.title, "error": str(err)},
        ) from err
