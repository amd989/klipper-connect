# klipper-connect

[![Tests](https://github.com/amd989/klipper-connect/actions/workflows/tests.yml/badge.svg)](https://github.com/amd989/klipper-connect/actions/workflows/tests.yml)

Use [Prusa Connect](https://connect.prusa3d.com) with a Klipper printer.
klipper-connect runs as a small service next to Moonraker. It uses Prusa's
own [Connect SDK](https://github.com/prusa3d/Prusa-Connect-SDK-Printer), the
same library PrusaLink uses, so Connect sees a regular Original Prusa
printer (MK3S tested; MINI experimental).

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

| Connect | Klipper / Moonraker |
|---|---|
| Printer state (idle, printing, paused, finished, stopped, busy, error) | `print_stats`, `idle_timeout`, Klippy state |
| Temperatures, targets, position, fan RPM/power, speed, flow | `extruder`, `heater_bed`, `toolhead`, `gcode_move`, fans |
| Progress, time printing, time remaining, material | `display_status`, `print_stats`, file metadata |
| Start / pause / resume / stop | `printer/print/*` |
| Send G-code | `printer/gcode/script` |
| File list, upload from Connect, delete, create folder | the `gcodes` folder directly |
| "Print after upload" | starts the print once the download finishes |
| Load / unload filament | `LOAD_FILAMENT` / `UNLOAD_FILAMENT` macros |
| Reset printer | `FIRMWARE_RESTART` |
| Set printer ready | handled by the SDK |
| Camera | snapshots from crowsnest (or any JPEG URL), see below |

Also: loaded material from Spoolman, lifetime totals from Moonraker's
history, and the bed mesh after leveling.

Not done yet: binary G-code (`.bgcode`), dialogs (e.g. M600 prompts),
Connect-triggered updates.

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

The installer adds this section to `moonraker.conf` (use `-M` to skip it and
add it yourself, e.g. if `moonraker.conf` is in a git repo):

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

### Prusa MINI (experimental)

Set `type: MINI` in `[printer]`; [examples/klipper_connect.mini.conf](examples/klipper_connect.mini.conf)
is a starting point (MINI fan names, `EXTRUDER=` for the filament macros).
klipper-connect then reports printer type 2.1.0, the same as the MINI's own
Buddy firmware. Connect has only seen MINIs running that firmware, so some
MINI-only features may not work; the log shows any command that fails.
Turn off binary G-code in PrusaSlicer's MINI profile.

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
