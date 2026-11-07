<p align="center"><img src="custom_components/rusthinq/brand/icon.png" alt="" width="128" /></p>

# rusthinq for Home Assistant

Local control of LG ThinQ appliances in Home Assistant through [rusthinq](https://github.com/3735943886/rusthinq),
without an MQTT broker or an add-on. Home Assistant runs the rusthinq daemon itself; the appliances connect to it
instead of LG's cloud, and their drivers' output becomes Home Assistant devices and entities through an embedded
[IL device](https://github.com/3735943886/ildevice-homeassistant) hub.

## Requirements

- Home Assistant 2025.1.3 or later.
- LG appliances must be able to reach this Home Assistant host on TCP 443 (or an alternative port via router DNAT;
  see [Ports](#ports)).
- For new installations, see [Connecting appliances](#connecting-appliances). Appliances already provisioned for an
  existing rusthinq can keep their CA and device setup when you [move the installation](#moving-an-existing-installation).

## Installation

1. In Marketplace, find **rusthinq** under Integrations and download it, then restart Home Assistant. If this repository
   has not yet appeared in the Marketplace default store, add `https://github.com/3735943886/rusthinq-local` using
   **Add from link** and download it.
2. Settings → Devices & services → Add integration → **rusthinq**, and choose:
   - **Run rusthinq in Home Assistant** for a new installation, or
   - **Move an existing rusthinq into Home Assistant** to keep an installation you already run
     (see [Moving an existing installation](#moving-an-existing-installation)).

On the first start Home Assistant installs the `rusthinq` package from PyPI (the daemon binaries), creates a CA and
downloads the drivers ([rusthinq-scripts](https://github.com/3735943886/rusthinq-scripts)). Everything lives in
`/config/rusthinq/`, which Home Assistant backups include. `ca.key` and the LG account files are secrets.

The rusthinq service device has a **Download backup** button. Press it, then follow the authenticated download link in
the Home Assistant notification. The archive contains the daemon configuration, CA, device ledger and LG account
files needed to move the installation. In **Configure**, **Reset rusthinq data** clears those files and retained device
state, then creates a new CA; appliances may need provisioning again. Downloaded binaries and drivers are kept.

## Ports

| Listener | Default | Notes |
|---|---|---|
| HTTPS | 443 | Appliances always open 443; fixed by their firmware. |
| MQTT | 8883 | Announced to appliances when they register; any free port works. The Mosquitto add-on also uses 8883 for TLS, so pick another if it runs. |
| ThinQ1 HTTPS / ThinQ1 | 46030 / 47878 | Older ThinQ1 appliances. |
| Management API | random, 127.0.0.1 only | Used by this integration; nothing to configure. |

The setup checks port availability before saving configuration. If a port becomes unusable later, an issue is raised
in Settings → Repairs, and rusthinq keeps retrying.

The default hostname `rusthinq.lan` serves as a fallback for environments where router DNAT is not available and
traffic must be steered via local DNS overrides instead. When using router DNAT, no DNS configuration is required.

When port 443 is already in use on the host (e.g. by a reverse proxy or SSL add-on), configure rusthinq's HTTPS to
another port (such as 8443). Outbound port 443 traffic from appliances is then routed to that port as described in
[Connecting appliances](#connecting-appliances). rusthinq continues to advertise port 443 to appliances regardless of
the listener port.

## Connecting appliances

Appliances already registered in the official LG ThinQ app do not need a factory reset or re-provisioning.
Connecting them requires routing their outbound TCP 443 traffic to this Home Assistant host (default port 443, or
another port like 8443 if 443 is taken).

1. **Network connectivity**:
   New appliances are first set up in the official LG ThinQ app to join the Wi-Fi network and obtain a DHCP lease
   (static DHCP reservations are recommended).
2. **Route appliance traffic (leave DNS untouched)**:
   In order of preference:
   - **Router / Gateway DNAT** (Recommended; standard for Home Assistant OS and IoT VLANs):
     Forward port 443 from appliance IP addresses to Home Assistant (see [Router configuration examples](docs/routers.md)):
     `iptables -t nat -A PREROUTING -p tcp -m iprange --src-range <appliance_ips> --dport 443 -j DNAT --to-destination <ha_ip>:<port>`
     (or with an ipset: `iptables -t nat -A PREROUTING -p tcp -m set --match-set lg_appliances src --dport 443 -j DNAT --to-destination <ha_ip>:<port>`)
   - **Host firewall** (Home Assistant Container, Supervised, Core on a custom Linux host):
     Redirect 443 locally for appliance source addresses:
     `iptables -t nat -A PREROUTING -p tcp -m iprange --src-range <appliance_ips> --dport 443 -j REDIRECT --to-ports <port>`
     (or with an ipset: `iptables -t nat -A PREROUTING -p tcp -m set --match-set lg_appliances src --dport 443 -j REDIRECT --to-ports <port>`)
   - **TLS passthrough proxy on 443** (nginx `stream` with `ssl_preread`, HAProxy, Traefik `HostSNI`):
     Send LG hostnames and connections without SNI to rusthinq unterminated. Some appliances send no SNI. Proxies
     terminating TLS do not work.
3. **Reconnect the appliance**:
   Power-cycle the appliance or disconnect/reconnect it from Wi-Fi so it drops the existing LG session and reconnects
   through the new route. Alternatively, clear active router connection tracking with
   `conntrack -D -p tcp -s <appliance_ip>`.
4. **Automatic discovery**:
   Once the appliance connects to rusthinq, Home Assistant automatically creates the corresponding device and entities.
5. **Retain the official LG ThinQ app (Optional)**:
   Sign in to the LG account under Settings → Devices & services → rusthinq → **Configure**. Then on the appliance's
   device page in Home Assistant, press **Pair with LG**. This starts the local LG cloud bridge, keeping the official
   mobile app operational alongside Home Assistant.

## Moving an existing installation

Moving keeps the CA the appliances trust, so none needs provisioning again. Add the integration with **Move an
existing rusthinq into Home Assistant** and take the old installation's backup one of two ways:

- **Copy it from the running rusthinq** (0.2.0 or later): give its management API address, and its user and password
  if it has them. rusthinq hands the backup out only behind credentials or on a loopback bind, since it holds the CA
  key and LG tokens.
- **Upload a backup file**: the dashboard's System → Download backup, or `sudo thinqctl backup`. Without either, an
  archive of `config.toml` and the files it names, made in its directory, works too:
  `tar czf rusthinq-export.tar.gz config.toml ca.key ca.cert devices.json account.json account.json.pairings`

The next step shows what the backup holds (host name, devices, LG account, the rusthinq version that wrote it) and
asks for this host's ports, filled in with the old installation's. Then:

1. Stop rusthinq on the old host and keep it stopped: two daemons would answer the same appliances and LG account.
   A backup copied from a running rusthinq is installed only once that rusthinq no longer answers.
2. Submit the ports, changing those this host cannot open (the form says which).
3. Point the appliances' route (DNS or redirect) at this host.

The CA, the device ledger, the LG account with its pairings and the settings appliances notice (`hostname`,
`legacy_tls`, `advertise_requested_host`, `bridge_dns`, the driver topic prefix) move over, and the listener ports
are suggested. External MQTT and its
retained-topic inventory stay behind. If files in `/config/rusthinq/` differ from the backup, the confirmation step lists
them and requires an explicit choice to overwrite those included in the backup. Files absent from the backup are kept.
Replacing the CA may require provisioning appliances again.

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

Deleting an appliance on its device page also forgets it in rusthinq. If it is paired with LG (*Pair with LG*), that
also removes it from the LG account, even with the LG bridge switched off. That is refused while it is connected, since
it would come right back: switch it off or take it off the network first. An appliance that connects again later
appears again. An appliance that is merely offline is never forgotten.

rusthinq's own dashboard is not exposed: its management API listens on 127.0.0.1 with credentials made at each start,
and the integration covers what the dashboard offers for everyday use.

## Troubleshooting

- The daemon's own log is the logger `custom_components.rusthinq.daemon`; the integration's is
  `custom_components.rusthinq`.
- A command that cannot be carried out (the appliance is offline, a value out of range…) fires the Home Assistant
  event `ildevice_command_rejected` with the property, a code and the reason, and is logged.
- A setup that keeps retrying shows its reason on the integration card (a port in use, for example).
- If Home Assistant was killed, a daemon it left running is stopped on the next start.
- *Download diagnostics* on the integration or on an appliance gives what to attach to an issue: rusthinq's own
  diagnostics, the devices as rusthinq lists them, the daemon's last log lines, and per appliance its descriptor and
  current values. Credentials are left out.

## The `rusthinq` package

The daemon binaries are published on PyPI as [`rusthinq`](https://pypi.org/project/rusthinq/), built from rusthinq's
releases by this repository (see [wheel/README.md](wheel/README.md)); they can be used outside Home Assistant too.

## License

GPL-2.0-or-later, like rusthinq ([LICENSE](LICENSE)).
