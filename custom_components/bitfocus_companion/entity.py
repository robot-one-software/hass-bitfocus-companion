"""Base entities."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from . import CompanionConfigEntry
from .const import CONF_COLUMN, CONF_PAGE, CONF_ROW, DOMAIN, MANUFACTURER, MODEL, signal_button, signal_connection
from .helpers import ButtonConfig, image_size_from_options
from .satellite import ButtonState, Location

MAX_STATE_LENGTH = 255


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
        return self._entry.runtime_data.available


class CompanionButtonEntity(CompanionEntity):
    """An entity tied to one Companion button, followed by its control id when known."""

    def __init__(self, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        super().__init__(entry)
        self._key = button.key
        self._initial = button

    @property
    def button(self) -> ButtonConfig:
        return self._entry.runtime_data.button(self._key) or self._initial

    @property
    def location(self) -> Location:
        return self._entry.runtime_data.location_of(self._key) or self._initial.location

    @property
    def button_state(self) -> ButtonState | None:
        return self._client.states.get(self.location)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_button(self._entry.entry_id, self._key), self._handle_update)
        )

    @callback
    def _handle_update(self, state: ButtonState | None) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        runtime = self._entry.runtime_data
        return runtime.available and self._client.subscriptions_supported and runtime.button_exists(self._key)

    def button_attributes(self) -> dict[str, Any]:
        """Everything Companion tells us about the button, plus where it is."""
        location = self.location
        button = self.button
        attributes: dict[str, Any] = {
            CONF_PAGE: location.page,
            CONF_ROW: location.row,
            CONF_COLUMN: location.column,
            "control_id": button.control_id,
            "rotary": bool(button.rotary),
            "exists": self._entry.runtime_data.button_exists(self._key),
        }
        if image_size_from_options(self._entry.options) is not None:
            # The image entity serving this button's rendered face, for dashboards to show.
            attributes["image_entity"] = er.async_get(self.hass).async_get_entity_id(
                "image", DOMAIN, f"{self._entry.entry_id}_{self._key}_image"
            )
        state = self.button_state
        if state is not None:
            attributes.update(
                {
                    "text": state.text,
                    "label": state.label,
                    "background_color": state.color,
                    "text_color": state.text_color,
                    "pressed": state.pressed,
                    "font_size": state.font_size,
                    "button_type": state.type,
                }
            )
        return attributes
