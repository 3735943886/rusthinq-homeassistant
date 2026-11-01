"""IL entities of this platform, created by the ildevice hub."""

from .platform import platform_setup

async_setup_entry = platform_setup(__name__)
