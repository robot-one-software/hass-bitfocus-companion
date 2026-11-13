"""Base entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import CompanionConfigEntry
from .const import DOMAIN, MANUFACTURER, MODEL, signal_connection, signal_state
from .helpers import ButtonConfig
from .satellite import ButtonState


class CompanionEntity(Entity):
    """An entity belonging to one Companion server."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: CompanionConfigEntry) -> None:
        self._entry = entry
        self._client = entry.runtime_data.client
        handshake = self._client.handshake
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=handshake.companion_version.split("+")[0] if handshake else None,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_connection(self._entry.entry_id), self._handle_connection)
        )

    @callback
    def _handle_connection(self, connected: bool) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.connected


class CompanionButtonEntity(CompanionEntity):
    """An entity tied to one Companion button location."""

    def __init__(self, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        super().__init__(entry)
        self._button = button
        self._location = button.location
        self._state: ButtonState | None = self._client.states.get(button.location)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_state(self._entry.entry_id, self._location), self._handle_state)
        )

    @callback
    def _handle_state(self, state: ButtonState) -> None:
        self._state = state
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.connected and self._client.subscriptions_supported
