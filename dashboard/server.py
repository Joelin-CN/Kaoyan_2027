# -*- coding: utf-8 -*-
"""
考研知识点看板 · 本地服务器（零第三方依赖，Python 3.8+）

用法：python dashboard/server.py  （或双击 start.bat）→ 自动打开 http://127.0.0.1:8787

职责：
  1. 启动时解析 知识点库/*.md → 知识点定义（ID/名称/科目/优先级/依赖/题包/⚡）
  2. 维护 data/state.json（状态唯一真源：掌握度/日期/历史）
  3. 提供 JSON API（抽取/结算/调整/总览/周报）
  4. 状态保存后自动 git commit（学习日志）
"""
import json
import math
import os
import random
import re
import subprocess
import threading
import webbrowser
from datetime import date, datetime, timedelta
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

# ---------------- 路径与常量 ----------------
DASH_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DASH_DIR)                       # 仓库根（考研/）
KB_DIR = os.path.join(ROOT, "03-备考计划", "知识点库")
DATA_DIR = os.path.join(DASH_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
PORT = 8787
AUTO_COMMIT = True                                     # 保存状态后自动 git commit

MD_FILES = [
    ("数学一-知识点清单.md", {"M": "高数", "L": "线代", "P": "概率"}),
    ("408-知识点清单.md",    {"D": "数据结构", "C": "计组", "O": "操作系统", "N": "网络"}),
]
REVIEW_CYCLE = {3: 7, 4: 30, 5: 90}                    # 掌握度 → 复查周期（天）
REDLINE_UNDERSTAND = date(2027, 2, 10)                 # 理解期红线
UNDERSTAND_TARGET = 0.95                               # 📖≥95%
SUBJECT_FLOOR = 0.70                                   # 每科底线

# ---------------- md 解析 ----------------
_ID_RE = re.compile(r"^[MLPDCON]\d{2}$")


def _parse_deps(cell):
    """解析前置列：'—' / 'M02' / 'M05,M06' / 'M42-M49'（完整ID对）→ ID列表"""
    cell = cell.strip()
    if not cell or cell in ("—", "-", "无"):
        return []
    ids = []
    for part in re.split(r"[，,、/]", cell):
        part = part.strip()
        m = re.match(r"^([A-Z]\d{2})-([A-Z]\d{2})$", part)     # M42-M49
        if m:
            a, b = m.group(1), m.group(2)
            if a[0] == b[0] and int(a[1:]) <= int(b[1:]):
                ids += ["%s%02d" % (a[0], i)
                        for i in range(int(a[1:]), int(b[1:]) + 1)]
                continue
        m = re.match(r"^([A-Z])(\d{2})-(\d{2})$", part)        # M42-49（兼容）
        if m:
            pfx, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            ids += ["%s%02d" % (pfx, i) for i in range(a, b + 1)]
        elif _ID_RE.match(part):
            ids.append(part)
    return ids


def load_defs():
    """解析两个知识点清单 md → {id: {...}}；容错：坏行跳过并收集警告"""
    defs, warns = {}, []
    for fname, prefix_map in MD_FILES:
        path = os.path.join(KB_DIR, fname)
        if not os.path.exists(path):
            warns.append("找不到文件: %s" % fname)
            continue
        with open(path, encoding="utf-8") as f:
            for ln, raw in enumerate(f, 1):
                line = raw.strip()
                if not line.startswith("|"):
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) < 7 or not _ID_RE.match(cells[0]):
                    continue
                pid = cells[0]
                subject = prefix_map.get(pid[0])
                if subject is None:
                    continue
                prio = cells[2][:1]
                name = cells[1].replace("**", "")
                defs[pid] = {
                    "id": pid,
                    "name": name,
                    "subject": subject,
                    "priority": prio if prio in "ABC" else "B",
                    "deps": _parse_deps(cells[3]),
                    "packet": cells[4].replace("**", ""),
                    "zap": "⚡" in raw,
                    "note": cells[6].replace("**", ""),
                }
    return defs, warns


# ---------------- state 管理 ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            print("[警告] state.json 解析失败，可用 git 历史恢复；本次从空状态启动")
    return {"version": 1, "points": {}, "days": {}}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def git_commit(msg):
    if not AUTO_COMMIT:
        return False
    try:
        rel = os.path.relpath(STATE_FILE, ROOT).replace("\\", "/")
        subprocess.run(["git", "add", rel], cwd=ROOT, check=True,
                       capture_output=True, timeout=15)
        r = subprocess.run(["git", "commit", "-m", msg], cwd=ROOT,
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# ---------------- 掌握度 → 协议状态换算 ----------------
def mastery_to_proto(ps):
    """⬜未学 / 🔁薄弱(m≤2) / 📖(m=3) / ✍️(m=4 或 3+题包) / ✅(m=5+题包)；冒烟未过叠⚠️"""
    if not ps:
        return "⬜"
    m = ps.get("mastery", 0)
    if m <= 2:
        st = "🔁"
    elif m == 3:
        st = "✍️" if ps.get("packet_done") else "📖"
    elif m == 4:
        st = "✍️"
    else:
        st = "✅" if ps.get("packet_done") else "✍️"
    smoke = ps.get("smoke") or {}
    if smoke and smoke.get("passed") is False:
        st += "⚠️"
    return st


def days_since(dstr, today):
    if not dstr:
        return None
    try:
        return (today - datetime.strptime(dstr, "%Y-%m-%d").date()).days
    except Exception:
        return None


def overdue_days(ps, today):
    """超过复查周期多少天（负数=未超期）；m≤2 视为永久超期"""
    if not ps:
        return 0
    m = ps.get("mastery", 0)
    if m <= 2:
        return 999
    cycle = REVIEW_CYCLE.get(m, 30)
    d = days_since(ps.get("last_reviewed"), today)
    if d is None:
        return 999
    return d - cycle


# ---------------- 业务计算 ----------------
def point_view(pid, d, state, today):
    ps = state["points"].get(pid)
    v = dict(d)
    if ps:
        v.update({
            "mastery": ps.get("mastery", 0),
            "first_learned": ps.get("first_learned"),
            "last_reviewed": ps.get("last_reviewed"),
            "review_count": ps.get("review_count", 0),
            "smoke": ps.get("smoke"),
            "packet_done": ps.get("packet_done", False),
            "age_days": days_since(ps.get("first_learned"), today),
            "since_review_days": days_since(ps.get("last_reviewed"), today),
            "overdue": overdue_days(ps, today),
            "proto": mastery_to_proto(ps),
            "history": ps.get("history", []),
        })
    else:
        v.update({"mastery": 0, "proto": "⬜", "age_days": None,
                  "since_review_days": None, "overdue": 0,
                  "smoke": None, "packet_done": False, "review_count": 0,
                  "history": []})
    return v


def overview(defs, state, today):
    n = len(defs)
    dist = {"⬜": 0, "🔁": 0, "📖": 0, "✍️": 0, "✅": 0, "⚠️": 0}
    subj = {}
    read_cnt = packet_cnt = 0
    for pid, d in defs.items():
        ps = state["points"].get(pid)
        st = mastery_to_proto(ps)
        base = st.replace("⚠️", "")
        if base in dist:
            dist[base] += 1
        if "⚠️" in st:
            dist["⚠️"] += 1
        m = ps.get("mastery", 0) if ps else 0
        s = subj.setdefault(d["subject"], {"total": 0, "read": 0})
        s["total"] += 1
        if m >= 3:
            s["read"] += 1
            read_cnt += 1
        if m >= 4 or (m == 3 and ps.get("packet_done")):
            packet_cnt += 1
    for s in subj.values():
        s["read_pct"] = round(s["read"] * 100.0 / s["total"], 1) if s["total"] else 0

    # 吞吐：按周统计 first_learned 新增数
    def week_key(dstr):
        try:
            return datetime.strptime(dstr, "%Y-%m-%d").isocalendar()[:2]
        except Exception:
            return None
    weeks = {}
    for pid, ps in state["points"].items():
        wk = week_key(ps.get("first_learned"))
        if wk:
            weeks[wk] = weeks.get(wk, 0) + 1
    this_week = weeks.get(today.isocalendar()[:2], 0)
    last4 = sorted(weeks.items())[-4:]

    today_str = today.isoformat()
    day = state["days"].get(today_str)
    return {
        "date": today_str,
        "total": n,
        "dist": dist,
        "read_pct": round(read_cnt * 100.0 / n, 1) if n else 0,
        "read_target_pct": UNDERSTAND_TARGET * 100,
        "packet_pct": round(packet_cnt * 100.0 / n, 1) if n else 0,
        "subjects": subj,
        "subject_floor_pct": SUBJECT_FLOOR * 100,
        "days_to_redline": (REDLINE_UNDERSTAND - today).days,
        "this_week_new": this_week,
        "last4_weeks": [["%d-W%02d" % wk, c] for wk, c in last4],
        "today": {
            "drawn": bool(day),
            "settled": bool(day and day.get("settled")),
            "energy": day.get("energy") if day else None,
            "new_ids": day.get("new_ids", []) if day else [],
            "review_ids": day.get("review_ids", []) if day else [],
        },
        "thin_pool_size": sum(
            1 for ps in state["points"].values() if ps.get("mastery", 0) <= 2
        ) + sum(1 for ps in state["points"].values()
                if ps.get("mastery", 0) >= 3 and overdue_days(ps, today) > 0),
    }


def thin_pool(defs, state, today):
    """薄弱池：m≤2 常驻；m≥3 超期入池。排序 = 薄弱优先 + 超期越久越前"""
    items = []
    for pid, d in defs.items():
        ps = state["points"].get(pid)
        if not ps:
            continue
        m = ps.get("mastery", 0)
        od = overdue_days(ps, today)
        if m <= 2 or od > 0:
            score = (6 - m) * 100 + min(od, 200)
            items.append((score, pid))
    items.sort(reverse=True)
    return [pid for _, pid in items]


def api_draw(defs, state, today, body):
    count = max(1, min(5, int(body.get("count", 3))))
    review_count = max(0, min(6, int(body.get("review_count", 2))))
    exclude = set(body.get("exclude", []))

    pts = state["points"]

    def learned(pid):
        return pts.get(pid, {}).get("mastery", 0) >= 3

    # 依赖约束仅在同学科内生效（M/L/P/D/C/O/N 各自内部）；跨学科依赖（如D29→P01、O16→C11）忽略
    learnable = [
        pid for pid, d in defs.items()
        if pid not in pts
        and all(learned(dep) for dep in d["deps"] if dep[0] == pid[0])
        and pid not in exclude
    ]
    a_pool = [p for p in learnable if defs[p]["priority"] == "A"]
    bc_pool = [p for p in learnable if defs[p]["priority"] != "A"]

    n_a = min(len(a_pool), math.ceil(count * 0.6))
    picked = random.sample(a_pool, n_a) + random.sample(bc_pool, min(len(bc_pool), count - n_a))
    if len(picked) < count:                              # 池不足时从剩余可学补齐
        rest = [p for p in learnable if p not in picked]
        picked += random.sample(rest, min(len(rest), count - len(picked)))

    reviews = thin_pool(defs, state, today)[:review_count]
    return {
        "new": [point_view(p, defs[p], state, today) for p in picked],
        "review": [point_view(p, defs[p], state, today) for p in reviews],
        "learnable_total": len(learnable),
        "learnable_a": len(a_pool),
        "note": ("A级可学池不足，本次A级配额未满足" if n_a < math.ceil(count * 0.6) else None),
    }


def _today():
    return date.today()


def apply_updates(state, today_str, updates, day):
    """结算/调整共用的状态写入"""
    new_ids = set(day.get("new_ids", [])) if day else set()
    review_ids = set(day.get("review_ids", [])) if day else set()
    for u in updates:
        pid = u.get("id")
        m = int(u.get("mastery", 0))
        if m < 1 or m > 5:
            continue
        ps = state["points"].get(pid)
        if ps is None:
            ps = {"mastery": m, "first_learned": today_str, "last_reviewed": today_str,
                  "review_count": 1, "smoke": None, "packet_done": False, "history": []}
            state["points"][pid] = ps
            ev = "learn"
        else:
            ev = "review" if pid in (new_ids | review_ids) else "adjust"
            ps["review_count"] += 1
            ps["last_reviewed"] = today_str
        old = ps["mastery"]
        ps["mastery"] = m
        if "smoke_passed" in u and u["smoke_passed"] is not None:
            ps["smoke"] = {"date": today_str, "passed": bool(u["smoke_passed"])}
        if "packet_done" in u and u["packet_done"] is not None:
            ps["packet_done"] = bool(u["packet_done"])
        ps["history"].append({"date": today_str, "event": ev, "from": old, "to": m})


def api_week(defs, state, today):
    """本周（ISO周）吞吐与疑点率，供周日检验"""
    wk = today.isocalendar()[:2]
    monday = date.fromisocalendar(wk[0], wk[1], 1)
    days = [ (monday + timedelta(days=i)).isoformat() for i in range(7) ]
    new_pts = [pid for pid, ps in state["points"].items()
               if ps.get("first_learned") in days]
    reviews = sum(1 for pid, ps in state["points"].items()
                  for h in ps.get("history", [])
                  if h.get("date") in days and h.get("event") == "review")
    smoke_fail = sum(1 for pid, ps in state["points"].items()
                     if (ps.get("smoke") or {}).get("date") in days
                     and (ps.get("smoke") or {}).get("passed") is False)
    settled_days = sum(1 for d in days if state["days"].get(d, {}).get("settled"))
    return {"week": "%d-W%02d" % wk, "range": [days[0], days[-1]],
            "new_count": len(new_pts), "new_ids": sorted(new_pts),
            "review_count": reviews, "smoke_fail": smoke_fail,
            "settled_days": settled_days,
            "smoke_fail_rate_pct": round(smoke_fail * 100.0 / max(1, len(new_pts)), 1)}


# ---------------- HTTP ----------------
DEFS, WARNS = {}, []


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DASH_DIR, **kw)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        state = load_state()
        today = _today()
        if path == "/api/overview":
            ov = overview(DEFS, state, today)
            ov["warns"] = WARNS
            return self._json(ov)
        if path == "/api/points":
            return self._json({
                "points": [point_view(pid, d, state, today)
                           for pid, d in sorted(DEFS.items())],
                "warns": WARNS,
            })
        if path == "/api/week":
            return self._json(api_week(DEFS, state, today))
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            body = self._body()
        except Exception:
            return self._json({"error": "bad json"}, 400)
        state = load_state()
        today = _today()
        today_str = today.isoformat()

        if path == "/api/draw":
            return self._json(api_draw(DEFS, state, today, body))

        if path == "/api/day/commit":
            day = {"energy": int(body.get("energy", 3)),
                   "new_ids": body.get("new_ids", []),
                   "review_ids": body.get("review_ids", []),
                   "settled": False}
            state["days"][today_str] = day
            save_state(state)                            # 抽取记录不单独 commit
            return self._json({"ok": True, "day": day})

        if path == "/api/settle":
            day = state["days"].get(today_str, {})
            updates = body.get("updates", [])
            apply_updates(state, today_str, updates, day)
            day["settled"] = True
            state["days"][today_str] = day
            save_state(state)
            ok = git_commit("log: %s 新%d复习%d" % (
                today_str, len(day.get("new_ids", [])), len(day.get("review_ids", []))))
            return self._json({"ok": True, "committed": ok,
                               "overview": overview(DEFS, state, today)})

        if path == "/api/adjust":
            pid = body.get("id")
            if pid not in DEFS:
                return self._json({"error": "unknown id %s" % pid}, 400)
            day = state["days"].get(today_str)
            old = state["points"].get(pid, {}).get("mastery", 0)
            apply_updates(state, today_str,
                          [{"id": pid, "mastery": int(body.get("mastery", 3)),
                            "packet_done": body.get("packet_done"),
                            "smoke_passed": body.get("smoke_passed")}], day)
            save_state(state)
            ok = git_commit("adjust: %s %s->%s" % (pid, old, body.get("mastery")))
            return self._json({"ok": True, "committed": ok})

        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


def main():
    global DEFS, WARNS
    DEFS, WARNS = load_defs()
    print("[看板] 知识点解析：%d 个（预期243）" % len(DEFS))
    for w in WARNS:
        print("[看板][警告] " + w)
    if len(DEFS) != 243:
        print("[看板][警告] 数量不符预期，请检查知识点清单 md 是否被改动")
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    url = "http://127.0.0.1:%d" % PORT
    print("[看板] 服务已启动：%s  （Ctrl+C 退出）" % url)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[看板] 已退出")


if __name__ == "__main__":
    main()
