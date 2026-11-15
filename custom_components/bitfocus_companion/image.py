"""Image entities showing each Companion button exactly as it is drawn."""

from __future__ import annotations

import asyncio
import io
import logging
from time import monotonic
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from . import CompanionConfigEntry
from .const import DOMAIN, ISSUE_IMAGES_RECORDED, signal_button
from .entity import CompanionButtonEntity
from .helpers import ButtonConfig, image_size_from_options
from .satellite import ButtonState

_LOGGER = logging.getLogger(__name__)

# Companion can redraw a meter button many times a second; Home Assistant gets at most
# this many frames per second per button, always the latest one.
MAX_FRAMES_PER_SECOND = 5
MIN_FRAME_INTERVAL = 1 / MAX_FRAMES_PER_SECOND


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
    # Every frame is a state change; keep the recorder from storing a copy of the
    # button attributes with each one.
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, hass: HomeAssistant, entry: CompanionConfigEntry, button: ButtonConfig) -> None:
        # ImageEntity keeps an httpx client in ``_client`` for URL-backed images; this entity
        # never fetches URLs, so the satellite client set afterwards takes that name instead.
        ImageEntity.__init__(self, hass)
        CompanionButtonEntity.__init__(self, entry, button)
        self._attr_unique_id = f"{entry.entry_id}_{button.key}_image"
        self._attr_name = f"{button.display_name} image"
        self._source: bytes | None = None
        self._image: bytes | None = None
        self._last_write = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None
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
        _check_recorder(self.hass, self._entry, self.entity_id)

    async def async_will_remove_from_hass(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_update(self, state: ButtonState | None) -> None:
        self._sync()
        if self._flush_handle is not None:
            return  # a write is already due; it will carry the latest frame
        since_last = monotonic() - self._last_write
        if since_last >= MIN_FRAME_INTERVAL:
            self._flush()
        else:
            self._flush_handle = self.hass.loop.call_later(MIN_FRAME_INTERVAL - since_last, self._flush)

    @callback
    def _flush(self) -> None:
        self._flush_handle = None
        self._last_write = monotonic()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.button_attributes()


def recorder_exclude_glob(entry: CompanionConfigEntry) -> str:
    """An entity glob matching this server's button images, for the recorder's exclude list."""
    return f"image.{slugify(entry.title)}_*_image"


@callback
def _check_recorder(hass: HomeAssistant, entry: CompanionConfigEntry, entity_id: str) -> None:
    """Point out, once, that button images are filling the history database.

    The issue is not persistent, so it comes back on each start only while an image
    entity is still being recorded.
    """
    if "recorder" not in hass.config.components:
        return
    from homeassistant.components.recorder import is_entity_recorded  # noqa: PLC0415

    if not is_entity_recorded(hass, entity_id):
        return
    # Every image entity of this server, so the notice can be pasted as-is.
    registry = er.async_get(hass)
    image_ids = sorted(
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id) if e.domain == "image"
    ) or [entity_id]
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_IMAGES_RECORDED}_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_IMAGES_RECORDED,
        translation_placeholders={
            "name": entry.title,
            "glob": recorder_exclude_glob(entry),
            "entities": "\n".join(f"      - {image_id}" for image_id in image_ids),
        },
        learn_more_url="https://github.com/robot-one-software/hass-bitfocus-companion#button-images",
    )
