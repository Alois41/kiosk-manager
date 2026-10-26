# Kiosk Manager

> **AI-generated code disclaimer:** This project was developed using AI-generated code. It may contain errors, security vulnerabilities, or incomplete features. Review and test it before deployment; use it at your own risk.

A lightweight web interface for managing a Linux/Chromium TV kiosk. Python standard library backend, French-language browser interface, and no Python package dependencies.

## Features

- Up to eight web pages, with automatic tab rotation or manual switching.
- Full-screen display and a separate login mode; ordinary page edits do not restart Chromium.
- Browser supervision and clean shutdown of the persistent profile.
- HDMI-CEC wake, source-selection requests, and TV power status (hardware dependent).
- HDMI soundboard, audio uploads, volume selection, and an API with a dedicated key.
- X11 resolution and refresh-rate settings for connected physical outputs, saved separately from page configuration.

## Requirements

Designed for Debian/Raspberry Pi OS with an existing graphical session. Chromium is currently launched from `/usr/lib/chromium/chromium`; adapt this path for other distributions.

Required tools: Python 3, Chromium, `xdotool`, `xprop`, `xrandr`, and `xset`. Optional features use `feh` (wallpaper), `cec-ctl` (HDMI-CEC), `ffmpeg`/`ffprobe`, and `aplay` (audio). HDMI and CEC availability depend on the hardware and user permissions.

Physical resolution controls currently support Xorg only. Wayland is explicitly rejected rather than attempting to change a physical screen through Xwayland.

## Install on an existing kiosk

Run as the graphical desktop user. Back up existing kiosk startup scripts and browser profiles before replacing an installation. These commands install the application, not a desktop environment or automatic login.

```sh
mkdir -p ~/.local/share/kiosk-manager ~/.config/kiosk-manager ~/.config/systemd/user
cp kiosk_manager.py display_control.py lan_cluster.py browser_tabs.py tv_control.py audio_control.py index.html ~/.local/share/kiosk-manager/
cp kiosk-manager.service ~/.config/systemd/user/
```

Create `~/.config/kiosk-manager/config.json`:

```json
{
  "pages": [
    {"name": "Example", "url": "https://example.org", "duration": 60}
  ]
}
```

An empty `pages` list stops the managed browser while retaining TV/audio controls. A duration of zero means manual switching.

The supplied user service assumes `DISPLAY=:0` and `XAUTHORITY=~/.Xauthority`; adjust these to the actual desktop session before starting it. Start it from the graphical session or its startup mechanism:

```sh
systemctl --user daemon-reload
systemctl --user start kiosk-manager
```

The unit intentionally has no generic enable target: integrate startup with your actual desktop rather than starting before the graphical session exists.

## Access and security

The server binds only to `127.0.0.1:8765`. On the kiosk open http://localhost:8765. For remote access, use an SSH tunnel:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@kiosk-host
```

Then open http://localhost:8765 on your computer. Do not expose the backend directly. Any reverse proxy must supply authentication, upload/time limits, and appropriate Host/Origin handling. A UI token is not a substitute for network access control.

Site authentication happens in Chromium, not through the manager. Browser profiles, cookies, audio API keys, uploaded sounds, configuration files, and logs are local/private data and are not included in this repository. Full-screen mode is not a security boundary for a hostile public kiosk.

## Display settings

Use the resolution controls to select an output, an advertised resolution and refresh rate, then apply explicitly. The backend reads the actual Xorg mode back before saving to `~/.config/kiosk-manager/display-settings.json` and restores saved modes when the manager starts. Without saved settings, startup does not change the current resolution. It does not generate custom modes.

API (UI token required for writes):

```text
GET /api/display
POST /api/display/settings
{"output":"HDMI-1","mode":"1920x1080","rate":"60.00"}
```

Resolution controls have been exercised in a real browser against two Xorg TV kiosks, including applying and reading back their existing Full HD modes. This does not establish support for every advertised mode or TV model.

## LAN discovery and managing multiple TVs

Open any paired dashboard, sign in using its existing HTTP login, then choose a TV in **TV à gérer**. Pages, browser actions, HDMI-CEC, display settings, soundboard, uploads, and audio settings are sent through that dashboard to the selected TV. The browser stays on the same origin, so it does not prompt for each TV's credentials. This is a single-login management gateway, not an OIDC provider or cross-domain cookie SSO. Directly opening another TV's URL still uses that TV's own login.

Discovery uses signed IPv4 multicast beacons on `239.255.87.65:18766/UDP`, with TTL 1. It does not scan the subnet. Only devices provisioned with the same pairing key and allowed LAN networks appear. Offline peers are marked unavailable; failed requests never silently operate on the local TV. Switching TVs reloads the page with a fixed target and waits for pending edits.

Provision `~/.config/kiosk-manager/cluster.json` privately, mode `0600`, on each TV. Required fields:

- `id`: a unique random 32-character hex node ID per TV.
- `key`: a shared random 64-character hex pairing secret, identical only among trusted TVs.
- `name`: a human-readable TV name.
- `address`: that TV's LAN IPv4 interface address.
- `port`: the common HTTP proxy port, normally `80`.
- `networks`: explicit allowed LAN CIDRs.

Generate IDs and keys with Python's `secrets.token_hex(16)` and `secrets.token_hex(32)` respectively. Transfer the shared key using SSH or another secure channel, never public source control. With no pairing configuration the application stays standalone. All paired devices are equally trusted to administer the group; to remove a compromised member, rotate the key on the remaining devices. This feature does not synchronize or replace the existing HTTP passwords.

Keep the backend bound to loopback. In your authenticated, LAN-restricted reverse proxy, allow machine-to-machine access only to the exact `/api/cluster/relay` location. That location must proxy to the loopback backend without Basic authentication, clear incoming UI-token/Authorization/Origin headers, preserve the signed `X-Kiosk-*` peer headers, and retain LAN access restrictions and body limits. All other locations must keep their existing authentication. The application checks paired identities, destination, HMAC signatures, timestamp freshness, replay nonces, request limits, and an explicit API allowlist; an unsigned relay request is denied. Do not expose the relay to the Internet.

The existing HTTP deployment is for a trusted LAN: HMAC authenticates messages but does not encrypt page configuration or audio content. Use HTTPS or a VPN on an untrusted network. Multicast must be allowed between the TVs; Wi-Fi client isolation or separate VLANs can prevent discovery.

## Development

```sh
python3 -m unittest -v test_display_control test_cluster_http test_lan_cluster
```

Tests use a simulated display-command runner and a real localhost HTTP server; they do not change a physical display. This is focused regression coverage, not an end-to-end hardware test suite.

Local one-off deployment scripts, operator notes, screenshots, and machine-specific reports are deliberately excluded from version control through an allowlist.
