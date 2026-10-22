"""Command line: `klipper-connect register` and `klipper-connect run`."""
import argparse
import logging
import signal
import sys
import threading

from . import __version__
from . import config as config_mod
from .bridge import Bridge, firmware_version, make_printer
from .moonraker import Moonraker, MoonrakerError

log = logging.getLogger("klipper-connect")

REGISTER_TIMEOUT = 30 * 60  # Connect's registration codes last 30 minutes


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The SDK logs every telemetry request at DEBUG; keep it quieter.
    if level != "DEBUG":
        logging.getLogger("connect-printer").setLevel(logging.WARNING)


def cmd_register(cfg: config_mod.Config) -> int:
    printer = make_printer(cfg)
    printer.server = cfg.connect_server
    printer.camera_controller.server = cfg.connect_server
    try:
        info = Moonraker(cfg.moonraker_url, cfg.moonraker_api_key)\
            .printer_info()
        printer.firmware = (cfg.firmware_version
                            or firmware_version(info.get("software_version")))
    except MoonrakerError:
        printer.firmware = cfg.firmware_version or None

    done = threading.Event()

    def on_token(token):
        config_mod.save_credentials(cfg, token)
        done.set()

    printer.register_handler = on_token
    code = printer.register()
    url = f"{cfg.connect_server}/add-printer/connect/{printer.type}/{code}"
    print("\nOpen this link to add the printer to Prusa Connect:\n")
    print(f"    {url}\n")
    print(f"(or enter code {code} at {cfg.connect_server}/add)\n")
    print("Waiting for confirmation... (Ctrl+C to cancel)")

    thread = threading.Thread(target=printer.loop, daemon=True)
    thread.start()
    try:
        ok = done.wait(REGISTER_TIMEOUT)
    except KeyboardInterrupt:
        ok = False
    printer.stop_loop()
    if not ok:
        print("Registration was not confirmed.")
        return 1
    print(f"Registered. Token saved to {cfg.credentials_file}")
    print("Now start the service: sudo systemctl restart klipper-connect")
    return 0


def cmd_run(cfg: config_mod.Config) -> int:
    bridge = Bridge(cfg)

    def on_signal(signum, _frame):
        log.info("Got signal %s, stopping", signum)
        bridge.stop_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        bridge.run()
    except FileNotFoundError as err:
        log.error("%s - run `klipper-connect register` first", err)
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="klipper-connect",
        description="Bridge a Klipper/Moonraker printer to Prusa Connect")
    parser.add_argument("-c", "--config",
                        default=config_mod.DEFAULT_CONFIG_PATH,
                        help="config file (default: %(default)s)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("register", help="link this printer to Prusa Connect")
    sub.add_parser("run", help="run the bridge (what the service does)")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        print("klipper-connect needs Linux (the Connect SDK uses inotify). "
              "Run it on the Klipper host.", file=sys.stderr)
        return 2

    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as err:
        print(f"Config error: {err}", file=sys.stderr)
        return 2
    setup_logging(cfg.log_level)
    if not cfg.sn_looks_valid:
        log.warning("Serial number %r doesn't look like a Prusa serial "
                    "(CZPX....X...X.....); Connect may reject it",
                    cfg.serial_number)

    if args.command == "register":
        return cmd_register(cfg)
    return cmd_run(cfg)


if __name__ == "__main__":
    sys.exit(main())
