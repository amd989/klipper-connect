# KIAUH extension

[KIAUH](https://github.com/dw-0/kiauh) extension that installs, updates and
removes klipper-connect from KIAUH's extensions menu.

It's meant to be contributed to KIAUH (`kiauh/extensions/klipper_connect`).
Until then, you can use it from your own KIAUH:

```bash
cp -r ~/klipper-connect/kiauh/klipper_connect ~/kiauh/kiauh/extensions/
```

(or clone this repo somewhere first). Start KIAUH and open
**Extensions → klipper-connect**. The copied folder survives KIAUH's
self-updates because git leaves untracked files alone.

## What it does

**Install**
- clones klipper-connect to `~/klipper-connect` and sets up
  `~/klipper-connect-env`
- for each Moonraker instance you confirm (one bridge per printer):
  - asks for the printer's serial number and type and writes
    `<printer_data>/config/klipper_connect.conf` (an existing one is kept)
  - creates `klipper-connect[-<instance>].service`, started after that
    instance's Moonraker
  - adds the service to `moonraker.asvc` and an
    `[update_manager klipper-connect[-<instance>]]` section to
    `moonraker.conf` (after a backup)
- offers to link each printer to Prusa Connect right away; the service is
  enabled once the printer is linked

**Update:** `git pull`, refreshes the Python environment and restarts the
klipper-connect services that were running.

**Remove:** stops and removes all klipper-connect services, the update
manager sections, the `moonraker.asvc` entries, the repo and the Python
environment. Configs and Prusa Connect tokens are kept.

## License

The files in this folder follow KIAUH's license (GPLv3), since they're
written to become part of KIAUH. The rest of klipper-connect is MIT.
