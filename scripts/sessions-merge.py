# sessions-merge.py
# Merge WorkBuddy tables across PCs via a NAS canonical store, so:
#   - `sessions`  -> /resume lists ALL sessions created on every PC
#   - `automations` / `automation_runtime_state` -> automations are visible & runnable
#     on BOTH PCs (C1: fixes the old "automations don't sync across devices" gap)
#
# IMPORTANT:
#   - Run on EACH PC (while WorkBuddy is CLOSED) after sync-bidirectional.py
#   - Conversation MESSAGE BODIES live locally in
#     ~/.workbuddy/projects/<workspace>/<sessionId>.jsonl (synced by
#     sync-bidirectional.py). They are NOT fetched from the cloud.
#   - This script also ENSURES each merged session's working directory (cwd)
#     exists locally. Sessions whose workspace dir is empty (chat-only) would
#     otherwise trigger "working directory may have been renamed or deleted"
#     on the other PC, because file-based sync never creates empty dirs.
#   - Safe: only mkdir (never overwrite). Deleted sessions' orphan files are
#     MOVED to a .trash/ dir (local + NAS), recoverable, never hard-deleted.
import os
import sqlite3
import socket
import shutil
import json
import time

from sync_config import load_nas_root
NAS_ROOT = load_nas_root()
USER_DIR = os.path.join(os.environ["USERPROFILE"], ".workbuddy")
LOCAL_DB = os.path.join(USER_DIR, "workbuddy.db")
CANON_DIR = os.path.join(NAS_ROOT, "sessions-merge")
CANON_DB = os.path.join(CANON_DIR, "canonical.db")

# Deletion-tombstone plumbing (task #4/#5/#6): when a session is deleted on one
# PC, its files must be removed from BOTH the local machine AND the NAS so the
# additive file-sync doesn't resurrect them. They go to a .trash/ dir (recoverable),
# not hard-deleted.
TASKS_DIR = os.path.join(USER_DIR, "tasks")
PROJECTS_DIR = os.path.join(USER_DIR, "projects")
TRASH_LOCAL = os.path.join(USER_DIR, ".trash")
TRASH_NAS = os.path.join(NAS_ROOT, ".trash")
SNAP_FILE = os.path.join(USER_DIR, ".sync_sessions_snapshot.json")


def get_rows(db, table, id_col="id"):
    """Return {id_col_value: row_dict} for `table`, or {} on error/missing db/table."""
    if not os.path.isfile(db):
        return {}
    try:
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = {r[id_col]: dict(r) for r in con.execute(f"SELECT * FROM {table}")}
        con.close()
        return rows
    except Exception as e:
        print(f"  [warn] cannot read {table} from {db}: {e}")
        return {}


def get_columns_for(db, table):
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    con.close()
    return cols


def table_exists_named(con, table):
    r = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return r is not None


def create_schema_named(con, table, ref_db):
    """Create `table` in `con` using the EXACT DDL from ref_db
    (preserves PRIMARY KEY, column types, etc.)."""
    rcon = sqlite3.connect(ref_db)
    sql = rcon.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    rcon.close()
    if sql and sql[0]:
        con.execute(sql[0])  # exact schema incl. PRIMARY KEY + types
        return True
    return False


def merge_choice(a, b):
    """Content: newer `updated_at` wins. Tombstone: a `deleted_at` that is >= the
    other side's content time PROPAGATES the deletion across PCs (fixes the old
    "deletion doesn't sync" gap). An offline edit made after the deletion (content
    time > deletion time) is kept alive -- resurrection is the safe choice."""
    if a is None:
        return b
    if b is None:
        return a
    at, bt = a.get("updated_at") or 0, b.get("updated_at") or 0
    base = a if at >= bt else b
    da, dbd = a.get("deleted_at"), b.get("deleted_at")
    dels = [x for x in (da, dbd) if x]  # truthy deletion times only
    if dels:
        latest_del = max(dels)
        # Compare the deletion time against the SURVIVING (newer-content) side's
        # own updated_at. If a PC edited AFTER the deletion, the edit wins.
        other_content = at if base is a else bt
        if latest_del >= other_content:
            merged = dict(base)
            merged["deleted_at"] = latest_del
            return merged
    return base


def merge_choice_runtime(a, b):
    """For automation_runtime_state: newer `last_run_at` wins."""
    if a is None:
        return b
    if b is None:
        return a
    at, bt = a.get("last_run_at") or 0, b.get("last_run_at") or 0
    return a if at >= bt else b


def ensure_columns(con, table, ref_db, cols):
    """Add columns present in `ref_db`'s `table` but missing from `con`'s table.

    Handles schema drift: WorkBuddy sometimes adds columns (e.g.
    `plugin_context_json`) to tables like `sessions` via a client update,
    while the NAS canonical DB still carries the older schema. `create_from`
    only creates a table when it does NOT exist, so an already-created
    canonical table never gets the new column -> INSERT fails with
    'no column named X'. This ALTERs the canonical table to match."""
    existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    rcon = sqlite3.connect(ref_db)
    ref_cols = {r[1]: (r[2] or "TEXT") for r in rcon.execute(
        f"PRAGMA table_info({table})")}
    rcon.close()
    for c in cols:
        if c not in existing and c in ref_cols:
            con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" {ref_cols[c]}')


def write_rows_table(db, table, id_col, cols, rows, create_from=None):
    con = sqlite3.connect(db)
    if create_from and not table_exists_named(con, table):
        create_schema_named(con, table, create_from)
    # Keep the target table's schema aligned with the reference DB so writes
    # never hit a missing-column error after a client schema update.
    ref = create_from or db
    ensure_columns(con, table, ref, cols)
    colnames = ", ".join(f'"{c}"' for c in cols)
    ph = ", ".join("?" * len(cols))
    for sid, row in rows.items():
        vals = [row.get(c) for c in cols]
        con.execute(
            f"INSERT OR REPLACE INTO {table} ({colnames}) VALUES ({ph})", vals
        )
    con.commit()
    con.close()


def _nas_host(path):
    h = path.replace("\\", "/").strip("/")
    return h.split("/")[0] if "/" in h else h


def ensure_cwd_dirs(local_db, wb_root=None):
    """Create (mkdir, never delete) the working directory (cwd) for every
    session in local_db, but ONLY if it lives under the user's WorkBuddy root.
    Returns the number of directories created.
    Fixes the 'working directory may have been renamed or deleted' warning on
    the other PC for chat-only sessions whose workspace dir was never synced
    (file-based sync does not replicate empty directories)."""
    if wb_root is None:
        wb_root = os.path.join(os.environ["USERPROFILE"], "WorkBuddy")
    wb_root_l = wb_root.lower()
    created = 0
    con = sqlite3.connect(local_db)
    con.row_factory = sqlite3.Row
    # Only NON-deleted sessions: skip soft-deleted ones (deleted_at set), otherwise
    # this resurrects the working dirs of tasks the user already deleted (observed
    # 2026-11-05: 26 empty dirs re-created right after a full orphan cleanup).
    for r in con.execute(
        "SELECT id, cwd, title FROM sessions WHERE deleted_at IS NULL"
    ):
        cwd = r["cwd"]
        if not cwd:
            continue
        # safety: only create dirs under the user's WorkBuddy root
        if not os.path.isabs(cwd) or not cwd.lower().startswith(wb_root_l):
            continue
        if not os.path.isdir(cwd):
            try:
                os.makedirs(cwd, exist_ok=True)
                created += 1
                t = (r["title"] or "")[:30]
                print(f"  created missing cwd: {cwd}  ({t})")
            except OSError as e:
                print(f"  [warn] cannot create {cwd}: {e}")
    con.close()
    return created


# ----------------------------------------------------------------------------
# Deletion tombstones (task #4/#5/#6)
# ----------------------------------------------------------------------------

def _trash_name(base):
    """Timestamped, FLAT name so multiple trashed items never collide. Path
    separators are encoded as '__' (restored on undelete) so a logical path
    like 'projects/ws1/X.jsonl' stays a single top-level entry in the trash
    dir instead of being interpreted as nested folders."""
    safe = base.replace("/", "__").replace("\\", "__")
    return f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}"


def _move_to_trash(src, trash_root, base):
    """Move `src` (file or dir) into `trash_root`, preserving its original
    relative location via `base` (e.g. USER_DIR or NAS_ROOT). The stored name is
    '<timestamp>_<relpath>' with path separators encoded as '__' (e.g.
    '20260831-120000_tasks__<id>' or '20260831-120000_projects__ws__<id>.jsonl')
    so undelete.py can restore it to the exact original path. Returns dest path,
    or None on failure."""
    if not os.path.exists(src):
        return None
    try:
        os.makedirs(trash_root, exist_ok=True)
        logical = os.path.relpath(src, base).replace(os.sep, "/")
        dest = os.path.join(trash_root, _trash_name(logical))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        i = 1
        while os.path.exists(dest):
            dest = os.path.join(trash_root, f"{_trash_name(logical)}_{i}")
            i += 1
        shutil.move(src, dest)
        return dest
    except Exception as e:
        print(f"  [warn] cannot move to trash {src}: {e}")
        return None


def ensure_tombstone_table(db):
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS deleted_sessions "
                "(id TEXT PRIMARY KEY, deleted_at INTEGER)")
    con.commit(); con.close()


def record_tombstone(sid, deleted_at_ms):
    """Persist a deletion tombstone in the dedicated table so it survives a
    hard-delete of the sessions row and propagates across PCs."""
    ensure_tombstone_table(LOCAL_DB)
    con = sqlite3.connect(LOCAL_DB)
    con.execute("INSERT OR REPLACE INTO deleted_sessions (id, deleted_at) VALUES (?,?)",
                (sid, deleted_at_ms))
    con.commit(); con.close()


def _tombstones():
    """Authoritative tombstone set: {session_id: deleted_at_ms}. Reads the
    dedicated deleted_sessions table (local + NAS canonical) and falls back to
    sessions.deleted_at for soft-deletes not yet mirrored. Each source is read
    independently so a missing table doesn't discard the others."""
    tombs = {}
    for db in (LOCAL_DB, CANON_DB):
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(db); con.row_factory = sqlite3.Row
        except Exception as e:
            print(f"  [warn] cannot open {db}: {e}")
            continue
        try:
            for r in con.execute("SELECT id, deleted_at FROM deleted_sessions"):
                tombs[r["id"]] = max(tombs.get(r["id"], 0), r["deleted_at"] or 0)
        except sqlite3.OperationalError:
            pass
        try:
            for r in con.execute(
                    "SELECT id, deleted_at FROM sessions WHERE deleted_at IS NOT NULL"):
                tombs[r["id"]] = max(tombs.get(r["id"], 0), r["deleted_at"] or 0)
        except sqlite3.OperationalError:
            pass
        con.close()
    return tombs


def _mirror_soft_deletes_to_tombstones():
    """Copy this PC's soft-deleted sessions (sessions.deleted_at set) into the
    dedicated deleted_sessions table, so a deletion survives even if WorkBuddy
    later hard-deletes the row."""
    try:
        con = sqlite3.connect(LOCAL_DB); con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, deleted_at FROM sessions WHERE deleted_at IS NOT NULL").fetchall()
        con.close()
    except Exception:
        return
    for r in rows:
        record_tombstone(r["id"], r["deleted_at"] or 0)


def _apply_tombstones_to_local_sessions():
    """Stamp local sessions.deleted_at from the merged tombstone set so the UI
    hides deleted tasks on this PC too. Only sets/updates; never clears."""
    tombs = _tombstones()
    if not tombs:
        return
    try:
        con = sqlite3.connect(LOCAL_DB)
        for sid, del_ms in tombs.items():
            con.execute(
                "UPDATE sessions SET deleted_at=? WHERE id=? "
                "AND (deleted_at IS NULL OR deleted_at<?)",
                (del_ms, sid, del_ms))
        con.commit(); con.close()
    except Exception as e:
        print(f"  [warn] cannot apply tombstones to sessions: {e}")


def _files_exist_for(sid):
    """True if any task/project artifact for `sid` still exists on local disk."""
    if os.path.isdir(os.path.join(TASKS_DIR, sid)):
        return True
    if not os.path.isdir(PROJECTS_DIR):
        return False
    for root, _, files in os.walk(PROJECTS_DIR):
        if f"{sid}.jsonl" in files:
            return True
    return False


def cleanup_deleted_files():
    """For every tombstoned session, remove its orphan files from BOTH the local
    machine and the NAS, moving them to .trash/ (recoverable) instead of
    hard-deleting. A file is only removed when its mtime is OLDER than the
    tombstone (delete-time > file-time rule): an offline edit made after the
    deletion survives (resurrection = safe). Returns number of items trashed."""
    tombs = _tombstones()
    if not tombs:
        return 0

    # map <sessionId> -> jsonl path, local + NAS
    proj_local, proj_nas = {}, {}
    for base, store in ((PROJECTS_DIR, proj_local),
                        (os.path.join(NAS_ROOT, "projects"), proj_nas)):
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for f in files:
                    if f.endswith(".jsonl"):
                        store[f[:-6]] = os.path.join(root, f)

    moved = 0
    for sid, del_ms in tombs.items():
        del_s = del_ms / 1000.0
        # tasks/<sid>/ on local + NAS
        for base_dir, trash, base in ((os.path.join(TASKS_DIR, sid), TRASH_LOCAL, USER_DIR),
                                      (os.path.join(NAS_ROOT, "tasks", sid), TRASH_NAS, NAS_ROOT)):
            if os.path.isdir(base_dir) and os.path.getmtime(base_dir) < del_s:
                dest = _move_to_trash(base_dir, trash, base)
                if dest:
                    moved += 1
                    print(f"  trashed {sid}/ (deleted {del_ms})")
        # projects <sid>.jsonl on local + NAS
        for store, trash, base in ((proj_local, TRASH_LOCAL, USER_DIR), (proj_nas, TRASH_NAS, NAS_ROOT)):
            fp = store.get(sid)
            if fp and os.path.isfile(fp) and os.path.getmtime(fp) < del_s:
                dest = _move_to_trash(fp, trash, base)
                if dest:
                    moved += 1
                    print(f"  trashed {os.path.basename(os.path.dirname(fp))}/{sid}.jsonl")
    return moved


def detect_and_snapshot_hard_deletes():
    """Defense against WorkBuddy hard-deleting a session row (instead of soft
    delete via deleted_at). Compare the current session ids against the last
    run's snapshot; a vanished id whose files are ALSO gone is synthesized into a
    tombstone (deleted_at = now) so it propagates. A large vanish (likely a db
    read glitch) is aborted to avoid mass tombstoning. Returns synthesized count."""
    try:
        con = sqlite3.connect(LOCAL_DB)
        cur_ids = set(r[0] for r in con.execute("SELECT id FROM sessions"))
        con.close()
    except Exception:
        return 0

    prev_ids = set()
    if os.path.isfile(SNAP_FILE):
        try:
            prev_ids = set(json.load(open(SNAP_FILE)).get("ids", []))
        except Exception:
            prev_ids = set()

    synthesized = 0
    vanished = prev_ids - cur_ids
    # Guard: only act on a plausible deletion count, not a db glitch.
    if 0 < len(vanished) <= max(5, int(len(prev_ids) * 0.2)):
        ensure_tombstone_table(LOCAL_DB)
        now = int(time.time() * 1000)
        for sid in vanished:
            if not _files_exist_for(sid):
                try:
                    con = sqlite3.connect(LOCAL_DB)
                    con.execute(
                        "INSERT OR REPLACE INTO deleted_sessions (id, deleted_at) VALUES (?,?)",
                        (sid, now))
                    con.commit()
                    con.close()
                    synthesized += 1
                except Exception as e:
                    print(f"  [warn] cannot synthesize tombstone for {sid}: {e}")
        if synthesized:
            print(f"  synthesized {synthesized} tombstone(s) from vanished session(s)")

    try:
        json.dump({"ids": list(cur_ids)}, open(SNAP_FILE, "w"))
    except Exception:
        pass
    return synthesized


def nas_reachable(host, timeout=4.0):
    """Return True if the SMB host answers on TCP 445/139 within `timeout`.
    A UNC path that is unreachable makes os.path.isfile() hang for minutes;
    this TCP probe fails fast instead."""
    for port in (445, 139):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def main():
    if not os.path.isfile(LOCAL_DB):
        print("Local workbuddy.db not found; nothing to do.")
        return

    # --- Fast NAS reachability pre-check (avoid SMB hang on dead UNC path) ---
    host = _nas_host(NAS_ROOT)
    print(f"Checking NAS ({host}) reachability ...")
    if not nas_reachable(host, timeout=2.5):
        print("ERROR: NAS host unreachable within 4s (no answer on TCP 445/139).")
        print("  The sessions-merge store lives on the NAS; cannot proceed.")
        print("  NAS_ROOT = " + NAS_ROOT)
        print("  Connect to the same network / VPN as the NAS, then re-run.")
        return

    # --- Cross-machine concurrency lock (same lock as sync-bidirectional.py) ---
    # Reuse the lock so the whole one-click flow (sync + merge) is mutually
    # exclusive across PCs and cannot clobber the NAS canonical store / files.
    import importlib.util as _ilu
    import atexit as _atexit
    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = _ilu.spec_from_file_location(
        "_sync_bidirectional", os.path.join(_here, "sync-bidirectional.py"))
    _sync = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_sync)
    if not _sync.acquire_lock():
        return
    _atexit.register(_sync.release_lock)

    os.makedirs(CANON_DIR, exist_ok=True)
    ensure_tombstone_table(LOCAL_DB)

    # Tables to merge across PCs. Each entry: (table, id_col, choice_fn).
    #  - sessions                  : conversation index (fixes /resume list)
    #  - automations               : automation DEFINITIONS (C1) -> visible & runnable on both PCs
    #  - automation_runtime_state  : last-run / running state for those automations
    #  - deleted_sessions          : DELETION TOMBSTONES (task #4/#5/#6) -> a delete on one
    #                               PC propagates to the other instead of being resurrected.
    # automation_runs (per-run execution history) is intentionally NOT merged, to avoid
    # bloating the canonical store with logs that are not needed for automations to work.
    MERGE_TABLES = [
        ("sessions", "id", merge_choice),
        ("automations", "id", merge_choice),
        ("automation_runtime_state", "automation_id", merge_choice_runtime),
        ("deleted_sessions", "id", merge_choice),
    ]

    for table, id_col, choice in MERGE_TABLES:
        con = sqlite3.connect(LOCAL_DB)
        has = table_exists_named(con, table)
        con.close()
        if not has:
            print(f"  [skip] {table}: not present in local db")
            continue
        local_rows = get_rows(LOCAL_DB, table, id_col)
        canon_rows = get_rows(CANON_DB, table, id_col)
        merged = dict(canon_rows)
        for rid, row in local_rows.items():
            merged[rid] = choice(merged.get(rid), row)
        cols = get_columns_for(LOCAL_DB, table)
        if not cols:
            print(f"  [skip] {table}: no columns")
            continue
        write_rows_table(CANON_DB, table, id_col, cols, merged, create_from=LOCAL_DB)
        write_rows_table(LOCAL_DB, table, id_col, cols, merged)
        print(f"  merged {table}: {len(merged)} total (local {len(local_rows)}, canon {len(canon_rows)})")

    # --- Deletion tombstones: propagate deletions across PCs (task #4/#5/#6) ---
    # 0) Mirror this PC's soft-deletes (sessions.deleted_at) into the dedicated
    #    tombstone table, so a deletion survives even a later hard-delete of the row.
    _mirror_soft_deletes_to_tombstones()
    # 1) Snapshot-based hard-delete detection (defense if WorkBuddy removes the
    #    row instead of soft-deleting). Aborts on a suspicious mass-vanish.
    synth = detect_and_snapshot_hard_deletes()
    if synth:
        print(f"Synthesized {synth} tombstone(s) from hard-deleted session(s).")
    # 2) Apply the (now merged) tombstones back onto local sessions.deleted_at so
    #    the UI hides deleted tasks on THIS pc too.
    _apply_tombstones_to_local_sessions()
    # 3) Remove orphaned files of deleted sessions from local + NAS, into .trash/.
    moved = cleanup_deleted_files()
    if moved:
        print(f"Moved {moved} deleted task file(s) to .trash/ (recoverable; see "
              f"~/.workbuddy/.trash and the NAS .trash).")

    # --- Ensure every merged session's working directory (cwd) exists locally ---
    # Fixes "该对话的工作目录可能已被重命名或删除" on the other PC for sessions
    # whose workspace dir is empty (file sync never creates empty dirs).
    created = ensure_cwd_dirs(LOCAL_DB)

    if created:
        print(f"Created {created} missing working-dir(s) so sessions open without the 'deleted workspace' warning.")

    print("Now restart WorkBuddy -> /resume should list all sessions from every PC,")
    print("and your automations should appear on both PCs.")


if __name__ == "__main__":
    main()
