"""Sensors showing the live text and colours of Companion buttons."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CompanionConfigEntry, variable_unique_id
from .const import CONF_COLUMN, CONF_PAGE, CONF_ROW
from .coordinator import VariablesCoordinator
from .entity import CompanionButtonEntity, CompanionEntity
from .helpers import ButtonConfig, VariableConfig

MAX_STATE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant, entry: CompanionConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: list[SensorEntity] = [CompanionButtonStateSensor(entry, button) for button in runtime.buttons]
    if runtime.coordinator is not None:
        entities.extend(CompanionVariableSensor(entry, runtime.coordinator, variable) for variable in runtime.variables)
    async_add_entities(entities)


def _as_number(text: str) -> int | float | None:
    """Numbers as Companion sends them: ``-6`` stays an int, ``-6.5`` a float."""
    value = text.strip()
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CompanionVariableSensor(CoordinatorEntity[VariablesCoordinator], CompanionEntity, SensorEntity):
    """The current value of one Companion variable, polled over the HTTP API."""

    _attr_icon = "mdi:variable"

    def __init__(
        self, entry: CompanionConfigEntry, coordinator: VariablesCoordinator, variable: VariableConfig
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        CompanionEntity.__init__(self, entry)
        self._variable = variable
        self._attr_unique_id = variable_unique_id(entry, variable)
        self._attr_name = variable.display_name
        if variable.unit:
            self._attr_native_unit_of_measurement = variable.unit
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def _raw(self) -> str | None:
        return self.coordinator.data.get(self._variable.ref.key) if self.coordinator.data else None

    @property
    def native_value(self) -> str | int | float | None:
        raw = self._raw
        if raw is None:
            return None
        if self._variable.unit:
            return _as_number(raw)
        return raw[:MAX_STATE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "variable": str(self._variable.ref),
            "connection": self._variable.ref.label,
            "defined": self._raw is not None,
        }


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
