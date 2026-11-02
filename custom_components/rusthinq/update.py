"""The rusthinq-scripts update entity of the embedded daemon: installed commit, upstream master, and installing it.

Installing changes the files under `scripts/` in place and the daemon reloads its drivers by itself (`scripts.py`).
The daemon binary has no update entity: it is pinned by the integration (`const.REQUIREMENT`) and moves with it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RusthinqConfigEntry, scripts
from .const import DOMAIN
from .embedded import data_dir
from .status import service_device

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=6)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: RusthinqConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([ScriptsUpdate(entry)])


def _short(sha: str | None) -> str | None:
    return sha[:7] if sha else None


class ScriptsUpdate(UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Drivers"
    _attr_title = "rusthinq-scripts"
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, entry: RusthinqConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-scripts"
        self._attr_device_info = service_device(entry)
        self._installed: str | None = None
        self._latest: str | None = None

    @property
    def installed_version(self) -> str | None:
        return _short(self._installed)

    @property
    def latest_version(self) -> str | None:
        # Unknown upstream: say nothing is newer rather than offering an update to nothing.
        return _short(self._latest or self._installed)

    @property
    def release_url(self) -> str | None:
        if self._installed and self._latest and self._installed != self._latest:
            return scripts.compare_url(self._installed, self._latest)
        return None

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        # Commits have no order to compare; upstream master differing from what is installed is the update.
        return latest_version != installed_version

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._installed = await self.hass.async_add_executor_job(scripts.installed_commit, data_dir(self.hass))
        # Upstream is checked after setup, not with `update_before_add`: setup, and with it Home Assistant's start,
        # would wait for GitHub (up to CHECK_TIMEOUT when it is slow or unreachable). Polling takes over after
        # SCAN_INTERVAL.
        self._entry.async_create_background_task(self.hass, self._async_first_update(), f"{DOMAIN} drivers check")

    async def _async_first_update(self) -> None:
        await self.async_update()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        self._installed = await self.hass.async_add_executor_job(scripts.installed_commit, data_dir(self.hass))
        try:
            self._latest = await scripts.latest_commit(async_get_clientsession(self.hass))
        except scripts.ScriptsError as err:
            _LOGGER.warning("%s", err)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        ref = self._latest or scripts.BRANCH
        try:
            tarball = await scripts.download(async_get_clientsession(self.hass), ref)
            await self.hass.async_add_executor_job(scripts.install, data_dir(self.hass), tarball)
        except scripts.ScriptsError as err:
            raise HomeAssistantError(str(err)) from err
        self._installed = await self.hass.async_add_executor_job(scripts.installed_commit, data_dir(self.hass))
        _LOGGER.info("rusthinq-scripts updated to %s; the daemon reloads its drivers", _short(self._installed))
