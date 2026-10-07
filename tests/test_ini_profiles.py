from pymooserver.moo12 import IniStore
from mooapi.moogame.ini import normalize_filename


def test_moogame_filename_normalization():
    assert normalize_filename(b"version") == b"version.imi"
    assert normalize_filename(b"version.ini") == b"version.imi"
    assert normalize_filename(b"version.exe") == b"version.imi"
    assert normalize_filename(b"folder/version.dat") == b"version.imi"


def test_moo12_ini_round_trip(tmp_path):
    store = IniStore(str(tmp_path))
    store.set("version.exe", "version", "version", "4")
    assert (tmp_path / "version.imi").read_bytes()
    assert store.get("version.ini", "version", "version", "") == "4"
