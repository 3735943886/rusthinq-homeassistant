# Router Configuration Guides

This document collects platform-specific configurations for routing outbound LG appliance traffic (TCP port 443)
to Home Assistant.

## Overview

To allow rusthinq to receive appliance traffic without modifying appliance firmware or re-provisioning, your router or gateway must intercept outbound TCP port 443 traffic from the appliances and forward it to the Home Assistant host IP and its configured listener port (default `443`, or an alternative port like `8443`).

---

## Linux / iptables / nftables

For generic Linux-based routers or gateways:

### iptables
```bash
# Using IP range
iptables -t nat -A PREROUTING -p tcp -m iprange --src-range <appliance_ips> --dport 443 -j DNAT --to-destination <ha_ip>:<port>
iptables -t nat -A PREROUTING -p tcp -m iprange --src-range <appliance_ips> --dport 8883 -j DNAT --to-destination <ha_ip>:<port>

# Using ipset
ipset create lg_appliances hash:ip
ipset add lg_appliances <appliance_1_ip>
ipset add lg_appliances <appliance_2_ip>
iptables -t nat -A PREROUTING -p tcp -m set --match-set lg_appliances src --dport 443 -j DNAT --to-destination <ha_ip>:<port>
iptables -t nat -A PREROUTING -p tcp -m set --match-set lg_appliances src --dport 8883 -j DNAT --to-destination <ha_ip>:<port>
```

---

## ipTIME (via WireGuard Client Policy Routing)

ipTIME routers do not support outbound DNAT (port redirect) in their stock web interface. However, modern ipTIME
firmware includes a **WireGuard Client** feature (`Management → VPN Setup → WireGuard Client`), which allows policy
routing traffic from specific client devices (by MAC address) through a WireGuard tunnel.

By deploying a lightweight WireGuard server on your local LAN (e.g., inside a Proxmox LXC, VM, or alongside Home Assistant)
and pointing the ipTIME WireGuard client to that local private IP, appliance traffic can be intercepted and redirected
using iptables DNAT on the WireGuard server.

### 1. WireGuard Server Configuration

Choose one of the following methods to deploy the WireGuard server:

#### Option A: Home Assistant OS WireGuard Add-on (Recommended)

If you are running Home Assistant OS, install the official **WireGuard** add-on from Settings → Add-ons → Add-on Store.
In the add-on's **Configuration** tab, switch to YAML edit mode and configure:

```yaml
server:
  host: <ha_lan_ip> # Your Home Assistant LAN IP (e.g. 192.168.0.50)
  addresses:
    - 10.10.10.1
  dns: []
  post_up: >-
    iptables -A FORWARD -i %i -j ACCEPT;
    iptables -A FORWARD -o %i -j ACCEPT;
    iptables -t nat -A PREROUTING -i %i -p tcp --dport 443 -j DNAT --to-destination <ha_lan_ip>:<port>;
    iptables -t nat -A PREROUTING -i %i -p tcp --dport 8883 -j DNAT --to-destination <ha_lan_ip>:8883;
    iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE;
  post_down: >-
    iptables -D FORWARD -i %i -j ACCEPT;
    iptables -D FORWARD -o %i -j ACCEPT;
    iptables -t nat -D PREROUTING -i %i -p tcp --dport 443 -j DNAT --to-destination <ha_lan_ip>:<port>;
    iptables -t nat -D PREROUTING -i %i -p tcp --dport 8883 -j DNAT --to-destination <ha_lan_ip>:8883;
    iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE;
peers:
  - name: iptime
    addresses:
      - 10.10.10.2
    allowed_ips: []
    client_allowed_ips:
      - 0.0.0.0/0
```
*(Replace `<ha_lan_ip>` with your Home Assistant's local IP, and `<port>` with rusthinq's HTTPS port like `8443` or `443`).*

Start the add-on. The generated client configuration for `iptime` (including its private key, server public key, and pre-shared key if generated) can be retrieved from the add-on log or under `/ssl/wireguard/iptime/`.

#### Option B: Standalone Linux Host or Proxmox LXC

If running a dedicated Linux host or LXC container, configure `/etc/wireguard/wg0.conf`:

```ini
[Interface]
Address = 10.10.10.1/24
ListenPort = 51820
PrivateKey = <Server_Private_Key>

# Enable IP forwarding and redirect appliance traffic to rusthinq
PostUp = sysctl -w net.ipv4.ip_forward=1
PostUp = iptables -A FORWARD -i %i -j ACCEPT
PostUp = iptables -A FORWARD -o %i -j ACCEPT
PostUp = iptables -t nat -A PREROUTING -i %i -p tcp --dport 443 -j DNAT --to-destination <ha_lan_ip>:<port>
PostUp = iptables -t nat -A PREROUTING -i %i -p tcp --dport 8883 -j DNAT --to-destination <ha_lan_ip>:8883
PostUp = iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE

PostDown = iptables -D FORWARD -i %i -j ACCEPT
PostDown = iptables -D FORWARD -o %i -j ACCEPT
PostDown = iptables -t nat -D PREROUTING -i %i -p tcp --dport 443 -j DNAT --to-destination <ha_lan_ip>:<port>
PostDown = iptables -t nat -D PREROUTING -i %i -p tcp --dport 8883 -j DNAT --to-destination <ha_lan_ip>:8883
PostDown = iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE

[Peer]
# ipTIME Router Peer
PublicKey = <ipTIME_Client_Public_Key>
AllowedIPs = 10.10.10.2/32
```

*(Replace `eth0` with your server's primary network interface, and `<ha_lan_ip>:<port>` with the Home Assistant host IP and rusthinq listener port).*

### 2. ipTIME WireGuard Client Setup

1. Open the ipTIME admin page and navigate to **VPN Setup → WireGuard Client** (`관리도구 › VPN 설정 › WireGuard 클라이언트`).
2. Add a new WireGuard connection profile:
   - **Endpoint Address**: Local LAN IP of the WireGuard server (e.g., `192.168.0.x`). ipTIME allows private LAN endpoints.
   - **AllowedIPs**: `0.0.0.0/0`
3. Under **Client Usage Management** (`클라이언트 사용 관리`):
   - Add policy rules selecting each LG appliance by its **MAC address**.
   - Destination: **All destinations** (`모든 목적지`).
   - Network Lock: **Off** (`끄기`).

> [!NOTE]
> Traffic between the appliances and other devices on the same local subnet does not enter the tunnel; only routed traffic
> passing through the gateway is forwarded into WireGuard.

### 3. Reconnect Appliances

Disconnect the appliances from Wi-Fi via the ipTIME client management screen (or power-cycle the appliances). Once
reconnected, their outbound connections will be routed through the WireGuard interface and redirected to rusthinq.
