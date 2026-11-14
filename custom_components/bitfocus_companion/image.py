"""Image entities showing each Companion button exactly as it is drawn."""

from __future__ import annotations

import io
import logging
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CompanionConfigEntry
from .const import signal_button
from .entity import CompanionButtonEntity
from .helpers import ButtonConfig, image_size_from_options
from .satellite import ButtonState

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: CompanionConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if image_size_from_options(entry.options) is None:
        return
    async_add_entities(CompanionButtonImage(hass, entry, button) for button in entry.runtime_data.buttons)


def rgb_to_png(pixels: bytes, size: int) -> bytes | None:
    """Encode Companion's raw rgb pixels (pre-API-1.12 servers) as PNG."""
    if size <= 0 or len(pixels) != size * size * 3:
        return None
    from PIL import Image  # noqa: PLC0415 - Pillow ships with Home Assistant; only needed on old servers

    buffer = io.BytesIO()
    Image.frombytes("RGB", (size, size), pixels).save(buffer, format="PNG")
    return buffer.getvalue()


class CompanionButtonImage(CompanionButtonEntity, ImageEntity):
    """The rendered face of one button, refreshed whenever Companion redraws it."""

    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        # ImageEntity keeps an httpx client in ``_client`` for URL-backed images; this entity
        # never fetches URLs, so the satellite client set afterwards takes that name instead.
        ImageEntity.__init__(self, hass)
        CompanionButtonEntity.__init__(self, entry, button)
        self._attr_unique_id = f"{entry.entry_id}_{button.key}_image"
        self._attr_name = f"{button.display_name} image"
        self._source: bytes | None = None
        self._image: bytes | None = None
        self._sync()

    def _sync(self) -> None:
        """Take the latest image from the client, noting when it actually changed."""
        state = self.button_state
        source = state.image if state is not None else None
        if source == self._source:
            return
        self._source = source
        self._image = None
        if state is not None and source is not None:
            if state.image_format == "rgb":
                self._image = rgb_to_png(source, state.image_size or 0)
                self._attr_content_type = "image/png"
            else:
                self._image = source
                self._attr_content_type = state.image_format or "image/png"
        self._attr_image_last_updated = dt_util.utcnow()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The button entity names this image in its attributes; it may have written its
        # state before this entity had an id, so have it refresh now.
        async_dispatcher_send(self.hass, signal_button(self._entry.entry_id, self._key), self.button_state)

    @callback
    def _handle_update(self, state: ButtonState | None) -> None:
        self._sync()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.button_attributes()
