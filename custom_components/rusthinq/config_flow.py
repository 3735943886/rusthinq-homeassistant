"""Config flow: Home Assistant runs rusthinq (embedded), from scratch or from an existing installation's backup
(`migrate.py`). A rusthinq that keeps running elsewhere reaches Home Assistant through its external MQTT and
ildevice-homeassistant instead; attaching this integration to it as well would only add its devices twice.

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
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from . import daemon_ports, migrate
from .client import ManagementError, basic_authorization
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
)
from .embedded import busy_ports, data_dir, reset_data
from .retained import RetainedStore

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
STEP_MIGRATE = "migrate"
"""Not `import`: that step name is Home Assistant's, for YAML."""
STEP_FETCH = "migrate_fetch"
STEP_UPLOAD = "migrate_upload"
STEP_CONFIRM = "migrate_confirm"
CONF_OVERWRITE = "overwrite_existing"
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)


def management_headers(data) -> dict[str, str]:
    """The headers that authenticate to an old rusthinq's management API (`migrate_fetch`), if it has a user."""
    user = data.get(CONF_USERNAME)
    return basic_authorization(user, data.get(CONF_PASSWORD) or "") if user else {}


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
        return self.async_show_menu(step_id="user", menu_options=[MODE_EMBEDDED, STEP_MIGRATE])

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
        """Read and validate an export, leaving any file conflicts for the confirmation step."""
        try:
            imported = await self.hass.async_add_executor_job(migrate.read_export, archive)
        except migrate.MigrateError as err:
            errors["base"] = "invalid_archive"
            placeholders["problems"] = str(err)
            return False
        self._imported = imported
        return True

    async def async_step_migrate_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """What the backup holds, and this host's ports, the old ones suggested. The backup stays in the flow, so a
        refused port or an old daemon still running only needs fixing, not another upload."""
        imported = self._imported
        assert imported is not None
        errors: dict[str, str] = {}
        clash = await self.hass.async_add_executor_job(migrate.conflicts, data_dir(self.hass), imported)
        placeholders = {
            "problems": "",
            "conflicts": ", ".join(clash) if clash else ("없음" if self.hass.config.language == "ko" else "none"),
            **_origin_placeholders(imported.origin),
        }
        if user_input is not None:
            settings = dict(imported.settings)
            ports = {key: user_input[key] for key in DEFAULT_PORTS}
            data = {CONF_MODE: MODE_EMBEDDED, **DEFAULT_PORTS, **imported.ports, **ports,
                    CONF_PREFIX: settings.pop("topic_prefix", DEFAULT_PREFIX), CONF_SETTINGS: settings}
            if clash and not user_input.get(CONF_OVERWRITE, False):
                errors["base"] = "overwrite_required"
            else:
                if self._source is not None and await self._answers(self._source):
                    errors["base"] = "source_running"
                    placeholders["problems"] = self._source[CONF_URL]
                elif await self._ports_ok(data, errors, placeholders):
                    await self.hass.async_add_executor_job(
                        migrate.install, data_dir(self.hass), imported, bool(clash)
                    )
                    return self.async_create_entry(title="rusthinq", data=data)
        schema = {vol.Required(key): PORT for key in DEFAULT_PORTS}
        if clash:
            schema[vol.Required(CONF_OVERWRITE, default=False)] = bool
        return self.async_show_form(
            step_id=STEP_CONFIRM, data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema),
                user_input or {**DEFAULT_PORTS, **imported.ports}),
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


def _ports_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HTTPS_PORT, default=DEFAULT_PORTS[CONF_HTTPS_PORT]): PORT,
            vol.Required(CONF_MQTT_PORT, default=DEFAULT_PORTS[CONF_MQTT_PORT]): PORT,
        }
    )


def _reset_schema() -> vol.Schema:
    return vol.Schema({vol.Required("confirm_reset", default=False): bool})


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
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_running")
        account = self._client.account
        signed_in = bool(account.get("loggedIn"))
        menu_options = (["logout", "refresh"] if signed_in else ["login"])
        if not self._client.features.get("bridge"):
            menu_options = []
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options + ["reset"],
            description_placeholders={
                "state": f"signed in ({account.get('country') or '?'})" if signed_in else "not signed in",
                "error": account.get("error") or "none",
            },
        )

    async def async_step_reset(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Clear rusthinq state and start again with a newly generated CA."""
        if user_input is not None:
            if not user_input.get("confirm_reset", False):
                return self.async_show_form(
                    step_id="reset", data_schema=_reset_schema(), errors={"base": "reset_required"}
                )
            entry = self.config_entry
            if not await self.hass.config_entries.async_unload(entry.entry_id):
                return self.async_abort(reason="reset_failed")
            await self.hass.async_add_executor_job(reset_data, data_dir(self.hass))
            await RetainedStore.async_remove(self.hass, entry.entry_id)
            self.hass.config_entries.async_update_entry(
                entry,
                data={
                    CONF_MODE: MODE_EMBEDDED,
                    CONF_PREFIX: DEFAULT_PREFIX,
                    **{key: entry.data.get(key, value) for key, value in DEFAULT_PORTS.items()},
                },
                options={},
            )
            if not await self.hass.config_entries.async_reload(entry.entry_id):
                return self.async_abort(reason="reset_failed")
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="reset", data_schema=_reset_schema())

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
