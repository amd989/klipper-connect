"""Test setup.

The SDK imports inotify_simple and creates an INotify() when a Printer is
built. That only works on Linux, so on other systems a do-nothing stand-in
is installed before the SDK is imported.
"""
import enum
import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

if not sys.platform.startswith("linux"):
    stub = types.ModuleType("inotify_simple")

    class flags(enum.IntFlag):  # noqa: N801 - mirrors inotify_simple
        CLOSE_WRITE = 0x8
        MOVED_FROM = 0x40
        MOVED_TO = 0x80
        CREATE = 0x100
        DELETE = 0x200
        DELETE_SELF = 0x400
        MOVE_SELF = 0x800
        IGNORED = 0x8000
        ISDIR = 0x40000000

        @classmethod
        def from_mask(cls, mask):
            return [f for f in cls if f & mask]

    class INotify:
        _wd = 0

        def add_watch(self, path, mask):
            INotify._wd += 1
            return INotify._wd

        def rm_watch(self, wd):
            pass

        def read(self, timeout=None, read_delay=None):
            return []

    stub.flags = flags
    stub.INotify = INotify
    sys.modules["inotify_simple"] = stub


# --- fake Moonraker -------------------------------------------------------


class FakeMoonraker:
    """Serves the few Moonraker endpoints the bridge uses."""

    def __init__(self):
        self.klippy_state = "ready"
        self.status = {
            "webhooks": {"state": "ready"},
            "print_stats": {"state": "standby", "filename": "",
                            "print_duration": 0.0, "total_duration": 0.0},
            "virtual_sdcard": {"progress": 0.0},
            "display_status": {"progress": 0.0},
            "extruder": {"temperature": 25.04, "target": 0.0},
            "heater_bed": {"temperature": 24.5, "target": 0.0},
            "toolhead": {"position": [10.0, 20.0, 5.0, 0.0]},
            "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
            "idle_timeout": {"state": "Idle"},
            "fan": {"speed": 0.5, "rpm": 3000.0},
            "heater_fan nozzle_cooling_fan": {"speed": 1.0, "rpm": 6900.0},
        }
        self.metadata = {}
        self.calls = []  # (method, path, body)
        self.fail_paths = set()

        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                path, _, query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.calls.append((method, path, body or query))
                if path in fake.fail_paths:
                    return self._send(400, {"error": {"message": "nope"}})
                if path == "/server/info":
                    return self._send(200, {"result": {
                        "klippy_state": fake.klippy_state}})
                if path == "/printer/info":
                    return self._send(200, {"result": {
                        "software_version": "v0.12.0-test"}})
                if path == "/printer/objects/query":
                    wanted = body["objects"]
                    status = {k: v for k, v in fake.status.items()
                              if k in wanted}
                    return self._send(200, {"result": {"status": status}})
                if path == "/server/files/metadata":
                    name = query.split("=", 1)[1]
                    if name in fake.metadata:
                        return self._send(200,
                                          {"result": fake.metadata[name]})
                    return self._send(404, {"error": {"message": "missing"}})
                if path.startswith("/printer/"):
                    return self._send(200, {"result": "ok"})
                return self._send(404, {"error": {"message": "unknown"}})

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def set_print(self, state, filename="", progress=0.0, duration=0.0):
        self.status["print_stats"].update(state=state, filename=filename,
                                          print_duration=duration,
                                          total_duration=duration)
        self.status["display_status"]["progress"] = progress
        self.status["virtual_sdcard"]["progress"] = progress

    def posts(self, path):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def moonraker():
    fake = FakeMoonraker()
    yield fake
    fake.close()
