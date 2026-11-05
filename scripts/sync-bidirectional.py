"""sync-bidirectional.py - Two-way sync between local and NAS
Run this on EITHER computer. It detects which side changed and syncs accordingly.

Conflict handling by file type:
- Daily logs (YYYY-MM-DD.md): auto-merge both sides, dedup lines, keep all content
- Conversation transcripts (*.jsonl): union both sides by record id, keep ALL messages
  (so editing the same session on two PCs never loses either side's chat content)
- Identity & memory markdown (SOUL.md / IDENTITY.md / USER.md / MEMORY.md):
  SECTION-LEVEL merge (B1). Union all top-level sections by heading; only genuinely
  diverged sections keep the newer side. One side's added sections are never lost.
- Task JSON files: save conflict backup, keep newer version
- Other .md / files: save conflict backup, keep newer version

User-level skills (~/.workbuddy/skills/<name>/): per-subdirectory ADDITIVE sync
  (union subdir names from both sides, copy what the other lacks). Top-level
  *.json migration markers are skipped; deletions are NOT propagated.

State is tracked PER-MACHINE (sync_state.<COMPUTERNAME>.json) and a cross-machine
advisory lock on the NAS prevents two PCs from syncing at the same instant.
"""
import os
import shutil
import hashlib
import json
import re
import sys
import socket
import atexit
import time
import fnmatch
from datetime import datetime

from sync_config import load_nas_root
NAS_ROOT = load_nas_root()
USER_DIR = os.path.join(os.environ["USERPROFILE"], ".workbuddy")
WORKBUDDY_ROOT = os.path.join(os.environ["USERPROFILE"], "WorkBuddy")
COMPUTER = os.environ.get("COMPUTERNAME", "unknown")

# --- Sync ignore list (like .gitignore, for workspace-files) ---
# File: scripts/sync-ignore.txt (synced itself via the scripts section, so both
# PCs share ONE list). Lines: workspace-relative paths with * wildcard, e.g.
#   2026-01-15-10-30-00/mytask/key.cache        (exact file)
#   *.conflict-*                                 (any conflict backup anywhere)
#   2026-01-15-10-30-00/mytask/dumps/            (trailing / = whole dir)
# Matching is case-insensitive; forward slashes; a dir pattern also excludes
# everything under it. Ignored files are NEVER pushed/pulled/compared.
IGNORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync-ignore.txt")

# --- Workspace-files deletion tombstones (v1.1: deletions propagate, like sessions) ---
# NAS-canonical store (shared across PCs) + local mirror (offline-readable). A deletion
# recorded on one PC is written here and suppressing the "resurrect" behavior everywhere.
WS_TOMB_NAS = os.path.join(NAS_ROOT, ".ws_tombstones.json")
WS_TOMB_LOCAL = os.path.join(USER_DIR, ".ws_tombstones.json")
# Per-machine snapshot of files present locally (used to detect genuine LOCAL deletions,
# so we never mis-classify a NAS-only file as "deleted here" and wipe it elsewhere).
LOCAL_INV_FILE = os.path.join(NAS_ROOT, f"sync_localinv.{COMPUTER}.json")
# Reusable recoverable trash (same location as sessions-merge.py's .trash).
TRASH_LOCAL = os.path.join(USER_DIR, ".trash")
TRASH_NAS = os.path.join(NAS_ROOT, ".trash")
WS_TOMBS = {}  # global, loaded in main() before the workspace-files section runs

def load_ignore_patterns():
    pats = []
    try:
        with open(IGNORE_FILE, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                pats.append(line.lower())
    except Exception:
        pass
    return pats

def is_ignored(rel_posix, patterns):
    """rel_posix like '2026-01-15-10-30-00/mytask/key.cache' (already lowered)."""
    for pat in patterns:
        if pat.endswith("/"):
            if rel_posix.startswith(pat) or rel_posix + "/" == pat:
                return True
        elif "*" in pat or "?" in pat:
            if fnmatch.fnmatch(rel_posix, pat):
                return True
        elif rel_posix == pat:
            return True
    return False

# Known sync endpoints. IMPORTANT: the "current" machine is NOT fixed — it changes
# depending on which PC is running WorkBuddy this session. NEVER assume which one
# is current from memory; ALWAYS detect via COMPUTERNAME (or HOSTNAME). Do NOT infer
# the machine from service state (e.g. a listening port) or file presence — both PCs
# may run the same local services.
# >>> Edit these to YOUR machine names (run `echo %COMPUTERNAME%` on each PC). <<<
KNOWN_MACHINES = {
    "OFFICE-PC": "办公电脑 (office)",
    "HOME-PC": "家里电脑 (home)",
}

# Optional guard for machine-specific one-off ops (e.g. cleanup). When set (via
# --require-machine), main() aborts if the current machine is not the required one.
REQUIRE_MACHINE = None


def detect_machine():
    """Return (id, label) for the current machine from COMPUTERNAME/HOSTNAME.
    This is the AUTHORITATIVE identity check — it makes machine-specific operations
    auditable and prevents acting on the wrong PC. Port/service-state inference is
    explicitly forbidden (both PCs can run the same services)."""
    name = (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "").strip().upper()
    if not name:
        return "UNKNOWN", "未识别 (unknown — COMPUTERNAME/HOSTNAME 均为空)"
    if name in KNOWN_MACHINES:
        return name, KNOWN_MACHINES[name]
    return name, "未识别 (unknown — 不在 KNOWN_MACHINES 清单)"

# Per-machine state file: each PC tracks its OWN last-seen hashes, so the two
# machines never clobber each other's state (the old shared sync_state.json could
# be overwritten by the other PC, causing wrong conflict detection).
STATE_FILE = os.path.join(NAS_ROOT, f"sync_state.{COMPUTER}.json")

# Cross-machine advisory lock on the NAS (prevents two PCs syncing simultaneously
# and clobbering files / each other's writes).
LOCK_FILE = os.path.join(NAS_ROOT, ".sync-lock")
LOCK_STALE_SECONDS = 600  # a lock older than this is considered dead

# Regex for daily log filenames: 2026-07-10.md
DAILY_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
# Conversation transcript files are line-delimited JSON keyed by sessionId.
JSONL_RE = re.compile(r"\.jsonl$")

# Stats
pushed = []
pulled = []
conflicts = []
merged = []
skipped = 0
vanished = []  # source files that disappeared mid-sync (transient service dumps)
trashed = []   # workspace-files deleted via tombstone (moved to .trash/, recoverable)

# --- Fast NAS reachability check (prevents Windows SMB hang on dead UNC path) ---
def _nas_host(path):
    h = path.replace("\\", "/").strip("/")
    return h.split("/")[0] if "/" in h else h

def nas_reachable(host, timeout=4.0):
    """Return True if the SMB host answers on TCP 445/139 within `timeout`.
    A UNC path that is unreachable makes os.path.isdir() hang for minutes;
    this TCP probe fails fast instead."""
    for port in (445, 139):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False



def file_hash(path):
    """SHA256 of file content. Returns None if file doesn't exist."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


class SourceVanished(OSError):
    """Raised by safe_copy2 when the source file disappeared between enumeration
    and the actual copy (e.g. a transient service dump cleaned up by the running
    process). Callers should treat this as 'nothing to sync' and skip, not abort."""
    pass


def safe_copy2(src, dst, max_tries=12):
    """Copy src -> dst, retrying on transient SMB oplock / PermissionError.

    The NAS SMB share occasionally raises PermissionError or truncates the target
    to 0 bytes mid-write (oplock not released). We retry with exponential backoff
    and verify by size + SHA256 before declaring success; on hard failure we raise
    so the caller can decide (better a noisy error than a silent 0-byte file).
    """
    src_hash = file_hash(src)
    if src_hash is None:
        raise SourceVanished(f"safe_copy2: source missing: {src}")
    src_size = os.path.getsize(src)
    last_err = None
    for attempt in range(1, max_tries + 1):
        try:
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            # Clear the readonly bit if set on the destination (occasionally on NAS files)
            if os.name == "nt" and os.path.exists(dst):
                try:
                    import ctypes
                    a = ctypes.windll.kernel32.GetFileAttributesW(dst)
                    if a != -1 and (a & 0x1):
                        ctypes.windll.kernel32.SetFileAttributesW(dst, a & ~0x1)
                except Exception:
                    pass
            shutil.copy2(src, dst)
            if os.path.getsize(dst) == src_size and file_hash(dst) == src_hash:
                return
            last_err = f"verify-mismatch (dst size {os.path.getsize(dst)} != {src_size})"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_tries:
            wait = 2.0 * (1.6 ** (attempt - 1))
            time.sleep(min(wait, 20))
    raise IOError(f"safe_copy2 FAILED after {max_tries} tries ({last_err}); src={src} dst={dst}")


def load_state():
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# --- Workspace-files deletion tombstones (v1.1) -----------------------------------
# Mirror the sessions tombstone design: a deletion made on one PC is recorded and then
# suppresses the "resurrect" behavior on every other node, so cleaning a synced file
# actually propagates (instead of bouncing forever). Files go to .trash/ (recoverable),
# never hard-deleted.

def load_ws_tombs():
    """Merge NAS-canonical + local-mirror tombstone stores; keep max delete_ms per key."""
    tombs = {}
    for path in (WS_TOMB_NAS, WS_TOMB_LOCAL):
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        try:
                            v = int(v)
                        except (TypeError, ValueError):
                            continue
                        tombs[k] = max(tombs.get(k, 0), v)
            except Exception:
                pass
    return tombs


def save_ws_tombs(tombs):
    """Persist tombstones: NAS canonical (authoritative) + local mirror."""
    for path in (WS_TOMB_NAS, WS_TOMB_LOCAL):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(tombs, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def load_local_inv():
    """Load this machine's last-run workspace-files inventory (set of keys)."""
    if os.path.isfile(LOCAL_INV_FILE):
        try:
            with open(LOCAL_INV_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(data) if isinstance(data, (list, set)) else set()
        except Exception:
            return set()
    return set()


def save_local_inv(inv):
    """Persist this machine's workspace-files inventory snapshot."""
    try:
        os.makedirs(os.path.dirname(LOCAL_INV_FILE), exist_ok=True)
        with open(LOCAL_INV_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(inv), f, ensure_ascii=False)
    except Exception:
        pass


def _ws_move_to_trash(src, trash_root):
    """Move a file into the trash dir with a timestamped flat name (recoverable).
    Returns the destination path, or None on failure. Mirrors sessions-merge.py."""
    if not os.path.isfile(src):
        return None
    try:
        os.makedirs(trash_root, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.basename(src)
        dest = os.path.join(trash_root, f"{ts}__{base}")
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(trash_root, f"{ts}__{i}_{base}")
            i += 1
        shutil.move(src, dest)
        return dest
    except Exception:
        return None


def _ws_tombstone_intercept(key, surviving_path, trash_root, state):
    """If `key` (workspace-files only) is tombstoned and the surviving file is not
    newer than the tombstone, move it to the trash and return a result string.
    Returns None when no tombstone applies (caller syncs normally). The
    'delete-time > file-time' rule keeps an offline edit made after the deletion.

    Side effects: pops `key` from state on trash; drops a stale tombstone when the
    file is newer (so a re-created/edited file is not re-deleted)."""
    if not key.startswith("workspace-files/"):
        return None
    del_ms = WS_TOMBS.get(key)
    if not del_ms:
        return None
    try:
        mtime_ms = int(os.path.getmtime(surviving_path) * 1000)
    except OSError:
        return None
    side = "NAS" if trash_root is TRASH_NAS else "local"
    if del_ms >= mtime_ms:
        dest = _ws_move_to_trash(surviving_path, trash_root)
        if dest:
            state.pop(key, None)
            return f"trashed -> {side} .trash (deleted tombstone)"
        return None  # move failed: don't lose the file, let normal sync handle it
    # File edited after the deletion time -> keep it, drop the now-stale tombstone.
    WS_TOMBS.pop(key, None)
    return None


def acquire_lock():
    """Advisory cross-machine lock on the NAS. Returns True if we hold it.

    Prevents two PCs from syncing at the same instant and clobbering files or
    each other's writes. A lock from another machine that is older than
    LOCK_STALE_SECONDS is treated as dead and taken over. Best-effort: if the
    lock file cannot be written we still proceed (we never block a sync just
    because the lock write failed).
    """
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    except Exception:
        pass
    now = time.time()
    if os.path.isfile(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                info = json.load(f)
            age = now - info.get("started_ts", 0)
            holder = info.get("machine", "unknown")
            if age > LOCK_STALE_SECONDS:
                # Stale lock from anyone — our own crashed run OR another machine
                # that vanished — is taken over. Never reuse a lock older than the
                # staleness threshold: a dead own-run's lock would block us forever
                # (and the lock file itself may be SMB-locked by a stale session).
                print(f"[lock] Removing stale lock from '{holder}' ({age:.0f}s old).")
            elif holder == COMPUTER:
                # own fresh lock -> assume a still-running own instance
                print(f"[lock] Reusing our own lock from previous run ({age:.0f}s old).")
            else:
                print("ERROR: Another machine is currently syncing.")
                print(f"  Holder : {holder} (pid {info.get('pid')})")
                print(f"  Started: {info.get('started', '?')}  ({age:.0f}s ago)")
                print(f"  If you are SURE no sync is running on '{holder}', delete:")
                print(f"    {LOCK_FILE}")
                print(f"  (auto-released after {LOCK_STALE_SECONDS}s of staleness)")
                return False
        except Exception:
            # unreadable lock -> assume dead, take over
            pass
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "machine": COMPUTER,
                "pid": os.getpid(),
                "started": datetime.now().isoformat(timespec="seconds"),
                "started_ts": now,
            }, f, indent=2)
        return True
    except Exception as e:
        print(f"[lock] WARNING: could not write lock file ({e}); continuing without lock.")
        return True


def release_lock():
    """Remove the lock only if it is ours (never delete the other machine's)."""
    try:
        if os.path.isfile(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r", encoding="utf-8") as f:
                    info = json.load(f)
                if info.get("machine") != COMPUTER:
                    return
            except Exception:
                pass
            os.remove(LOCK_FILE)
    except Exception:
        pass


def merge_daily_log(local_path, nas_path):
    """Merge two daily log files. Both are append-only, so we dedup and combine.
    Returns (merged_content, nas_backup_name) or raises on error."""
    with open(local_path, "r", encoding="utf-8") as f:
        local_lines = f.readlines()
    with open(nas_path, "r", encoding="utf-8") as f:
        nas_lines = f.readlines()

    # Extract header line (should be "# YYYY-MM-DD" in both)
    header = local_lines[0].rstrip("\n") if local_lines else "# Untitled"
    local_body = local_lines[1:] if len(local_lines) > 1 else []
    nas_body = nas_lines[1:] if len(nas_lines) > 1 else []

    # Normalize: strip trailing whitespace, skip blank-only lines for dedup
    def normalize(line):
        return line.rstrip("\n").rstrip()

    seen = set()
    merged_body = []

    # Add all non-blank, unique lines from both files (local first, then NAS)
    for line in local_body + nas_body:
        norm = normalize(line)
        if norm and norm not in seen:
            seen.add(norm)
            merged_body.append(line.rstrip("\n"))
        elif not norm and (not merged_body or merged_body[-1] != ""):
            # Single blank line as separator
            merged_body.append("")

    # Save backup of NAS version before overwriting
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_name = f"{os.path.basename(nas_path)}.merged-{ts}"
    safe_copy2(nas_path, os.path.join(os.path.dirname(nas_path), backup_name))

    merged_content = header + "\n" + "\n".join(merged_body) + "\n"
    return merged_content, backup_name


def _jsonl_identity(line):
    """Stable dedup identity for one jsonl transcript record.

    Prefers an explicit id-like field (id/uuid/blockId/messageId) so that the
    same message is recognized regardless of JSON key ordering or whitespace.
    Falls back to compact sorted JSON, then to the raw line.
    """
    s = line.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return s
    if isinstance(obj, dict):
        for k in ("id", "uuid", "blockId", "messageId"):
            if k in obj and obj[k] is not None:
                return (k, str(obj[k]))
        try:
            return ("json", json.dumps(obj, sort_keys=True, ensure_ascii=False))
        except Exception:
            return s
    return s


def merge_jsonl(local_path, nas_path):
    """Union two conversation transcript jsonl files with NO data loss.

    Each line is a message/tool-call block. We keep every unique record, local
    lines first then NAS-unique lines (so local order is preserved and the
    other PC's appended messages are appended after). Returns merged content.
    """
    with open(local_path, "r", encoding="utf-8", errors="replace") as f:
        local_lines = f.read().splitlines()
    with open(nas_path, "r", encoding="utf-8", errors="replace") as f:
        nas_lines = f.read().splitlines()

    seen = set()
    merged = []

    def add(line):
        s = line.strip()
        if not s:
            return
        ident = _jsonl_identity(s)
        if ident in seen:
            return
        seen.add(ident)
        merged.append(s)

    for ln in local_lines:
        add(ln)
    for ln in nas_lines:
        add(ln)

    return "\n".join(merged) + ("\n" if merged else "")


# --- Markdown section-level merge (B1) ---
# Identity / memory markdown files (SOUL.md, IDENTITY.md, USER.md, MEMORY.md) are
# mostly edited by *appending* new sections. A whole-file conflict would discard the
# other PC's edits entirely. Instead we split by H1-H3 headings, union all sections,
# and only the genuinely-divergent ones keep the newer side. See SYNC-RISK-ANALYSIS.md B1.
MARKDOWN_MERGE_NAMES = {"SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"}


def is_markdown_merge_file(fname):
    """True for the identity / memory markdown files that should be section-merged."""
    return fname in MARKDOWN_MERGE_NAMES


def _md_key(heading_text):
    m = re.match(r"^#{1,3}\s+(.*\S)\s*$", heading_text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1).strip().lower())


def split_md_sections(text):
    """Split markdown into ordered sections delimited by H1-H3 headings.
    The preamble before the first heading is a section with key=None."""
    lines = text.split("\n")
    sections = []
    cur = {"key": None, "heading": None, "body": []}
    for line in lines:
        if re.match(r"^#{1,3}\s+\S", line):
            sections.append(cur)
            cur = {"key": _md_key(line), "heading": line.rstrip(), "body": []}
        else:
            cur["body"].append(line)
    sections.append(cur)
    return sections


def merge_sections(local_text, nas_text, local_mtime, nas_mtime):
    """Union two markdown files by section heading. Returns (merged_text, diverged).

    - Sections present on only one side are kept.
    - Sections on both sides with identical bodies are kept once.
    - Sections on both sides with different bodies -> keep the side with the later
      mtime; `diverged` is set True so a NAS backup of the superseded side is kept.
    """
    local_secs = split_md_sections(local_text)
    nas_secs = split_md_sections(nas_text)

    local_map = {}
    for s in local_secs:
        local_map.setdefault(s["key"], s)
    nas_map = {}
    for s in nas_secs:
        nas_map.setdefault(s["key"], s)

    merged = []
    used = set()
    diverged = False

    # Walk local order first, resolving overlaps by newer mtime.
    for s in local_secs:
        k = s["key"]
        if k in nas_map:
            used.add(k)
            ns = nas_map[k]
            lb = "\n".join(s["body"]).strip()
            nb = "\n".join(ns["body"]).strip()
            if lb == nb:
                merged.append(s)
            else:
                diverged = True
                merged.append(ns if nas_mtime > local_mtime else s)
        else:
            merged.append(s)

    # Append sections that exist only on the NAS side.
    for s in nas_secs:
        if s["key"] not in used:
            used.add(s["key"])
            merged.append(s)

    out = []
    for s in merged:
        if s["heading"] is not None:
            out.append(s["heading"])
        out.extend(s["body"])
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse runs of blank lines
    return text, diverged


def merge_markdown(local_path, nas_path):
    """Section-merge two markdown files. Returns (merged_text, backup_name_or_None).
    When a section genuinely diverged, the SUPERSEDED (older) side's full file is
    saved as a `.merged-<ts>` backup on the NAS so its content stays recoverable."""
    with open(local_path, "r", encoding="utf-8") as f:
        local_text = f.read()
    with open(nas_path, "r", encoding="utf-8") as f:
        nas_text = f.read()
    if local_text == nas_text:
        return local_text, None
    local_mtime = os.path.getmtime(local_path)
    nas_mtime = os.path.getmtime(nas_path)
    merged_text, diverged = merge_sections(local_text, nas_text, local_mtime, nas_mtime)
    backup_name = None
    if diverged:
        # The older side is the one whose diverged section gets dropped -> back IT up.
        loser_path = local_path if nas_mtime > local_mtime else nas_path
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{os.path.basename(loser_path)}.merged-{ts}"
        backup_dir = os.path.dirname(nas_path)
        os.makedirs(backup_dir, exist_ok=True)
        safe_copy2(loser_path, os.path.join(backup_dir, backup_name))
    return merged_text, backup_name


def _write_both(local_path, nas_path, text):
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(nas_path, "w", encoding="utf-8") as f:
        f.write(text)


def resolve_conflict(local_path, nas_path, key, state):
    """Resolve a both-changed / cold-start conflict. Order of preference:
    daily log -> jsonl -> markdown section merge (B1) -> keep-newer-with-backup."""
    global conflicts
    fname = os.path.basename(nas_path)
    local_exists = os.path.isfile(local_path)
    nas_exists = os.path.isfile(nas_path)

    # --- Daily log files: AUTO-MERGE both sides (line dedup) ---
    if DAILY_LOG_RE.match(fname):
        try:
            merged_text, backup_name = merge_daily_log(local_path, nas_path)
            _write_both(local_path, nas_path, merged_text)
            state[key] = file_hash(nas_path)
            info = f"{key} (merged both, saved NAS backup as {backup_name})"
            merged.append(info)
            return f"MERGED: {info}"
        except Exception:
            pass

    # --- Conversation transcript jsonl: UNION both sides (no data loss) ---
    if JSONL_RE.search(fname):
        try:
            merged_text = merge_jsonl(local_path, nas_path)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_name = f"{fname}.merged-{ts}"
            safe_copy2(nas_path, os.path.join(os.path.dirname(nas_path), backup_name))
            _write_both(local_path, nas_path, merged_text)
            state[key] = file_hash(nas_path)
            info = f"{key} (jsonl merged both, saved NAS backup as {backup_name})"
            merged.append(info)
            return f"MERGED: {info}"
        except Exception:
            pass

    # --- Identity / memory markdown: SECTION-LEVEL merge (B1) ---
    if is_markdown_merge_file(fname):
        try:
            merged_text, backup_name = merge_markdown(local_path, nas_path)
            _write_both(local_path, nas_path, merged_text)
            state[key] = file_hash(nas_path)
            suffix = f", saved NAS backup as {backup_name}" if backup_name else ", no conflicting sections"
            info = f"{key} (markdown-merged both{suffix})"
            merged.append(info)
            return f"MERGED: {info}"
        except Exception:
            pass

    # --- All other files: keep newer, save conflict backup ---
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    conflict_name = f"{fname}.conflict-{ts}"
    conflict_path = os.path.join(os.path.dirname(nas_path), conflict_name)
    safe_copy2(nas_path, conflict_path)
    local_mtime = os.path.getmtime(local_path) if local_exists else 0
    nas_mtime = os.path.getmtime(nas_path) if nas_exists else 0
    local_size = os.path.getsize(local_path) if local_exists else 0
    nas_size = os.path.getsize(nas_path) if nas_exists else 0
    # Safety guard: never let a 0-byte (truncated/empty) side win over a non-empty
    # one. SMB oplock truncation can leave a fresh-mtime 0-byte file on either side.
    if local_size > 0 and nas_size == 0:
        keep_local = True
    elif nas_size > 0 and local_size == 0:
        keep_local = False
    else:
        keep_local = (local_mtime >= nas_mtime)
    if keep_local:
        safe_copy2(local_path, nas_path)
        state[key] = file_hash(local_path) if local_exists else None
        info = f"{key} (kept local v, saved NAS as {conflict_name})"
    else:
        safe_copy2(nas_path, local_path)
        state[key] = file_hash(nas_path) if nas_exists else None
        info = f"{key} (kept NAS v, saved NAS as {conflict_name})"
    conflicts.append(info)
    return f"CONFLICT: {info}"


def sync_single_file(local_path, nas_path, state, key):
    """Sync one file bidirectionally. Returns action string."""
    global skipped, conflicts

    local_exists = os.path.isfile(local_path)
    nas_exists = os.path.isfile(nas_path)

    if not local_exists and not nas_exists:
        skipped += 1
        return "skip"

    last_hash = state.get(key)
    local_h = file_hash(local_path) if local_exists else None
    nas_h = file_hash(nas_path) if nas_exists else None

    # --- Cold start: no prior state ---
    if last_hash is None:
        # Only local exists → push
        if local_exists and not nas_exists:
            # Local-only file in cold start. If tombstoned (deleted on another PC)
            # and not newer than the tombstone, trash it instead of pushing.
            res = _ws_tombstone_intercept(key, local_path, TRASH_LOCAL, state)
            if res:
                return res
            os.makedirs(os.path.dirname(nas_path), exist_ok=True)
            safe_copy2(local_path, nas_path)
            state[key] = local_h
            pushed.append(key)
            return "pushed -> NAS (cold start)"
        # Only NAS exists → pull
        if nas_exists and not local_exists:
            # NAS-only file in cold start. If tombstoned and not newer, trash the
            # NAS copy instead of pulling it back (fixes the "deletion bounces" bug).
            res = _ws_tombstone_intercept(key, nas_path, TRASH_NAS, state)
            if res:
                return res
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            safe_copy2(nas_path, local_path)
            state[key] = nas_h
            pulled.append(key)
            return "pulled <- NAS (cold start)"
        # Both exist, same hash → record and skip
        if local_h == nas_h:
            state[key] = local_h
            skipped += 1
            return "skip (identical, cold start)"
        # Both exist, different hash → genuine conflict (e.g. both PCs initialized separately)
        return resolve_conflict(local_path, nas_path, key, state)
        # --- /Cold start ---

    local_changed = (local_h != last_hash)
    nas_changed = (nas_h != last_hash)

    # Neither changed
    if not local_changed and not nas_changed:
        skipped += 1
        return "skip"

    # Only local changed → push to NAS
    if local_changed and not nas_changed:
        if not local_exists:
            # File exists on NAS (unchanged) but not locally.
            # This means another computer pushed it → pull, not delete.
            # UNLESS it was tombstoned (deleted here) → trash the NAS copy.
            if nas_exists:
                res = _ws_tombstone_intercept(key, nas_path, TRASH_NAS, state)
                if res:
                    return res
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                safe_copy2(nas_path, local_path)
                pulled.append(key)
                return "pulled <- NAS (from other PC)"
            else:
                state.pop(key, None)
                return "state cleanup"
        os.makedirs(os.path.dirname(nas_path), exist_ok=True)
        safe_copy2(local_path, nas_path)
        state[key] = local_h
        pushed.append(key)
        return "pushed -> NAS"

    # Only NAS changed → pull from NAS
    if nas_changed and not local_changed:
        if not nas_exists:
            # File exists locally (unchanged) but not on NAS.
            # Another computer may have deleted it → push local copy back.
            # UNLESS it was tombstoned (deleted elsewhere) → trash local copy.
            if local_exists:
                res = _ws_tombstone_intercept(key, local_path, TRASH_LOCAL, state)
                if res:
                    return res
                os.makedirs(os.path.dirname(nas_path), exist_ok=True)
                safe_copy2(local_path, nas_path)
                state[key] = local_h
                pushed.append(key)
                return "pushed -> NAS (restored)"
            else:
                state.pop(key, None)
                return "state cleanup"
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        safe_copy2(nas_path, local_path)
        state[key] = nas_h
        pulled.append(key)
        return "pulled <- NAS"

    # BOTH changed → CONFLICT (strategy depends on file type)
    if local_changed and nas_changed:
        return resolve_conflict(local_path, nas_path, key, state)

    skipped += 1
    return "skip"


def sync_dir_tasks(state):
    """Sync tasks/ directory (session dirs with JSON files). Considers files on
    BOTH sides so NAS-only files (pushed by the other PC) get pulled."""
    task_src = os.path.join(USER_DIR, "tasks")
    task_dst = os.path.join(NAS_ROOT, "tasks")

    if not os.path.isdir(task_src) and not os.path.isdir(task_dst):
        return

    # Get all task JSON files from BOTH sides
    local_files = {}
    if os.path.isdir(task_src):
        for root, dirs, files in os.walk(task_src):
            for fname in files:
                if fname.endswith(".json"):
                    full = os.path.join(root, fname)
                    try:
                        key = "tasks/" + os.path.relpath(full, task_src).replace("\\", "/")
                    except ValueError:
                        print(f"  [warn] 跳过无法求相对路径的条目: {full}")
                        continue
                    local_files[key] = full

    nas_files = {}
    if os.path.isdir(task_dst):
        for root, dirs, files in os.walk(task_dst):
            for fname in files:
                if fname.endswith(".json"):
                    full = os.path.join(root, fname)
                    try:
                        key = "tasks/" + os.path.relpath(full, task_dst).replace("\\", "/")
                    except ValueError:
                        print(f"  [warn] 跳过无法求相对路径的条目: {full}")
                        continue
                    nas_files[key] = full

    all_keys = set(local_files.keys()) | set(nas_files.keys())

    for key in sorted(all_keys):
        local_p = local_files.get(key)
        nas_p = nas_files.get(key)
        if local_p and nas_p:
            result = sync_single_file(local_p, nas_p, state, key)
        elif local_p:
            # New local file, push
            nas_p = os.path.join(NAS_ROOT, key.replace("/", os.sep))
            result = sync_single_file(local_p, nas_p, state, key)
        elif nas_p:
            # New NAS file, pull
            local_p = os.path.join(USER_DIR, key.replace("/", os.sep))
            result = sync_single_file(local_p, nas_p, state, key)
        if result not in ("skip", "skip (identical)", "skip (identical, cold start)"):
            print(f"  {key}: {result}")


def sync_dir_bidirectional(state, local_base, nas_base, prefix, print_each=False):
    """Sync a whole directory tree bidirectionally.

    Unlike a loop over only local files, this collects file keys from BOTH
    sides, so files that exist only on NAS (pushed by the other PC) are
    correctly pulled to the local machine.
    """
    if not os.path.isdir(local_base) and not os.path.isdir(nas_base):
        return 0
    local_files = {}
    if os.path.isdir(local_base):
        for root, dirs, files in os.walk(local_base):
            for fname in files:
                full = os.path.join(root, fname)
                try:
                    rel = os.path.relpath(full, local_base).replace("\\", "/")
                except ValueError:
                    print(f"  [warn] 跳过无法求相对路径的条目: {full}")
                    continue
                local_files[f"{prefix}/{rel}"] = full
    nas_files = {}
    if os.path.isdir(nas_base):
        for root, dirs, files in os.walk(nas_base):
            for fname in files:
                full = os.path.join(root, fname)
                try:
                    rel = os.path.relpath(full, nas_base).replace("\\", "/")
                except ValueError:
                    print(f"  [warn] 跳过无法求相对路径的条目: {full}")
                    continue
                nas_files[f"{prefix}/{rel}"] = full
    all_keys = set(local_files) | set(nas_files)
    changed = 0
    for key in sorted(all_keys):
        rel = key[len(prefix) + 1:]
        local_p = os.path.join(local_base, rel.replace("/", os.sep))
        nas_p = os.path.join(nas_base, rel.replace("/", os.sep))
        result = sync_single_file(local_p, nas_p, state, key)
        if print_each and result not in ("skip", "skip (identical)", "skip (identical, cold start)"):
            print(f"  {rel}: {result}")
            changed += 1
    return changed


def main():
    global skipped

    # --- Fast NAS reachability pre-check (avoid SMB hang on dead UNC path) ---
    host = _nas_host(NAS_ROOT)
    print(f"Checking NAS ({host}) reachability ...")
    if not nas_reachable(host, timeout=2.5):
        print("ERROR: NAS host unreachable within 4s (no answer on TCP 445/139).")
        print("  This machine likely cannot reach the NAS right now. Common causes:")
        print("    - You're on a different network (e.g. home PC vs office NAS IP).")
        print("    - VPN / Tailscale / WireGuard is disconnected.")
        print("    - The NAS is off, or its IP address changed.")
        print("  NAS_ROOT = " + NAS_ROOT)
        print("  Fix: connect to the same network / VPN as the NAS, then re-run.")
        sys.exit(1)

    if not os.path.isdir(NAS_ROOT):
        print("ERROR: NAS host is reachable, but the share path was not found:")
        print("  " + NAS_ROOT)
        print("  Verify the share name and that the subfolder exists.")
        sys.exit(1)

    machine, machine_label = detect_machine()
    print(f"[Machine] 当前运行机: {machine} = {machine_label}")
    if machine not in KNOWN_MACHINES:
        print("  [WARN] COMPUTERNAME 不在 KNOWN_MACHINES 清单，请先在 KNOWN_MACHINES 中登记本机名。")
    print(f"Two-way sync: {machine} <--> NAS")
    print("=" * 50)

    # --- Optional machine guard: refuse to run if a specific machine was required ---
    if REQUIRE_MACHINE and machine != REQUIRE_MACHINE:
        print(f"[ABORT] --require-machine={REQUIRE_MACHINE} 但当前机器是 {machine}，"
              f"拒绝执行以避免在错误机器上误操作。")
        sys.exit(2)

    # --- Cross-machine concurrency lock (advisory) ---
    if not acquire_lock():
        sys.exit(1)
    atexit.register(release_lock)

    state = load_state()

    # --- 1. Identity files ---
    print("\n[Identity files]")
    for fname in ["SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"]:
        local_p = os.path.join(USER_DIR, fname)
        nas_p = os.path.join(NAS_ROOT, "identity", fname)
        result = sync_single_file(local_p, nas_p, state, f"identity/{fname}")
        if result not in ("skip", "skip (identical)"):
            print(f"  {fname}: {result}")

    # --- 2. Project memory files ---
    print("\n[Project memory]")
    local_mem = {}
    if os.path.isdir(WORKBUDDY_ROOT):
        for item in os.listdir(WORKBUDDY_ROOT):
            mem_dir = os.path.join(WORKBUDDY_ROOT, item, ".workbuddy", "memory")
            if os.path.isdir(mem_dir):
                for fname in os.listdir(mem_dir):
                    if fname.endswith(".md") and fname not in local_mem:
                        local_mem[fname] = os.path.join(mem_dir, fname)
    nas_mem_dir = os.path.join(NAS_ROOT, "memory")
    os.makedirs(nas_mem_dir, exist_ok=True)
    nas_mem = {}
    if os.path.isdir(nas_mem_dir):
        for fname in os.listdir(nas_mem_dir):
            if fname.endswith(".md"):
                nas_mem[fname] = os.path.join(nas_mem_dir, fname)
    # Pick a local destination workspace for files that exist only on NAS
    local_target_base = None
    if os.path.isdir(WORKBUDDY_ROOT):
        for item in os.listdir(WORKBUDDY_ROOT):
            md = os.path.join(WORKBUDDY_ROOT, item, ".workbuddy", "memory")
            if os.path.isdir(md):
                local_target_base = md
                break
    if local_target_base is None:
        local_target_base = os.path.join(WORKBUDDY_ROOT, "synced-memory")
        os.makedirs(local_target_base, exist_ok=True)
    mem_changed = 0
    for fname in sorted(set(local_mem) | set(nas_mem)):
        # Always provide a valid path on BOTH sides (never None)
        local_p = local_mem.get(fname) or os.path.join(local_target_base, fname)
        nas_p = nas_mem.get(fname) or os.path.join(nas_mem_dir, fname)
        result = sync_single_file(local_p, nas_p, state, f"memory/{fname}")
        if result not in ("skip", "skip (identical)", "skip (identical, cold start)"):
            print(f"  {fname}: {result}")
            mem_changed += 1
    if mem_changed == 0:
        print("  (all up to date)")

    # --- 3. Task lists ---
    print("\n[Task lists]")
    sync_dir_tasks(state)

    # --- 4. Sync scripts themselves ---
    print("\n[Sync scripts]")
    SCRIPTS_DIR = os.path.join(WORKBUDDY_ROOT, "scripts")
    if os.path.isdir(SCRIPTS_DIR):
        nas_scripts_dir = os.path.join(NAS_ROOT, "scripts")
        os.makedirs(nas_scripts_dir, exist_ok=True)
        # Iterate BOTH local and NAS so NAS-only script files (e.g. sync-config.txt
        # pushed from the other PC) are pulled too — not just files already local.
        local_names = set(os.listdir(SCRIPTS_DIR))
        nas_names = set(os.listdir(nas_scripts_dir)) if os.path.isdir(nas_scripts_dir) else set()
        for fname in sorted(local_names | nas_names):
            if fname.endswith((".py", ".bat", ".txt")):
                local_p = os.path.join(SCRIPTS_DIR, fname)
                nas_p = os.path.join(nas_scripts_dir, fname)
                result = sync_single_file(local_p, nas_p, state, f"scripts/{fname}")
                if result not in ("skip", "skip (identical)"):
                    print(f"  {fname}: {result}")

    # --- 5. Config files (settings.json, mcp.json) ---
    # IMPORTANT: workbuddy.db is DELIBERATELY EXCLUDED from file sync.
    # It is a live SQLite database; a whole-file push/pull across PCs would
    # overwrite the other PC's locally-stored automations / workspaces /
    # session_usage tables (sessions-merge.py only merges the `sessions` table).
    # Cross-PC session history is handled by `sessions-merge.py` (record-level
    # merge of the `sessions` table), NOT by file copy.
    print("\n[Config files]  (workbuddy.db excluded: merged by sessions-merge.py)")
    nas_config_dir = os.path.join(NAS_ROOT, "config")
    os.makedirs(nas_config_dir, exist_ok=True)
    for fname in ["settings.json", "mcp.json"]:
        local_p = os.path.join(USER_DIR, fname)
        nas_p = os.path.join(nas_config_dir, fname)
        result = sync_single_file(local_p, nas_p, state, f"config/{fname}")
        if result not in ("skip", "skip (identical)"):
            print(f"  {fname}: {result}")

    # --- 5.1 Move any legacy workbuddy.db that an earlier script version
    #        pushed to NAS out of the sync path. It is misleading (and would be
    #        re-pulled if the exclusion above were ever removed). Rename to
    #        .LEGACY-IGNORED so it stays recoverable but is never synced.
    legacy_db = os.path.join(nas_config_dir, "workbuddy.db")
    if os.path.isfile(legacy_db):
        try:
            renamed = os.path.join(nas_config_dir, "workbuddy.db.LEGACY-IGNORED")
            if not os.path.isfile(renamed):
                os.rename(legacy_db, renamed)
                print("  (moved legacy NAS workbuddy.db -> workbuddy.db.LEGACY-IGNORED)")
        except OSError as e:
            print(f"  [warn] could not move legacy NAS workbuddy.db: {e}")

    # --- 5.5 User-level skills (self-built, kept in ~/.workbuddy/skills/) ---
    # Additive per-subdirectory sync. Each skill is its own folder; we union the
    # subdir names from BOTH sides and copy whichever files are missing on the
    # other side. Top-level *.json migration markers inside skills/ are excluded
    # (they are local-only bookkeeping, not skill content). Deletions are NOT
    # propagated across machines (matches the additive-merge policy).
    print("\n[User skills]  ~/.workbuddy/skills/  (per-subdir additive; top-level *.json skipped)")
    skills_local = os.path.join(USER_DIR, "skills")
    skills_nas = os.path.join(NAS_ROOT, "skills")
    if os.path.isdir(skills_local) or os.path.isdir(skills_nas):
        os.makedirs(skills_nas, exist_ok=True)
        local_sub = {d for d in (os.listdir(skills_local) if os.path.isdir(skills_local) else [])
                     if os.path.isdir(os.path.join(skills_local, d))}
        nas_sub = {d for d in (os.listdir(skills_nas) if os.path.isdir(skills_nas) else [])
                   if os.path.isdir(os.path.join(skills_nas, d))}
        skills_changed = 0
        for name in sorted(local_sub | nas_sub):
            lb = os.path.join(skills_local, name)
            nb = os.path.join(skills_nas, name)
            skills_changed += sync_dir_bidirectional(state, lb, nb, f"skills/{name}", print_each=True)
        if skills_changed == 0:
            print("  (all up to date)")
    else:
        print("  (no skills directory on either side)")

    # --- 6. Session metadata (runtime heartbeat files) ---
    # NOTE: WorkBuddy's /resume list is driven by the `sessions` TABLE inside
    # workbuddy.db, NOT by these runtime JSON heartbeat files. Syncing them
    # does NOT change /resume and risks PID-name collisions between PCs.
    # Cross-PC session history is handled by `sessions-merge.py` (merges the db
    # table) and, more reliably, by WorkBuddy's own cloud account sync.
    # Skipped on purpose.
    print("\n[Session metadata]  (skipped: /resume reads workbuddy.db, not these files)")

    # --- 6.5 Conversation transcripts (THE real fix for "no conversation" on other PC) ---
    # WorkBuddy stores the actual chat transcript per session in:
    #   ~/.workbuddy/projects/<workspace-path-encoded>/<sessionId>.jsonl
    # These are KEYED BY sessionId (the same id merged by sessions-merge.py) and the
    # workspace sub-dir name encodes the absolute path (identical on both PCs since
    # both use %USERPROFILE%\WorkBuddy\...). So syncing this dir is fully portable and
    # is what the UI reads when you open a session / click a task.
    # Synced ADDITIVELY (copy what the other side lacks; never delete/overwrite local).
    print("\n[Conversation transcripts]  ~/.workbuddy/projects/  (keyed by sessionId)")
    proj_local = os.path.join(USER_DIR, "projects")
    proj_nas = os.path.join(NAS_ROOT, "projects")
    if os.path.isdir(proj_local) or os.path.isdir(proj_nas):
        os.makedirs(proj_nas, exist_ok=True)
        n = sync_dir_bidirectional(state, proj_local, proj_nas, "projects", print_each=True)
        if n == 0:
            print("  (all up to date)")
    else:
        print("  (no projects directory on either side)")

    # NOTE: ~/.workbuddy/traces/<pid>/ is telemetry/debug data keyed by the OS process
    # id (ephemeral) and is NOT what the UI renders. It is intentionally NOT synced to
    # avoid pushing ~268MB of non-rendering data every run.

    save_state(state)

    # --- 7. Workspace output files (bidirectional, INCLUDING NAS-only workspaces) ---
    print("\n[Workspace files]")
    SKIP_DIRS = {".workbuddy", "node_modules", ".git", "__pycache__",
                 "dist", "build", ".next", ".cache", ".vscode", "__pypackages__",
                 # Python virtual environments: machine-specific, regenerable
                 # from requirements (like node_modules); syncing them pushes
                 # thousands of site-packages files over SMB and looks frozen.
                 ".venv", ".venv2", "venv", ".env", "env",
                 # Crash dumps written by local services (e.g. .dmp files from
                 # background tools). These are transient + machine-specific +
                 # cleaned up by the running service; a .dmp vanishing mid-sync
                 # caused a TOCTOU crash. Never sync this directory.
                 "dumps"}
    SKIP_EXT = {".pyc", ".pyo", ".log", ".tmp", ".dmp"}
    IGNORE_PATTERNS = load_ignore_patterns()
    if IGNORE_PATTERNS:
        print(f"  ignore-list active: {len(IGNORE_PATTERNS)} pattern(s) from sync-ignore.txt")
    ws_count = 0

    # --- v1.1: workspace-files deletion tombstones (deletions propagate) ---
    global WS_TOMBS
    WS_TOMBS = load_ws_tombs()
    prev_inv = load_local_inv()      # keys present on THIS machine at last run
    pending_tombs = {}               # deletions detected this run (key -> delete_ms)
    current_inv = set()              # keys present locally this run

    def collect_ws_files(base_dir, ws_name=""):
        out = {}
        if not os.path.isdir(base_dir):
            return out
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if os.path.splitext(fname)[1] in SKIP_EXT:
                    continue
                full = os.path.join(root, fname)
                try:
                    rel = os.path.relpath(full, base_dir)
                except ValueError:
                    # 跳过无法求相对路径的条目（如 Windows 保留设备名 nul/con/prn/aux…，
                    # 或跨挂载点路径），避免单条坏路径中断整轮同步。
                    print(f"  [warn] 跳过无法求相对路径的条目: {full}")
                    continue
                # NOTE: ignore patterns in sync-ignore.txt are written relative to the
                # workspace-FILES root and INCLUDE the workspace-name prefix
                # (e.g. "2026-01-15-10-30-00/mytask/decrypted/"), but `rel` here is
                # relative to the individual workspace folder (no prefix). Prefix it
                # back so is_ignored() can match the documented patterns.
                ig_rel = ((ws_name + "/") if ws_name else "") + rel.replace(os.sep, "/")
                if IGNORE_PATTERNS and is_ignored(ig_rel.lower(), IGNORE_PATTERNS):
                    continue
                # NOTE: keys must be stored in forward-slash form so the tombstone
                # deletion check (`rel in local_files`, where rel is sliced from a
                # forward-slash key) matches correctly. On Windows os.path.relpath
                # returns backslash paths, which would otherwise never match the
                # forward-slash rel and cause EVERY present file to be falsely
                # recorded as a "local deletion" (data-loss bug).
                out[rel.replace(os.sep, "/")] = full
        return out

    nas_ws_root = os.path.join(NAS_ROOT, "workspace-files")
    local_names = set(os.listdir(WORKBUDDY_ROOT)) if os.path.isdir(WORKBUDDY_ROOT) else set()
    nas_names = set(os.listdir(nas_ws_root)) if os.path.isdir(nas_ws_root) else set()
    # Union of both sides, minus scripts (handled in section 4); keep only dirs that exist on at least one side
    all_ws = (local_names | nas_names) - {"scripts"}
    all_ws = {w for w in all_ws
              if os.path.isdir(os.path.join(WORKBUDDY_ROOT, w))
              or os.path.isdir(os.path.join(nas_ws_root, w))}

    for ws_name in sorted(all_ws):
        local_ws = os.path.join(WORKBUDDY_ROOT, ws_name)
        nas_ws = os.path.join(nas_ws_root, ws_name)
        local_files = collect_ws_files(local_ws, ws_name)
        nas_files = collect_ws_files(nas_ws, ws_name)
        prefix = f"workspace-files/{ws_name}/"
        current_inv |= {prefix + r.replace(os.sep, "/") for r in local_files}
        # Detect genuine LOCAL deletions: keys we tracked last run that are now
        # absent locally (and not ignored) -> record a tombstone so the deletion
        # propagates to NAS + the other PC (instead of being resurrected).
        ws_prev = {k for k in prev_inv if k.startswith(prefix)}
        for k in ws_prev:
            rel = k[len(prefix):]
            if rel in local_files:
                continue
            if IGNORE_PATTERNS and is_ignored(((ws_name + "/") + rel.replace(os.sep, "/")).lower(), IGNORE_PATTERNS):
                continue
            pending_tombs[k] = int(time.time() * 1000)
        ws_total = len(set(local_files) | set(nas_files))
        if ws_total > 200:
            print(f"  [{ws_name}] {ws_total} files to check (virtualenvs are skipped) ...")
        done = 0
        for rel in sorted(set(local_files) | set(nas_files)):
            local_p = local_files.get(rel) or os.path.join(local_ws, rel)
            nas_p = nas_files.get(rel) or os.path.join(nas_ws, rel)
            key = f"workspace-files/{ws_name}/{rel.replace(os.sep, '/')}"
            try:
                result = sync_single_file(local_p, nas_p, state, key)
            except SourceVanished:
                # Source file vanished between enumeration and copy (transient
                # service dump / temp file cleaned up by the running process).
                # Skip it instead of aborting the whole sync.
                vanished.append(rel)
                result = "skip (source vanished)"
                print(f"  WARN skipped (source vanished, transient): {ws_name}/{rel}", flush=True)
            done += 1
            if result.startswith("trashed ->"):
                trashed.append(key)
            if result not in ("skip", "skip (identical)", "skip (identical, cold start)",
                              "skip (source vanished)"):
                print(f"  {ws_name}/{rel}: {result}", flush=True)
                ws_count += 1
            elif ws_total > 200 and done % 200 == 0:
                print(f"  [{ws_name}] checked {done}/{ws_total} ...", flush=True)
    if ws_count == 0:
        print("  (all up to date)")

    # Persist recorded deletions into the cross-PC tombstone store (NAS canonical +
    # local mirror) and update this machine's local inventory snapshot.
    if pending_tombs:
        WS_TOMBS.update(pending_tombs)
        print(f"  [tombstone] recorded {len(pending_tombs)} local deletion(s) for propagation")
    save_ws_tombs(WS_TOMBS)
    save_local_inv(current_inv)

    save_state(state)
    print("\n" + "=" * 50)
    print("SUMMARY")
    print(f"  Pushed -> NAS:  {len(pushed)} files")
    print(f"  Pulled <- NAS:  {len(pulled)} files")
    print(f"  Merged:         {len(merged)} files")
    print(f"  Conflicts:      {len(conflicts)} files")
    print(f"  Skipped:        {skipped} files")
    if vanished:
        print(f"  Skipped (source vanished): {len(vanished)} files (transient, e.g. service dumps)")
    if trashed:
        print(f"  Trashed (deleted via tombstone): {len(trashed)} file(s) -> .trash/ (recoverable)")
    if merged:
        print("\n  MERGED (both sides' content preserved):")
        for m in merged:
            print(f"    - {m}")
    if conflicts:
        print("\n  CONFLICTS (newer version kept, old saved as .conflict-*):")
        for c in conflicts:
            print(f"    - {c}")

    total = len(pushed) + len(pulled)
    print(f"\nDone. {total} files synced.")


if __name__ == "__main__":
    # Parse optional machine guard BEFORE any work happens, so machine-specific
    # one-off ops (e.g. cleanup) can't run on the wrong PC.
    if "--require-machine" in sys.argv:
        i = sys.argv.index("--require-machine")
        if i + 1 < len(sys.argv):
            REQUIRE_MACHINE = sys.argv[i + 1].strip().upper()
    main()
