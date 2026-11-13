"""Sensors showing the live text and colours of Companion buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CompanionConfigEntry
from .const import CONF_COLUMN, CONF_PAGE, CONF_ROW
from .entity import CompanionButtonEntity
from .helpers import ButtonConfig

MAX_STATE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant, entry: CompanionConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(CompanionButtonStateSensor(entry, button) for button in entry.runtime_data.buttons)


class CompanionButtonStateSensor(CompanionButtonEntity, SensorEntity):
    """State is the button's text; colours and pressed state are attributes."""

    _attr_icon = "mdi:card-text-outline"

    def __init__(self, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        super().__init__(entry, button)
        location = button.location
        self._attr_unique_id = f"{entry.entry_id}_{location.page}_{location.row}_{location.column}_state"
        self._attr_name = f"{button.display_name} text"

    @property
    def native_value(self) -> str | None:
        if self._state is None or self._state.text is None:
            return None
        return self._state.text[:MAX_STATE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            CONF_PAGE: self._location.page,
            CONF_ROW: self._location.row,
            CONF_COLUMN: self._location.column,
        }
        if self._state is not None:
            attributes.update(
                {
                    "pressed": self._state.pressed,
                    "background_color": self._state.color,
                    "text_color": self._state.text_color,
                    "font_size": self._state.font_size,
                    "button_type": self._state.type,
                }
            )
        return attributes
