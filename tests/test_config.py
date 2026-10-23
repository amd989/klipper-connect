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


def test_mini_type_and_example(tmp_path):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "examples",
                           "klipper_connect.mini.conf")) as f:
        text = f.read()
    data = tmp_path / "printer_data"
    cfg = config_mod.load(write(data / "config" / "klipper_connect.conf",
                                text))
    assert cfg.printer_type.name == "MINI"
    assert cfg.printer_type.value == (2, 1, 0)
    assert str(cfg.printer_type) == "2.1.0"
    assert cfg.hotend_fan == "heater_fan extruder_fan"
    assert cfg.filament_temp_param == "EXTRUDER"
    assert cfg.camera_url == ""
    assert cfg.storage_name == "usb"


def test_storage_default_depends_on_type(tmp_path):
    mk3 = config_mod.load(write(tmp_path / "a" / "c.conf"))
    assert mk3.storage_name == "local"
    mini = config_mod.load(write(tmp_path / "b" / "c.conf",
                                 MINIMAL + "type: MINI\n"))
    assert mini.storage_name == "usb"
    assert mk3.file_info_count == 20 and mini.file_info_count == 0


def test_moonraker_url_from_moonraker_conf(tmp_path):
    cfg_dir = tmp_path / "mini_data" / "config"
    path = write(cfg_dir / "klipper_connect.conf")
    (cfg_dir / "moonraker.conf").write_text(
        "[server]\nhost: 0.0.0.0\nport: 7127   # mini\n"
        "[update_manager]\n[update_manager]\n")  # duplicates are tolerated
    assert config_mod.load(path).moonraker_url == "http://127.0.0.1:7127"


def test_moonraker_url_explicit_wins_and_default(tmp_path):
    cfg_dir = tmp_path / "a" / "config"
    (cfg_dir).mkdir(parents=True)
    (cfg_dir / "moonraker.conf").write_text("[server]\nport: 7127\n")
    path = write(cfg_dir / "klipper_connect.conf",
                 MINIMAL + "[moonraker]\nurl: http://10.0.0.5:7200/\n")
    assert config_mod.load(path).moonraker_url == "http://10.0.0.5:7200"
    # No moonraker.conf next to it -> 7125
    other = config_mod.load(write(tmp_path / "b" / "c.conf"))
    assert other.moonraker_url == "http://127.0.0.1:7125"
    # moonraker.conf without a port -> Moonraker's default 7125
    (tmp_path / "b" / "moonraker.conf").write_text("[server]\n")
    assert config_mod.load(str(tmp_path / "b" / "c.conf")).moonraker_url \
        == "http://127.0.0.1:7125"
