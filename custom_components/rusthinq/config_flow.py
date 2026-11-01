"""Config flow: Home Assistant runs rusthinq (embedded), or connects to a running one (development).

There is one embedded entry per Home Assistant. Its ports are checked here, before anything is written.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import daemon_ports, management_headers
from .client import REQUEST_TIMEOUT
from .const import (
    CONF_HTTPS_PORT,
    CONF_MODE,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_URL,
    DEFAULT_PORTS,
    DEFAULT_PREFIX,
    DEFAULT_URL,
    DOMAIN,
    MODE_EMBEDDED,
    MODE_URL,
)
from .embedded import busy_ports

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))


class RusthinqConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Home Assistant's advanced mode is deprecated (2026.9), so the development entry cannot be hidden behind it;
        # it is labelled as such instead.
        return self.async_show_menu(step_id="user", menu_options=[MODE_EMBEDDED, MODE_URL])

    async def async_step_embedded(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(MODE_EMBEDDED)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            data = {CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: DEFAULT_PREFIX, **DEFAULT_PORTS, **user_input}
            if len({data[k] for k in DEFAULT_PORTS}) < len(DEFAULT_PORTS):
                errors["base"] = "duplicate_ports"
            elif problems := await self.hass.async_add_executor_job(busy_ports, daemon_ports(data)):
                errors["base"] = "ports_unavailable"
                placeholders["problems"] = "; ".join(problems)
            else:
                return self.async_create_entry(title="rusthinq", data=data)
        schema = vol.Schema(
            {
                vol.Required(CONF_HTTPS_PORT, default=DEFAULT_PORTS[CONF_HTTPS_PORT]): PORT,
                vol.Required(CONF_MQTT_PORT, default=DEFAULT_PORTS[CONF_MQTT_PORT]): PORT,
            }
        )
        return self.async_show_form(
            step_id="embedded", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_url(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {**user_input, CONF_MODE: MODE_URL, CONF_URL: user_input[CONF_URL].rstrip("/"),
                          CONF_PREFIX: user_input[CONF_PREFIX].strip("/")}
            await self.async_set_unique_id(user_input[CONF_URL])
            self._abort_if_unique_id_configured()
            if not user_input[CONF_PREFIX]:
                errors[CONF_PREFIX] = "invalid_prefix"
            elif error := await self._check(user_input):
                errors["base"] = error
            else:
                return self.async_create_entry(title=user_input[CONF_URL], data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Optional(CONF_USERNAME): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): str,
            }
        )
        return self.async_show_form(
            step_id="url", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors
        )

    async def _check(self, data: dict[str, Any]) -> str | None:
        try:
            async with async_get_clientsession(self.hass).get(
                f"{data[CONF_URL]}/api/devices", headers=management_headers(data), timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 401:
                    return "invalid_auth"
                response.raise_for_status()
                await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return "cannot_connect"
        return None
