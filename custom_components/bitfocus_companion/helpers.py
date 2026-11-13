"""Small shared helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import CONF_BUTTONS, CONF_COLUMN, CONF_CONTROL_ID, CONF_PAGE, CONF_ROW, CONF_UNIT, CONF_VARIABLES
from .http_api import VariableRef, parse_variable_ref
from .satellite import Location


@dataclass(frozen=True)
class ButtonConfig:
    """A Companion button the user chose to expose as entities.

    ``location`` is where it was last seen; ``control_id`` is Companion's stable id for
    the button, which survives moves between positions and pages.
    """

    location: Location
    name: str | None = None
    control_id: str | None = None

    @property
    def key(self) -> str:
        """Stable key for signals and unique ids: the control id when known."""
        if self.control_id:
            return f"ctl_{self.control_id}"
        return f"{self.location.page}_{self.location.row}_{self.location.column}"

    @property
    def display_name(self) -> str:
        return self.name or default_button_name(self.location)

    def to_option(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            CONF_PAGE: self.location.page,
            CONF_ROW: self.location.row,
            CONF_COLUMN: self.location.column,
        }
        if self.name:
            item["name"] = self.name
        if self.control_id:
            item[CONF_CONTROL_ID] = self.control_id
        return item


def default_button_name(location: Location) -> str:
    return f"Button {location}"


def buttons_from_options(options: Mapping[str, Any]) -> list[ButtonConfig]:
    """Read the configured buttons out of a config entry's options."""
    buttons: list[ButtonConfig] = []
    seen: set[Location] = set()
    for item in options.get(CONF_BUTTONS, []):
        try:
            location = Location(int(item[CONF_PAGE]), int(item[CONF_ROW]), int(item[CONF_COLUMN]))
        except (KeyError, TypeError, ValueError):
            continue
        if location in seen:
            continue
        seen.add(location)
        name = item.get("name")
        control_id = item.get(CONF_CONTROL_ID)
        buttons.append(
            ButtonConfig(
                location=location,
                name=str(name).strip() or None if name else None,
                control_id=str(control_id) if control_id else None,
            )
        )
    return buttons


@dataclass(frozen=True)
class VariableConfig:
    """A Companion variable the user chose to expose as a sensor."""

    ref: VariableRef
    name: str | None = None
    unit: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or f"{self.ref.label} {self.ref.name}"


def variables_from_options(options: Mapping[str, Any]) -> list[VariableConfig]:
    """Read the configured variables out of a config entry's options."""
    variables: list[VariableConfig] = []
    seen: set[str] = set()
    for item in options.get(CONF_VARIABLES, []):
        ref = parse_variable_ref(str(item.get("ref", "")))
        if ref is None or ref.key in seen:
            continue
        seen.add(ref.key)
        name = item.get("name")
        unit = item.get(CONF_UNIT)
        variables.append(
            VariableConfig(
                ref=ref,
                name=str(name).strip() or None if name else None,
                unit=str(unit).strip() or None if unit else None,
            )
        )
    return variables
