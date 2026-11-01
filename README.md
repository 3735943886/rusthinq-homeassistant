# rusthinq for Home Assistant

Home Assistant integration for [rusthinq](https://github.com/3735943886/rusthinq) (local control of LG ThinQ
appliances), without an MQTT broker: it runs the rusthinq daemon inside Home Assistant or connects to one you
already run, and turns its devices into entities through an embedded
[IL device](https://github.com/3735943886/ildevice-homeassistant) hub.

**Status:** in development.

The rusthinq binaries are also published on PyPI as [`rusthinq`](https://pypi.org/project/rusthinq/) for use outside
Home Assistant; see [wheel/README.md](wheel/README.md).
