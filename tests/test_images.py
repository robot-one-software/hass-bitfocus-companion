"""Button image entities: Companion's rendered button faces served by Home Assistant."""

from __future__ import annotations

import asyncio
import io

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.bitfocus_companion.const import (
    CONF_BUTTON_IMAGES,
    CONF_BUTTONS,
    CONF_IMAGE_SIZE,
    DOMAIN,
)
from custom_components.bitfocus_companion.helpers import image_size_from_options
from custom_components.bitfocus_companion.image import rgb_to_png
from custom_components.bitfocus_companion.satellite import decode_bitmap

from .conftest import FakeCompanion

IMAGE_ENTITY = "image.companion_test_camera_1_image"
BUTTON_ENTITY = "button.companion_test_camera_1"


async def _wait_until(condition, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


async def _setup(hass: HomeAssistant, port: int, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={CONF_BUTTONS: [{"page": 1, "row": 0, "column": 2, "name": "Camera 1"}], **options},
        title="Companion (test)",
        unique_id="test-machine",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _fetch(hass_client: ClientSessionGenerator, hass: HomeAssistant, entity_id: str) -> tuple[str, Image.Image]:
    state = hass.states.get(entity_id)
    client = await hass_client()
    response = await client.get(state.attributes["entity_picture"])
    assert response.status == 200
    body = await response.read()
    return response.content_type, Image.open(io.BytesIO(body))


def test_decode_bitmap_forms() -> None:
    import base64

    png = base64.b64encode(b"\x89PNG...").decode()
    assert decode_bitmap(f"data:image/png;base64,{png}") == (b"\x89PNG...", "image/png")
    assert decode_bitmap(base64.b64encode(b"\xff\x00\x00").decode()) == (b"\xff\x00\x00", "rgb")
    assert decode_bitmap("") is None
    assert decode_bitmap(True) is None
    assert decode_bitmap("data:image/png;base64") is None


def test_rgb_to_png_round_trip() -> None:
    pixels = bytes([0, 255, 0] * 4)
    png = rgb_to_png(pixels, 2)
    assert png is not None
    image = Image.open(io.BytesIO(png))
    assert image.size == (2, 2) and image.getpixel((1, 1)) == (0, 255, 0)
    assert rgb_to_png(pixels, 3) is None


def test_image_size_from_options() -> None:
    assert image_size_from_options({}) is None
    assert image_size_from_options({CONF_BUTTON_IMAGES: False, CONF_IMAGE_SIZE: 96}) is None
    assert image_size_from_options({CONF_BUTTON_IMAGES: True}) == 72
    assert image_size_from_options({CONF_BUTTON_IMAGES: True, CONF_IMAGE_SIZE: "96"}) == 96
    assert image_size_from_options({CONF_BUTTON_IMAGES: True, CONF_IMAGE_SIZE: 50}) == 72


async def test_images_off_by_default(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    await _setup(hass, fake_companion.port)
    assert hass.states.get(IMAGE_ENTITY) is None
    assert "image_entity" not in hass.states.get(BUTTON_ENTITY).attributes
    assert "BITMAP" not in fake_companion.last_add_sub


async def test_image_entity_serves_png_and_follows_redraws(
    hass: HomeAssistant, fake_companion: FakeCompanion, hass_client: ClientSessionGenerator
) -> None:
    fake_companion.texts["1/0/2"] = "CAM 1"
    await _setup(hass, fake_companion.port, **{CONF_BUTTON_IMAGES: True, CONF_IMAGE_SIZE: 96})

    assert fake_companion.last_add_sub["BITMAP"] == "96"
    assert fake_companion.last_add_sub["BITMAP_FORMAT"] == "png"

    await _wait_until(lambda: hass.states.get(IMAGE_ENTITY) is not None and hass.states.get(IMAGE_ENTITY).state)
    first = hass.states.get(IMAGE_ENTITY)
    assert first.attributes["text"] == "CAM 1"
    assert hass.states.get(BUTTON_ENTITY).attributes["image_entity"] == IMAGE_ENTITY

    content_type, image = await _fetch(hass_client, hass, IMAGE_ENTITY)
    assert content_type == "image/png"
    assert image.size == (96, 96)
    assert image.getpixel((10, 10)) == (255, 0, 0)

    # Companion redraws the button: a new image, a new timestamp.
    await fake_companion.push_state("1/0/2", "")
    await _wait_until(lambda: hass.states.get(IMAGE_ENTITY).state != first.state)
    _, image = await _fetch(hass_client, hass, IMAGE_ENTITY)
    assert image.getpixel((10, 10)) == (0, 0, 0)

    # An identical redraw does not bump the timestamp.
    second = hass.states.get(IMAGE_ENTITY).state
    await fake_companion.push_state("1/0/2", "")
    await asyncio.sleep(0.2)
    assert hass.states.get(IMAGE_ENTITY).state == second


async def test_old_companion_sends_raw_rgb(hass: HomeAssistant, hass_client: ClientSessionGenerator) -> None:
    """Before API 1.12 there is no CAPS line and bitmaps are raw rgb; we still serve PNG."""
    server = FakeCompanion(send_caps=False, api_version="1.11.0")
    await server.start()
    try:
        server.texts["1/0/2"] = "CAM 1"
        await _setup(hass, server.port, **{CONF_BUTTON_IMAGES: True})
        assert server.last_add_sub["BITMAP"] == "72"
        assert "BITMAP_FORMAT" not in server.last_add_sub
        await _wait_until(lambda: (state := hass.states.get(IMAGE_ENTITY)) is not None and state.state)
        content_type, image = await _fetch(hass_client, hass, IMAGE_ENTITY)
        assert content_type == "image/png"
        assert image.size == (72, 72)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    finally:
        await server.stop()


async def test_turning_images_off_removes_entities(hass: HomeAssistant, fake_companion: FakeCompanion) -> None:
    entry = await _setup(hass, fake_companion.port, **{CONF_BUTTON_IMAGES: True})
    await _wait_until(lambda: hass.states.get(IMAGE_ENTITY) is not None)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_BUTTON_IMAGES: False})
    await hass.async_block_till_done()
    assert hass.states.get(IMAGE_ENTITY) is None
    assert "image_entity" not in hass.states.get(BUTTON_ENTITY).attributes


@pytest.mark.parametrize("size", [72, 144])
async def test_sizes_are_requested(hass: HomeAssistant, fake_companion: FakeCompanion, size: int) -> None:
    await _setup(hass, fake_companion.port, **{CONF_BUTTON_IMAGES: True, CONF_IMAGE_SIZE: size})
    assert fake_companion.last_add_sub["BITMAP"] == str(size)
