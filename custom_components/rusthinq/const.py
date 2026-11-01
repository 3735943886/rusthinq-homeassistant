"""Constants of the rusthinq integration."""

DOMAIN = "rusthinq"

CONF_URL = "url"
CONF_PREFIX = "prefix"

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
"""ildevice's platforms (`custom_components.ildevice.const.PLATFORMS`); each needs a module here."""
