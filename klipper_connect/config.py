"""Bridge configuration (INI file)."""
import configparser
import os
import re
from dataclasses import dataclass

from prusa.connect.printer.const import PrinterType

# Prusa serial numbers look like CZPX1234X123XC12345 (same check PrusaLink uses)
SN_REGEX = re.compile(r"^CZPX\d{4}X\d{3}X.\d{5}$")

PRINTER_TYPES = {
    "MK3": PrinterType.I3MK3,
    "MK3S": PrinterType.I3MK3S,
    "MK25": PrinterType.I3MK25,
    "MK25S": PrinterType.I3MK25S,
}

CAMERA_TRIGGERS = ("10", "30", "60", "layer", "5layers", "manual")

DEFAULT_CONFIG_PATH = "~/printer_data/config/klipper_connect.conf"


@dataclass
class Config:
    # [printer]
    serial_number: str
    printer_type: PrinterType
    part_fan: str = "fan"
    hotend_fan: str = "heater_fan nozzle_cooling_fan"
    # Firmware version reported to Connect. Empty = Klipper's version string.
    firmware_version: str = ""

    # [connect]
    connect_hostname: str = "connect.prusa3d.com"
    connect_tls: bool = True
    connect_port: int = 0
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

    # [camera] - only active when snapshot_url is set
    camera_url: str = ""
    camera_name: str = "Webcam"
    # 10, 30, 60 (seconds), layer, 5layers or manual
    camera_trigger: str = "30"
    camera_file: str = "~/printer_data/klipper_connect/camera.json"

    # [bridge]
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

    cfg.connect_hostname = get("connect", "hostname", cfg.connect_hostname)
    if ini.has_option("connect", "tls"):
        cfg.connect_tls = ini.getboolean("connect", "tls")
    if ini.has_option("connect", "port"):
        cfg.connect_port = ini.getint("connect", "port")
    cfg.credentials_file = _path(
        get("connect", "credentials_file", cfg.credentials_file))
    cfg.state_file = _path(get("connect", "state_file", cfg.state_file))

    cfg.moonraker_url = get("moonraker", "url", cfg.moonraker_url).rstrip("/")
    cfg.moonraker_api_key = get("moonraker", "api_key", "")

    cfg.storage_name = get("storage", "name", cfg.storage_name)
    if "/" in cfg.storage_name or not cfg.storage_name:
        raise ValueError("[storage] name must be non-empty and contain no '/'")
    cfg.gcodes_path = _path(get("storage", "gcodes_path", cfg.gcodes_path))

    cfg.load_filament = get("macros", "load_filament", cfg.load_filament)
    cfg.unload_filament = get("macros", "unload_filament", cfg.unload_filament)
    cfg.filament_temp_param = get("macros", "filament_temp_param",
                                  cfg.filament_temp_param)

    cfg.camera_url = get("camera", "snapshot_url", "")
    cfg.camera_name = get("camera", "name", cfg.camera_name)
    cfg.camera_trigger = get("camera", "trigger", cfg.camera_trigger).lower()
    if cfg.camera_trigger not in CAMERA_TRIGGERS:
        raise ValueError(f"[camera] trigger must be one of "
                         f"{', '.join(CAMERA_TRIGGERS)}")
    cfg.camera_file = _path(get("camera", "token_file", cfg.camera_file))

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
