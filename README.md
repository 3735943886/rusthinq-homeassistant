# rusthinq for Home Assistant

Local control of LG ThinQ appliances in Home Assistant through [rusthinq](https://github.com/3735943886/rusthinq),
without an MQTT broker or an add-on. Home Assistant runs the rusthinq daemon itself; the appliances connect to it
instead of LG's cloud, and their drivers' output becomes Home Assistant devices and entities through an embedded
[IL device](https://github.com/3735943886/ildevice-homeassistant) hub.

**Status:** in development (rusthinq 0.2.0-rc8). Linux x86_64 and aarch64 hosts (Home Assistant OS, Container, Core).

## Requirements

- Home Assistant 2025.1.3 or later.
- [IL device](https://github.com/3735943886/ildevice-homeassistant), installed through HACS. This integration uses its
  entity code; you do not need to set it up, and no MQTT broker is needed.
- Appliances must reach this host on TCP 443 (see [Ports](#ports)) and be provisioned for rusthinq, or already be
  provisioned for an existing rusthinq that you move in.

## Installation

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/3735943886/rusthinq-homeassistant`
   (category Integration). Install **IL device** and **rusthinq**, then restart Home Assistant.
2. Settings → Devices & services → Add integration → **rusthinq**, and choose:
   - **Run rusthinq in Home Assistant** for a new installation, or
   - **Move an existing rusthinq into Home Assistant** to keep an installation you already run
     (see [Moving an existing installation](#moving-an-existing-installation)).

On the first start Home Assistant installs the `rusthinq` package from PyPI (the daemon binaries), creates a CA and
downloads the drivers ([rusthinq-scripts](https://github.com/3735943886/rusthinq-scripts)). Everything lives in
`/config/rusthinq/`, which Home Assistant backups include. `ca.key` and the LG account files are secrets.

## Ports

| Listener | Default | Notes |
|---|---|---|
| HTTPS | 443 | Appliances always open 443; fixed by their firmware. |
| MQTT | 8883 | Announced to appliances when they register; any free port works. The Mosquitto add-on also uses 8883 for TLS, so pick another if it runs. |
| ThinQ1 HTTPS / ThinQ1 | 46030 / 47878 | Older ThinQ1 appliances. |
| Management API | random, 127.0.0.1 only | Used by this integration; nothing to configure. |

The setup checks the ports before writing anything. A port that becomes unusable later is raised in Settings →
Repairs, and rusthinq keeps retrying.

When 443 is already taken on the host, run rusthinq's HTTPS on another port (8443, say) and send the appliances'
port 443 there. rusthinq then tells appliances 443 anyway. In order of preference:

1. **Host firewall** (Home Assistant Container, Supervised, Core): redirect 443 from the appliances only, e.g. with
   their DHCP-reserved addresses in an ipset:
   `iptables -t nat -A PREROUTING -p tcp --dport 443 -m set --match-set lg_appliances src -j REDIRECT --to-ports 8443`
2. **Router** (Home Assistant OS, which has no host firewall to change): a DNAT rule for the appliances' 443 to this
   host's 8443. The appliances' traffic must pass the router (an IoT VLAN, for instance).
3. **TLS passthrough proxy on 443** (nginx `stream` with `ssl_preread`, HAProxy, Traefik `HostSNI`): send LG's host
   names **and connections without SNI** to rusthinq unterminated. Some appliances send no SNI. A proxy that
   terminates TLS does not work.

## Moving an existing installation

Moving keeps the CA the appliances trust, so none needs provisioning again.

1. Stop rusthinq on the old host and keep it stopped: two daemons would answer the same appliances and LG account.
2. In the directory of its `config.toml`, archive it with the files it names, for example:
   `tar czf rusthinq-export.tar.gz config.toml ca.key ca.cert devices.json account.json account.json.pairings`
3. Add the integration with **Move an existing rusthinq into Home Assistant**, upload the archive and choose this
   host's ports.
4. Point the appliances' route (DNS or redirect) at this host.

The CA, the device ledger, the LG account with its pairings and the settings appliances notice (`hostname`,
`legacy_tls`, `advertise_requested_host`, `bridge_dns`, the driver topic prefix) move over. Different files already in
`/config/rusthinq/` are never replaced.

## LG account

Settings → Devices & services → rusthinq → **Configure** signs rusthinq in to LG and out. Signing in opens LG's page;
after it, copy the address the browser ends on (`…/login/iabClose…`) back into the dialog. With an account, devices
take the names they have in the LG ThinQ app, and each device's LG bridge keeps the ThinQ app working.

## What appears

- **Each appliance**: the entities its driver describes (climate, sensors, switches…), plus
  - diagnostic: *Local connection*, *LG bridge*, *Driver* (a problem when the driver failed or is missing), *Firmware*;
  - configuration (with an LG account): *LG bridge* switch (enable/disable), *Pair with LG* and *Unpair from LG*.
- **rusthinq** (a service device): *LG account*, and *Drivers*, an update entity that installs the latest
  rusthinq-scripts; the daemon reloads them without restarting.

Entities are unavailable while the daemon is not running. An appliance that is off (a washer leaves Wi-Fi when switched
off) keeps its entities across Home Assistant restarts, unavailable until it connects again.

## Troubleshooting

- The daemon's own log is the logger `custom_components.rusthinq.daemon`; the integration's is
  `custom_components.rusthinq`.
- A command that cannot be carried out (the appliance is offline, a value out of range…) fires the Home Assistant
  event `ildevice_command_rejected` with the property, a code and the reason, and is logged.
- A setup that keeps retrying shows its reason on the integration card (a port in use, for example).
- If Home Assistant was killed, a daemon it left running is stopped on the next start.

## The `rusthinq` package

The daemon binaries are published on PyPI as [`rusthinq`](https://pypi.org/project/rusthinq/), built from rusthinq's
releases by this repository (see [wheel/README.md](wheel/README.md)); they can be used outside Home Assistant too.

## License

GPL-2.0-or-later, like rusthinq ([LICENSE](LICENSE)).
