"""Send webcam snapshots (crowsnest / ustreamer / any JPEG URL) to Connect.

The SDK does the Connect side: registering the camera, triggering it on a
timer or on layer changes, and uploading snapshots. This module adds a
driver that fetches a JPEG over HTTP, and stores the camera token.
"""
import json
import logging
import os
import time
from typing import Callable, Dict, Optional

import requests
from prusa.connect.printer import Printer
from prusa.connect.printer.camera import Camera, Snapshot
from prusa.connect.printer.camera_driver import CameraDriver
from prusa.connect.printer.const import CapabilityType, TriggerScheme
from prusa.connect.printer.util import get_timestamp

log = logging.getLogger("klipper-connect.camera")

JPEG_MAGIC = b"\xff\xd8"
REGISTER_RETRY = 60.0  # seconds between registration attempts


class SnapshotUrlDriver(CameraDriver):
    """Takes photos by downloading a JPEG from a URL."""

    name = "SnapshotURL"
    REQUIRES_SETTINGS = {"url": "URL that returns a single JPEG image"}

    def __init__(self, camera_id, config, disconnected_cb):
        super().__init__(camera_id, config, disconnected_cb)
        self.timeout = float(config.get("timeout", 5))
        # Called when a photo fails, so the Camera stops waiting for it
        self.failed_cb: Callable[[], None] = lambda: None
        self._failing = False

    def _connect(self):
        self._capabilities = {CapabilityType.TRIGGER_SCHEME,
                              CapabilityType.IMAGING}

    def take_a_photo(self) -> bytes:
        res = requests.get(self.config["url"], timeout=self.timeout)
        res.raise_for_status()
        if not res.content.startswith(JPEG_MAGIC):
            raise ValueError("Snapshot URL didn't return a JPEG")
        return res.content

    def _photo_taker(self, snapshot: Snapshot) -> None:
        # The SDK's version disconnects the camera on the first error. The
        # webcam may just be restarting, so log it and try again next time.
        try:
            data = self.take_a_photo()
        except Exception as err:  # pylint: disable=broad-except
            if not self._failing:
                log.warning("Could not get a snapshot from %s: %s",
                            self.config["url"], err)
            self._failing = True
            self.failed_cb()
            return
        if self._failing:
            log.info("Snapshots are working again")
        self._failing = False
        snapshot.data = data
        snapshot.timestamp = get_timestamp()
        self._last_snapshot = snapshot
        self.photo_cb(snapshot)


class CameraStore:
    """Keeps the camera token Connect gives us across restarts."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[str, str]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("Could not read %s, the camera will re-register",
                        self.path)
            return {}

    def save(self, data: Dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)


class WebcamBridge:
    """Owns the one Connect camera and keeps it registered."""

    def __init__(self, printer: Printer, url: str, name: str,
                 trigger: TriggerScheme, store_path: str,
                 clock: Callable[[], float] = time.monotonic):
        self.printer = printer
        self.store = CameraStore(store_path)
        self.clock = clock
        self._last_register: Optional[float] = None
        self._last_layer: Optional[int] = None

        # Stable id: same printer + same URL -> same camera in Connect
        camera_id = CameraDriver.make_hash(
            f"klipper-connect/{printer.sn}/{url}")
        saved = self.store.load()
        config = {"name": name, "driver": SnapshotUrlDriver.name, "url": url,
                  "trigger_scheme": trigger.name}
        if saved.get("camera_id") == camera_id and saved.get("token"):
            config["token"] = saved["token"]

        self.driver = SnapshotUrlDriver(camera_id, config,
                                        self._disconnected)
        self.driver.connect()
        self.driver.store_cb = self._store
        self.camera = Camera(self.driver)
        # pylint: disable=protected-access
        self.driver.failed_cb = self.camera._become_ready

        controller = printer.camera_controller
        controller.add_camera(self.camera)
        controller.set_camera_order([camera_id])

    @property
    def controller(self):
        return self.printer.camera_controller

    def _store(self, camera_id: str) -> None:
        token = self.camera.token
        self.store.save({"camera_id": camera_id, "token": token or ""})
        if token:
            log.info("Camera registered with Connect")

    def _disconnected(self, driver) -> None:
        # Our driver never disconnects on its own; nothing to clean up.
        log.debug("Camera %s disconnected", driver.camera_id)

    def tick(self) -> None:
        """Register the camera with Connect if it has no token yet.

        Connect drops the token if the camera is removed in the web UI, so
        this keeps checking (the SDK resets the token on 401/403).
        """
        if self.camera.is_registered or not self.printer.token:
            return
        now = self.clock()
        if (self._last_register is not None
                and now - self._last_register < REGISTER_RETRY):
            return
        self._last_register = now
        log.info("Registering camera with Connect")
        self.controller.register_camera(self.camera.camera_id)

    def snap(self) -> None:
        """Take a photo now (e.g. at print start/end)."""
        if not self.camera.is_registered or self.camera.is_busy:
            return
        try:
            self.camera.trigger_a_photo()
        except Exception:  # pylint: disable=broad-except
            log.debug("Snapshot skipped", exc_info=True)

    def layer(self, current_layer: Optional[int]) -> None:
        """Feed Klipper's current layer; triggers layer-based schemes."""
        if current_layer is None:
            self._last_layer = None
            return
        if self._last_layer is not None and current_layer != self._last_layer:
            self.controller.layer_trigger()
        self._last_layer = current_layer

    def stop(self) -> None:
        self.controller.stop()


TRIGGERS = {
    "10": TriggerScheme.TEN_SEC,
    "30": TriggerScheme.THIRTY_SEC,
    "60": TriggerScheme.SIXTY_SEC,
    "layer": TriggerScheme.EACH_LAYER,
    "5layers": TriggerScheme.FIFTH_LAYER,
    "manual": TriggerScheme.MANUAL,
}
