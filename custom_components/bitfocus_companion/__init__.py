"""Bitfocus Companion integration for Home Assistant.

Connects to a Companion server over its satellite protocol so that Companion
buttons can be pressed from Home Assistant and their state can be seen here, and
reads Companion variables over its HTTP API.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_PRESSED,
    ATTR_SERVER,
    ATTR_TEXT,
    AUTO_SYNC_DEBOUNCE,
    CONF_AUTO_ADD,
    CONF_AUTO_REMOVE,
    CONF_BUTTON_SENSORS,
    CONF_BUTTONS,
    CONF_COLUMN,
    CONF_HTTP_HOST,
    CONF_HTTP_PORT,
    CONF_PAGE,
    CONF_POLL_INTERVAL,
    CONF_ROW,
    CONNECT_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    EVENT_BUTTON,
    ISSUE_HTTP_UNREACHABLE,
    ISSUE_IMAGES_RECORDED,
    ISSUE_PAGES_UNAVAILABLE,
    ISSUE_SUBSCRIPTIONS,
    ISSUE_TOO_OLD,
    ISSUE_VARIABLES_RECORDED,
    MIN_SUBSCRIPTION_API,
    signal_button,
    signal_connection,
)
from .coordinator import VariablesCoordinator
from .helpers import (
    ButtonConfig,
    VariableConfig,
    buttons_from_options,
    image_size_from_options,
    variables_from_options,
)
from .http_api import DEFAULT_HTTP_PORT, CompanionHttpClient
from .satellite import (
    DEFAULT_PORT,
    ButtonState,
    CompanionError,
    CompanionSatelliteClient,
    CompanionSubscriptionsDisabled,
    Location,
)
from .services import async_setup_services
from .trpc import (
    CompanionTrpcError,
    PageMap,
    PageMapWatcher,
    async_fetch_control_infos,
    async_fetch_page_map,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.IMAGE, Platform.SENSOR]

# A reconnect usually lands within a second or two; only mark entities unavailable
# once a drop has lasted this long, so brief blips don't litter their history.
AVAILABILITY_GRACE = 5.0
# The page-map watch reconnects on its own; only raise a Repairs notice when it has
# been down this long, so a restart of Companion does not produce a warning.
PAGES_ISSUE_GRACE = 90.0

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class CompanionRuntime:
    """Per-entry runtime state."""

    client: CompanionSatelliteClient
    buttons: list[ButtonConfig]
    http: CompanionHttpClient
    variables: list[VariableConfig]
    coordinator: VariablesCoordinator | None = None
    available: bool = True
    # Button key -> where that button currently sits in Companion.
    locations: dict[str, Location] = field(default_factory=dict)
    page_map: PageMap | None = None
    watcher: PageMapWatcher | None = None
    suppress_reload: bool = False
    # Set once the options update listener exists; before that, writes never trigger it.
    listener_attached: bool = False
    # Entity ids seen changing fast enough to flood the recorder (see recorder_notice).
    churny_entities: set[str] = field(default_factory=set)
    # Pending debounced auto-sync after a page-map change.
    auto_sync_handle: asyncio.TimerHandle | None = None

    def key_at(self, location: Location) -> str | None:
        for key, current in self.locations.items():
            if current == location:
                return key
        return None

    def button(self, key: str) -> ButtonConfig | None:
        for button in self.buttons:
            if button.key == key:
                return button
        return None

    def location_of(self, key: str) -> Location | None:
        return self.locations.get(key)

    def button_exists(self, key: str) -> bool:
        """False once Companion's page map says the control is gone."""
        button = self.button(key)
        if button is None:
            return False
        if self.page_map is None or button.control_id is None:
            return True
        return button.control_id in self.page_map.controls


type CompanionConfigEntry = ConfigEntry[CompanionRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the domain services; entries are set up separately."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CompanionConfigEntry) -> bool:
    """Connect to one Companion server."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, DEFAULT_PORT)

    @callback
    def _on_state(state: ButtonState, pressed_changed: bool) -> None:
        runtime: CompanionRuntime | None = getattr(entry, "runtime_data", None)
        key = runtime.key_at(state.location) if runtime is not None else None
        if key is not None:
            async_dispatcher_send(hass, signal_button(entry.entry_id, key), state)
        if pressed_changed:
            hass.bus.async_fire(
                EVENT_BUTTON,
                {
                    "entry_id": entry.entry_id,
                    ATTR_SERVER: entry.title,
                    CONF_PAGE: state.location.page,
                    CONF_ROW: state.location.row,
                    CONF_COLUMN: state.location.column,
                    "control_id": runtime.button(key).control_id if runtime and key and runtime.button(key) else None,
                    ATTR_PRESSED: state.pressed,
                    ATTR_TEXT: state.text,
                },
            )

    grace_handle: asyncio.TimerHandle | None = None

    @callback
    def _set_available(value: bool) -> None:
        runtime: CompanionRuntime | None = getattr(entry, "runtime_data", None)
        if runtime is not None:
            runtime.available = value
        async_dispatcher_send(hass, signal_connection(entry.entry_id), value)

    @callback
    def _grace_expired() -> None:
        nonlocal grace_handle
        grace_handle = None
        if not client.connected:
            _set_available(False)

    @callback
    def _on_connection(connected: bool) -> None:
        nonlocal grace_handle
        if grace_handle is not None:
            grace_handle.cancel()
            grace_handle = None
        if connected:
            _update_issues(hass, entry, client)
            _set_available(True)
        else:
            # The Connected sensor reflects this at once; entities wait out the grace period.
            async_dispatcher_send(hass, signal_connection(entry.entry_id), False)
            grace_handle = hass.loop.call_later(AVAILABILITY_GRACE, _grace_expired)

    @callback
    def _cancel_grace() -> None:
        if grace_handle is not None:
            grace_handle.cancel()

    entry.async_on_unload(_cancel_grace)

    client = CompanionSatelliteClient(
        host,
        port,
        state_callback=_on_state,
        connection_callback=_on_connection,
        bitmap_size=image_size_from_options(entry.options),
    )
    await client.async_start()
    try:
        await client.async_wait_connected(CONNECT_TIMEOUT)
    except TimeoutError as err:
        await client.async_stop()
        raise ConfigEntryNotReady(f"Cannot connect to Companion at {host}:{port}") from err

    session = async_get_clientsession(hass)
    buttons = buttons_from_options(entry.options)
    variables = variables_from_options(entry.options)
    http = CompanionHttpClient(
        session,
        str(entry.options.get(CONF_HTTP_HOST) or host),
        int(entry.options.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)),
    )
    coordinator: VariablesCoordinator | None = None
    if variables:
        coordinator = VariablesCoordinator(
            hass, entry, http, variables, float(entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
        )
        # A first failure must not block setup: sensors start unavailable and a Repairs
        # issue explains that the HTTP API could not be reached.
        await coordinator.async_refresh()
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_HTTP_UNREACHABLE}_{entry.entry_id}")

    runtime = CompanionRuntime(client=client, buttons=buttons, http=http, variables=variables, coordinator=coordinator)
    runtime.locations = {button.key: button.location for button in buttons}
    entry.runtime_data = runtime

    if buttons or entry.options.get(CONF_AUTO_ADD):
        # Learn each button's stable control id and current position before entities exist,
        # so unique ids are right from the start and moved buttons are found again.
        try:
            page_map = await async_fetch_page_map(session, http.host, http.port)
        except CompanionTrpcError as err:
            _LOGGER.debug("Page map unavailable at setup for %s: %s", entry.title, err)
        else:
            await _async_apply_page_map(hass, entry, runtime, page_map)
            await _async_learn_rotary(hass, entry, runtime, session)
            await _async_auto_sync(hass, entry, runtime)

    _prune_removed_entities(hass, entry, runtime)
    if image_size_from_options(entry.options) is None:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_IMAGES_RECORDED}_{entry.entry_id}")

    for key, location in list(runtime.locations.items()):
        try:
            await client.async_subscribe(location)
        except CompanionSubscriptionsDisabled:
            break
        except CompanionError as err:
            _LOGGER.warning("Could not subscribe to Companion button %s at %s: %s", key, location, err)

    if buttons or entry.options.get(CONF_AUTO_ADD):

        @callback
        def _on_page_map(page_map: PageMap) -> None:
            entry.async_create_background_task(
                hass, _async_apply_page_map(hass, entry, runtime, page_map), f"{DOMAIN} page map {entry.entry_id}"
            )
            _schedule_auto_sync(hass, entry, runtime)

        pages_issue_handle: asyncio.TimerHandle | None = None

        @callback
        def _pages_issue_due() -> None:
            nonlocal pages_issue_handle
            pages_issue_handle = None
            if runtime.watcher is not None and not runtime.watcher.available:
                _update_pages_issue(hass, entry, False)

        @callback
        def _on_watch_available(available: bool) -> None:
            nonlocal pages_issue_handle
            if pages_issue_handle is not None:
                pages_issue_handle.cancel()
                pages_issue_handle = None
            if available:
                _update_pages_issue(hass, entry, True)
            else:
                pages_issue_handle = hass.loop.call_later(PAGES_ISSUE_GRACE, _pages_issue_due)

        @callback
        def _cancel_pages_issue_timer() -> None:
            if pages_issue_handle is not None:
                pages_issue_handle.cancel()
            if runtime.auto_sync_handle is not None:
                runtime.auto_sync_handle.cancel()
                runtime.auto_sync_handle = None

        entry.async_on_unload(_cancel_pages_issue_timer)
        runtime.watcher = PageMapWatcher(session, http.host, http.port, _on_page_map, _on_watch_available)
        runtime.watcher.start()
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_PAGES_UNAVAILABLE}_{entry.entry_id}")

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    runtime.listener_attached = True
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CompanionConfigEntry) -> bool:
    """Disconnect from a Companion server."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = entry.runtime_data
    if runtime.watcher is not None:
        await runtime.watcher.stop()
    await runtime.client.async_stop()
    for issue in (
        ISSUE_SUBSCRIPTIONS,
        ISSUE_TOO_OLD,
        ISSUE_HTTP_UNREACHABLE,
        ISSUE_PAGES_UNAVAILABLE,
        ISSUE_IMAGES_RECORDED,
        ISSUE_VARIABLES_RECORDED,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{issue}_{entry.entry_id}")
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: CompanionConfigEntry) -> None:
    """Options changed: reload so the entity list matches (unless we wrote them ourselves)."""
    runtime: CompanionRuntime | None = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.suppress_reload:
        runtime.suppress_reload = False
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_apply_page_map(
    hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime, page_map: PageMap
) -> None:
    """Reconcile configured buttons with Companion's page map.

    Buttons without a control id get one from their position; buttons whose control has
    moved are followed to the new position and resubscribed. Changes are persisted to the
    options, and the entry is reloaded only when a key changed (so entities get new ids).
    """
    runtime.page_map = page_map
    registry = er.async_get(hass)
    changed = False
    key_changed = False
    new_buttons: list[ButtonConfig] = []
    for button in runtime.buttons:
        location = runtime.locations.get(button.key, button.location)
        if button.control_id is None:
            control_id = page_map.locations.get(location)
            if control_id is not None:
                old_key = button.key
                button = ButtonConfig(location=location, name=button.name, control_id=control_id, rotary=button.rotary)
                _migrate_unique_ids(registry, entry, old_key, button.key)
                runtime.locations.pop(old_key, None)
                runtime.locations[button.key] = location
                changed = key_changed = True
        else:
            current = page_map.controls.get(button.control_id)
            if current is not None and current != location:
                _LOGGER.info("Companion button '%s' moved from %s to %s", button.display_name, location, current)
                await _async_resubscribe(runtime.client, location, current)
                runtime.locations[button.key] = current
                button = ButtonConfig(
                    location=current, name=button.name, control_id=button.control_id, rotary=button.rotary
                )
                changed = True
        new_buttons.append(button)
    runtime.buttons = new_buttons
    for button in new_buttons:
        state = runtime.client.states.get(runtime.locations.get(button.key, button.location))
        async_dispatcher_send(hass, signal_button(entry.entry_id, button.key), state)
    if changed:
        _persist_buttons(hass, entry, runtime, reload=key_changed)


async def _async_learn_rotary(
    hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime, session
) -> None:
    """Buttons added before rotary detection existed: ask Companion once whether they rotate."""
    unknown = [button.control_id for button in runtime.buttons if button.rotary is None and button.control_id]
    if not unknown:
        return
    try:
        infos = await async_fetch_control_infos(session, runtime.http.host, runtime.http.port, unknown)
    except CompanionTrpcError as err:
        _LOGGER.debug("Could not read control details for %s: %s", entry.title, err)
        return
    changed = False
    buttons: list[ButtonConfig] = []
    for button in runtime.buttons:
        info = infos.get(button.control_id or "")
        if button.rotary is None and info is not None:
            button = ButtonConfig(button.location, button.name, button.control_id, rotary=info.rotary)
            changed = True
        buttons.append(button)
    runtime.buttons = buttons
    if changed:
        # Entities for this entry are created right after this, so no reload is needed.
        _persist_buttons(hass, entry, runtime, reload=False)


@callback
def _schedule_auto_sync(hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime) -> None:
    """Run the auto-sync once the page map has been quiet for a moment."""
    if not (entry.options.get(CONF_AUTO_ADD) or entry.options.get(CONF_AUTO_REMOVE)):
        return
    if runtime.auto_sync_handle is not None:
        runtime.auto_sync_handle.cancel()

    @callback
    def _due() -> None:
        runtime.auto_sync_handle = None
        entry.async_create_background_task(
            hass, _async_auto_sync(hass, entry, runtime), f"{DOMAIN} auto sync {entry.entry_id}"
        )

    runtime.auto_sync_handle = hass.loop.call_later(AUTO_SYNC_DEBOUNCE, _due)


async def _async_auto_sync(hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime) -> None:
    """Keep the button list in step with Companion, when the options ask for it.

    Auto-add creates a button for every control Companion has that is not here yet
    (page navigation keys excepted), named after what Companion draws on it. Auto-remove
    drops buttons whose control no longer exists, instead of leaving them unavailable.
    Both are off by default. After setup, a change reloads the entry so entities follow.
    """
    page_map = runtime.page_map
    if page_map is None:
        return
    auto_add = bool(entry.options.get(CONF_AUTO_ADD))
    auto_remove = bool(entry.options.get(CONF_AUTO_REMOVE))
    if not (auto_add or auto_remove):
        return
    buttons = list(runtime.buttons)
    changed = False

    if auto_remove:
        kept: list[ButtonConfig] = []
        for button in buttons:
            if button.control_id is not None and button.control_id not in page_map.controls:
                _LOGGER.info("Companion button '%s' was deleted in Companion; removing it", button.display_name)
                runtime.locations.pop(button.key, None)
                changed = True
                continue
            kept.append(button)
        buttons = kept

    if auto_add:
        known_ids = {button.control_id for button in buttons if button.control_id}
        known_locations = {runtime.locations.get(button.key, button.location) for button in buttons}
        candidates = {
            control_id: location
            for control_id, location in page_map.controls.items()
            if control_id not in known_ids and location not in known_locations
        }
        if candidates:
            try:
                infos = await async_fetch_control_infos(
                    async_get_clientsession(hass), runtime.http.host, runtime.http.port, sorted(candidates)
                )
            except CompanionTrpcError as err:
                _LOGGER.debug("Auto-add: control details unavailable for %s: %s", entry.title, err)
                infos = {}
            wanted = {cid: loc for cid, loc in candidates.items() if cid in infos and not infos[cid].is_navigation}
            rendered: dict[Location, str] = {}
            if wanted:
                try:
                    states = await runtime.client.async_peek(list(wanted.values()))
                    rendered = {loc: state.label for loc, state in states.items() if state.label}
                except CompanionError as err:
                    _LOGGER.debug("Auto-add: rendered text unavailable for %s: %s", entry.title, err)
            in_deck_order = sorted(wanted.items(), key=lambda item: (item[1].page, item[1].row, item[1].column))
            for control_id, location in in_deck_order:
                info = infos[control_id]
                name = rendered.get(location) or info.label or None
                button = ButtonConfig(location=location, name=name, control_id=control_id, rotary=info.rotary)
                _LOGGER.info("Companion button '%s' at %s added automatically", button.display_name, location)
                buttons.append(button)
                runtime.locations[button.key] = location
                changed = True

    if not changed:
        return
    runtime.buttons = buttons
    # During setup the entities are created right after this; later, a reload makes them.
    _persist_buttons(hass, entry, runtime, reload=runtime.listener_attached)


async def _async_resubscribe(client: CompanionSatelliteClient, old: Location, new: Location) -> None:
    try:
        await client.async_unsubscribe(old)
        await client.async_subscribe(new)
    except CompanionError as err:
        _LOGGER.warning("Could not move Companion subscription from %s to %s: %s", old, new, err)


@callback
def _persist_buttons(
    hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime, *, reload: bool
) -> None:
    options = {**entry.options, CONF_BUTTONS: [button.to_option() for button in runtime.buttons]}
    if options == dict(entry.options):
        return
    if not reload and runtime.listener_attached:
        runtime.suppress_reload = True
    hass.config_entries.async_update_entry(entry, options=options)


@callback
def _migrate_unique_ids(registry: er.EntityRegistry, entry: ConfigEntry, old_key: str, new_key: str) -> None:
    """Move entities from position-based unique ids to control-id based ones."""
    for platform, suffix in ((Platform.BUTTON, "press"), (Platform.SENSOR, "state")):
        old_unique_id = f"{entry.entry_id}_{old_key}_{suffix}"
        new_unique_id = f"{entry.entry_id}_{new_key}_{suffix}"
        entity_id = registry.async_get_entity_id(platform, DOMAIN, old_unique_id)
        if entity_id is None or registry.async_get_entity_id(platform, DOMAIN, new_unique_id) is not None:
            continue
        registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
        _LOGGER.debug("Migrated %s to control-id unique id", entity_id)


@callback
def _prune_removed_entities(hass: HomeAssistant, entry: CompanionConfigEntry, runtime: CompanionRuntime) -> None:
    """Drop registry entries for buttons, sensors and variables no longer configured."""
    keep = {f"{entry.entry_id}_connected"}
    sensors = bool(entry.options.get(CONF_BUTTON_SENSORS, False))
    images = image_size_from_options(entry.options) is not None
    for button in runtime.buttons:
        keep.add(f"{entry.entry_id}_{button.key}_press")
        if sensors:
            keep.add(f"{entry.entry_id}_{button.key}_state")
        if images:
            keep.add(f"{entry.entry_id}_{button.key}_image")
        if button.rotary:
            keep.update({f"{entry.entry_id}_{button.key}_left", f"{entry.entry_id}_{button.key}_right"})
    for variable in runtime.variables:
        keep.add(variable_unique_id(entry, variable))
    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.unique_id not in keep:
            registry.async_remove(registry_entry.entity_id)


@callback
def _update_pages_issue(hass: HomeAssistant, entry: CompanionConfigEntry, available: bool) -> None:
    issue_id = f"{ISSUE_PAGES_UNAVAILABLE}_{entry.entry_id}"
    runtime: CompanionRuntime | None = getattr(entry, "runtime_data", None)
    if available or runtime is None or not runtime.buttons:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PAGES_UNAVAILABLE,
        translation_placeholders={
            "name": entry.title,
            "host": runtime.http.host,
            "port": str(runtime.http.port),
        },
    )


@callback
def _update_issues(hass: HomeAssistant, entry: CompanionConfigEntry, client: CompanionSatelliteClient) -> None:
    """Raise or clear repair issues based on what the server told us on connect."""
    handshake = client.handshake
    if handshake is None:
        return
    placeholders = {"name": entry.title, "host": entry.data[CONF_HOST]}

    too_old_id = f"{ISSUE_TOO_OLD}_{entry.entry_id}"
    if handshake.api_version and handshake.api_version_tuple < MIN_SUBSCRIPTION_API:
        ir.async_create_issue(
            hass,
            DOMAIN,
            too_old_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_TOO_OLD,
            translation_placeholders={**placeholders, "version": handshake.companion_version},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, too_old_id)

    subs_id = f"{ISSUE_SUBSCRIPTIONS}_{entry.entry_id}"
    if not handshake.subscriptions_enabled:
        ir.async_create_issue(
            hass,
            DOMAIN,
            subs_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SUBSCRIPTIONS,
            translation_placeholders=placeholders,
            learn_more_url="https://companion.free/for-developers/Satellite-API",
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, subs_id)


def variable_unique_id(entry: ConfigEntry, variable: VariableConfig) -> str:
    return f"{entry.entry_id}_var_{variable.ref.label}_{variable.ref.name}"
