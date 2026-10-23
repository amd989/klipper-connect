"""The bridge: Prusa Connect SDK on one side, Moonraker on the other.

Threads:
  * SDK loop        - sends queued telemetry/events, receives commands
  * download loop   - downloads files Connect sends into the gcodes folder
  * inotify loop    - keeps the SDK's file listing in sync with the folder
  * telemetry loop  - polls Moonraker, updates state, queues telemetry
  * camera loop     - uploads webcam snapshots (only with [camera])
  * main thread     - runs Connect commands as they arrive
"""
import json
import logging
import os
import posixpath
import re
import socket
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional

from prusa.connect.printer import Printer
from prusa.connect.printer.command import Command, CommandFailed
from prusa.connect.printer.const import Command as CommandType
from prusa.connect.printer.const import (GCODE_EXTENSIONS, Event, Source,
                                         State)
from prusa.connect.printer.util import make_fingerprint

from . import __version__
from .camera import TRIGGERS, WebcamBridge
from .config import Config, is_buddy
from .moonraker import Moonraker, MoonrakerError
from .state import (ACTIVE_PRINT_STATES, UNREACHABLE, StateTracker,
                    build_telemetry, resample_mesh)

# How often to refresh things that change slowly (spool, lifetime totals)
EXTRAS_INTERVAL = 30.0

log = logging.getLogger("klipper-connect")

# States in which Connect may start a new print
STARTABLE = {State.IDLE, State.READY, State.FINISHED, State.STOPPED}
# States in which a job (and its job_id) is still current
JOB_STATES = {State.PRINTING, State.PAUSED, State.FINISHED, State.STOPPED}


KLIPPER_VERSION = re.compile(r"^v?(\d+\.\d+\.\d+)(?:-(\d+))?")


def firmware_version(klipper_version: Optional[str]) -> Optional[str]:
    """'v0.12.0-123-gabc' -> '0.12.0-123' (Prusa firmware style, which is
    the format Connect understands)."""
    if not klipper_version:
        return None
    match = KLIPPER_VERSION.match(klipper_version)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2) or 0}"


class KlipperPrinter(Printer):
    """SDK printer that adds the fields PrusaLink puts in its INFO report.

    Connect reads the PrusaLink version from `prusalink`; without it the
    printer page says the version is deprecated.
    """

    nozzle_diameter: Optional[float] = None
    material: Optional[str] = None
    # Report like Buddy firmware (MINI) instead of PrusaLink
    buddy: bool = False
    # Events Buddy firmware never sends; Connect answers them with 503
    BUDDY_SKIPPED_EVENTS = {Event.MEDIUM_INSERTED, Event.MEDIUM_EJECTED,
                            Event.MESH_BED_DATA}

    def event_cb(self, event, source, timestamp=None, command_id=None,
                 **kwargs):
        if self.buddy and event in self.BUDDY_SKIPPED_EVENTS:
            log.debug("Not sending %s (MINI)", event.value)
            return
        super().event_cb(event, source, timestamp=timestamp,
                         command_id=command_id, **kwargs)

    def get_info(self) -> Dict[str, Any]:
        info = super().get_info()
        info["nozzle_diameter"] = self.nozzle_diameter
        if self.buddy:
            # Buddy firmware lists its storage this way; without it Connect
            # says "No storage available"
            info["storages"] = [self._storage_info(name, storage)
                                for name, storage in
                                self.fs.storage_dict.items()]
            # Buddy reports its (single) tool like this
            tool: Dict[str, Any] = {"high_flow": False, "hardened": False,
                                    "material": self.material or "---"}
            if self.nozzle_diameter is not None:
                tool["nozzle_diameter"] = self.nozzle_diameter
            info["tools"] = {"1": tool}
        else:
            info["prusalink"] = self.software
            info["prusa_link"] = self.software
        return info

    @staticmethod
    def _storage_info(name: str, storage) -> Dict[str, Any]:
        data = {
            "mountpoint": f"/{name}",
            "type": "USB",
            "read_only": False,
            "is_sfn": False,  # long file names are fine
        }
        free = storage.get_space_info().get("free_space")
        if free is not None:
            data["free_space"] = free
        return data

    def get_file_info(self, caller: Command) -> Dict[str, Any]:
        """Buddy-style FILE_INFO: Connect browses a MINI's storage by asking
        for folders (starting with /usb), and expects their children back.
        The SDK only answers for files."""
        if not self.buddy:
            return super().get_file_info(caller)
        path = (caller.kwargs or {}).get("path")
        if not path:
            raise ValueError("SEND_FILE_INFO requires kwargs")
        node = self.fs.get(path)
        if node is None:
            raise ValueError(f"File does not exist: {path}")
        os_path = self.fs.get_os_path(path)
        common = {
            "source": Source.CONNECT,
            "event": Event.FILE_INFO,
            "path": path.rstrip("/") or "/",
            "display_name": os.path.basename(path.rstrip("/")),
            "read_only": False,
        }
        if not node.is_dir:
            info = super().get_file_info(caller)
            info.update({k: v for k, v in common.items()
                         if k not in info})
            info["type"] = _file_type(os_path, False)
            return info
        children = []
        try:
            entries = sorted(os.scandir(os_path), key=lambda e: e.name)
        except OSError as err:
            raise ValueError(f"Can't list {path}: {err}") from err
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            is_dir = entry.is_dir()
            children.append({
                "name": entry.name,
                "display_name": entry.name,
                "size": 0 if is_dir else stat.st_size,
                "m_timestamp": int(stat.st_mtime),
                "read_only": False,
                "type": _file_type(entry.name, is_dir),
            })
        common.update(type="FOLDER", children=children,
                      file_count=len(children))
        try:
            common["m_timestamp"] = int(os.stat(os_path).st_mtime)
        except OSError:
            pass
        return common


def _file_type(name: str, is_dir: bool) -> str:
    if is_dir:
        return "FOLDER"
    if name.lower().endswith(GCODE_EXTENSIONS):
        return "PRINT_FILE"
    return "FILE"


def _log_failed_requests(res, *args, **kwargs):
    """Log which Connect request failed; the SDK only logs the body."""
    if res.status_code >= 400:
        log.warning("Connect %s %s -> %s", res.request.method,
                    res.request.path_url.split("?")[0], res.status_code)


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def make_printer(cfg: Config) -> KlipperPrinter:
    printer = KlipperPrinter(cfg.printer_type, cfg.serial_number,
                             make_fingerprint(cfg.serial_number))
    printer.buddy = is_buddy(cfg.printer_type)
    # Buddy firmware has no separate PrusaLink version; Connect then reads
    # the firmware version from the same header
    printer.software = None if printer.buddy else cfg.link_version
    printer.conn.hooks["response"].append(_log_failed_requests)
    return printer


def network_info() -> Dict[str, Optional[str]]:
    ip = None
    try:
        # No packet is sent; this only picks the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except OSError:
        pass
    return {"hostname": socket.gethostname(), "lan_ipv4": ip}


@dataclass
class Job:
    id: int
    filename: str  # relative to the gcodes folder, as Klipper sees it
    start_cmd_id: Optional[int] = None


class JobStore:
    """Persists job ids so they keep counting up across restarts."""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {"last_job_id": 0, "active": None}
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("Could not read %s, starting job ids from 0", path)

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def begin(self, filename: str, resuming: bool) -> int:
        """Return the job id for a print of `filename`.

        When the bridge (re)starts in the middle of a print of the same file,
        the previous id is reused so Connect sees one continuous job.
        """
        active = self.data.get("active")
        if resuming and active and active.get("filename") == filename:
            return int(active["id"])
        job_id = int(self.data.get("last_job_id", 0)) + 1
        self.data["last_job_id"] = job_id
        self.data["active"] = {"id": job_id, "filename": filename}
        self._save()
        return job_id

    def end(self):
        if self.data.get("active") is not None:
            self.data["active"] = None
            self._save()


class Bridge:

    def __init__(self, cfg: Config, moonraker: Optional[Moonraker] = None,
                 printer: Optional[Printer] = None):
        self.cfg = cfg
        self.mr = moonraker or Moonraker(cfg.moonraker_url,
                                         cfg.moonraker_api_key)
        self.printer = printer or make_printer(cfg)
        self.tracker = StateTracker()
        self.jobs = JobStore(cfg.state_file)
        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.status: Dict[str, Any] = {}
        self.job: Optional[Job] = None
        self.first_poll = True
        self.pending_start_cmd_id: Optional[int] = None
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self._moonraker_down = False
        self.webcam: Optional[WebcamBridge] = None
        self.info_sent = False
        # Slow-changing values for telemetry: material, lifetime totals
        self.extras: Dict[str, Any] = {}
        self._extras_at: Optional[float] = None
        self._mesh_key: Optional[str] = None
        self._file_info_sent: set = set()

        self._register_handlers()

    # --- paths --------------------------------------------------------------

    def to_klipper(self, path: Optional[str]) -> str:
        """'/local/dir/a.gcode' -> 'dir/a.gcode' (relative to gcodes)."""
        if not path:
            raise CommandFailed("No file path given")
        prefix = f"/{self.cfg.storage_name}/"
        if not path.startswith(prefix):
            raise CommandFailed(f"File is not on storage "
                                f"'{self.cfg.storage_name}': {path}")
        rel = posixpath.normpath(path[len(prefix):])
        if rel.startswith("..") or rel.startswith("/") or rel == ".":
            raise CommandFailed(f"Invalid path: {path}")
        return rel

    def to_connect(self, filename: str) -> str:
        return f"/{self.cfg.storage_name}/{filename}"

    def os_path(self, filename: str) -> str:
        return os.path.join(self.cfg.gcodes_path, filename)

    # --- setup --------------------------------------------------------------

    @property
    def query_objects(self):
        return [
            "webhooks", "print_stats", "virtual_sdcard", "display_status",
            "extruder", "heater_bed", "toolhead", "gcode_move",
            "idle_timeout", "bed_mesh", self.cfg.part_fan,
            self.cfg.hotend_fan
        ]

    def _register_handlers(self):
        p = self.printer
        handlers = {
            CommandType.START_PRINT: self.cmd_start_print,
            CommandType.STOP_PRINT: self.cmd_stop_print,
            CommandType.PAUSE_PRINT: self.cmd_pause_print,
            CommandType.RESUME_PRINT: self.cmd_resume_print,
            CommandType.GCODE: self.cmd_gcode,
            CommandType.SEND_JOB_INFO: self.cmd_job_info,
            CommandType.LOAD_FILAMENT: self.cmd_load_filament,
            CommandType.UNLOAD_FILAMENT: self.cmd_unload_filament,
            CommandType.RESET_PRINTER: self.cmd_reset_printer,
            CommandType.DELETE_FILE: self.cmd_delete_file,
            CommandType.DELETE_FOLDER: self.cmd_delete_folder,
            CommandType.DELETE_DIRECTORY: self.cmd_delete_folder,
        }
        for command, handler in handlers.items():
            p.set_handler(command, handler)
        p.printed_file_cb = self.printed_file
        # The SDK copies this callback into DownloadMgr at init time, so it
        # has to be set on the download manager directly.
        p.download_mgr.download_finished_cb = self.download_finished

    def setup(self):
        p = self.printer
        p.connection_from_config(self.cfg.credentials_file)
        p.firmware = self.cfg.firmware_version or self._klipper_firmware()
        p.network_info.update(network_info())
        os.makedirs(self.cfg.gcodes_path, exist_ok=True)
        p.attach(self.cfg.gcodes_path, self.cfg.storage_name)
        if self.cfg.camera_url:
            try:
                self.webcam = WebcamBridge(p, self.cfg.camera_url,
                                           self.cfg.camera_name,
                                           TRIGGERS[self.cfg.camera_trigger],
                                           self.cfg.camera_file)
            except Exception:  # pylint: disable=broad-except
                log.exception("Camera setup failed; running without it")

    def _klipper_firmware(self) -> Optional[str]:
        """Klipper's version, Prusa-style. Moonraker reports it even while
        Klipper is halted, so the version header is right from the start."""
        try:
            return firmware_version(
                self.mr.printer_info().get("software_version"))
        except MoonrakerError as err:
            log.warning("Could not read Klipper's version yet: %s", err)
            return None

    def send_info(self) -> bool:
        """Send the full printer info (firmware, versions, nozzle, files).

        PrusaLink sends this on startup; Connect otherwise keeps showing the
        values from registration. Needs Klipper to be ready, so it's called
        from the poll loop until it succeeds.
        """
        p = self.printer
        try:
            version = self.mr.printer_info().get("software_version")
            extruder = self.mr.query(["configfile"]).get(
                "configfile", {}).get("settings", {}).get("extruder", {})
        except MoonrakerError as err:
            log.debug("Printer info not available yet: %s", err)
            return False
        p.firmware = self.cfg.firmware_version or firmware_version(version)
        nozzle = extruder.get("nozzle_diameter")
        p.nozzle_diameter = float(nozzle) if nozzle is not None else None
        p.event_cb(**p.get_info())
        log.info("Sent printer info: firmware %s, PrusaLink %s, nozzle %s",
                 p.firmware, p.software, p.nozzle_diameter)
        return True

    # --- polling / state ----------------------------------------------------

    def poll(self) -> State:
        """Read Klipper's status and update the Connect state."""
        try:
            klippy_state = self.mr.server_info().get("klippy_state",
                                                     "disconnected")
            status = (self.mr.query(self.query_objects)
                      if klippy_state == "ready" else {})
            if self._moonraker_down:
                log.info("Moonraker is reachable again")
            self._moonraker_down = False
        except MoonrakerError as err:
            if not self._moonraker_down:
                log.warning("%s", err)
            self._moonraker_down = True
            klippy_state, status = UNREACHABLE, {}

        if klippy_state == "ready" and not self.info_sent:
            self.info_sent = self.send_info()
        elif klippy_state != "ready":
            # Klipper restarted (maybe updated): send fresh info when back
            self.info_sent = False

        if "bed_mesh" in status:
            self._update_mesh(status["bed_mesh"])

        print_stats = status.get("print_stats", {})
        print_state = print_stats.get("state", "standby")
        filename = print_stats.get("filename") or None
        idle_state = status.get("idle_timeout", {}).get("state", "Idle")

        state = self.tracker.update(klippy_state, print_state, idle_state,
                                    self.printer.ready)

        new_job = False
        with self.lock:
            self.status = status
            if (print_state in ACTIVE_PRINT_STATES and filename
                    and (self.job is None or self.job.filename != filename)):
                self._begin_job(filename)
                new_job = True
            elif state not in JOB_STATES and self.job is not None:
                self._end_job()
            self.first_poll = False

        changed = state != self.printer.state
        if changed:
            log.info("State %s -> %s", self.printer.state.value, state.value)
            self.printer.set_state(state, Source.FIRMWARE)
        if new_job:
            self._send_job_info()
        if self.webcam:
            layer = print_stats.get("info", {}).get("current_layer")
            self.webcam.layer(layer if state == State.PRINTING else None)
            if changed and state in (State.PRINTING, State.FINISHED,
                                     State.STOPPED):
                self.webcam.snap()
        return state

    def _begin_job(self, filename: str):
        job_id = self.jobs.begin(filename, resuming=self.first_poll)
        self.job = Job(job_id, filename, self.pending_start_cmd_id)
        self.pending_start_cmd_id = None
        self.printer.job_id = job_id
        log.info("Job %s started: %s", job_id, filename)
        if filename not in self.metadata:
            try:
                self.metadata[filename] = self.mr.metadata(filename)
            except MoonrakerError as err:
                log.debug("No metadata for %s: %s", filename, err)
                self.metadata[filename] = {}

    def _end_job(self):
        log.info("Job %s ended", self.job.id)
        self.metadata.pop(self.job.filename, None)
        self.job = None
        self.printer.job_id = None
        self.jobs.end()

    def _update_mesh(self, bed_mesh: Dict[str, Any]):
        """Send the bed mesh to Connect when it changes (after G80 etc.)."""
        matrix = bed_mesh.get("probed_matrix") or []
        key = json.dumps([bed_mesh.get("profile_name"), matrix])
        if key == self._mesh_key:
            return
        first = self._mesh_key is None
        self._mesh_key = key
        mbl = resample_mesh(matrix)
        self.printer.mbl = mbl
        if mbl is None:
            return
        log.info("Bed mesh %s (%dx%d) %s", bed_mesh.get("profile_name") or
                 "(unsaved)", len(matrix[0]), len(matrix),
                 "loaded" if first else "changed, sending to Connect")
        if not first:
            self.printer.event_cb(Event.MESH_BED_DATA, Source.FIRMWARE,
                                  mbl=mbl)

    def refresh_extras(self, force: bool = False):
        """Refresh the active spool's material and lifetime totals."""
        now = time.monotonic()
        if (not force and self._extras_at is not None
                and now - self._extras_at < EXTRAS_INTERVAL):
            return
        self._extras_at = now
        extras: Dict[str, Any] = {}
        try:
            totals = self.mr.history_totals()
            extras["total_print_time"] = int(totals.get("total_print_time")
                                             or 0)
            extras["total_filament"] = int(totals.get("total_filament_used")
                                           or 0)
        except MoonrakerError as err:
            log.debug("No history totals: %s", err)
        try:
            spool_id = self.mr.active_spool_id()
            if spool_id is not None:
                filament = self.mr.spool(spool_id).get("filament", {})
                extras["material"] = filament.get("material")
        except MoonrakerError as err:
            log.debug("No Spoolman spool: %s", err)
        if self.cfg.default_material and not extras.get("material"):
            extras["material"] = self.cfg.default_material
        if extras.get("material") != self.extras.get("material"):
            log.info("Loaded material: %s", extras.get("material"))
        with self.lock:
            self.extras = extras
        self.printer.material = extras.get("material")

    def send_file_info(self, connect_path: str) -> bool:
        """Push a file's details (metadata + thumbnail) to Connect."""
        try:
            caller = SimpleNamespace(kwargs={"path": connect_path})
            info = self.printer.get_file_info(caller)
        except Exception as err:  # pylint: disable=broad-except
            log.debug("No file info for %s: %s", connect_path, err)
            return False
        self.printer.event_cb(**info)
        return True

    def recent_files(self) -> list:
        """Newest printable files as (mtime, connect path), newest first."""
        found = []
        root = self.cfg.gcodes_path
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith(".") or \
                        not name.lower().endswith(GCODE_EXTENSIONS):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    mtime = os.stat(full).st_mtime
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                found.append((mtime, self.to_connect(rel)))
        found.sort(reverse=True)
        return found[:self.cfg.file_info_count]

    def push_file_info(self):
        """Send details of recent files Connect hasn't been sent yet."""
        if self.cfg.file_info_count <= 0:
            return
        sent = 0
        for mtime, path in self.recent_files():
            key = f"{path}@{mtime}"
            if key in self._file_info_sent:
                continue
            # The SDK's file tree is updated by inotify; wait until it is
            if self.printer.fs.get(path) is None:
                continue
            if self.send_file_info(path):
                sent += 1
            self._file_info_sent.add(key)
        if sent:
            log.info("Sent details of %d file(s) to Connect", sent)

    def telemetry(self) -> Dict[str, Any]:
        with self.lock:
            status = self.status
            meta = self.metadata.get(self.job.filename, {}) if self.job else {}
            extras = dict(self.extras)
        return build_telemetry(status, self.printer.state, self.cfg.part_fan,
                               self.cfg.hotend_fan, meta, extras)

    def job_info(self) -> Dict[str, Any]:
        with self.lock:
            job = self.job
        if job is None:
            raise CommandFailed("No job in progress")
        data: Dict[str, Any] = {
            "job_id": job.id,
            "state": self.printer.state,
            "path": self.to_connect(job.filename),
        }
        if job.start_cmd_id is not None:
            data["start_cmd_id"] = job.start_cmd_id
        try:
            stat = os.stat(self.os_path(job.filename))
            data["size"] = stat.st_size
            data["m_timestamp"] = int(stat.st_mtime)
        except OSError:
            pass
        telemetry = self.telemetry()
        for key in ("progress", "time_printing", "time_remaining"):
            if telemetry.get(key) is not None:
                data[key] = telemetry[key]
        return data

    def _send_job_info(self):
        try:
            self.printer.event_cb(Event.JOB_INFO, Source.FIRMWARE,
                                  **self.job_info())
        except CommandFailed:
            pass

    # --- SDK callbacks ------------------------------------------------------

    def printed_file(self) -> Optional[str]:
        """OS path of the file being printed (the SDK won't overwrite it)."""
        with self.lock:
            job = self.job
        if job and self.printer.state in (State.PRINTING, State.PAUSED):
            return os.path.abspath(self.os_path(job.filename))
        return None

    def download_finished(self, transfer):
        """Start the print if Connect asked to print after downloading."""
        if not transfer.to_print or transfer.stop_ts:
            return
        try:
            filename = self.to_klipper(transfer.path)
            self._start(filename, transfer.start_cmd_id)
        except Exception as err:  # pylint: disable=broad-except
            log.error("Could not start downloaded file %s: %s",
                      transfer.path, err)

    # --- command handlers ---------------------------------------------------

    def _require(self, allowed, action: str):
        state = self.printer.state
        if state not in allowed:
            raise CommandFailed(f"Can't {action} while {state.value}")

    def _start(self, filename: str, start_cmd_id: Optional[int]):
        self._require(STARTABLE, "start a print")
        if not os.path.isfile(self.os_path(filename)):
            raise CommandFailed(f"File not found: {filename}")
        with self.lock:
            self.pending_start_cmd_id = start_cmd_id
        self.mr.start_print(filename)
        log.info("Started print of %s", filename)

    def cmd_start_print(self, caller: Command) -> Dict[str, Any]:
        kwargs = caller.kwargs or {}
        path = kwargs.get("path") or (caller.args[0] if caller.args else None)
        self._start(self.to_klipper(path), caller.command_id)
        return {"source": Source.CONNECT}

    def cmd_stop_print(self, caller: Command) -> Dict[str, Any]:
        self._require({State.PRINTING, State.PAUSED}, "stop")
        self.mr.cancel()
        return {"source": Source.CONNECT}

    def cmd_pause_print(self, caller: Command) -> Dict[str, Any]:
        self._require({State.PRINTING}, "pause")
        self.mr.pause()
        return {"source": Source.CONNECT}

    def cmd_resume_print(self, caller: Command) -> Dict[str, Any]:
        self._require({State.PAUSED}, "resume")
        self.mr.resume()
        return {"source": Source.CONNECT}

    def cmd_gcode(self, caller: Command) -> Dict[str, Any]:
        kwargs = caller.kwargs or {}
        script = kwargs.get("gcode") or (caller.args[0] if caller.args else "")
        script = script.strip()
        if not script:
            raise CommandFailed("Empty G-code")
        self._require({
            State.IDLE, State.READY, State.BUSY, State.FINISHED,
            State.STOPPED, State.PAUSED, State.PRINTING
        }, "run G-code")
        if self.printer.state not in (State.PRINTING, State.PAUSED):
            script = self.translate_gcode(script)
        self.mr.gcode(script)
        return {"source": Source.CONNECT}

    def translate_gcode(self, script: str) -> str:
        """Adapt Prusa-firmware G-code from Connect to this Klipper setup."""
        lines = []
        for line in script.splitlines():
            line = line.strip()
            lines.append(line)
            if line.upper() == "G28" and self.cfg.mbl_gcode:
                lines.append(self.cfg.mbl_gcode)
        result = "\n".join(lines)
        if result != script:
            log.info("G-code from Connect run as: %r", result)
        return result

    def cmd_job_info(self, caller: Command) -> Dict[str, Any]:
        data = self.job_info()
        data.update(event=Event.JOB_INFO, source=Source.CONNECT)
        return data

    def _filament(self, caller: Command, macro: str) -> Dict[str, Any]:
        self._require({
            State.IDLE, State.READY, State.FINISHED, State.STOPPED,
            State.PAUSED
        }, "change filament")
        kwargs = caller.kwargs or {}
        temp = kwargs.get("nozzle_temperature")
        script = macro
        if temp and self.cfg.filament_temp_param:
            script += f" {self.cfg.filament_temp_param}={int(temp)}"
        self.mr.gcode(script)
        return {"source": Source.CONNECT}

    def cmd_load_filament(self, caller: Command) -> Dict[str, Any]:
        return self._filament(caller, self.cfg.load_filament)

    def cmd_unload_filament(self, caller: Command) -> Dict[str, Any]:
        return self._filament(caller, self.cfg.unload_filament)

    def cmd_reset_printer(self, caller: Command) -> Dict[str, Any]:
        self._require({
            State.IDLE, State.READY, State.FINISHED, State.STOPPED,
            State.ERROR, State.BUSY
        }, "reset the printer")
        self.tracker.expect_restart()
        self.mr.firmware_restart()
        return {"source": Source.CONNECT}

    def _printing_path(self) -> Optional[str]:
        with self.lock:
            job = self.job
        if job and self.printer.state in (State.PRINTING, State.PAUSED):
            return self.to_connect(job.filename)
        return None

    def cmd_delete_file(self, caller: Command) -> Dict[str, Any]:
        path = (caller.kwargs or {}).get("path")
        if path and path == self._printing_path():
            raise CommandFailed("This file is being printed")
        return Printer.delete_file(self.printer, caller)

    def cmd_delete_folder(self, caller: Command) -> Dict[str, Any]:
        path = (caller.kwargs or {}).get("path", "").rstrip("/")
        printing = self._printing_path()
        if path and printing and printing.startswith(path + "/"):
            raise CommandFailed("A file in this folder is being printed")
        return Printer.delete_folder(self.printer, caller)

    # --- threads ------------------------------------------------------------

    def _telemetry_loop(self):
        interval = self.cfg.telemetry_interval
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll()
                self.refresh_extras()
                self.printer.telemetry(**self.telemetry())
                if self.webcam:
                    self.webcam.tick()
            except Exception:  # pylint: disable=broad-except
                log.exception("Telemetry loop error")
            self.stop_event.wait(max(0.0,
                                     interval - (time.monotonic() - started)))

    def _file_info_loop(self):
        while not self.stop_event.is_set():
            if self.printer.token:
                try:
                    self.push_file_info()
                except Exception:  # pylint: disable=broad-except
                    log.exception("File info error")
            self.stop_event.wait(10)

    def _inotify_loop(self):
        while not self.stop_event.is_set():
            try:
                self.printer.inotify_handler()
            except Exception:  # pylint: disable=broad-except
                log.exception("File watcher error")
            self.stop_event.wait(0.2)

    def start_threads(self):
        targets = {
            "sdk-loop": self.printer.loop,
            "download-loop": self.printer.download_mgr.loop,
            "inotify-loop": self._inotify_loop,
            "telemetry-loop": self._telemetry_loop,
            "file-info-loop": self._file_info_loop,
        }
        if self.webcam:
            targets["camera-loop"] = self.printer.camera_controller.\
                snapshot_loop
        for name, target in targets.items():
            threading.Thread(target=target, name=name, daemon=True).start()

    def run_commands(self):
        """Run Connect commands until stop() is called (main thread)."""
        cmd = self.printer.command
        while not self.stop_event.is_set():
            if cmd.new_cmd_evt.wait(0.2):
                if cmd.state is None:
                    continue
                name, args = cmd.command_name, cmd.kwargs or cmd.args
                log.info("Connect command %s %s: %s", cmd.command_id, name,
                         _short(args))
                cmd()
                log.info("Connect command %s %s -> %s", cmd.command_id,
                         name, cmd.last_state.value)

    def run(self):
        self.setup()
        self.start_threads()
        log.info("klipper-connect %s", __version__)
        log.info("Bridge running: %s (%s) -> %s", self.cfg.serial_number,
                 self.cfg.printer_type.name, self.cfg.connect_server)
        try:
            self.run_commands()
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        self.printer.stop_loop()
        self.printer.download_mgr.stop_loop()
        if self.webcam:
            self.webcam.stop()
