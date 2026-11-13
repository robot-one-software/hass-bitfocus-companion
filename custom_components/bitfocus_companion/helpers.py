"""Small shared helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .const import CONF_BUTTONS, CONF_COLUMN, CONF_PAGE, CONF_ROW
from .satellite import Location


@dataclass(frozen=True)
class ButtonConfig:
    """A Companion button the user chose to expose as entities."""

    location: Location
    name: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or default_button_name(self.location)


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
        buttons.append(ButtonConfig(location=location, name=str(name).strip() or None if name else None))
    return buttons
