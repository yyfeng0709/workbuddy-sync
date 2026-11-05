"""sync_config.py - read the NAS root path from sync-config.txt.

The NAS path lives in one config file so the user edits a single place instead
of every script. If the file is missing or corrupt, fall back to a default and
write it back so subsequent runs are stable.
"""
import os

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync-config.txt")
# Generic placeholder default — each user overrides it in sync-config.txt.
DEFAULT_NAS_ROOT = r"\\YOUR-NAS-IP\your-share\workbuddy"


def load_nas_root():
    """Return the NAS root from sync-config.txt, or a default if absent/corrupt."""
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("NAS_ROOT"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        if val:
                            return val
        except Exception:
            pass
    # Missing/corrupt -> persist the default so future runs are stable
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("NAS_ROOT=" + DEFAULT_NAS_ROOT + "\n")
    except Exception:
        pass
    return DEFAULT_NAS_ROOT
