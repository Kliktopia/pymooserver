"""Platform-native default storage locations for PyMooServer."""

from __future__ import annotations

import os
import platform
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Mapping


def default_data_root(
    *,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: str | Path | None = None,
) -> str:
    """Return the platform-native default data directory.

    Explicit CLI paths always take precedence over these defaults.  Parameters
    are injectable so path selection can be tested without running every branch
    on its native operating system.
    """
    system = system or platform.system()
    environ = os.environ if environ is None else environ

    if system == "Windows":
        return str(PureWindowsPath(r"C:\Moo\data"))

    # Construct POSIX paths with POSIX semantics even when a different host OS
    # (for example the Windows CI runner) is exercising this branch in tests.
    home_value = str(Path.home()) if home is None else str(home)
    home_path = PurePosixPath(home_value.replace("\\", "/"))

    if system == "Darwin":
        return str(home_path / "Library" / "Application Support" / "PyMooServer")

    xdg = environ.get("XDG_DATA_HOME", "").strip()
    if xdg.startswith("~/"):
        base = home_path / xdg[2:]
    elif xdg == "~":
        base = home_path
    else:
        base = PurePosixPath(xdg) if xdg else home_path / ".local" / "share"
    return str(base / "pymooserver")


def default_ini_root(**kwargs) -> str:
    root = default_data_root(**kwargs)
    if (kwargs.get("system") or platform.system()) == "Windows":
        return str(PureWindowsPath(root) / "ini")
    return str(PurePosixPath(root) / "ini")


def default_backup_root(**kwargs) -> str:
    root = default_data_root(**kwargs)
    if (kwargs.get("system") or platform.system()) == "Windows":
        return str(PureWindowsPath(root) / "backups")
    return str(PurePosixPath(root) / "backups")


def default_log_file(**kwargs) -> str:
    root = default_data_root(**kwargs)
    if (kwargs.get("system") or platform.system()) == "Windows":
        return str(PureWindowsPath(root) / "logs" / "moo-server.log")
    return str(PurePosixPath(root) / "logs" / "moo-server.log")
