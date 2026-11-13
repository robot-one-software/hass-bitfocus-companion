"""Button entities that press a Companion button and carry its live state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CompanionConfigEntry
from .const import DOMAIN
from .entity import CompanionButtonEntity
from .helpers import ButtonConfig
from .satellite import CompanionError, CompanionSubscriptionsDisabled


async def async_setup_entry(
    hass: HomeAssistant, entry: CompanionConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(CompanionPressButton(entry, button) for button in entry.runtime_data.buttons)


class CompanionPressButton(CompanionButtonEntity, ButtonEntity):
    """Pressing this entity presses the Companion button, like a Stream Deck key.

    Attributes carry the button's live text, colours and pressed state.
    """

    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        super().__init__(entry, button)
        self._attr_unique_id = f"{entry.entry_id}_{button.key}_press"
        self._attr_name = button.display_name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.button_attributes()

    async def async_press(self) -> None:
        try:
            await self._client.async_press(self.location)
        except CompanionSubscriptionsDisabled as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="subscriptions_disabled",
                translation_placeholders={"name": self._entry.title},
            ) from err
        except CompanionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"name": self._entry.title, "error": str(err)},
            ) from err
