#!/usr/bin/env python3
# undelete.py - 救回被删除(已移入回收站)的任务。
#
# 工作流：
#   1) 列出所有已删除任务（本地 + NAS 的 sessions.deleted_at 与 deleted_sessions 墓碑表），
#      同时显示每个任务的标题，便于判断要救回哪个。
#   2) 你选择要救回的任务（编号 / a=全部 / q=取消）。
#   3) 对每项：从本地 + NAS 回收站把文件移回原位置，并清除墓碑，
#      这样下次同步(sync-all)不会再把它删掉。
#
# 用法：
#   python undelete.py            # 交互式
#   python undelete.py <任务ID>   # 直接救回指定任务（可多个，空格分隔）

import os
import sys
import time
import sqlite3
import shutil
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("sm", os.path.join(HERE, "sessions-merge.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

USER_DIR = sm.USER_DIR
NAS_ROOT = sm.NAS_ROOT
LOCAL_DB = sm.LOCAL_DB
CANON_DB = sm.CANON_DB
TRASH_LOCAL = sm.TRASH_LOCAL
TRASH_NAS = sm.TRASH_NAS
nas_reachable = sm.nas_reachable


def list_deleted():
    """合并本地 + NAS 的 sessions.deleted_at 与 deleted_sessions 墓碑表，
    返回 {任务ID: {'del': 删除时间毫秒, 'title': 标题或None}}。"""
    # 先收集标题（来自 sessions 表，本地 + NAS 都查；硬删除导致行消失则无标题）
    titles = {}
    for db in (LOCAL_DB, CANON_DB):
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(db)
            try:
                for sid, t in con.execute("SELECT id, title FROM sessions"):
                    if t:
                        titles[sid] = t
            except sqlite3.OperationalError:
                pass
            con.close()
        except Exception:
            pass
    # 再收集删除时间（sessions 软删 + deleted_sessions 墓碑表），并合并标题
    deld = {}
    for db in (LOCAL_DB, CANON_DB):
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(db)
            try:
                for sid, ts in con.execute(
                        "SELECT id, deleted_at FROM sessions WHERE deleted_at IS NOT NULL"):
                    if not ts:
                        continue
                    cur = deld.get(sid, {"del": 0, "title": None})
                    cur["del"] = max(cur["del"], ts or 0)
                    cur["title"] = titles.get(sid) or cur["title"]
                    deld[sid] = cur
            except sqlite3.OperationalError:
                pass
            try:
                for sid, ts in con.execute("SELECT id, deleted_at FROM deleted_sessions"):
                    if not ts:
                        continue
                    cur = deld.get(sid, {"del": 0, "title": None})
                    cur["del"] = max(cur["del"], ts or 0)
                    cur["title"] = titles.get(sid) or cur["title"]
                    deld[sid] = cur
            except sqlite3.OperationalError:
                pass
            con.close()
        except Exception as e:
            print(f"  [warn] 读取 {db} 失败: {e}")
    return deld


def _logical_from_trash_name(name):
    """回收站文件名形如 '<时间戳>_<相对路径>',路径分隔符被编码成 '__'。
    去掉时间戳前缀并把 '__' 还原成 '/',得到相对路径。"""
    parts = name.split("_", 1)
    if len(parts) == 2 and len(parts[0]) == 15 and parts[0][8] == "-":
        return parts[1].replace("__", "/")
    return name.replace("__", "/")


def _find_in_trash(trash_root, sid):
    """在回收站里找出与 sid 相关的项，返回 [(相对路径, 完整路径), ...]。"""
    found = []
    if not os.path.isdir(trash_root):
        return found
    for name in os.listdir(trash_root):
        full = os.path.join(trash_root, name)
        logical = _logical_from_trash_name(name)
        if logical == f"tasks/{sid}" or logical.endswith(f"/{sid}.jsonl"):
            found.append((logical, full))
        elif logical == f"{sid}.jsonl":  # 防御性：异常命名也能救
            found.append((logical, full))
    return found


def restore_file(sid):
    """从本地 + NAS 回收站找回该任务文件，移回原位置。返回恢复项数。"""
    restored = 0
    # 本地回收站 -> 恢复到 USER_DIR/<相对路径>
    for logical, full in _find_in_trash(TRASH_LOCAL, sid):
        dest = os.path.join(USER_DIR, logical)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(full, dest)
            print(f"  恢复本地: {logical}")
            restored += 1
        except Exception as e:
            print(f"  [warn] 恢复本地失败 {logical}: {e}")
    # NAS 回收站 -> 恢复到 NAS_ROOT/<相对路径>（不可达则跳过）
    if nas_reachable(_nas_host_of(NAS_ROOT)):
        for logical, full in _find_in_trash(TRASH_NAS, sid):
            dest = os.path.join(NAS_ROOT, logical)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(full, dest)
                print(f"  恢复NAS: {logical}")
                restored += 1
            except Exception as e:
                print(f"  [warn] 恢复NAS失败 {logical}: {e}")
    else:
        print("  [skip] NAS 不可达，跳过 NAS 回收站恢复（可稍后重跑）")
    return restored


def _nas_host_of(path):
    # UNC 形如 //host/share/...；本地路径返回 None
    if path.startswith("//") or path.startswith("\\\\"):
        return path.lstrip("/\\").split("/")[0]
    return "127.0.0.1"


def clear_tombstone(sid):
    """从 deleted_sessions 表删除墓碑，并清除 sessions.deleted_at（本地 + NAS）。"""
    for db in (LOCAL_DB, CANON_DB):
        if not os.path.isfile(db):
            continue
        try:
            con = sqlite3.connect(db)
            try:
                con.execute("DELETE FROM deleted_sessions WHERE id=?", (sid,))
            except sqlite3.OperationalError:
                pass
            try:
                con.execute("UPDATE sessions SET deleted_at=NULL WHERE id=?", (sid,))
            except sqlite3.OperationalError:
                pass
            con.commit()
            con.close()
        except Exception as e:
            print(f"  [warn] 清除墓碑失败 {db}: {e}")


def main():
    args = sys.argv[1:]
    deld = list_deleted()
    if not deld:
        print("没有找到已删除(已进入回收站 / 已标记删除)的任务。")
        return

    if args:
        picks = [a for a in args if a in deld]
        missing = [a for a in args if a not in deld]
        if missing:
            print(f"以下任务ID不在已删除清单中，已跳过: {', '.join(missing)}")
        if not picks:
            print("没有可救回的任务。")
            return
    else:
        items = sorted(deld.items(), key=lambda x: -x[1]["del"])
        print("已删除任务清单 (编号 | 删除时间 | 标题 | 任务ID):")
        for i, (sid, info) in enumerate(items, 1):
            ds = time.strftime("%Y-%m-%d %H:%M", time.localtime(info["del"] / 1000))
            title = info["title"] or "(标题不可用)"
            title = title.replace("\r", " ").replace("\n", " ").strip()
            if len(title) > 70:
                title = title[:67] + "..."
            print(f"  {i:2d}. [{ds}] {title}")
            print(f"      ID: {sid}")
        print()
        choice = input("输入要救回的任务编号(多个用空格, 全部输入 a, 取消输入 q): ").strip()
        if choice.lower() == "q":
            print("已取消。")
            return
        if choice.lower() == "a":
            picks = list(deld.keys())
        else:
            picks = []
            for tok in choice.split():
                try:
                    idx = int(tok) - 1
                    if 0 <= idx < len(items):
                        picks.append(items[idx][0])
                except ValueError:
                    pass
            if not picks:
                print("没有有效选择，已取消。")
                return

    for sid in picks:
        info = deld.get(sid, {})
        title = (info.get("title") or "(标题不可用)").replace("\r", " ").replace("\n", " ").strip()
        print(f"\n[救回] {sid}  {title}")
        n = restore_file(sid)
        clear_tombstone(sid)
        print(f"  完成 (恢复文件 {n} 项, 墓碑已清除)")


if __name__ == "__main__":
    main()
