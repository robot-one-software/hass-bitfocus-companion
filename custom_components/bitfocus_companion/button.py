"""Button entities that press a Companion button."""

from __future__ import annotations

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
    """Pressing this entity presses the Companion button, like a Stream Deck key."""

    _attr_icon = "mdi:gesture-tap-button"

    def __init__(self, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        super().__init__(entry, button)
        location = button.location
        self._attr_unique_id = f"{entry.entry_id}_{location.page}_{location.row}_{location.column}_press"
        self._attr_name = button.display_name

    async def async_press(self) -> None:
        try:
            await self._client.async_press(self._location)
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
