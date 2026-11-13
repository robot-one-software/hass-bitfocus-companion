"""Polls the chosen Companion variables over the HTTP API."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, ISSUE_HTTP_UNREACHABLE
from .helpers import VariableConfig
from .http_api import CompanionHttpClient, CompanionHttpError, CompanionHttpNotFound

_LOGGER = logging.getLogger(__name__)


class VariablesCoordinator(DataUpdateCoordinator[dict[str, str | None]]):
    """Data is a map of ``label:name`` to the current text value (None when undefined)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        http: CompanionHttpClient,
        variables: list[VariableConfig],
        poll_interval: float,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} variables {entry.title}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self.http = http
        self.variables = variables
        self._issue_id = f"{ISSUE_HTTP_UNREACHABLE}_{entry.entry_id}"
        self._entry = entry

    async def _async_update_data(self) -> dict[str, str | None]:
        results = await asyncio.gather(
            *(self.http.async_get_variable(variable.ref) for variable in self.variables), return_exceptions=True
        )
        data: dict[str, str | None] = {}
        unreachable: CompanionHttpError | None = None
        for variable, result in zip(self.variables, results, strict=True):
            if isinstance(result, CompanionHttpNotFound):
                data[variable.ref.key] = None
            elif isinstance(result, CompanionHttpError):
                unreachable = result
            elif isinstance(result, BaseException):
                raise result
            else:
                data[variable.ref.key] = result
        if unreachable is not None and not data:
            self._set_issue(True)
            raise UpdateFailed(str(unreachable))
        self._set_issue(False)
        return data

    def _set_issue(self, unreachable: bool) -> None:
        if unreachable:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_HTTP_UNREACHABLE,
                translation_placeholders={
                    "name": self._entry.title,
                    "host": self.http.host,
                    "port": str(self.http.port),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
