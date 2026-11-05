# tasks-report.py
# Generate a single self-contained HTML page listing ALL WorkBuddy tasks
# across every session, grouped by session. Run on either PC after sync.
import os
import json
import html
from datetime import datetime, timezone

USER_DIR = os.path.join(os.environ["USERPROFILE"], ".workbuddy")
TASKS_DIR = os.path.join(USER_DIR, "tasks")
SESSIONS_DIR = os.path.join(USER_DIR, "sessions")
OUT_PATH = os.path.join(os.environ["USERPROFILE"], "WorkBuddy", "TaskReports", "tasks-report.html")


def ts_to_str(ms):
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def load_sessions():
    """Map sessionId -> {cwd, hostname, startedAt}."""
    mapping = {}
    if not os.path.isdir(SESSIONS_DIR):
        return mapping
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        sid = data.get("sessionId")
        if not sid:
            continue
        mapping[sid] = {
            "cwd": data.get("cwd", ""),
            "hostname": data.get("hostname", ""),
            "startedAt": data.get("startedAt", 0),
            "mode": data.get("mode", ""),
        }
    return mapping


STATUS_LABEL = {
    "pending": "待办",
    "in_progress": "进行中",
    "completed": "已完成",
    "deleted": "已删除",
}
STATUS_COLOR = {
    "pending": "#f59e0b",
    "in_progress": "#2563eb",
    "completed": "#16a34a",
    "deleted": "#9ca3af",
}


def main():
    sessions = load_sessions()
    groups = []  # list of (sessionId, meta, [tasks])

    if os.path.isdir(TASKS_DIR):
        for sid in sorted(os.listdir(TASKS_DIR)):
            sdir = os.path.join(TASKS_DIR, sid)
            if not os.path.isdir(sdir):
                continue
            tasks = []
            for tf in sorted(os.listdir(sdir)):
                if not tf.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(sdir, tf), encoding="utf-8") as f:
                        t = json.load(f)
                except Exception:
                    continue
                tasks.append(t)
            if not tasks:
                continue
            meta = sessions.get(sid, {})
            groups.append((sid, meta, tasks))

    # Sort groups by startedAt desc (most recent session first)
    groups.sort(key=lambda g: g[1].get("startedAt", 0), reverse=True)

    # Stats
    total = sum(len(g[2]) for g in groups)
    by_status = {}
    for _, _, tasks in groups:
        for t in tasks:
            st = t.get("status", "pending")
            by_status[st] = by_status.get(st, 0) + 1

    # Build HTML
    parts = []
    parts.append("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 任务汇总</title>
<style>
:root{ --bg:#f7f8fa; --card:#fff; --border:#e5e7eb; --text:#1f2937; --muted:#6b7280; --accent:#2563eb; }
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;gap:12px;justify-content:space-between;margin-bottom:8px}
h1{font-size:22px;margin:0}
.sub{color:var(--muted);font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.chip{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:8px 14px;font-size:13px}
.chip b{font-size:18px;display:block}
input#q{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:10px;font-size:14px;background:var(--card);margin:8px 0 20px}
.group{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.group h2{font-size:15px;margin:0 0 4px}
.group .meta{color:var(--muted);font-size:12px;margin-bottom:12px;word-break:break-all}
.task{border-top:1px solid var(--border);padding:10px 0;display:flex;gap:10px;align-items:flex-start}
.task:first-of-type{border-top:none}
.badge{flex:none;font-size:12px;color:#fff;border-radius:999px;padding:2px 10px;margin-top:2px}
.task .body{flex:1}
.task .subj{font-weight:600}
.task .desc{color:var(--muted);font-size:13px;margin-top:2px;white-space:pre-wrap}
.task .time{color:var(--muted);font-size:11px;margin-top:4px}
.empty{color:var(--muted);text-align:center;padding:40px}
</style></head><body><div class="wrap">""")

    parts.append(
        f'<header><div><h1>WorkBuddy 任务汇总</h1>'
        f'<div class="sub">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")} · '
        f'共 {total} 个任务 / {len(groups)} 个会话</div></div></header>'
    )

    # stat chips
    parts.append('<div class="stats">')
    parts.append(f'<div class="chip">总任务<b>{total}</b></div>')
    parts.append(f'<div class="chip">会话<b>{len(groups)}</b></div>')
    for st in ["in_progress", "pending", "completed", "deleted"]:
        if st in by_status:
            parts.append(f'<div class="chip">{STATUS_LABEL.get(st, st)}<b>{by_status[st]}</b></div>')
    parts.append("</div>")

    parts.append('<input id="q" placeholder="搜索任务标题或描述…" oninput="filter()">')

    if not groups:
        parts.append('<div class="empty">没有找到任何任务。</div>')
    else:
        for sid, meta, tasks in groups:
            cwd = meta.get("cwd", "")
            ws = os.path.basename(cwd) if cwd else "(未记录工作区)"
            host = meta.get("hostname", "")
            started = ts_to_str(meta.get("startedAt", 0))
            head = f"{ws}"
            if host:
                head += f" · {host}"
            if started and started != "-":
                head += f" · {started}"
            parts.append('<div class="group">')
            parts.append(f'<h2>{html.escape(head)}</h2>')
            parts.append(f'<div class="meta">sessionId: {html.escape(sid)}'
                         + (f' · 工作区: {html.escape(cwd)}' if cwd else '') + '</div>')
            for t in sorted(tasks, key=lambda x: x.get("createdAt", 0)):
                st = t.get("status", "pending")
                label = STATUS_LABEL.get(st, st)
                color = STATUS_COLOR.get(st, "#6b7280")
                subj = html.escape(t.get("subject", "(无标题)"))
                desc = html.escape(t.get("description", ""))
                created = ts_to_str(t.get("createdAt", 0))
                parts.append('<div class="task">')
                parts.append(f'<span class="badge" style="background:{color}">{label}</span>')
                parts.append('<div class="body">')
                parts.append(f'<div class="subj">{subj}</div>')
                if desc:
                    parts.append(f'<div class="desc">{desc}</div>')
                parts.append(f'<div class="time">创建：{created}</div>')
                parts.append('</div></div>')
            parts.append('</div>')

    parts.append("""<script>
function filter(){
  var q=document.getElementById('q').value.toLowerCase();
  document.querySelectorAll('.group').forEach(function(g){
    var hit=false;
    g.querySelectorAll('.task').forEach(function(t){
      var txt=t.innerText.toLowerCase();
      var show=txt.indexOf(q)>-1;
      t.style.display=show?'':'none';
      if(show)hit=true;
    });
    g.style.display=hit?'':'none';
  });
}
</script></div></body></html>""")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    print(f"OK: report written -> {OUT_PATH}")
    print(f"     {total} tasks across {len(groups)} sessions")


if __name__ == "__main__":
    main()
