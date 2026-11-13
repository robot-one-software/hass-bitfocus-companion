"""Config flow: discover Companion over mDNS or add it by address; options pick buttons."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_BUTTONS,
    CONF_COLUMN,
    CONF_HTTP_PORT,
    CONF_MACHINE_ID,
    CONF_PAGE,
    CONF_POLL_INTERVAL,
    CONF_ROW,
    CONF_UNIT,
    CONF_VARIABLES,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .helpers import buttons_from_options, variables_from_options
from .http_api import (
    DEFAULT_HTTP_PORT,
    CompanionHttpClient,
    CompanionHttpError,
    CompanionHttpNotFound,
    VariableRef,
    parse_variable_ref,
)
from .satellite import (
    DEFAULT_PORT,
    ButtonState,
    CompanionConnectionError,
    CompanionError,
    CompanionSubscriptionsDisabled,
    HandshakeInfo,
    async_probe,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): TextSelector(),
        vol.Required(CONF_PORT, default=DEFAULT_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
    }
)

ADD_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PAGE, default=1): NumberSelector(
            NumberSelectorConfig(min=1, max=999, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_ROW, default=0): NumberSelector(
            NumberSelectorConfig(min=0, max=99, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_COLUMN, default=0): NumberSelector(
            NumberSelectorConfig(min=0, max=99, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(CONF_NAME): TextSelector(),
    }
)


SCAN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PAGE, default=1): NumberSelector(
            NumberSelectorConfig(min=1, max=999, mode=NumberSelectorMode.BOX)
        ),
        vol.Required("max_row", default=3): NumberSelector(
            NumberSelectorConfig(min=0, max=15, mode=NumberSelectorMode.BOX)
        ),
        vol.Required("max_column", default=7): NumberSelector(
            NumberSelectorConfig(min=0, max=31, mode=NumberSelectorMode.BOX)
        ),
    }
)


HTTP_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): NumberSelector(
            NumberSelectorConfig(min=0.5, max=60, step=0.5, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
        ),
    }
)


def _short_version(info: HandshakeInfo) -> str:
    return info.companion_version.split("+")[0] or "unknown"


class CompanionConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Companion server."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str | None = None
        self._machine_id: str | None = None
        self._version: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> CompanionOptionsFlow:
        return CompanionOptionsFlow()

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """Companion announced its satellite port on the network."""
        self._host = discovery_info.host
        self._port = discovery_info.port or DEFAULT_PORT
        properties = {str(key): str(value) for key, value in discovery_info.properties.items()}
        self._machine_id = properties.get("id") or None
        self._version = properties.get("version") or None
        self._name = discovery_info.name.split("._", 1)[0] or f"Companion ({self._host})"

        if self._machine_id:
            await self.async_set_unique_id(self._machine_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self._async_abort_entries_match({CONF_HOST: self._host, CONF_PORT: self._port})

        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user confirm the discovered server."""
        assert self._host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await async_probe(self._host, self._port)
            except CompanionConnectionError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=self._name or f"Companion ({self._host})",
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_MACHINE_ID: self._machine_id,
                        "companion_version": _short_version(info),
                    },
                )
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "name": self._name or "",
                "host": self._host,
                "version": self._version or "unknown",
            },
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add a server by address."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            port = int(user_input[CONF_PORT])
            self._async_abort_entries_match({CONF_HOST: host, CONF_PORT: port})
            try:
                info = await async_probe(host, port)
            except CompanionConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error probing Companion at %s:%s", host, port)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Companion ({host})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_MACHINE_ID: None,
                        "companion_version": _short_version(info),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )


class CompanionOptionsFlow(OptionsFlow):
    """Choose which Companion buttons become entities."""

    def __init__(self) -> None:
        self._found: dict[str, ButtonState] = {}

    def _existing_keys(self) -> set[str]:
        return {
            f"{item[CONF_PAGE]}/{item[CONF_ROW]}/{item[CONF_COLUMN]}"
            for item in self.config_entry.options.get(CONF_BUTTONS, [])
        }

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["scan_page", "add_button"]
        if self.config_entry.options.get(CONF_BUTTONS):
            options.append("remove_button")
        options.append("add_variables")
        if self.config_entry.options.get(CONF_VARIABLES):
            options.append("remove_variables")
        options.append("http_settings")
        return self.async_show_menu(step_id="init", menu_options=options)

    def _http_client(self, port: int | None = None) -> CompanionHttpClient:
        host = self.config_entry.data[CONF_HOST]
        if port is None:
            port = int(self.config_entry.options.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT))
        return CompanionHttpClient(async_get_clientsession(self.hass), host, port)

    async def async_step_http_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Where Companion's HTTP API lives and how often variables are polled."""
        errors: dict[str, str] = {}
        placeholders = {"host": self.config_entry.data[CONF_HOST]}
        if user_input is not None:
            port = int(user_input[CONF_HTTP_PORT])
            try:
                await self._http_client(port).async_probe()
            except CompanionHttpError:
                errors["base"] = "http_unreachable"
            else:
                return self.async_create_entry(
                    data={
                        **self.config_entry.options,
                        CONF_HTTP_PORT: port,
                        CONF_POLL_INTERVAL: float(user_input[CONF_POLL_INTERVAL]),
                    }
                )
        suggested = user_input or {
            CONF_HTTP_PORT: self.config_entry.options.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            CONF_POLL_INTERVAL: self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        }
        return self.async_show_form(
            step_id="http_settings",
            data_schema=self.add_suggested_values_to_schema(HTTP_SETTINGS_SCHEMA, suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_add_variables(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Add one or more variables by name, checking each against Companion."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"missing": ""}
        http = self._http_client()
        labels: list[str] = []
        try:
            connections = await http.async_get_connections()
        except CompanionHttpError:
            if user_input is None:
                errors["base"] = "http_unreachable"
        else:
            labels = sorted({connection.label for connection in connections if connection.label})

        if user_input is not None and "base" not in errors:
            label = str(user_input.get("label", "")).strip()
            refs: list[VariableRef] = []
            bad: list[str] = []
            for raw in str(user_input.get("names", "")).replace(",", "\n").splitlines():
                token = raw.strip()
                if not token:
                    continue
                ref = parse_variable_ref(token if ":" in token or not label else f"{label}:{token}")
                if ref is None:
                    bad.append(token)
                else:
                    refs.append(ref)
            if bad or not refs:
                errors["base"] = "invalid_variable"
                placeholders["missing"] = ", ".join(bad)
            else:
                missing: list[str] = []
                for ref in refs:
                    try:
                        await http.async_get_variable(ref)
                    except CompanionHttpNotFound:
                        missing.append(ref.key)
                    except CompanionHttpError:
                        errors["base"] = "http_unreachable"
                        break
                if "base" not in errors and missing:
                    errors["base"] = "variables_not_found"
                    placeholders["missing"] = ", ".join(missing)
                if "base" not in errors:
                    existing = list(self.config_entry.options.get(CONF_VARIABLES, []))
                    known = {variable.ref.key for variable in variables_from_options(self.config_entry.options)}
                    unit = str(user_input.get(CONF_UNIT, "")).strip()
                    for ref in refs:
                        if ref.key in known:
                            continue
                        item: dict[str, Any] = {"ref": ref.key}
                        if unit:
                            item[CONF_UNIT] = unit
                        existing.append(item)
                        known.add(ref.key)
                    return self.async_create_entry(data={**self.config_entry.options, CONF_VARIABLES: existing})

        label_options = [SelectOptionDict(value=label, label=label) for label in labels]
        label_options.append(SelectOptionDict(value="custom", label="custom (Companion custom variables)"))
        schema = vol.Schema(
            {
                vol.Optional("label"): SelectSelector(
                    SelectSelectorConfig(options=label_options, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("names"): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_UNIT): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="add_variables",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_remove_variables(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        variables = variables_from_options(self.config_entry.options)
        if user_input is not None:
            remove = set(user_input.get(CONF_VARIABLES, []))
            remaining = [
                item for item in self.config_entry.options.get(CONF_VARIABLES, []) if item.get("ref") not in remove
            ]
            return self.async_create_entry(data={**self.config_entry.options, CONF_VARIABLES: remaining})
        choices = [
            SelectOptionDict(value=variable.ref.key, label=f"{variable.display_name} ({variable.ref})")
            for variable in variables
        ]
        return self.async_show_form(
            step_id="remove_variables",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_VARIABLES): SelectSelector(
                        SelectSelectorConfig(options=choices, multiple=True, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_scan_page(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Look at every cell of one Companion page and offer the ones with something on them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            runtime = getattr(self.config_entry, "runtime_data", None)
            client = runtime.client if runtime is not None else None
            if client is None or not client.connected:
                errors["base"] = "not_connected"
            elif not client.subscriptions_supported:
                errors["base"] = "subscriptions_disabled"
            else:
                try:
                    found = await client.async_scan_page(
                        int(user_input[CONF_PAGE]), int(user_input["max_row"]), int(user_input["max_column"])
                    )
                except CompanionSubscriptionsDisabled:
                    errors["base"] = "subscriptions_disabled"
                except CompanionError:
                    errors["base"] = "cannot_connect"
                else:
                    self._found = {
                        str(location): state for location, state in sorted(found.items()) if not state.is_empty
                    }
                    if not self._found:
                        errors["base"] = "nothing_found"
                    else:
                        return await self.async_step_pick_buttons()
        return self.async_show_form(
            step_id="scan_page",
            data_schema=self.add_suggested_values_to_schema(SCAN_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_pick_buttons(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        existing = self._existing_keys()
        if user_input is not None:
            items = list(self.config_entry.options.get(CONF_BUTTONS, []))
            for key in user_input.get(CONF_BUTTONS, []):
                state = self._found.get(key)
                if state is None or key in existing:
                    continue
                location = state.location
                item: dict[str, Any] = {CONF_PAGE: location.page, CONF_ROW: location.row, CONF_COLUMN: location.column}
                if state.label:
                    item["name"] = state.label
                items.append(item)
                existing.add(key)
            return self.async_create_entry(data={**self.config_entry.options, CONF_BUTTONS: items})

        choices: list[SelectOptionDict] = []
        for key, state in self._found.items():
            label = state.label or "(no text)"
            if state.type != "BUTTON":
                label = f"{label} [{state.type.lower()}]"
            if key in existing:
                label = f"{label} (already added)"
            choices.append(SelectOptionDict(value=key, label=f"{key}  {label}"))
        return self.async_show_form(
            step_id="pick_buttons",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BUTTONS, default=[]): SelectSelector(
                        SelectSelectorConfig(options=choices, multiple=True, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
            description_placeholders={"count": str(len(choices))},
        )

    async def async_step_add_button(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            page, row, column = (int(user_input[key]) for key in (CONF_PAGE, CONF_ROW, CONF_COLUMN))
            if f"{page}/{row}/{column}" in self._existing_keys():
                errors["base"] = "already_added"
            else:
                item: dict[str, Any] = {CONF_PAGE: page, CONF_ROW: row, CONF_COLUMN: column}
                name = str(user_input.get(CONF_NAME, "")).strip()
                if name:
                    item["name"] = name
                existing = list(self.config_entry.options.get(CONF_BUTTONS, []))
                existing.append(item)
                return self.async_create_entry(data={**self.config_entry.options, CONF_BUTTONS: existing})
        return self.async_show_form(
            step_id="add_button",
            data_schema=self.add_suggested_values_to_schema(ADD_BUTTON_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_remove_button(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        buttons = buttons_from_options(self.config_entry.options)
        if user_input is not None:
            remove = set(user_input.get(CONF_BUTTONS, []))
            remaining = [
                item
                for item in self.config_entry.options.get(CONF_BUTTONS, [])
                if f"{item[CONF_PAGE]}/{item[CONF_ROW]}/{item[CONF_COLUMN]}" not in remove
            ]
            return self.async_create_entry(data={**self.config_entry.options, CONF_BUTTONS: remaining})
        choices = [
            SelectOptionDict(value=str(button.location), label=f"{button.display_name} ({button.location})")
            for button in buttons
        ]
        return self.async_show_form(
            step_id="remove_button",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BUTTONS): SelectSelector(
                        SelectSelectorConfig(options=choices, multiple=True, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )
