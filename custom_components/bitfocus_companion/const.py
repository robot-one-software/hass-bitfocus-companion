"""Constants for the Bitfocus Companion integration."""

from __future__ import annotations

from .satellite import Location

DOMAIN = "bitfocus_companion"

MANUFACTURER = "Bitfocus"
MODEL = "Companion"

CONF_BUTTONS = "buttons"
CONF_PAGE = "page"
CONF_ROW = "row"
CONF_COLUMN = "column"
CONF_MACHINE_ID = "machine_id"
CONF_CONTROL_ID = "control_id"
CONF_ROTARY = "rotary"
CONF_BUTTON_SENSORS = "button_sensors"
CONF_BUTTON_IMAGES = "button_images"
CONF_IMAGE_SIZE = "image_size"
CONF_HTTP_PORT = "http_port"
CONF_HTTP_HOST = "http_host"
CONF_VARIABLES = "variables"
CONF_POLL_INTERVAL = "poll_interval"
CONF_UNIT = "unit"

DEFAULT_POLL_INTERVAL = 1.0
# Pixel sizes a button image can be streamed at. 72 is what a classic Stream Deck key shows.
IMAGE_SIZES = (72, 96, 144)
DEFAULT_IMAGE_SIZE = 72

ATTR_SERVER = "server"
ATTR_DIRECTION = "direction"
ATTR_AMOUNT = "amount"
ATTR_PRESSED = "pressed"
ATTR_TEXT = "text"
ATTR_VALUE = "value"
ATTR_NAME = "name"

SERVICE_PRESS = "press"
SERVICE_DOWN = "down"
SERVICE_UP = "up"
SERVICE_ROTATE = "rotate"
SERVICE_SET_CUSTOM_VARIABLE = "set_custom_variable"

EVENT_BUTTON = f"{DOMAIN}_button"

ISSUE_SUBSCRIPTIONS = "subscriptions_disabled"
ISSUE_TOO_OLD = "companion_too_old"
ISSUE_HTTP_UNREACHABLE = "http_unreachable"
ISSUE_PAGES_UNAVAILABLE = "pages_unavailable"

# Companion gained button subscriptions with satellite API 1.10 (Companion 4.3).
MIN_SUBSCRIPTION_API = (1, 10, 0)

CONNECT_TIMEOUT = 10.0

# mDNS service types Companion advertises its satellite TCP port under.
# The long form is what 5.0 and 5.1 announce; the short form arrives in 5.2.
ZEROCONF_TYPES = ("_companion-satellite-tcp._tcp.local.", "_comp-sat-tcp._tcp.local.")


def signal_connection(entry_id: str) -> str:
    """Dispatcher signal fired when a server connects or disconnects."""
    return f"{DOMAIN}_{entry_id}_connection"


def signal_state(entry_id: str, location: Location) -> str:
    """Dispatcher signal fired when a subscribed button is redrawn."""
    return f"{DOMAIN}_{entry_id}_state_{location.page}_{location.row}_{location.column}"


def signal_button(entry_id: str, key: str) -> str:
    """Dispatcher signal fired when a configured button's state or position changes."""
    return f"{DOMAIN}_{entry_id}_button_{key}"
