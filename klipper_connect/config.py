"""Bridge configuration (INI file)."""
import configparser
import os
import re
from dataclasses import dataclass
from typing import Optional

from prusa.connect.printer.const import PrinterType

# Prusa serial numbers look like CZPX1234X123XC12345 (same check PrusaLink uses)
SN_REGEX = re.compile(r"^CZPX\d{4}X\d{3}X.\d{5}$")

class ExtraPrinterType:
    """A printer type the SDK's PrinterType enum doesn't list.

    The SDK only knows the printers PrusaLink supports. Others (like the
    MINI, which normally talks to Connect from its own Buddy firmware) are
    described the same way: a (type, version, subversion) tuple that
    Connect sees as "type.version.subversion".
    """

    def __init__(self, name: str, value: tuple, buddy: bool = False):
        self.name = name
        self.value = value
        # Normally runs Prusa's Buddy firmware, which reports itself to
        # Connect a bit differently from PrusaLink (see KlipperPrinter)
        self.buddy = buddy

    def __str__(self):
        return ".".join(str(i) for i in self.value)

    def __repr__(self):
        return f"<ExtraPrinterType.{self.name}: {self.value}>"

    def __eq__(self, other):
        return getattr(other, "value", None) == self.value

    def __hash__(self):
        return hash(self.value)


PRINTER_TYPES = {
    "MK3": PrinterType.I3MK3,
    "MK3S": PrinterType.I3MK3S,
    "MK25": PrinterType.I3MK25,
    "MK25S": PrinterType.I3MK25S,
    # Same numbers the MINI's Buddy firmware reports (PRINTER_TYPE 2)
    "MINI": ExtraPrinterType("MINI", (2, 1, 0), buddy=True),
}

def is_buddy(printer_type) -> bool:
    return bool(getattr(printer_type, "buddy", False))


CAMERA_TRIGGERS = ("10", "30", "60", "layer", "5layers", "manual")

DEFAULT_CONFIG_PATH = "~/printer_data/config/klipper_connect.conf"


@dataclass
class Config:
    # [printer]
    serial_number: str
    printer_type: PrinterType
    part_fan: str = "fan"
    hotend_fan: str = "heater_fan nozzle_cooling_fan"
    # Firmware version reported to Connect. Empty = Klipper's version,
    # reformatted the way Prusa firmware reports it (e.g. 0.12.0-123).
    firmware_version: str = ""
    # Material shown in Connect when Spoolman has no active spool
    default_material: str = ""

    # [connect]
    connect_hostname: str = "connect.prusa3d.com"
    connect_tls: bool = True
    connect_port: int = 0
    # Connect treats this as the PrusaLink version and flags anything older
    # than its latest release as deprecated.
    link_version: str = "0.8.1"
    # File holding the token. Kept outside the Klipper config folder on
    # purpose, so it doesn't end up in a config backup/git repo.
    credentials_file: str = "~/printer_data/klipper_connect/prusa_connect.ini"
    state_file: str = "~/printer_data/klipper_connect/state.json"

    # [moonraker]
    moonraker_url: str = "http://127.0.0.1:7125"
    moonraker_api_key: str = ""

    # [storage]
    storage_name: str = "local"
    gcodes_path: str = "~/printer_data/gcodes"

    # [macros]
    load_filament: str = "LOAD_FILAMENT"
    unload_filament: str = "UNLOAD_FILAMENT"
    # Parameter name for the nozzle temperature, empty to never pass one
    filament_temp_param: str = "TEMP"
    # Prusa firmware runs mesh bed leveling after a plain G28 (G28 W skips
    # it), and Connect's "Start MBL" sends just G28. A plain G28 line from
    # Connect is followed by this G-code. Empty = send G28 as is.
    mbl_gcode: str = "G80"

    # [camera] - only active when snapshot_url is set
    camera_url: str = ""
    camera_name: str = "Webcam"
    # 10, 30, 60 (seconds), layer, 5layers or manual
    camera_trigger: str = "30"
    camera_file: str = "~/printer_data/klipper_connect/camera.json"

    # [bridge]
    # Send details (incl. thumbnail) of this many most recent files to
    # Connect up front, so the file list has thumbnails. 0 = only on demand.
    file_info_count: int = 20
    telemetry_interval: float = 1.0
    log_level: str = "INFO"

    @property
    def connect_server(self) -> str:
        proto = "https" if self.connect_tls else "http"
        if self.connect_port:
            return f"{proto}://{self.connect_hostname}:{self.connect_port}"
        return f"{proto}://{self.connect_hostname}"

    @property
    def sn_looks_valid(self) -> bool:
        return bool(SN_REGEX.match(self.serial_number))


def _path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def default_data_dir(config_path: str) -> str:
    """printer_data folder for a config file.

    <data>/config/klipper_connect.conf -> <data>; anything else falls back
    to ~/printer_data.
    """
    config_dir = os.path.dirname(config_path)
    if os.path.basename(config_dir) == "config":
        return os.path.dirname(config_dir)
    return _path("~/printer_data")


def moonraker_url_from_conf(config_dir: str) -> Optional[str]:
    """URL of the Moonraker whose moonraker.conf sits in `config_dir`.

    With several Klipper instances on one host, each Moonraker listens on
    its own port (7125, 7126, ...); its moonraker.conf says which.
    """
    conf = os.path.join(config_dir, "moonraker.conf")
    if not os.path.exists(conf):
        return None
    ini = configparser.ConfigParser(strict=False, interpolation=None,
                                    inline_comment_prefixes=("#", ";"))
    try:
        ini.read(conf)
        port = ini.getint("server", "port", fallback=7125)
    except (configparser.Error, ValueError):
        return None
    return f"http://127.0.0.1:{port}"


def load(path: str) -> Config:
    path = _path(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    ini = configparser.ConfigParser()
    ini.read(path)

    def get(section, key, default=None):
        if ini.has_option(section, key):
            return ini.get(section, key).strip()
        return default

    # Paths not set in the file are relative to the printer_data folder
    data_dir = _path(get("bridge", "data_dir") or default_data_dir(path))
    own_dir = os.path.join(data_dir, "klipper_connect")

    serial_number = get("printer", "serial_number")
    if not serial_number:
        raise ValueError("[printer] serial_number is required "
                         "(the CZPX... number on the printer's sticker)")
    type_name = get("printer", "type", "MK3S").upper().replace(".", "")
    if type_name not in PRINTER_TYPES:
        raise ValueError(f"[printer] type must be one of "
                         f"{', '.join(PRINTER_TYPES)}")

    cfg = Config(serial_number=serial_number,
                 printer_type=PRINTER_TYPES[type_name])
    cfg.part_fan = get("printer", "part_fan", cfg.part_fan)
    cfg.hotend_fan = get("printer", "hotend_fan", cfg.hotend_fan)
    cfg.firmware_version = get("printer", "firmware_version", "")
    cfg.default_material = get("printer", "default_material", "")

    cfg.connect_hostname = get("connect", "hostname", cfg.connect_hostname)
    if ini.has_option("connect", "tls"):
        cfg.connect_tls = ini.getboolean("connect", "tls")
    if ini.has_option("connect", "port"):
        cfg.connect_port = ini.getint("connect", "port")
    cfg.link_version = get("connect", "link_version", cfg.link_version)
    cfg.credentials_file = _path(get(
        "connect", "credentials_file",
        os.path.join(own_dir, "prusa_connect.ini")))
    cfg.state_file = _path(get("connect", "state_file",
                               os.path.join(own_dir, "state.json")))

    cfg.moonraker_url = (get("moonraker", "url")
                         or moonraker_url_from_conf(os.path.dirname(path))
                         or cfg.moonraker_url).rstrip("/")
    cfg.moonraker_api_key = get("moonraker", "api_key", "")

    # Buddy printers (MINI) store files on "usb"; Connect looks for that
    default_storage = "usb" if is_buddy(cfg.printer_type) else "local"
    cfg.storage_name = get("storage", "name", default_storage)
    if "/" in cfg.storage_name or not cfg.storage_name:
        raise ValueError("[storage] name must be non-empty and contain no '/'")
    cfg.gcodes_path = _path(get("storage", "gcodes_path",
                                os.path.join(data_dir, "gcodes")))

    cfg.load_filament = get("macros", "load_filament", cfg.load_filament)
    cfg.unload_filament = get("macros", "unload_filament", cfg.unload_filament)
    cfg.filament_temp_param = get("macros", "filament_temp_param",
                                  cfg.filament_temp_param)
    cfg.mbl_gcode = get("macros", "mbl_gcode", cfg.mbl_gcode)

    cfg.camera_url = get("camera", "snapshot_url", "")
    cfg.camera_name = get("camera", "name", cfg.camera_name)
    cfg.camera_trigger = get("camera", "trigger", cfg.camera_trigger).lower()
    if cfg.camera_trigger not in CAMERA_TRIGGERS:
        raise ValueError(f"[camera] trigger must be one of "
                         f"{', '.join(CAMERA_TRIGGERS)}")
    cfg.camera_file = _path(get("camera", "token_file",
                                os.path.join(own_dir, "camera.json")))

    if ini.has_option("bridge", "file_info_count"):
        cfg.file_info_count = ini.getint("bridge", "file_info_count")
    elif is_buddy(cfg.printer_type):
        # Connect browses a MINI's folders itself; pushing file details up
        # front isn't how Buddy firmware works
        cfg.file_info_count = 0
    if ini.has_option("bridge", "telemetry_interval"):
        cfg.telemetry_interval = ini.getfloat("bridge", "telemetry_interval")
    cfg.log_level = get("bridge", "log_level", cfg.log_level).upper()
    return cfg


def save_credentials(cfg: Config, token: str) -> None:
    """Write the token in the format the SDK's connection_from_config reads."""
    ini = configparser.ConfigParser()
    ini["service::connect"] = {
        "hostname": cfg.connect_hostname,
        "tls": "True" if cfg.connect_tls else "False",
        "port": str(cfg.connect_port),
        "token": token,
    }
    os.makedirs(os.path.dirname(cfg.credentials_file), exist_ok=True)
    fd = os.open(cfg.credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 0o600)
    with os.fdopen(fd, "w") as f:
        ini.write(f)
