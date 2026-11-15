"""Point out entities that are filling the history database.

Companion variables such as audio levels change on every poll, so a sensor for
one writes a history row every second for as long as Home Assistant runs. There
is no way for an integration to exclude its own entities from the recorder, so
the next best thing is to notice the churn and hand the user the exact lines to
paste. The notice is not persistent: it comes back on each start only while a
fast-changing entity is still being recorded.
"""

from __future__ import annotations

from collections import deque
from time import monotonic

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_VARIABLES_RECORDED

# "Fast-changing": at least this many state changes inside this many seconds.
CHURN_CHANGES = 60
CHURN_WINDOW = 300.0

LEARN_MORE_URL = "https://github.com/robot-one-software/hass-bitfocus-companion#variables-optional"


class ChurnTracker:
    """Counts an entity's state changes and says when they are fast enough to matter."""

    def __init__(self) -> None:
        self._times: deque[float] = deque(maxlen=CHURN_CHANGES)

    def note_change(self) -> bool:
        """Record a change; True once the recent changes exceed the threshold."""
        now = monotonic()
        self._times.append(now)
        return len(self._times) == CHURN_CHANGES and now - self._times[0] <= CHURN_WINDOW


def entity_is_recorded(hass: HomeAssistant, entity_id: str) -> bool:
    if "recorder" not in hass.config.components:
        return False
    from homeassistant.components.recorder import is_entity_recorded  # noqa: PLC0415

    return is_entity_recorded(hass, entity_id)


@callback
def note_churny_entity(hass: HomeAssistant, entry, entity_id: str) -> None:
    """Add a fast-changing, recorded entity to the server's notice (creating it if needed)."""
    if not entity_is_recorded(hass, entity_id):
        return
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        return
    if entity_id in runtime.churny_entities:
        return
    runtime.churny_entities.add(entity_id)
    entities = sorted(runtime.churny_entities)
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_VARIABLES_RECORDED}_{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_VARIABLES_RECORDED,
        translation_placeholders={
            "name": entry.title,
            "count": str(len(entities)),
            "entities": "\n".join(f"      - {entity}" for entity in entities),
        },
        learn_more_url=LEARN_MORE_URL,
    )
