"""Process environment first; Windows user settings support an already-running host."""

import os


def _user_environment(name):
    if os.name != "nt":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else None
    except OSError:
        return None


def getenv(name, default=None):
    return os.environ.get(name) or _user_environment(name) or default
