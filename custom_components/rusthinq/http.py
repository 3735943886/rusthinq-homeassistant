"""Authenticated download of the embedded daemon's portable backup."""

from __future__ import annotations

from datetime import UTC, datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .backup import create
from .const import DOMAIN
from .embedded import data_dir


class BackupView(HomeAssistantView):
    url = f"/api/{DOMAIN}/backup/{{entry_id}}"
    name = f"api:{DOMAIN}:backup"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise web.HTTPNotFound()
        try:
            archive = await hass.async_add_executor_job(create, data_dir(hass))
        except (OSError, ValueError) as err:
            raise web.HTTPServiceUnavailable(text=f"Could not create rusthinq backup: {err}") from err
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return web.Response(
            body=archive,
            content_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="rusthinq-backup-{stamp}.tar.gz"'},
        )


