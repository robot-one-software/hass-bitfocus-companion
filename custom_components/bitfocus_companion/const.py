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

ATTR_SERVER = "server"
ATTR_DIRECTION = "direction"
ATTR_AMOUNT = "amount"
ATTR_PRESSED = "pressed"
ATTR_TEXT = "text"

SERVICE_PRESS = "press"
SERVICE_DOWN = "down"
SERVICE_UP = "up"
SERVICE_ROTATE = "rotate"

EVENT_BUTTON = f"{DOMAIN}_button"

ISSUE_SUBSCRIPTIONS = "subscriptions_disabled"
ISSUE_TOO_OLD = "companion_too_old"

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
