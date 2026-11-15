"""Services: press, down, up and rotate Companion buttons, by entity or by location."""

from __future__ import annotations

from functools import partial

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_AMOUNT,
    ATTR_DIRECTION,
    ATTR_NAME,
    ATTR_SERVER,
    ATTR_VALUE,
    CONF_COLUMN,
    CONF_PAGE,
    CONF_ROW,
    DOMAIN,
    SERVICE_DOWN,
    SERVICE_PRESS,
    SERVICE_ROTATE,
    SERVICE_SET_CUSTOM_VARIABLE,
    SERVICE_UP,
)
from .http_api import CompanionHttpError, CompanionHttpNotFound
from .satellite import CompanionError, CompanionSubscriptionsDisabled, Location

# Unique ids of entities that belong to one button: ``{entry_id}_{key}{suffix}``.
_BUTTON_ENTITY_SUFFIXES = ("_press", "_left", "_right", "_state", "_image")


def _needs_entity_or_location(data: dict) -> dict:
    has_location = all(field in data for field in (CONF_PAGE, CONF_ROW, CONF_COLUMN))
    if not data.get(ATTR_ENTITY_ID) and not has_location:
        raise vol.Invalid("Choose a button entity, or give page, row and column")
    return data


_LOCATION_FIELDS = {
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    vol.Optional(CONF_PAGE): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional(CONF_ROW): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional(CONF_COLUMN): vol.All(vol.Coerce(int), vol.Range(min=0)),
    vol.Optional(ATTR_SERVER): cv.string,
}

PRESS_SCHEMA = vol.All(vol.Schema(_LOCATION_FIELDS), _needs_entity_or_location)
SET_CUSTOM_VARIABLE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(ATTR_VALUE): vol.Any(cv.string, vol.Coerce(float), bool, int, None),
        vol.Optional(ATTR_SERVER): cv.string,
    }
)

ROTATE_SCHEMA = vol.All(
    vol.Schema(
        {
            **_LOCATION_FIELDS,
            # Optional so a "rotate left" entity can imply its side.
            vol.Optional(ATTR_DIRECTION): vol.In(["left", "right"]),
            vol.Optional(ATTR_AMOUNT, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        }
    ),
    _needs_entity_or_location,
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
    hass.services.async_register(
        DOMAIN, SERVICE_SET_CUSTOM_VARIABLE, partial(_async_set_custom_variable, hass), SET_CUSTOM_VARIABLE_SCHEMA
    )


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


def _targets(hass: HomeAssistant, call: ServiceCall) -> list[tuple[ConfigEntry, Location, str | None]]:
    """Where the call is aimed: (entry, location, implied rotate side) per target."""
    targets: list[tuple[ConfigEntry, Location, str | None]] = []
    for entity_id in call.data.get(ATTR_ENTITY_ID) or []:
        targets.append(_resolve_entity(hass, entity_id))
    if all(field in call.data for field in (CONF_PAGE, CONF_ROW, CONF_COLUMN)):
        entry = _resolve_entry(hass, call.data.get(ATTR_SERVER))
        targets.append((entry, Location(call.data[CONF_PAGE], call.data[CONF_ROW], call.data[CONF_COLUMN]), None))
    return targets


def _resolve_entity(hass: HomeAssistant, entity_id: str) -> tuple[ConfigEntry, Location, str | None]:
    """Map one of this integration's button entities to its server and current position."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    entry = (
        hass.config_entries.async_get_entry(registry_entry.config_entry_id)
        if registry_entry is not None and registry_entry.platform == DOMAIN and registry_entry.config_entry_id
        else None
    )
    if registry_entry is None or entry is None or entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_button", translation_placeholders={"entity": entity_id}
        )
    prefix = f"{entry.entry_id}_"
    unique_id = registry_entry.unique_id or ""
    key: str | None = None
    side: str | None = None
    if unique_id.startswith(prefix):
        for suffix in _BUTTON_ENTITY_SUFFIXES:
            if unique_id.endswith(suffix):
                key = unique_id[len(prefix) : -len(suffix)]
                side = suffix[1:] if suffix in ("_left", "_right") else None
                break
    location = entry.runtime_data.location_of(key) if key else None
    if location is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_button", translation_placeholders={"entity": entity_id}
        )
    return entry, location, side


async def _async_handle(hass: HomeAssistant, action: str, call: ServiceCall) -> None:
    for entry, location, side in _targets(hass, call):
        await _async_act(entry, action, location, side, call)


async def _async_act(entry: ConfigEntry, action: str, location: Location, side: str | None, call: ServiceCall) -> None:
    client = entry.runtime_data.client
    try:
        if action == SERVICE_PRESS:
            await client.async_press(location)
        elif action == SERVICE_DOWN:
            await client.async_set_pressed(location, True)
        elif action == SERVICE_UP:
            await client.async_set_pressed(location, False)
        elif action == SERVICE_ROTATE:
            amount = call.data[ATTR_AMOUNT]
            direction = call.data.get(ATTR_DIRECTION) or side or "right"
            await client.async_rotate(location, amount if direction == "right" else -amount)
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


def _typed(value: object) -> object:
    """Keep numbers and booleans typed; everything else goes across as text."""
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = str(value).strip()
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() and "." not in text else number


async def _async_set_custom_variable(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _resolve_entry(hass, call.data.get(ATTR_SERVER))
    http = entry.runtime_data.http
    name = call.data[ATTR_NAME]
    try:
        await http.async_set_custom_variable(name, _typed(call.data[ATTR_VALUE]))
    except CompanionHttpNotFound as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="custom_variable_not_found",
            translation_placeholders={"name": entry.title, "variable": name},
        ) from err
    except CompanionHttpError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="http_unreachable",
            translation_placeholders={"name": entry.title, "url": http.base_url, "error": str(err)},
        ) from err
