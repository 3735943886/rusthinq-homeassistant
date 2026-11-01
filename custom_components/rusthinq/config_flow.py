"""Config flow: Home Assistant runs rusthinq (embedded), from scratch or from an existing installation's export
(`migrate.py`), or connects to a running one (development).

There is one embedded entry per Home Assistant. Its ports are checked here, before anything is written.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import FileSelector, FileSelectorConfig

from . import daemon_ports, management_headers, migrate
from .client import REQUEST_TIMEOUT
from .const import (
    CONF_ARCHIVE,
    CONF_HTTPS_PORT,
    CONF_MODE,
    CONF_MQTT_PORT,
    CONF_PREFIX,
    CONF_SETTINGS,
    CONF_URL,
    DEFAULT_PORTS,
    DEFAULT_PREFIX,
    DEFAULT_URL,
    DOMAIN,
    MODE_EMBEDDED,
    MODE_URL,
)
from .embedded import busy_ports, data_dir

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
STEP_MIGRATE = "migrate"
"""Not `import`: that step name is Home Assistant's, for YAML."""


class RusthinqConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        # Home Assistant's advanced mode is deprecated (2026.9), so the development entry cannot be hidden behind it;
        # it is labelled as such instead.
        return self.async_show_menu(step_id="user", menu_options=[MODE_EMBEDDED, STEP_MIGRATE, MODE_URL])

    async def async_step_embedded(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(MODE_EMBEDDED)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            data = {CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: DEFAULT_PREFIX, **DEFAULT_PORTS, **user_input}
            if await self._ports_ok(data, errors, placeholders):
                return self.async_create_entry(title="rusthinq", data=data)
        return self.async_show_form(
            step_id="embedded", data_schema=self.add_suggested_values_to_schema(_ports_schema(), user_input),
            errors=errors, description_placeholders=placeholders,
        )

    async def async_step_migrate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(MODE_EMBEDDED)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            ports = {k: v for k, v in user_input.items() if k in DEFAULT_PORTS}
            data = {CONF_MODE: MODE_EMBEDDED, CONF_PREFIX: DEFAULT_PREFIX, **DEFAULT_PORTS, **ports}
            try:
                imported = await self.hass.async_add_executor_job(_read_upload, self.hass, user_input[CONF_ARCHIVE])
                directory = data_dir(self.hass)
                if clash := await self.hass.async_add_executor_job(migrate.conflicts, directory, imported):
                    raise migrate.MigrateError(f"{directory} already holds a different {', '.join(clash)}")
            except migrate.MigrateError as err:
                errors["base"] = "invalid_archive"
                placeholders["problems"] = str(err)
            else:
                settings = dict(imported.settings)
                data[CONF_PREFIX] = settings.pop("topic_prefix", DEFAULT_PREFIX)
                data[CONF_SETTINGS] = settings
                if await self._ports_ok(data, errors, placeholders):
                    await self.hass.async_add_executor_job(migrate.install, directory, imported)
                    return self.async_create_entry(title="rusthinq", data=data)
        schema = vol.Schema({vol.Required(CONF_ARCHIVE): FileSelector(FileSelectorConfig(accept=".tar.gz,.tgz,.tar"))})
        schema = schema.extend(_ports_schema().schema)
        return self.async_show_form(
            step_id=STEP_MIGRATE, data_schema=self.add_suggested_values_to_schema(
                schema, {k: v for k, v in (user_input or {}).items() if k != CONF_ARCHIVE}),
            errors=errors, description_placeholders=placeholders,
        )

    async def _ports_ok(self, data: dict[str, Any], errors: dict[str, str], placeholders: dict[str, str]) -> bool:
        if len({data[k] for k in DEFAULT_PORTS}) < len(DEFAULT_PORTS):
            errors["base"] = "duplicate_ports"
            return False
        if problems := await self.hass.async_add_executor_job(busy_ports, daemon_ports(data)):
            errors["base"] = "ports_unavailable"
            placeholders["problems"] = "; ".join(problems)
            return False
        return True

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


def _ports_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HTTPS_PORT, default=DEFAULT_PORTS[CONF_HTTPS_PORT]): PORT,
            vol.Required(CONF_MQTT_PORT, default=DEFAULT_PORTS[CONF_MQTT_PORT]): PORT,
        }
    )


def _read_upload(hass, file_id: str) -> migrate.Imported:
    """Read an uploaded export (the upload is gone afterwards). Runs in the executor."""
    try:
        with process_uploaded_file(hass, file_id) as path:
            archive = path.read_bytes()
    except (ValueError, OSError) as err:
        raise migrate.MigrateError(f"the upload cannot be read: {err}") from err
    return migrate.read_export(archive)
