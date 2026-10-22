"""Translate Klipper status into Prusa Connect states and telemetry.

Everything here is plain functions/classes with no I/O so it can be tested
without a printer.
"""
import time
from typing import Any, Callable, Dict, List, Optional

from prusa.connect.printer.const import State

# How long FINISHED/STOPPED stays visible before going back to IDLE.
# Same value PrusaLink uses; it gives Connect time to grab a final snapshot.
PRINT_END_HOLD = 11.0

ACTIVE_PRINT_STATES = {"printing", "paused"}
END_PRINT_STATES = {
    "complete": State.FINISHED,
    "cancelled": State.STOPPED,
    "error": State.STOPPED,
}


class StateTracker:
    """Work out the Connect state from Klipper's state.

    Klipper's print_stats stays at "complete"/"cancelled" until the next
    print, while Connect expects FINISHED/STOPPED to be short-lived, so the
    end states are held for PRINT_END_HOLD seconds and then become IDLE.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.last_print_state: Optional[str] = None
        self.ended_at: Optional[float] = None

    def update(self, klippy_state: str, print_state: str, idle_state: str,
               ready: bool) -> State:
        previous = self.last_print_state
        self.last_print_state = print_state

        if print_state in END_PRINT_STATES and previous in ACTIVE_PRINT_STATES:
            self.ended_at = self.clock()
        elif print_state in ACTIVE_PRINT_STATES:
            self.ended_at = None

        if klippy_state == "startup":
            return State.BUSY
        if klippy_state != "ready":
            return State.ERROR
        if print_state == "printing":
            return State.PRINTING
        if print_state == "paused":
            return State.PAUSED
        if (print_state in END_PRINT_STATES and self.ended_at is not None
                and self.clock() - self.ended_at < PRINT_END_HOLD):
            return END_PRINT_STATES[print_state]
        if idle_state == "Printing":
            # Klipper is busy with G-code that isn't a print (homing, etc.)
            return State.BUSY
        return State.READY if ready else State.IDLE


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _rpm(fan: Dict[str, Any]) -> Optional[int]:
    rpm = fan.get("rpm")
    return int(rpm) if rpm is not None else None


def _power(fan: Dict[str, Any]) -> Optional[int]:
    """Fan power on the 0-255 scale PrusaLink reports."""
    speed = fan.get("speed")
    return int(round(speed * 255)) if speed is not None else None


def time_remaining(print_duration: float, progress: float,
                   estimated_time: Optional[float]) -> Optional[int]:
    """Seconds left, preferring the slicer estimate when there is one."""
    if estimated_time:
        return max(0, int(estimated_time - print_duration))
    if progress > 0.01 and print_duration > 0:
        return max(0, int(print_duration / progress - print_duration))
    return None


def material_from_metadata(meta: Dict[str, Any]) -> Optional[str]:
    value = meta.get("filament_type")
    if not value:
        return None
    # Multi-material slicer output looks like "PLA;PLA;PETG"
    return str(value).split(";")[0].strip() or None


MBL_POINTS = 7  # Prusa MK3 firmware reports a 7x7 mesh


def resample_mesh(matrix: List[List[float]],
                  points: int = MBL_POINTS) -> Optional[List[float]]:
    """Resample Klipper's mesh to the 7x7 grid PrusaLink sends.

    `matrix` is Klipper's bed_mesh matrix (rows go front to back). The
    result is flattened back row first, like Prusa's G81 output.
    """
    if not matrix or not matrix[0]:
        return None
    rows, cols = len(matrix), len(matrix[0])

    def sample(y: float, x: float) -> float:
        y0, x0 = int(y), int(x)
        y1, x1 = min(y0 + 1, rows - 1), min(x0 + 1, cols - 1)
        fy, fx = y - y0, x - x0
        top = matrix[y0][x0] * (1 - fx) + matrix[y0][x1] * fx
        bottom = matrix[y1][x0] * (1 - fx) + matrix[y1][x1] * fx
        return top * (1 - fy) + bottom * fy

    def axis(n: int) -> List[float]:
        if n == 1:
            return [0.0] * points
        return [i * (n - 1) / (points - 1) for i in range(points)]

    result = []
    for y in reversed(axis(rows)):
        for x in axis(cols):
            result.append(round(sample(y, x), 3))
    return result


def build_telemetry(status: Dict[str, Any], state: State, part_fan: str,
                    hotend_fan: str,
                    meta: Optional[Dict[str, Any]] = None,
                    extras: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
    """Build the telemetry kwargs for Printer.telemetry().

    `status` is the result of a Moonraker objects query. Missing values are
    left as None; the SDK drops them before sending.
    """
    meta = meta or {}
    extruder = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    toolhead = status.get("toolhead", {})
    gcode_move = status.get("gcode_move", {})
    print_stats = status.get("print_stats", {})
    part = status.get(part_fan, {})
    hotend = status.get(hotend_fan, {})

    data: Dict[str, Any] = {
        "temp_nozzle": _round(extruder.get("temperature")),
        "target_nozzle": _round(extruder.get("target")),
        "temp_bed": _round(bed.get("temperature")),
        "target_bed": _round(bed.get("target")),
        "fan_print": _rpm(part),
        "target_fan_print": _power(part),
        "fan_hotend": _rpm(hotend),
        "fan_extruder": _rpm(hotend),
        "target_fan_hotend": _power(hotend),
        "target_fan_extruder": _power(hotend),
    }

    if gcode_move.get("speed_factor") is not None:
        data["speed"] = int(round(gcode_move["speed_factor"] * 100))
    if gcode_move.get("extrude_factor") is not None:
        data["flow"] = int(round(gcode_move["extrude_factor"] * 100))

    position = toolhead.get("position")
    if position and len(position) >= 3:
        data["axis_x"] = _round(position[0], 2)
        data["axis_y"] = _round(position[1], 2)
        data["axis_z"] = _round(position[2], 2)

    extras = extras or {}
    for key in ("material", "total_print_time", "total_filament"):
        if extras.get(key) is not None:
            data[key] = extras[key]

    if state in (State.PRINTING, State.PAUSED):
        progress = status.get("display_status", {}).get("progress")
        if progress is None:
            progress = status.get("virtual_sdcard", {}).get("progress", 0.0)
        progress = progress or 0.0
        print_duration = print_stats.get("print_duration") or 0.0
        estimated = meta.get("estimated_time")
        data["progress"] = int(progress * 100)
        data["time_printing"] = int(print_stats.get("total_duration") or 0)
        data["time_remaining"] = time_remaining(print_duration, progress,
                                                estimated)
        data["inaccurate_estimates"] = not estimated
        data["material"] = (extras.get("material")
                            or material_from_metadata(meta))

    return data
