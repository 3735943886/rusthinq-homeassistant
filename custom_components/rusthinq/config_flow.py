"""Config flow: Home Assistant runs rusthinq (embedded), from scratch or from an existing installation's backup
(`migrate.py`), or connects to a running one (development).

Moving an installation in takes its backup either straight from the old daemon (`GET /api/backup`) or as an uploaded
file, shows what it holds, and installs it once the ports are free and, for a fetched backup, the old daemon has been
stopped.

There is one embedded entry per Home Assistant. Its ports are checked here, before anything is written.

The options flow signs the daemon in to LG and out again, as the rusthinq dashboard does: rusthinq returns LG's sign-in
address, the user signs in in a browser and pastes the address it ends on (`login/iabClose`).
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import daemon_ports, management_headers, migrate
from .client import REQUEST_TIMEOUT, ManagementError
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
STEP_FETCH = "migrate_fetch"
STEP_UPLOAD = "migrate_upload"
STEP_CONFIRM = "migrate_confirm"
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)


class RusthinqConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._imported: migrate.Imported | None = None
        self._source: dict[str, Any] | None = None
        """The running daemon the backup came from (`migrate_fetch`), to check it has been stopped."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LgAccountFlow()

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
        return self.async_show_menu(step_id=STEP_MIGRATE, menu_options=[STEP_FETCH, STEP_UPLOAD])

    async def async_step_migrate_fetch(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Take the backup straight from the old daemon's management API."""
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            source = {**user_input, CONF_URL: user_input[CONF_URL].strip().rstrip("/")}
            try:
                archive = await migrate.fetch(async_get_clientsession(self.hass), source[CONF_URL],
                                              management_headers(source))
            except migrate.FetchError as err:
                errors["base"] = err.reason
                placeholders["problems"] = str(err)
            else:
                if await self._read(archive, errors, placeholders):
                    self._source = source
                    return await self.async_step_migrate_confirm()
        schema = vol.Schema(
            {
                vol.Required(CONF_URL, default=DEFAULT_URL): str,
                vol.Optional(CONF_USERNAME): str,
                vol.Optional(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        )
        return self.async_show_form(
            step_id=STEP_FETCH, data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors, description_placeholders=placeholders,
        )

    async def async_step_migrate_upload(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """A backup file (the dashboard's, `thinqctl backup`'s) or a hand-made export."""
        errors: dict[str, str] = {}
        placeholders = {"problems": ""}
        if user_input is not None:
            try:
                archive = await self.hass.async_add_executor_job(_read_upload, self.hass, user_input[CONF_ARCHIVE])
            except migrate.MigrateError as err:
                errors["base"] = "invalid_archive"
                placeholders["problems"] = str(err)
            else:
                if await self._read(archive, errors, placeholders):
                    self._source = None
                    return await self.async_step_migrate_confirm()
        schema = vol.Schema({vol.Required(CONF_ARCHIVE): FileSelector(FileSelectorConfig(accept=".tar,.tar.gz,.tgz"))})
        return self.async_show_form(step_id=STEP_UPLOAD, data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    async def _read(self, archive: bytes, errors: dict[str, str], placeholders: dict[str, str]) -> bool:
        """Read an export into the flow, refusing one that cannot be installed here."""
        try:
            imported = await self.hass.async_add_executor_job(migrate.read_export, archive)
            await self._check_conflicts(imported)
        except migrate.MigrateError as err:
            errors["base"] = "invalid_archive"
            placeholders["problems"] = str(err)
            return False
        self._imported = imported
        return True

    async def _check_conflicts(self, imported: migrate.Imported) -> None:
        directory = data_dir(self.hass)
        if clash := await self.hass.async_add_executor_job(migrate.conflicts, directory, imported):
            raise migrate.MigrateError(f"{directory} already holds a different {', '.join(clash)}")

    async def async_step_migrate_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What the backup holds, and this host's ports. The backup stays in the flow, so a refused port or an old
        daemon still running only needs fixing, not another upload."""
        imported = self._imported
        assert imported is not None
        errors: dict[str, str] = {}
        placeholders = {"problems": "", **_origin_placeholders(imported.origin)}
        if user_input is not None:
            settings = dict(imported.settings)
            data = {CONF_MODE: MODE_EMBEDDED, **DEFAULT_PORTS, **user_input,
                    CONF_PREFIX: settings.pop("topic_prefix", DEFAULT_PREFIX), CONF_SETTINGS: settings}
            try:
                await self._check_conflicts(imported)
            except migrate.MigrateError as err:
                errors["base"] = "invalid_archive"
                placeholders["problems"] = str(err)
            else:
                if self._source is not None and await self._answers(self._source):
                    errors["base"] = "source_running"
                    placeholders["problems"] = self._source[CONF_URL]
                elif await self._ports_ok(data, errors, placeholders):
                    await self.hass.async_add_executor_job(migrate.install, data_dir(self.hass), imported)
                    return self.async_create_entry(title="rusthinq", data=data)
        return self.async_show_form(
            step_id=STEP_CONFIRM, data_schema=self.add_suggested_values_to_schema(_ports_schema(), user_input),
            errors=errors, description_placeholders=placeholders,
        )

    async def _answers(self, source: dict[str, Any]) -> bool:
        """Whether the daemon the backup was taken from still runs: two would answer the same appliances and LG
        account, and it would go on changing what was copied."""
        try:
            async with async_get_clientsession(self.hass).get(
                f"{source[CONF_URL]}/api/health", headers=management_headers(source), timeout=PROBE_TIMEOUT
            ):
                return True
        except (aiohttp.ClientError, TimeoutError):
            return False

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


def _read_upload(hass, file_id: str) -> bytes:
    """The uploaded file (the upload is gone afterwards). Runs in the executor."""
    try:
        with process_uploaded_file(hass, file_id) as path:
            if path.stat().st_size > migrate.MAX_ARCHIVE:
                raise migrate.MigrateError("the archive exceeds 64 MiB")
            return path.read_bytes()
    except (ValueError, OSError) as err:
        raise migrate.MigrateError(f"the upload cannot be read: {err}") from err


def _origin_placeholders(origin: migrate.Origin) -> dict[str, str]:
    unknown = "–"
    return {
        "hostname": origin.hostname or unknown,
        "devices": unknown if origin.devices is None else str(origin.devices),
        "account": origin.account or unknown,
        "rusthinq": origin.rusthinq or unknown,
        "created": dt_util.as_local(origin.created).strftime("%Y-%m-%d %H:%M") if origin.created else unknown,
    }


COUNTRY = vol.All(str, vol.Strip, vol.Upper, vol.Match(r"^[A-Z]{2}$"))


class LgAccountFlow(OptionsFlow):
    """Sign the daemon in to LG, out, or in again with the saved credentials."""

    def __init__(self) -> None:
        self._sign_in_url = ""

    @property
    def _client(self):
        return self.config_entry.runtime_data.client

    def _done(self) -> ConfigFlowResult:
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self.config_entry.state is not ConfigEntryState.LOADED or not self._client.connected.is_set():
            return self.async_abort(reason="not_running")
        if not self._client.features.get("bridge"):
            return self.async_abort(reason="no_bridge")
        account = self._client.account
        signed_in = bool(account.get("loggedIn"))
        return self.async_show_menu(
            step_id="init",
            menu_options=["logout", "refresh"] if signed_in else ["login"],
            description_placeholders={
                "state": f"signed in ({account.get('country') or '?'})" if signed_in else "not signed in",
                "error": account.get("error") or "none",
            },
        )

    async def async_step_login(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"problem": ""}
        if user_input is not None:
            try:
                self._sign_in_url = await self._client.cloud_login(user_input["country"])
            except ManagementError as err:
                errors["base"] = "cloud_failed"
                placeholders["problem"] = str(err)
            else:
                return await self.async_step_login_complete()
        default = self._client.account.get("country") or self.hass.config.country or "KR"
        return self.async_show_form(
            step_id="login", data_schema=vol.Schema({vol.Required("country", default=default): COUNTRY}),
            errors=errors, description_placeholders=placeholders,
        )

    async def async_step_login_complete(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"sign_in_url": self._sign_in_url, "problem": ""}
        if user_input is not None:
            try:
                await self._client.cloud_complete(user_input["url"].strip())
            except ManagementError as err:
                errors["base"] = "cloud_failed"
                placeholders["problem"] = str(err)
            else:
                return self._done()
        return self.async_show_form(
            step_id="login_complete", data_schema=vol.Schema({vol.Required("url"): str}), errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_logout(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"problem": ""}
        if user_input is not None:
            try:
                await self._client.cloud_logout()
            except ManagementError as err:
                errors["base"] = "cloud_failed"
                placeholders["problem"] = str(err)
            else:
                return self._done()
        return self.async_show_form(step_id="logout", data_schema=vol.Schema({}), errors=errors,
                                    description_placeholders=placeholders)

    async def async_step_refresh(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        try:
            await self._client.cloud_refresh()
        except ManagementError as err:
            return self.async_abort(reason="cloud_failed", description_placeholders={"problem": str(err)})
        return self._done()
