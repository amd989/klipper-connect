import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Empty

import pytest
from prusa.connect.printer.command import CommandFailed
from prusa.connect.printer.const import Event, PrinterType, State
from prusa.connect.printer.models import Event as EventModel
from prusa.connect.printer.models import Telemetry

from klipper_connect import config as config_mod
from klipper_connect.bridge import Bridge
from klipper_connect.moonraker import Moonraker

SN = "CZPX1234X123XC12345"


def make_config(tmp_path, connect_port=0, connect_host="connect.invalid"):
    gcodes = tmp_path / "gcodes"
    gcodes.mkdir()
    cfg = config_mod.Config(serial_number=SN,
                            printer_type=PrinterType.I3MK3S)
    cfg.gcodes_path = str(gcodes)
    cfg.credentials_file = str(tmp_path / "creds" / "prusa_connect.ini")
    cfg.state_file = str(tmp_path / "state" / "state.json")
    cfg.connect_hostname = connect_host
    cfg.connect_tls = False
    cfg.connect_port = connect_port
    cfg.telemetry_interval = 0.1
    config_mod.save_credentials(cfg, "test-token")
    return cfg


@pytest.fixture
def bridge(tmp_path, moonraker):
    cfg = make_config(tmp_path)
    (tmp_path / "gcodes" / "cube.gcode").write_text("G28\n")
    b = Bridge(cfg, Moonraker(moonraker.url))
    b.setup()
    yield b
    b.stop()


def drain(printer):
    items = []
    while True:
        try:
            items.append(printer.queue.get_nowait())
        except Empty:
            return items


def events(items, kind=None):
    out = [i for i in items if isinstance(i, EventModel)]
    return [e for e in out if kind is None or e.event == kind]


def run_command(printer, name, kwargs=None, command_id=1):
    printer.command.accept(command_id, name, kwargs=kwargs or {})
    printer.command()
    return events(drain(printer))


def test_setup_reports_files(bridge):
    assert bridge.printer.token == "test-token"
    inserted = events(drain(bridge.printer), Event.MEDIUM_INSERTED)
    assert inserted and inserted[0].data["root"] == "/local"


def test_info_sent_once_klipper_is_ready(bridge, moonraker):
    drain(bridge.printer)
    moonraker.klippy_state = "startup"
    bridge.poll()
    assert not events(drain(bridge.printer), Event.INFO)

    moonraker.klippy_state = "ready"
    bridge.poll()
    info = events(drain(bridge.printer), Event.INFO)
    assert len(info) == 1
    data = info[0].data
    assert data["firmware"] == "0.12.0-0"
    assert data["prusalink"] == "0.8.1"
    assert data["sdk"] == "0.8.1"
    assert data["nozzle_diameter"] == 0.4
    assert data["sn"] == SN
    assert data["type"] == 1 and data["version"] == 3

    bridge.poll()
    assert not events(drain(bridge.printer), Event.INFO)

    # Klipper restart (e.g. after an update) -> info is sent again
    moonraker.klippy_state = "shutdown"
    bridge.poll()
    moonraker.klippy_state = "ready"
    bridge.poll()
    assert len(events(drain(bridge.printer), Event.INFO)) == 1


def test_firmware_override(tmp_path, moonraker):
    cfg = make_config(tmp_path)
    cfg.firmware_version = "3.14.1-8237"
    cfg.link_version = "0.9.0"
    b = Bridge(cfg, Moonraker(moonraker.url))
    b.setup()
    b.poll()
    data = events(drain(b.printer), Event.INFO)[0].data
    assert data["firmware"] == "3.14.1-8237"
    assert data["prusalink"] == "0.9.0"
    headers = b.printer.make_headers()
    assert headers["User-Agent-Version"] == "0.9.0"


def test_idle_poll_and_telemetry(bridge, moonraker):
    assert bridge.poll() == State.IDLE
    data = bridge.telemetry()
    assert data["temp_nozzle"] == 25.0
    assert data["fan_print"] == 3000
    assert data["fan_hotend"] == 6900


def test_start_print_and_job_lifecycle(bridge, moonraker):
    bridge.poll()
    drain(bridge.printer)

    evts = run_command(bridge.printer, "START_PRINT",
                       {"path": "/local/cube.gcode"}, command_id=42)
    assert evts[-1].event == Event.FINISHED
    assert moonraker.posts("/printer/print/start")[0][2] == {
        "filename": "cube.gcode"}

    moonraker.metadata["cube.gcode"] = {"estimated_time": 1000,
                                       "filament_type": "PLA"}
    moonraker.set_print("printing", "cube.gcode", progress=0.25,
                        duration=100)
    assert bridge.poll() == State.PRINTING
    assert bridge.printer.job_id == 1
    items = drain(bridge.printer)
    job_info = events(items, Event.JOB_INFO)[0]
    assert job_info.job_id == 1
    assert job_info.data["path"] == "/local/cube.gcode"
    assert job_info.data["start_cmd_id"] == 42
    assert job_info.data["size"] == 4
    changed = events(items, Event.STATE_CHANGED)[0]
    assert changed.state == State.PRINTING and changed.job_id == 1

    data = bridge.telemetry()
    assert data["progress"] == 25
    assert data["time_remaining"] == 900
    assert data["material"] == "PLA"

    # SEND_JOB_INFO answered from the current job
    evts = run_command(bridge.printer, "SEND_JOB_INFO", command_id=43)
    assert evts[-1].event == Event.JOB_INFO and evts[-1].job_id == 1

    # Pause / resume / stop are forwarded
    run_command(bridge.printer, "PAUSE_PRINT", command_id=44)
    assert moonraker.posts("/printer/print/pause")
    moonraker.set_print("paused", "cube.gcode", 0.3, 120)
    assert bridge.poll() == State.PAUSED
    run_command(bridge.printer, "RESUME_PRINT", command_id=45)
    assert moonraker.posts("/printer/print/resume")

    moonraker.set_print("complete", "cube.gcode", 1.0, 1000)
    assert bridge.poll() == State.FINISHED
    assert bridge.printer.job_id == 1  # kept while FINISHED is shown

    bridge.tracker.ended_at -= 60
    assert bridge.poll() == State.IDLE
    assert bridge.printer.job_id is None

    # The next print gets a new job id
    moonraker.set_print("printing", "cube.gcode")
    bridge.poll()
    assert bridge.printer.job_id == 2


def test_wrong_state_commands_fail(bridge, moonraker):
    bridge.poll()
    drain(bridge.printer)
    for name in ("PAUSE_PRINT", "RESUME_PRINT", "STOP_PRINT"):
        evts = run_command(bridge.printer, name)
        assert evts[-1].event == Event.FAILED, name
    assert not moonraker.posts("/printer/print/pause")

    moonraker.set_print("printing", "cube.gcode")
    bridge.poll()
    evts = run_command(bridge.printer, "START_PRINT",
                       {"path": "/local/cube.gcode"}, command_id=9)
    assert evts[-1].event == Event.FAILED


def test_start_print_rejects_bad_paths(bridge):
    for path in ("/usb/cube.gcode", "/local/../etc/passwd",
                 "/local/missing.gcode"):
        with pytest.raises(CommandFailed):
            bridge._start(bridge.to_klipper(path), None)


def test_gcode_and_filament(bridge, moonraker):
    bridge.poll()
    run_command(bridge.printer, "GCODE", {"gcode": "G28 X\nM104 S200\n"})
    assert moonraker.posts("/printer/gcode/script")[-1][2] == {
        "script": "G28 X\nM104 S200"}
    run_command(bridge.printer, "LOAD_FILAMENT", {
        "nozzle_temperature": 230, "bed_temperature": 85})
    assert moonraker.posts("/printer/gcode/script")[-1][2] == {
        "script": "LOAD_FILAMENT TEMP=230"}
    run_command(bridge.printer, "UNLOAD_FILAMENT")
    assert moonraker.posts("/printer/gcode/script")[-1][2] == {
        "script": "UNLOAD_FILAMENT"}


def test_moonraker_error_marks_command_failed(bridge, moonraker):
    bridge.poll()
    moonraker.fail_paths.add("/printer/gcode/script")
    evts = run_command(bridge.printer, "GCODE", {"gcode": "G28"})
    assert evts[-1].event == Event.FAILED


def test_cannot_delete_file_being_printed(bridge, moonraker, tmp_path):
    moonraker.set_print("printing", "cube.gcode")
    bridge.poll()
    evts = run_command(bridge.printer, "DELETE_FILE",
                       {"path": "/local/cube.gcode"})
    assert evts[-1].event == Event.FAILED
    assert (tmp_path / "gcodes" / "cube.gcode").exists()
    assert bridge.printed_file() == str(tmp_path / "gcodes" / "cube.gcode")


def test_delete_idle_file(bridge, tmp_path):
    bridge.poll()
    evts = run_command(bridge.printer, "DELETE_FILE",
                       {"path": "/local/cube.gcode"})
    assert evts[-1].event == Event.FINISHED
    assert not (tmp_path / "gcodes" / "cube.gcode").exists()


def test_klipper_down_is_error(bridge, moonraker):
    moonraker.klippy_state = "shutdown"
    assert bridge.poll() == State.ERROR
    moonraker.close()
    assert bridge.poll() == State.ERROR


def test_job_id_survives_restart_mid_print(tmp_path, moonraker):
    cfg = make_config(tmp_path)
    (tmp_path / "gcodes" / "cube.gcode").write_text("G28\n")
    moonraker.set_print("printing", "cube.gcode")
    first = Bridge(cfg, Moonraker(moonraker.url))
    first.setup()
    first.poll()
    assert first.printer.job_id == 1

    second = Bridge(cfg, Moonraker(moonraker.url))
    second.setup()
    second.poll()
    assert second.printer.job_id == 1


def test_download_finished_starts_print(bridge, moonraker):
    bridge.poll()

    class Transfer:
        to_print = True
        stop_ts = 0
        path = "/local/cube.gcode"
        start_cmd_id = 77

    bridge.download_finished(Transfer())
    assert moonraker.posts("/printer/print/start")[0][2] == {
        "filename": "cube.gcode"}
    assert bridge.pending_start_cmd_id == 77


# --- end to end against a fake Connect server -------------------------------


class FakeConnect:
    def __init__(self, commands):
        self.commands = list(commands)
        self.events = []
        self.telemetry = []
        self.camera_registrations = []
        self.snapshots = []  # (headers, body)
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"null")
                assert self.headers["Token"] == "test-token"
                if self.path == "/p/telemetry":
                    fake.telemetry.append(body)
                    if fake.commands:
                        cmd_id, cmd = fake.commands.pop(0)
                        data = json.dumps(cmd).encode()
                        self.send_response(200)
                        self.send_header("Command-Id", str(cmd_id))
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                elif self.path == "/p/events":
                    fake.events.append(body)
                elif self.path == "/p/camera":
                    fake.camera_registrations.append(body)
                    self.send_response(200)
                    self.send_header("Token", "cam-token")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(204)
                self.end_headers()

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if self.path.startswith("/c/snapshot"):
                    fake.snapshots.append((dict(self.headers), body))
                self.send_response(204)
                self.end_headers()

            def do_GET(self):  # noqa: N802
                if self.path in fake.files:
                    data = fake.files[self.path]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/x.gcode")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(404)
                self.end_headers()

        self.files = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_end_to_end_with_fake_connect(tmp_path, moonraker):
    connect = FakeConnect([
        (100, {"command": "START_PRINT",
               "kwargs": {"path": "/local/cube.gcode"}}),
    ])
    cfg = make_config(tmp_path, connect.port, "127.0.0.1")
    (tmp_path / "gcodes" / "cube.gcode").write_text("G28\n")
    bridge = Bridge(cfg, Moonraker(moonraker.url))
    runner = threading.Thread(target=bridge.run, daemon=True)
    runner.start()
    try:
        def answered(cmd_id, kind):
            return any(e.get("command_id") == cmd_id and e["event"] == kind
                       for e in connect.events)

        assert wait_for(lambda: answered(100, "FINISHED"))
        assert answered(100, "ACCEPTED")
        assert moonraker.posts("/printer/print/start")

        moonraker.set_print("printing", "cube.gcode", 0.1, 10)
        assert wait_for(lambda: any(
            t.get("state") == "PRINTING" and t.get("job_id") == 1
            for t in connect.telemetry))
        assert wait_for(lambda: any(
            e["event"] == "JOB_INFO" and e["data"].get("start_cmd_id") == 100
            for e in connect.events))
        assert all(t["state"] in {s.value for s in State}
                   for t in connect.telemetry)
        assert any(t.get("temp_nozzle") == 25.0 for t in connect.telemetry)
    finally:
        bridge.stop_event.set()
        runner.join(5)
        connect.server.shutdown()


def test_telemetry_items_are_queued(bridge):
    bridge.poll()
    bridge.printer.telemetry(**bridge.telemetry())
    items = [i for i in drain(bridge.printer) if isinstance(i, Telemetry)]
    payload = items[-1].to_payload()
    assert payload["state"] == "IDLE"
    assert payload["temp_bed"] == 24.5


linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="needs inotify")


@linux_only
def test_new_files_show_up_in_connect_listing(bridge, tmp_path):
    drain(bridge.printer)
    (tmp_path / "gcodes" / "new.gcode").write_text("G28\n")
    assert wait_for(lambda: (bridge.printer.inotify_handler(),
                             bridge.printer.fs.get("/local/new.gcode"))[1])
    changed = events(drain(bridge.printer), Event.FILE_CHANGED)
    assert any(e.data.get("new_path") == "/local/new.gcode" for e in changed)


@linux_only
def test_end_to_end_download_then_print(tmp_path, moonraker):
    connect = FakeConnect([
        (200, {"command": "START_CONNECT_DOWNLOAD",
               "kwargs": {"path": "/local/sub/benchy.gcode", "team_id": 1,
                          "hash": "abc", "printing": True}}),
    ])
    connect.files["/p/teams/1/files/abc/raw"] = b"G28\nG1 X10\n" * 100
    cfg = make_config(tmp_path, connect.port, "127.0.0.1")
    bridge = Bridge(cfg, Moonraker(moonraker.url))
    runner = threading.Thread(target=bridge.run, daemon=True)
    runner.start()
    try:
        target = tmp_path / "gcodes" / "sub" / "benchy.gcode"
        assert wait_for(lambda: target.exists())
        assert target.read_bytes() == connect.files["/p/teams/1/files/abc/raw"]
        assert wait_for(lambda: moonraker.posts("/printer/print/start"))
        assert moonraker.posts("/printer/print/start")[0][2] == {
            "filename": "sub/benchy.gcode"}
        assert wait_for(lambda: any(e["event"] == "TRANSFER_FINISHED"
                                    for e in connect.events))
    finally:
        bridge.stop_event.set()
        runner.join(5)
        connect.server.shutdown()


def test_firmware_version_format():
    from klipper_connect.bridge import firmware_version
    assert firmware_version("v0.12.0-123-gabcdef") == "0.12.0-123"
    assert firmware_version("v0.13.0") == "0.13.0-0"
    assert firmware_version("v0.12.0-test") == "0.12.0-0"
    assert firmware_version("weird") is None
    assert firmware_version(None) is None


def test_material_and_totals_from_moonraker(bridge, moonraker):
    bridge.poll()
    bridge.refresh_extras(force=True)
    data = bridge.telemetry()
    assert data["material"] == "PLA"
    assert data["total_print_time"] == 2265
    assert data["total_filament"] == 2385

    # No active spool -> falls back to default_material, else nothing
    moonraker.spool_id = None
    bridge.refresh_extras(force=True)
    assert "material" not in bridge.telemetry()
    bridge.cfg.default_material = "PETG"
    bridge.refresh_extras(force=True)
    assert bridge.telemetry()["material"] == "PETG"


def test_extras_survive_missing_spoolman(bridge, moonraker):
    moonraker.fail_paths.update({"/server/spoolman/spool_id",
                                 "/server/history/totals"})
    bridge.refresh_extras(force=True)
    data = bridge.telemetry()
    assert "material" not in data and "total_print_time" not in data


def test_mesh_sent_when_it_changes(bridge, moonraker):
    moonraker.status["bed_mesh"] = {"profile_name": "",
                                    "probed_matrix": [[]]}
    bridge.poll()
    drain(bridge.printer)
    assert bridge.printer.mbl is None

    moonraker.status["bed_mesh"] = {
        "profile_name": "default",
        "probed_matrix": [[0.1, 0.2], [0.3, 0.4]]}
    bridge.poll()
    mesh = events(drain(bridge.printer), Event.MESH_BED_DATA)
    assert len(mesh) == 1 and len(mesh[0].data["mbl"]) == 49
    assert bridge.printer.mbl == mesh[0].data["mbl"]

    bridge.poll()  # unchanged -> not sent again
    assert not events(drain(bridge.printer), Event.MESH_BED_DATA)
    # The mesh is also part of the INFO report
    assert bridge.printer.get_info()["mbl"] == mesh[0].data["mbl"]


def test_commands_are_logged(bridge, moonraker, caplog):
    import logging
    bridge.poll()
    bridge.printer.command.accept(5, "GCODE", kwargs={"gcode": "G28"})
    caplog.set_level(logging.INFO, logger="klipper-connect")
    worker = threading.Thread(target=bridge.run_commands, daemon=True)
    worker.start()
    assert wait_for(lambda: "-> FINISHED" in caplog.text)
    bridge.stop_event.set()
    worker.join(2)
    assert "Connect command 5 GCODE: {'gcode': 'G28'}" in caplog.text


def test_plain_g28_from_connect_also_levels(bridge, moonraker):
    bridge.poll()
    # What Connect's "Start MBL" sends
    run_command(bridge.printer, "GCODE", {"gcode": "G28\r\n"})
    assert moonraker.posts("/printer/gcode/script")[-1][2] == {
        "script": "G28\nG80"}
    # G28 W (home without MBL) and axis homing are left alone
    for gcode in ("G28 W", "G28 X Y", "g28 z"):
        run_command(bridge.printer, "GCODE", {"gcode": gcode})
        assert moonraker.posts("/printer/gcode/script")[-1][2] == {
            "script": gcode}
    # Never during a print
    moonraker.set_print("printing", "cube.gcode")
    bridge.poll()
    run_command(bridge.printer, "GCODE", {"gcode": "G28"})
    assert moonraker.posts("/printer/gcode/script")[-1][2] == {
        "script": "G28"}
    # Can be turned off
    bridge.cfg.mbl_gcode = ""
    assert bridge.translate_gcode("G28") == "G28"


def test_reset_shows_busy_while_klipper_restarts(bridge, moonraker):
    bridge.poll()
    evts = run_command(bridge.printer, "RESET_PRINTER")
    assert evts[-1].event == Event.FINISHED
    assert moonraker.posts("/printer/firmware_restart")
    for klippy in ("disconnected", "shutdown", "startup"):
        moonraker.klippy_state = klippy
        assert bridge.poll() == State.BUSY, klippy
    moonraker.klippy_state = "ready"
    assert bridge.poll() == State.IDLE
    # Without a reset, a shutdown is an error again
    moonraker.klippy_state = "shutdown"
    assert bridge.poll() == State.ERROR


def test_file_info_pushed_for_recent_files(bridge, moonraker, tmp_path):
    import os
    gcodes = tmp_path / "gcodes"
    thumb = "; thumbnail begin 4x4 8\n; aGVsbG8h\n; thumbnail end\n"
    for i, name in enumerate(["old.gcode", "mid.gcode", "new.gcode"]):
        f = gcodes / name
        f.write_text(thumb + "G28\n")
        os.utime(f, (1000 + i, 1000 + i))
    (gcodes / "notes.txt").write_text("x")
    os.utime(gcodes / "cube.gcode", (1, 1))  # fixture file: oldest
    # Rebuild the SDK's file tree (inotify does this on the Pi)
    bridge.printer.fs.detach("local")
    bridge.printer.attach(str(gcodes), "local")
    drain(bridge.printer)

    bridge.cfg.file_info_count = 2
    bridge.push_file_info()
    infos = events(drain(bridge.printer), Event.FILE_INFO)
    assert [e.data["path"] for e in infos] == ["/local/new.gcode",
                                               "/local/mid.gcode"]
    assert infos[0].data["preview"] == "aGVsbG8h"

    bridge.push_file_info()  # already sent -> nothing new
    assert not events(drain(bridge.printer), Event.FILE_INFO)

    os.utime(gcodes / "old.gcode", (2000, 2000))  # file replaced
    bridge.push_file_info()
    infos = events(drain(bridge.printer), Event.FILE_INFO)
    assert [e.data["path"] for e in infos] == ["/local/old.gcode"]
