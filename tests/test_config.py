import os

import pytest

from klipper_connect import config as config_mod

MINIMAL = "[printer]\nserial_number: CZPX1234X123XC12345\n"


def write(path, text=MINIMAL):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def test_paths_follow_the_printer_data_folder(tmp_path):
    data = tmp_path / "printer_2_data"
    cfg = config_mod.load(write(data / "config" / "klipper_connect.conf"))
    assert cfg.gcodes_path == str(data / "gcodes")
    assert cfg.credentials_file == str(
        data / "klipper_connect" / "prusa_connect.ini")
    assert cfg.state_file == str(data / "klipper_connect" / "state.json")
    assert cfg.camera_file == str(data / "klipper_connect" / "camera.json")


def test_data_dir_option_and_explicit_paths(tmp_path):
    other = tmp_path / "elsewhere"
    text = MINIMAL + (f"[bridge]\ndata_dir: {other}\n"
                      f"[storage]\ngcodes_path: {tmp_path / 'g'}\n")
    cfg = config_mod.load(write(tmp_path / "x" / "kc.conf", text))
    assert cfg.gcodes_path == str(tmp_path / "g")
    assert cfg.state_file == str(other / "klipper_connect" / "state.json")


def test_example_config_loads(tmp_path):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "klipper_connect.conf.example")) as f:
        text = f.read()
    data = tmp_path / "printer_data"
    cfg = config_mod.load(write(data / "config" / "klipper_connect.conf",
                                text))
    assert cfg.gcodes_path == str(data / "gcodes")
    assert cfg.camera_url == ""
    assert cfg.mbl_gcode == "G80"


def test_missing_serial_number(tmp_path):
    with pytest.raises(ValueError):
        config_mod.load(write(tmp_path / "c.conf", "[printer]\n"))
