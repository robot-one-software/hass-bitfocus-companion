"""Thin client for Companion's HTTP API (variables, custom variables, connections).

Companion serves this on the same port as its admin interface (8000 by default).
Reference: https://companion.free/user-guide/v5.1/remote-control/http-remote-control
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiohttp

DEFAULT_HTTP_PORT = 8000
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=5)


class CompanionHttpError(Exception):
    """Companion's HTTP API could not be reached or answered unexpectedly."""


class CompanionHttpNotFound(CompanionHttpError):
    """The variable or connection does not exist."""


@dataclass(frozen=True)
class CompanionConnection:
    """One connection (module instance) configured in Companion."""

    id: str
    label: str
    module_id: str
    enabled: bool
    status: str | None


@dataclass(frozen=True)
class VariableRef:
    """A Companion variable, ``label:name``; the label ``custom`` means a custom variable."""

    label: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.label}:{self.name}"

    @property
    def is_custom(self) -> bool:
        return self.label == "custom"

    def __str__(self) -> str:
        return f"$({self.key})"


def parse_variable_ref(text: str) -> VariableRef | None:
    """Accept ``$(label:name)`` or ``label:name``."""
    value = text.strip()
    if value.startswith("$(") and value.endswith(")"):
        value = value[2:-1]
    label, separator, name = value.partition(":")
    label, name = label.strip(), name.strip()
    if not separator or not label or not name or any(ch in label + name for ch in " /$()"):
        return None
    return VariableRef(label, name)


class CompanionHttpClient:
    """Async HTTP client; one per Companion server."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int = DEFAULT_HTTP_PORT) -> None:
        self._session = session
        self.host = host
        self.port = port

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> tuple[int, str]:
        try:
            async with self._session.request(method, self._url(path), timeout=HTTP_TIMEOUT, **kwargs) as response:
                return response.status, await response.text()
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            raise CompanionHttpError(f"Cannot reach Companion's HTTP API at {self.base_url}: {err}") from err

    async def async_get_connections(self) -> list[CompanionConnection]:
        status, body = await self._request("GET", "/connections")
        if status != 200:
            raise CompanionHttpError(f"GET /api/connections returned {status}")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise CompanionHttpError("GET /api/connections did not return JSON") from err
        connections: list[CompanionConnection] = []
        for item in data if isinstance(data, list) else []:
            status_obj = item.get("status") or {}
            connections.append(
                CompanionConnection(
                    id=str(item.get("id", "")),
                    label=str(item.get("label", "")),
                    module_id=str(item.get("moduleId", "")),
                    enabled=bool(item.get("enabled", True)),
                    status=str(status_obj.get("category"))
                    if isinstance(status_obj, dict) and status_obj.get("category")
                    else None,
                )
            )
        return connections

    async def async_get_variable(self, ref: VariableRef) -> str:
        """Return the variable's value as text. Raises CompanionHttpNotFound if undefined."""
        path = f"/custom-variable/{ref.name}/value" if ref.is_custom else f"/variable/{ref.label}/{ref.name}/value"
        status, body = await self._request("GET", path)
        if status == 404:
            raise CompanionHttpNotFound(f"Variable {ref} is not defined in Companion")
        if status != 200:
            raise CompanionHttpError(f"GET {path} returned {status}")
        return body

    async def async_set_custom_variable(self, name: str, value: Any) -> None:
        """Set a custom variable, keeping numbers and booleans typed."""
        status, body = await self._request(
            "POST",
            f"/custom-variable/{name}/value",
            data=json.dumps(value),
            headers={"Content-Type": "application/json"},
        )
        if status == 404:
            raise CompanionHttpNotFound(f"Custom variable {name} does not exist in Companion")
        if status != 200:
            raise CompanionHttpError(f"Setting custom variable {name} returned {status}: {body.strip()}")

    async def async_probe(self) -> list[CompanionConnection]:
        """Check the API is reachable; the connection list doubles as the result."""
        return await self.async_get_connections()
