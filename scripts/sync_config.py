"""sync_config.py - read the sync root path from a config file.

The path lives in one config file so the user edits a single place instead of
every script. If the file is missing or corrupt, fall back to a default and
write it back so subsequent runs are stable.

Dual-track support (2026-11-09):
- WB_SYNC_ROOT env var, if set, overrides everything (quick manual override).
- WB_SYNC_CONFIG env var, if set, points to the track's config file (e.g.
  sync-config-cloud.txt set by sync-cloud.bat) — this keeps .bat files pure
  ASCII while letting the CLOUD track use a mount path with non-ASCII names
  (e.g. a Chinese cloud-drive folder). cmd.exe corrupts UTF-8 batch parsing,
  so paths with non-ASCII characters must NEVER be set inside a .bat file.
- Otherwise: sync-config.txt (NAS track, SMB share).
"""
import os

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_SCRIPT_DIR, "sync-config.txt")
# Generic placeholder — each user sets the real path in sync-config.txt
DEFAULT_NAS_ROOT = r"\\YOUR-NAS-IP\your-share\workbuddy"


def _read_config(path):
    """Return the NAS_ROOT value from `path`, or None."""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("NAS_ROOT"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        if val:
                            return val
        except Exception:
            pass
    return None


def load_nas_root():
    """Resolution order: WB_SYNC_ROOT env > WB_SYNC_CONFIG env (file) >
    sync-config.txt > persisted default."""
    env = os.environ.get("WB_SYNC_ROOT", "").strip()
    if env:
        return env
    cfg = os.environ.get("WB_SYNC_CONFIG", "").strip()
    if cfg:
        val = _read_config(cfg)
        if val:
            return val
        # Pointed at a missing/corrupt file — hard error beats silently
        # syncing the WRONG track (NAS) when the user asked for cloud.
        raise RuntimeError(
            f"WB_SYNC_CONFIG points to '{cfg}' but no valid NAS_ROOT= line "
            f"was found in it. Fix the file (one line: NAS_ROOT=Z:\\...\\workbuddy)."
        )
    val = _read_config(CONFIG_FILE)
    if val:
        return val
    # Missing/corrupt -> persist the default so future runs are stable
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("NAS_ROOT=" + DEFAULT_NAS_ROOT + "\n")
    except Exception:
        pass
    return DEFAULT_NAS_ROOT
