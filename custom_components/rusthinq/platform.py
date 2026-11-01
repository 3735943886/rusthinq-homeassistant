"""What every platform module does: hand its `async_add_entities` to the ildevice hub, which creates the entities as
descriptors arrive."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import RusthinqConfigEntry


def platform_setup(module: str):
    """The `async_setup_entry` of the platform module named `module` (its `__name__`). `binary_sensor` and `sensor`
    also carry this integration's device status entities (`status.py`)."""
    platform = module.rsplit(".", 1)[-1]

    async def async_setup_entry(
        hass: HomeAssistant, entry: RusthinqConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        entry.runtime_data.hub.register_platform(platform, async_add_entities)
        if platform in ("binary_sensor", "sensor"):
            entry.runtime_data.status.register_platform(platform, async_add_entities)

    return async_setup_entry
