<p align="center">
  <img src="docs/logo.png" alt="klipper-connect logo" width="128" height="128">
</p>

<h1 align="center">klipper-connect</h1>

<p align="center">Use Prusa Connect with Klipper printers.</p>

<p align="center">
  <a href="https://github.com/amd989/klipper-connect/actions/workflows/tests.yml"><img src="https://github.com/amd989/klipper-connect/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/amd989/klipper-connect" alt="License: MIT"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.9%2B-2547D0?logo=python&logoColor=white" alt="Python 3.9+"></a>
  <a href="https://github.com/amd989/klipper-connect/commits/main"><img src="https://img.shields.io/github/last-commit/amd989/klipper-connect" alt="Last commit"></a>
  <a href="https://amd989.github.io/klipper-connect/"><img src="https://img.shields.io/badge/project%20page-see%20it%20working-2547D0" alt="Project page"></a>
</p>

klipper-connect runs as a small service next to Moonraker and connects your
Klipper printer to [Prusa Connect](https://connect.prusa3d.com). See it side
by side with Mainsail on the
[project page](https://amd989.github.io/klipper-connect/). It uses Prusa's
own [Connect SDK](https://github.com/prusa3d/Prusa-Connect-SDK-Printer), the
same library PrusaLink uses, so Connect sees a regular Original Prusa
printer. Tested on an MK3S and a MINI+ running Klipper.

```mermaid
flowchart LR
    connect["Prusa Connect"]
    bridge["klipper-connect<br/>(Connect SDK)"]
    moonraker["Moonraker"]
    klipper["Klipper"]

    connect <-- "HTTPS" --> bridge
    bridge <-- "HTTP" --> moonraker
    moonraker <--> klipper
```

> This is an independent community project. It isn't affiliated with,
> endorsed by or supported by Prusa Research. "Prusa" and "Prusa Connect"
> are used only to describe what it works with.

## What works

| In Prusa Connect | Comes from / goes to |
|---|---|
| Printer state (idle, printing, paused, finished, stopped, busy, error) | `print_stats`, `idle_timeout`, Klippy state |
| Temperatures, targets, position, fan RPM/power, speed, flow | `extruder`, `heater_bed`, `toolhead`, `gcode_move`, fans |
| Progress, time printing, time remaining | `display_status`, `print_stats`, file metadata |
| Start / pause / resume / stop | `printer/print/*` |
| Set temperatures, move, home, send G-code | `printer/gcode/script` |
| Start MBL | `G28` then `G80` (see [Notes](#notes)) |
| File list with thumbnails, upload from Connect, delete, create folder | the `gcodes` folder |
| "Print after upload" | starts the print once the download finishes |
| Load / unload filament | `LOAD_FILAMENT` / `UNLOAD_FILAMENT` macros |
| Loaded material | the active [Spoolman](#spoolman) spool |
| Print history and statistics | jobs Connect sees, plus Moonraker's lifetime totals |
| Bed mesh | Klipper's `bed_mesh`, sent after each leveling |
| Reset printer | `FIRMWARE_RESTART` (shown as busy while Klipper restarts) |
| Set printer ready | handled by the SDK |
| Camera | snapshots from crowsnest (or any JPEG URL), see below |

Several printers on one host are supported, with one bridge per printer.

Not supported: binary G-code (`.bgcode`), Connect's on-printer dialogs (e.g.
M600 prompts), updates started from Connect, and Connect's spool library
(see [Spoolman](#spoolman)).

## Install (on the Klipper host)

```bash
git clone https://github.com/amd989/klipper-connect.git ~/klipper-connect
cd ~/klipper-connect
./scripts/install.sh
```

The installer:
- sets up a Python environment in `~/klipper-connect-env`
- copies the example config to `<config>/klipper_connect.conf` (an
  existing config is kept)
- installs and enables the `klipper-connect` systemd service
- adds klipper-connect to Moonraker's update manager in `moonraker.conf`
  and restarts Moonraker, so it shows up in Mainsail/Fluidd's update panel
  (skipped if an `[update_manager klipper-connect]` section already exists)

It can be run again at any time; it keeps your config.

Options for other folder layouts (multiple printers, custom paths):

| Option | Meaning | Default |
|---|---|---|
| `-d DIR` | printer_data folder | `~/printer_data` |
| `-c DIR` | Klipper config folder | `<printer_data>/config` |
| `-e DIR` | Python environment | `~/klipper-connect-env` |
| `-m NAME` | Moonraker's systemd service | `moonraker` |
| `-n NAME` | this bridge's systemd service | `klipper-connect` |
| `-M` | don't touch `moonraker.conf` | |

**Several printers on one host** (e.g. installed with KIAUH): run the
installer once per printer, each with its own data folder, Moonraker service
and bridge name:

```bash
./scripts/install.sh -d ~/mini_data -m moonraker-mini -n klipper-connect-mini
./scripts/install.sh -d ~/ender3s1_data -m moonraker-ender3s1 -n klipper-connect-ender
```

Each bridge talks to its own Moonraker: the port is read from the
`moonraker.conf` next to its `klipper_connect.conf` (or set `[moonraker] url`).
The Python environment and the repo are shared.

Then:

1. Edit `<config>/klipper_connect.conf` and set
   `serial_number` to the `CZPX...` number on the printer's sticker.
2. Link the printer to your Connect account:
   ```bash
   ~/klipper-connect-env/bin/klipper-connect -c <config>/klipper_connect.conf register
   ```
   (the installer prints the exact command). Open the link it prints and
   confirm. The token goes to
   `<printer_data>/klipper_connect/prusa_connect.ini`, which is outside the
   config folder so it doesn't end up in config backups.
3. Start it (use the `-n` name if you gave one):
   ```bash
   sudo systemctl start klipper-connect
   journalctl -u klipper-connect -f
   ```

### With KIAUH

[kiauh/](kiauh/) has a KIAUH extension that does the same from KIAUH's
extensions menu, for every printer on the host. See [kiauh/README.md](kiauh/README.md).

### Camera (optional)

Point `[camera] snapshot_url` at a URL that returns one JPEG. With crowsnest
that's the ustreamer port of your camera:

```ini
[camera]
snapshot_url: http://127.0.0.1:8080/?action=snapshot
name: Webcam
trigger: 30
```

The camera registers with Connect by itself and appears under the printer's
Camera tab. It sends a snapshot every 10, 30 or 60 seconds, or on each layer
(`layer`) or every fifth layer (`5layers`). It also sends one when a print
starts, finishes or is stopped. Layer triggers need the slicer to report
layers to Klipper (`SET_PRINT_STATS_INFO CURRENT_LAYER=...`, which
PrusaSlicer can add in its layer-change G-code). The camera token is stored
in `<printer_data>/klipper_connect/camera.json`. If the webcam is
unreachable, klipper-connect logs it once and keeps trying.

### Moonraker update manager

The installer adds this section to `moonraker.conf`. With `-n`, the section
and `managed_services` use that name instead. Use `-M` to skip this and add
it yourself, e.g. if `moonraker.conf` is in a git repo:

```ini
[update_manager klipper-connect]
type: git_repo
path: ~/klipper-connect
origin: https://github.com/amd989/klipper-connect.git
primary_branch: main
virtualenv: ~/klipper-connect-env
requirements: requirements.txt
managed_services: klipper-connect
```

### Spoolman

If `moonraker.conf` has a `[spoolman]` section, klipper-connect reports the
**material of the active spool** (e.g. `PLA`) to Connect as the loaded
filament, refreshed every 30 seconds. The active spool is the one picked in
Mainsail/Fluidd's Spoolman panel or with `SET_ACTIVE_SPOOL`.

- Connect needs a loaded material before it enables "assign filament" on
  the Tools tab.
- While printing, the spool's material wins over the one in the G-code file.
- Without Spoolman or an active spool, `default_material` in `[printer]` is
  used, if set.

Only the material type is sent. Connect's own spool library (consumed
grams, colour, tags) is separate: the printer connection can't select a
Connect spool or update it. Spoolman keeps tracking usage through Moonraker
as usual; klipper-connect only reads from it.

### Prusa MINI

Set `type: MINI` in `[printer]`.
[examples/klipper_connect.mini.conf](examples/klipper_connect.mini.conf) is a
starting point (MINI fan names, `EXTRUDER=` for the filament macros). Connect
normally talks to MINIs running Prusa's Buddy firmware, so klipper-connect
reports itself the way Buddy does:

- printer type 2.1.0, with files on a `usb` storage (`/usb/...` in Connect)
- folders are listed when Connect browses them
- **encrypted downloads**: Connect sends a MINI files encrypted, from
  `buddy-a.connect.prusa3d.com`. klipper-connect decrypts them (AES-CBC or
  AES-CTR, detected automatically) using `pycryptodome`, which the installer
  pulls in.
- Connect shows the firmware version as "deprecated" (it expects Buddy
  8.x). Set `firmware_version` in `[printer]` to hide that, e.g. `8.0.0`.
- the "Start MBL" button isn't shown for a MINI; send `G80` from the G-code
  box instead.

Recent Buddy firmware talks to Connect over a websocket; klipper-connect
uses the older HTTP protocol, which Connect still accepts. If Prusa stops
accepting it for MINIs, MINI support would need updating. The MK3S is
unaffected (it goes through the same route as PrusaLink).

Turn off binary G-code in PrusaSlicer's MINI profile.

### Troubleshooting

Every command from Connect and its result is logged:

```bash
journalctl -u klipper-connect --since "15 min ago" | grep -E "Connect command|ERROR|WARNING"
```

- `Connect command 12 GCODE: {...} -> FINISHED` lines show what Connect
  asked for and whether it worked.
- `Connect POST /p/events -> 503` style lines show which request Connect
  rejected.
- A printer stuck in ERROR usually means Klipper isn't ready, or the bridge
  is talking to the wrong Moonraker. Check `[moonraker] url`, or the port in
  the `moonraker.conf` next to `klipper_connect.conf`.

## Notes

- **Slicer output:** Klipper reads plain-text G-code only, so turn off
  binary G-code in PrusaSlicer (Printer Settings → General → Binary G-code).
- **Prusa-specific G-code:** files sliced with Prusa's MK3S profile contain
  `M862.x` checks and similar commands. Your Klipper config needs macros for
  these, as it does today when you print from Mainsail.
- **Start MBL:** Connect sends a plain `G28`, because Prusa firmware levels
  the bed after homing. The bridge runs `G28` followed by `G80` for such a
  command (`mbl_gcode` in `[macros]`; `G28 W` and per-axis homing are left
  alone, and nothing is changed during a print).
- **Thumbnails:** details of the 20 newest files (and any new ones) are
  sent to Connect up front, so the file list can show thumbnails
  (`file_info_count` in `[bridge]`).
- **State mapping:** Klipper keeps "complete" or "cancelled" until the next
  print. klipper-connect shows FINISHED/STOPPED for 11 seconds (as PrusaLink
  does), then IDLE.
- **Serial number:** Connect identifies the printer by its serial number
  (and a hash of it). Use the real one from the sticker. If the printer
  is connected over USB, it is also in the
  `/dev/serial/by-id/...Original_Prusa_i3_MK3_CZPX...` device name.
- **Job ids** are stored in `state.json`, so Connect sees one continuous job
  if the bridge restarts mid-print.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The SDK's file watcher needs Linux (inotify). On macOS the tests stub it
out; to run them with the real watcher:

```bash
docker run --rm -v "$PWD":/src -w /src python:3.11-slim \
    sh -c "pip install -q -e '.[dev]' && pytest -q"
```

## License

MIT, see [LICENSE](LICENSE). The Prusa Connect SDK is not part of this
repository: it's installed from PyPI as a dependency and published by Prusa
Research under its own terms.
