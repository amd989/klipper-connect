import threading

from prusa.connect.printer.const import TriggerScheme

from klipper_connect.bridge import Bridge
from klipper_connect.camera import WebcamBridge
from klipper_connect.moonraker import Moonraker
from test_bridge import FakeConnect, make_config, wait_for


def camera_config(tmp_path, moonraker, connect, trigger="manual"):
    cfg = make_config(tmp_path, connect.port, "127.0.0.1")
    cfg.camera_url = f"{moonraker.url}/webcam/snapshot"
    cfg.camera_name = "MK3S cam"
    cfg.camera_trigger = trigger
    cfg.camera_file = str(tmp_path / "state" / "camera.json")
    (tmp_path / "gcodes" / "cube.gcode").write_text("G28\n")
    return cfg


def start(cfg, moonraker):
    bridge = Bridge(cfg, Moonraker(moonraker.url))
    thread = threading.Thread(target=bridge.run, daemon=True)
    thread.start()
    return bridge, thread


def stop(bridge, thread):
    bridge.stop_event.set()
    thread.join(5)


def test_camera_registers_and_sends_snapshot_on_print_start(tmp_path,
                                                             moonraker):
    connect = FakeConnect([])
    cfg = camera_config(tmp_path, moonraker, connect)
    bridge, thread = start(cfg, moonraker)
    try:
        assert wait_for(lambda: connect.camera_registrations)
        reg = connect.camera_registrations[0]
        assert reg["config"]["name"] == "MK3S cam"
        assert "imaging" in reg["capabilities"]
        assert wait_for(lambda: bridge.webcam.camera.is_registered)

        moonraker.set_print("printing", "cube.gcode")
        assert wait_for(lambda: connect.snapshots)
        headers, body = connect.snapshots[0]
        assert body == moonraker.jpeg
        assert headers["Token"] == "cam-token"
        assert headers["Fingerprint"] == bridge.webcam.camera.fingerprint
    finally:
        stop(bridge, thread)
        connect.server.shutdown()

    # Restarting reuses the saved token instead of registering again
    connect2 = FakeConnect([])
    cfg.connect_port = connect2.port
    from klipper_connect import config as config_mod
    config_mod.save_credentials(cfg, "test-token")
    moonraker.set_print("standby")
    bridge, thread = start(cfg, moonraker)
    try:
        assert wait_for(lambda: bridge.webcam is not None
                        and bridge.webcam.camera.is_registered)
        moonraker.set_print("printing", "cube.gcode")
        assert wait_for(lambda: connect2.snapshots)
        assert connect2.camera_registrations == []
    finally:
        stop(bridge, thread)
        connect2.server.shutdown()


def test_layer_trigger(tmp_path, moonraker):
    connect = FakeConnect([])
    cfg = camera_config(tmp_path, moonraker, connect, trigger="layer")
    bridge, thread = start(cfg, moonraker)
    try:
        assert wait_for(lambda: bridge.webcam
                        and bridge.webcam.camera.is_registered)
        assert bridge.webcam.camera.trigger_scheme == TriggerScheme.EACH_LAYER
        moonraker.set_print("printing", "cube.gcode")
        assert wait_for(lambda: len(connect.snapshots) == 1)  # print start
        moonraker.status["print_stats"]["info"] = {"current_layer": 1}
        assert wait_for(lambda: bridge.webcam._last_layer == 1)
        moonraker.status["print_stats"]["info"] = {"current_layer": 2}
        assert wait_for(lambda: len(connect.snapshots) == 2)
    finally:
        stop(bridge, thread)
        connect.server.shutdown()


def test_webcam_failure_does_not_wedge_camera(tmp_path, moonraker):
    connect = FakeConnect([])
    cfg = camera_config(tmp_path, moonraker, connect)
    bridge = Bridge(cfg, Moonraker(moonraker.url))
    bridge.setup()
    webcam = bridge.webcam
    webcam.camera.set_token("cam-token")
    queue = bridge.printer.camera_controller.snapshot_queue

    moonraker.webcam_down = True
    webcam.snap()
    assert wait_for(lambda: not webcam.camera.is_busy)
    assert queue.empty()

    moonraker.webcam_down = False
    webcam.snap()
    assert wait_for(lambda: not queue.empty())
    assert queue.get().data == moonraker.jpeg
    connect.server.shutdown()


def test_camera_id_is_stable(tmp_path, moonraker):
    connect = FakeConnect([])
    cfg = camera_config(tmp_path, moonraker, connect)
    ids = set()
    for _ in range(2):
        bridge = Bridge(cfg, Moonraker(moonraker.url))
        bridge.setup()
        ids.add(bridge.webcam.camera.camera_id)
        assert isinstance(bridge.webcam, WebcamBridge)
    assert len(ids) == 1
    connect.server.shutdown()
