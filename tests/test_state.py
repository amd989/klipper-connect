from prusa.connect.printer.const import State

from klipper_connect.state import (PRINT_END_HOLD, StateTracker,
                                   build_telemetry, time_remaining)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_basic_states():
    t = StateTracker()
    assert t.update("ready", "standby", "Idle", False) == State.IDLE
    assert t.update("ready", "standby", "Idle", True) == State.READY
    assert t.update("ready", "standby", "Printing", False) == State.BUSY
    assert t.update("ready", "printing", "Printing", False) == State.PRINTING
    assert t.update("ready", "paused", "Idle", False) == State.PAUSED
    assert t.update("startup", "standby", "Idle", False) == State.BUSY
    assert t.update("shutdown", "standby", "Idle", False) == State.ERROR
    assert t.update("disconnected", "standby", "Idle", False) == State.ERROR


def test_finished_is_held_then_idle():
    clock = Clock()
    t = StateTracker(clock)
    t.update("ready", "printing", "Printing", False)
    assert t.update("ready", "complete", "Idle", False) == State.FINISHED
    clock.now += PRINT_END_HOLD - 1
    assert t.update("ready", "complete", "Idle", False) == State.FINISHED
    clock.now += 2
    assert t.update("ready", "complete", "Idle", False) == State.IDLE


def test_cancel_from_pause_is_stopped():
    clock = Clock()
    t = StateTracker(clock)
    t.update("ready", "paused", "Idle", False)
    assert t.update("ready", "cancelled", "Idle", False) == State.STOPPED


def test_old_complete_state_at_startup_is_idle():
    # Klipper still says "complete" from a print hours ago
    t = StateTracker(Clock())
    assert t.update("ready", "complete", "Idle", False) == State.IDLE


def test_time_remaining():
    assert time_remaining(600, 0.5, 3000) == 2400
    assert time_remaining(4000, 0.9, 3000) == 0
    assert time_remaining(600, 0.25, None) == 1800
    assert time_remaining(10, 0.0, None) is None


def test_telemetry_idle():
    status = {
        "extruder": {"temperature": 215.04, "target": 215.0},
        "heater_bed": {"temperature": 59.96, "target": 60.0},
        "toolhead": {"position": [1.234, 2.0, 3.0, 0.0]},
        "gcode_move": {"speed_factor": 1.25, "extrude_factor": 0.95},
        "fan": {"speed": 0.5, "rpm": 3000.7},
        "heater_fan nozzle_cooling_fan": {"speed": 1.0, "rpm": None},
        "print_stats": {"print_duration": 10},
    }
    data = build_telemetry(status, State.IDLE, "fan",
                           "heater_fan nozzle_cooling_fan")
    assert data["temp_nozzle"] == 215.0
    assert data["target_bed"] == 60.0
    assert data["axis_x"] == 1.23
    assert data["speed"] == 125
    assert data["flow"] == 95
    assert data["fan_print"] == 3000
    assert data["target_fan_print"] == 128
    assert data["fan_hotend"] is None
    assert data["target_fan_hotend"] == 255
    assert "progress" not in data


def test_telemetry_printing_uses_metadata():
    status = {
        "print_stats": {"print_duration": 600, "total_duration": 650},
        "display_status": {"progress": 0.42},
    }
    meta = {"estimated_time": 1800, "filament_type": "PETG;PLA"}
    data = build_telemetry(status, State.PRINTING, "fan", "x", meta)
    assert data["progress"] == 42
    assert data["time_printing"] == 650
    assert data["time_remaining"] == 1200
    assert data["inaccurate_estimates"] is False
    assert data["material"] == "PETG"
