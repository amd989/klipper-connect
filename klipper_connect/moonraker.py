"""Small synchronous Moonraker HTTP client."""
import logging
from typing import Any, Dict, Iterable, Optional

import requests

log = logging.getLogger("klipper-connect.moonraker")


class MoonrakerError(RuntimeError):
    """Moonraker returned an error or could not be reached."""


class Moonraker:
    def __init__(self, url: str, api_key: str = "", timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-Api-Key"] = api_key

    def _request(self, method: str, path: str, timeout: Optional[float] = None,
                 **kwargs) -> Any:
        try:
            res = self.session.request(method, self.url + path,
                                       timeout=timeout or self.timeout,
                                       **kwargs)
        except requests.RequestException as err:
            raise MoonrakerError(f"Moonraker unreachable: {err}") from err
        if res.status_code != 200:
            try:
                message = res.json()["error"]["message"]
            except Exception:  # pylint: disable=broad-except
                message = res.text
            raise MoonrakerError(f"{method} {path} -> {res.status_code}: "
                                 f"{message}")
        return res.json().get("result")

    # --- status -----------------------------------------------------------

    def server_info(self) -> Dict[str, Any]:
        return self._request("GET", "/server/info")

    def printer_info(self) -> Dict[str, Any]:
        return self._request("GET", "/printer/info")

    def query(self, objects: Iterable[str]) -> Dict[str, Any]:
        """Return the full status of each requested Klipper object."""
        body = {"objects": {name: None for name in objects}}
        result = self._request("POST", "/printer/objects/query", json=body)
        return result.get("status", {})

    def metadata(self, filename: str) -> Dict[str, Any]:
        return self._request("GET", "/server/files/metadata",
                             params={"filename": filename})

    def history_totals(self) -> Dict[str, Any]:
        """Lifetime totals: total_print_time (s), total_filament_used (mm)."""
        return self._request("GET", "/server/history/totals").get(
            "job_totals", {})

    def active_spool_id(self) -> Optional[int]:
        """Spoolman's active spool, or None (also if Spoolman isn't set up)."""
        return self._request("GET", "/server/spoolman/spool_id").get(
            "spool_id")

    def spool(self, spool_id: int) -> Dict[str, Any]:
        return self._request("POST", "/server/spoolman/proxy", json={
            "request_method": "GET",
            "path": f"/v1/spool/{spool_id}",
        })

    # --- actions ----------------------------------------------------------

    def start_print(self, filename: str) -> None:
        self._request("POST", "/printer/print/start",
                      json={"filename": filename})

    def pause(self) -> None:
        self._request("POST", "/printer/print/pause")

    def resume(self) -> None:
        self._request("POST", "/printer/print/resume")

    def cancel(self) -> None:
        self._request("POST", "/printer/print/cancel")

    def gcode(self, script: str, timeout: float = 600.0) -> None:
        """Run G-code. Blocks until Klipper finishes it (G28 etc. take time)."""
        self._request("POST", "/printer/gcode/script", timeout=timeout,
                      json={"script": script})

    def firmware_restart(self) -> None:
        self._request("POST", "/printer/firmware_restart")
