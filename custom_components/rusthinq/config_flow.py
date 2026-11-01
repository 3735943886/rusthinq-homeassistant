"""Config flow: the management API of a running rusthinq.

This is the development entry of M1; the embedded daemon (M2) replaces it as the normal setup.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import basic_auth
from .client import REQUEST_TIMEOUT
from .const import CONF_PREFIX, CONF_URL, DEFAULT_PREFIX, DEFAULT_URL, DOMAIN


class RusthinqConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = {**user_input, CONF_URL: user_input[CONF_URL].rstrip("/"),
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
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors
        )

    async def _check(self, data: dict[str, Any]) -> str | None:
        try:
            async with async_get_clientsession(self.hass).get(
                f"{data[CONF_URL]}/api/devices", auth=basic_auth(data), timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status == 401:
                    return "invalid_auth"
                response.raise_for_status()
                await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return "cannot_connect"
        return None
