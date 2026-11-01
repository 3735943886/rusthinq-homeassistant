"""Constants of the rusthinq integration."""

DOMAIN = "rusthinq"

CONF_MODE = "mode"
MODE_EMBEDDED = "embedded"
"""Home Assistant runs the daemon (the normal setup)."""
MODE_URL = "url"
"""A daemon already running at `CONF_URL` (development; entries without `CONF_MODE` are this)."""

CONF_URL = "url"
CONF_PREFIX = "prefix"
CONF_HTTPS_PORT = "https_port"
CONF_MQTT_PORT = "mqtt_port"
CONF_THINQ1_HTTPS_PORT = "thinq1_https_port"
CONF_THINQ1_PORT = "thinq1_port"
CONF_SETTINGS = "settings"
"""An imported installation's daemon settings (`migrate.Imported.settings`), kept for every start."""
CONF_ARCHIVE = "archive"

DEFAULT_PORTS = {CONF_HTTPS_PORT: 443, CONF_MQTT_PORT: 8883, CONF_THINQ1_HTTPS_PORT: 46030, CONF_THINQ1_PORT: 47878}
"""The appliance-facing listeners, at `config-0.2.sh`'s defaults. Appliances always open HTTPS on 443; another HTTPS
port needs a redirect in front of it. The MQTT port is announced to them, so any free port works."""

REQUIREMENT = "rusthinq==0.2.0"

DEFAULT_URL = "http://127.0.0.1:44401"
DEFAULT_PREFIX = "rusthinq"
"""rusthinq's `[drivers] topic_prefix`: the IL prefix of its devices and the root of their command topics."""

PLATFORMS = [
    "alarm_control_panel",
    "binary_sensor",
    "button",
    "climate",
    "cover",
    "event",
    "fan",
    "humidifier",
    "light",
    "lock",
    "number",
    "select",
    "sensor",
    "siren",
    "switch",
    "text",
    "vacuum",
    "valve",
]
"""ildevice's platforms (`ildevice_ha.const.PLATFORMS`); each needs a module here."""

ENTRY_PLATFORMS = [*PLATFORMS, "update"]
"""What an entry forwards: ildevice's platforms and this integration's own `update` (the drivers of the embedded
daemon)."""
