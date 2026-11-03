import os
import threading

import pytest
from Crypto.Cipher import AES
from prusa.connect.printer.const import PrinterType

from klipper_connect import config as config_mod
from klipper_connect.bridge import Bridge
from klipper_connect.downloads import (encrypted_url, make_decryptor,
                                       parse_block)
from klipper_connect.moonraker import Moonraker
from test_bridge import FakeConnect, make_config, wait_for

KEY = bytes(range(16))
IV = bytes.fromhex("00112233445566778899aabbccddeeff")


def buddy_ctr(data: bytes) -> bytes:
    """Independent AES-CTR the way Buddy firmware does it: the last 4 bytes
    of the iv are a big-endian block counter starting at 0."""
    ecb = AES.new(KEY, AES.MODE_ECB)
    out = bytearray()
    for i in range(0, len(data), 16):
        counter = IV[:12] + (i // 16).to_bytes(4, "big")
        stream = ecb.encrypt(counter)
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], stream))
    return bytes(out)


def test_decryptor_matches_buddy_counter_layout():
    plain = os.urandom(1000)
    encrypted = buddy_ctr(plain)
    dec = make_decryptor(KEY, IV)
    # decrypt in uneven chunks, like a streamed download
    got = dec.decrypt(encrypted[:7]) + dec.decrypt(encrypted[7:600]) + \
        dec.decrypt(encrypted[600:])
    assert got == plain


def test_encrypted_url():
    assert encrypted_url("https://connect.prusa3d.com", "ab") == \
        "http://connect.prusa3d.com:80/f/ab/raw"
    assert encrypted_url("http://127.0.0.1:8000", "AB") == \
        "http://127.0.0.1:8000/f/ab/raw"
    assert encrypted_url("https://connect.prusa3d.com", "ab", 8080) == \
        "http://connect.prusa3d.com:8080/f/ab/raw"


def test_parse_block():
    assert parse_block(IV.hex(), "iv") == IV
    with pytest.raises(ValueError):
        parse_block("abcd", "iv")
    with pytest.raises(ValueError):
        parse_block("not hex at all, not hex at all!!", "key")


def mini_bridge(tmp_path, moonraker, connect):
    cfg = make_config(tmp_path, connect.port, "127.0.0.1")
    cfg.printer_type = config_mod.PRINTER_TYPES["MINI"]
    cfg.storage_name = "usb"
    bridge = Bridge(cfg, Moonraker(moonraker.url))
    thread = threading.Thread(target=bridge.run, daemon=True)
    thread.start()
    return bridge, thread


def events_named(connect, name):
    return [e for e in connect.events if e["event"] == name]


def test_encrypted_download_then_print(tmp_path, moonraker):
    plain = b"G28\nG1 X10 Y10\n" * 500
    # Connect may pad the encrypted file; orig_size cuts it back
    served = buddy_ctr(plain + b"\0" * 9)
    connect = FakeConnect([
        (300, {"command": "START_ENCRYPTED_DOWNLOAD",
               "kwargs": {"path": "/usb/enc_cube.gcode", "key": KEY.hex(),
                          "iv": IV.hex(), "orig_size": len(plain),
                          "printing": True}}),
    ])
    connect.files[f"/f/{IV.hex()}/raw"] = served
    bridge, thread = mini_bridge(tmp_path, moonraker, connect)
    try:
        target = tmp_path / "gcodes" / "enc_cube.gcode"
        assert wait_for(lambda: target.exists())
        assert target.read_bytes() == plain
        assert wait_for(lambda: moonraker.posts("/printer/print/start"))
        assert moonraker.posts("/printer/print/start")[0][2] == {
            "filename": "enc_cube.gcode"}
        assert wait_for(lambda: events_named(connect, "TRANSFER_FINISHED"))
        # Download commands are answered with the transfer's details
        # (TRANSFER_INFO), as the SDK does for START_CONNECT_DOWNLOAD
        answer = [e for e in connect.events if e.get("command_id") == 300
                  and e["event"] not in ("ACCEPTED",)]
        assert answer and answer[0]["event"] == "TRANSFER_INFO"
        assert answer[0]["data"]["path"] == "/usb/enc_cube.gcode"
    finally:
        bridge.stop_event.set()
        thread.join(5)
        connect.server.shutdown()


def test_inline_download(tmp_path, moonraker):
    connect = FakeConnect([
        (301, {"command": "START_INLINE_DOWNLOAD",
               "kwargs": {"path": "/usb/inline.gcode", "team_id": 7,
                          "hash": "xyz", "orig_size": 4}}),
    ])
    connect.files["/p/teams/7/files/xyz/raw"] = b"G28\n"
    bridge, thread = mini_bridge(tmp_path, moonraker, connect)
    try:
        target = tmp_path / "gcodes" / "inline.gcode"
        assert wait_for(lambda: target.exists())
        assert target.read_bytes() == b"G28\n"
        assert wait_for(lambda: events_named(connect, "TRANSFER_FINISHED"))
        assert not moonraker.posts("/printer/print/start")  # not asked to
    finally:
        bridge.stop_event.set()
        thread.join(5)
        connect.server.shutdown()


def test_bad_encrypted_command_fails_cleanly(tmp_path, moonraker):
    connect = FakeConnect([
        (302, {"command": "START_ENCRYPTED_DOWNLOAD",
               "kwargs": {"path": "/usb/x.gcode", "iv": IV.hex(),
                          "orig_size": 10}}),  # no key
        (303, {"command": "START_ENCRYPTED_DOWNLOAD",
               "kwargs": {"path": "/usb/x.gcode", "key": "abcd",
                          "iv": IV.hex(), "orig_size": 10}}),
    ])
    bridge, thread = mini_bridge(tmp_path, moonraker, connect)
    try:
        def failed(cmd_id):
            return any(e.get("command_id") == cmd_id
                       and e["event"] == "FAILED" for e in connect.events)
        assert wait_for(lambda: failed(302) and failed(303))
        assert not (tmp_path / "gcodes" / "x.gcode").exists()
    finally:
        bridge.stop_event.set()
        thread.join(5)
        connect.server.shutdown()
