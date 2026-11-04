"""Download commands Buddy firmware (MINI) understands but the SDK doesn't.

Prusa Connect may send a MINI either of these instead of
START_CONNECT_DOWNLOAD:

* START_INLINE_DOWNLOAD: same arguments (path, team_id, hash) and the same
  file in Connect's storage; newer Buddy firmware streams it over its
  websocket, we just fetch it like START_CONNECT_DOWNLOAD.
* START_ENCRYPTED_DOWNLOAD: the file is served over plain HTTP from
  /f/<iv as hex>/raw on Buddy's Connect host (buddy-a.connect.prusa3d.com,
  not connect.prusa3d.com, which returns its web app for that path),
  AES-128 encrypted with the `key` and `iv` from the command, and cut to
  `orig_size` after decryption.

  Current Buddy firmware decrypts with AES-CTR (the iv's last 4 bytes are
  the block counter, from 0). Connect was observed sending AES-CBC with
  zero padding instead (Nov 2026), presumably because of the firmware
  version we report. Which one is used is detected from the first block:
  the plain text is G-code (text) or binary G-code (starts with GCDE).

  See Prusa-Firmware-Buddy src/connect/planner.cpp,
  src/transfers/decrypt.cpp and config_store/defaults.hpp.
"""
import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from prusa.connect.printer.command import Command
from prusa.connect.printer.const import (CONNECTION_TIMEOUT, Event, Source,
                                         TransferType)
from prusa.connect.printer.download import (DownloadMgr,
                                            TransferAbortedError,
                                            TransferStoppedError)

log = logging.getLogger("klipper-connect")

START_INLINE_DOWNLOAD = "START_INLINE_DOWNLOAD"
START_ENCRYPTED_DOWNLOAD = "START_ENCRYPTED_DOWNLOAD"

BLOCK_SIZE = 16
CTR_COUNTER_SIZE = 4

# Encrypted files for Buddy printers are served by this host
CONNECT_HOST = "connect.prusa3d.com"
BUDDY_CONNECT_HOST = "buddy-a.connect.prusa3d.com"

# (key, iv, orig_size) for the transfer that is about to run
Encryption = Tuple[bytes, bytes, int]


def _aes():
    try:
        from Crypto.Cipher import AES  # pycryptodome
    except ImportError as err:
        raise TransferAbortedError(
            "Encrypted downloads need pycryptodome: run "
            "~/klipper-connect-env/bin/pip install pycryptodome") from err
    return AES


def _looks_like_print_file(data: bytes) -> float:
    """How much `data` looks like the start of a G-code file (0..1)."""
    if data.startswith(b"GCDE"):  # binary G-code
        return 1.0
    if not data:
        return 0.0
    text = sum(32 <= b < 127 or b in (9, 10, 13) for b in data)
    return text / len(data)


class StreamDecryptor:
    """Decrypts a streamed download chunk by chunk, in CBC or CTR mode.

    The mode is picked from the first block; CBC needs whole blocks, so
    a partial block is kept until the next chunk.
    """

    def __init__(self, key: bytes, iv: bytes):
        self.key = key
        self.iv = iv
        self.mode: Optional[str] = None
        self._cipher = None
        self._pending = b""

    def _new(self, mode: str):
        aes = _aes()
        if mode == "cbc":
            return aes.new(self.key, aes.MODE_CBC, iv=self.iv)
        return aes.new(self.key, aes.MODE_CTR,
                       nonce=self.iv[:BLOCK_SIZE - CTR_COUNTER_SIZE],
                       initial_value=0)

    def _choose_mode(self, first_block: bytes):
        scores = {mode: _looks_like_print_file(
            self._new(mode).decrypt(first_block)) for mode in ("cbc", "ctr")}
        self.mode = max(scores, key=scores.get)
        if scores[self.mode] < 0.9:
            log.warning("Encrypted download doesn't look like G-code in "
                        "either mode (%s); using %s", scores, self.mode)
        else:
            log.info("Encrypted download uses AES-%s", self.mode.upper())
        self._cipher = self._new(self.mode)

    def decrypt(self, data: bytes) -> bytes:
        data = self._pending + data
        self._pending = b""
        if self.mode is None:
            if len(data) < BLOCK_SIZE:
                self._pending = data
                return b""
            self._choose_mode(data[:BLOCK_SIZE])
        if self.mode == "cbc":
            whole = len(data) - len(data) % BLOCK_SIZE
            data, self._pending = data[:whole], data[whole:]
        return self._cipher.decrypt(data) if data else b""

    def finish(self) -> bytes:
        """Call after the last chunk."""
        if self.mode is None and self._pending:
            # tiny file: shorter than one block, can only be CTR
            self.mode, self._cipher = "ctr", self._new("ctr")
            data, self._pending = self._pending, b""
            return self._cipher.decrypt(data)
        if self._pending:
            raise TransferAbortedError(
                "Encrypted download isn't a whole number of AES blocks")
        return b""


def make_decryptor(key: bytes, iv: bytes) -> StreamDecryptor:
    _aes()  # fail early, before downloading, if pycryptodome is missing
    return StreamDecryptor(key, iv)


def encrypted_url(server: str, iv_hex: str,
                  port_override: Optional[int] = None) -> str:
    """Where Buddy firmware fetches an encrypted file from.

    Like Buddy: Buddy's Connect host (for connect.prusa3d.com) or the
    configured host, plain HTTP (443 with TLS becomes 80); a `port` in the
    command overrides it.
    """
    parsed = urlparse(server)
    host = parsed.hostname
    if host == CONNECT_HOST:
        host = BUDDY_CONNECT_HOST
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port == 443 and parsed.scheme == "https":
        port = 80
    if port_override:
        port = int(port_override)
    return f"http://{host}:{port}/f/{iv_hex.lower()}/raw"


def parse_block(value: Any, name: str) -> bytes:
    try:
        block = bytes.fromhex(str(value))
    except ValueError as err:
        raise ValueError(f"{name} is not hex") from err
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"{name} must be {BLOCK_SIZE} bytes")
    return block


class DecryptingDownloadMgr(DownloadMgr):
    """The SDK's download manager, plus decryption for encrypted transfers.

    Everything else (transfer ids, progress, TRANSFER_* events, the
    download-finished callback, renaming the .part file) stays the SDK's.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.encryption: Optional[Encryption] = None

    @classmethod
    def replacing(cls, old: DownloadMgr) -> "DecryptingDownloadMgr":
        new = cls(old.fs, old.transfer, old.conn_details_cb, old.event_cb,
                  old.printed_file_cb, old.download_finished_cb)
        return new

    def start_encrypted(self, path: str, url: str, encryption: Encryption,
                        to_print: bool = False, to_select: bool = False,
                        start_cmd_id: Optional[int] = None) -> dict:
        if self.transfer.in_progress:
            return {
                "event": Event.REJECTED,
                "source": Source.CONNECT,
                "reason": "Another transfer in progress",
            }
        # Set before start(): the download loop may pick the transfer up
        # as soon as it exists
        self.encryption = encryption
        retval = self.start(TransferType.FROM_CONNECT, path, url,
                            to_print=to_print, to_select=to_select,
                            start_cmd_id=start_cmd_id)
        if retval.get("event") == Event.REJECTED:
            self.encryption = None
        return retval

    def download(self):
        encryption, self.encryption = self.encryption, None
        if encryption is None:
            return super().download()
        return self._download_encrypted(*encryption)

    def _download_encrypted(self, key: bytes, iv: bytes, orig_size: int):
        """SDK's download(), decrypting each chunk before writing it."""
        self.transfer.start_ts = time.time()
        decryptor = make_decryptor(key, iv)
        _server, headers = self.conn_details_cb()
        # Not the authenticated API: only send the User-Agent
        res = requests.get(self.transfer.url, stream=True,
                           headers={"User-Agent": headers.get("User-Agent")},
                           timeout=CONNECTION_TIMEOUT)
        if res.status_code != 200:
            raise TransferAbortedError(
                f"Invalid status code: {res.status_code}")
        mime_type = res.headers.get("Content-Type")
        if mime_type and mime_type.lower() not in self.VALID_MIME_TYPES:
            raise TransferAbortedError(f"Invalid content type: {mime_type}")
        length = res.headers.get("Content-Length")
        self.transfer.size = int(length) if length is not None else orig_size

        left = orig_size
        with open(self.tmp_filename(), "wb") as f:
            self.event_cb(Event.TRANSFER_INFO, Source.WUI, **self.info())
            for data in res.iter_content(chunk_size=self.buffer_size):
                if self.transfer.stop_ts > 0:
                    raise TransferStoppedError("Transfer was stopped")
                if not self._running_loop:
                    raise TransferAbortedError("Transfer was aborted")
                plain = decryptor.decrypt(data)
                if left > 0:
                    f.write(plain[:left])
                    left -= min(left, len(plain))
                self.transfer.transferred += len(data)
            plain = decryptor.finish()
            if left > 0 and plain:
                f.write(plain[:left])
                left -= min(left, len(plain))
            f.flush()
            os.fsync(f.fileno())

        if left > 0:
            raise TransferAbortedError(
                f"Download ended {left} bytes short of {orig_size}")


class KlipperCommand(Command):
    """The SDK's command runner, plus commands its enum doesn't know.

    The SDK rejects any command name missing from its Command enum before
    looking at handlers, so extra commands are looked up here first.
    """

    extra_handlers: Dict[str, Callable[[Command], Dict[str, Any]]]

    def __call__(self):
        handlers = getattr(self, "extra_handlers", {})
        if self.state != Event.ACCEPTED or self.command_name not in handlers:
            return super().__call__()
        command_id = self.command_id
        try:
            kwargs = handlers[self.command_name](self)
            return self.finish(command_id=command_id, **kwargs)
        except Exception as err:  # pylint: disable=broad-except
            log.exception("%s failed", self.command_name)
            return self.failed(Source.WUI, reason="Command error",
                               error=repr(err), command_id=command_id)
