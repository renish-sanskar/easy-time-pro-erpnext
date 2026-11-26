from contextlib import contextmanager
import csv, hmac, io, os, secrets, sqlite3, threading, time
from datetime import datetime, timedelta
import requests
from flask import Flask, Response, flash, jsonify, redirect, render_template_string, request, url_for

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attendance.db")  # not cwd: boot tasks start in System32
PER_PAGE = 50
LABEL = {"ok": "Pushed", "duplicate": "Already in ERPNext", "error": "Failed", "pending": "Waiting", "run": "Running"}
app = Flask(__name__)
app.secret_key = secrets.token_hex(16)  # only signs flash messages; a restart just clears them
lock = threading.Lock()
http = requests.Session()  # reuses connections across the many small ERPNext calls

DEFAULTS = {
    "etp_url": "http://192.168.1.100:8081", "etp_user": "", "etp_pass": "",
    "erp_url": "http://localhost:8000", "erp_key": "", "erp_secret": "",
    "employee_field": "attendance_device_id",  # Employee field holding the ETP emp_code
    "start_date": datetime.now().strftime("%Y-%m-%d"),  # first sync pulls from here
    "interval_min": "5", "auto_sync": "0", "last_sync": "", "lookback_days": "1",
    "webhook_token": secrets.token_urlsafe(16), "last_ok_sync": "",
}
# columns added after v1: (name, ddl) -> applied to an existing db by init()
LOG_COLS = {"employee": "TEXT", "employee_name": "TEXT", "checkin": "TEXT",
            "attempts": "INTEGER DEFAULT 0", "run_id": "INTEGER", "created": "TEXT"}


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        c.execute("CREATE TABLE IF NOT EXISTS config(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS log(
            tx_id TEXT PRIMARY KEY, emp_code TEXT, punch_time TEXT, log_type TEXT,
            device TEXT, status TEXT, message TEXT, updated TEXT)""")
        have = {r["name"] for r in c.execute("PRAGMA table_info(log)")}
        for col, ddl in LOG_COLS.items():
            if col not in have:
                c.execute(f"ALTER TABLE log ADD COLUMN {col} {ddl}")
        c.execute("""CREATE TABLE IF NOT EXISTS sync_run(
            id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, trigger TEXT,
            fetched INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, duplicate INTEGER DEFAULT 0,
            error INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
        c.execute("CREATE INDEX IF NOT EXISTS log_time ON log(punch_time)")
        c.execute("DROP INDEX IF EXISTS log_status")
        c.execute("CREATE INDEX IF NOT EXISTS log_status_upd ON log(status, updated)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (k, v))


def cfg():
    with db() as c:
        return {r["k"]: r["v"] for r in c.execute("SELECT * FROM config")}


def set_cfg(**kw):
    with db() as c:
        for k, v in kw.items():
            c.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (k, v))


def etp_token(c):
    r = http.post(c["etp_url"].rstrip("/") + "/jwt-api-token-auth/",
                      json={"username": c["etp_user"], "password": c["etp_pass"]}, timeout=20)
    r.raise_for_status()
    return r.json()["token"]


def fetch_punches(c, since, until):
    h = {"Authorization": "JWT " + etp_token(c)}
    url = c["etp_url"].rstrip("/") + "/iclock/api/transactions/"
    params = {"start_time": since, "end_time": until, "page_size": 200, "page": 1}
    while True:
        r = http.get(url, headers=h, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        yield from j["data"]
        if not j.get("next"):
            return
        params["page"] += 1


class ERPDown(Exception):
    """ERPNext unreachable / rejecting our credentials: not a problem with this one punch."""


def push(c, row):
    """-> (status, message, employee, employee_name, checkin_name)"""
    r = http.post(
        c["erp_url"].rstrip("/") + "/api/method/hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field",
        headers={"Authorization": f"token {c['erp_key']}:{c['erp_secret']}"},
        json={"employee_field_value": row["emp_code"], "timestamp": row["punch_time"],
              "device_id": row["device"], "log_type": row["log_type"],
              "employee_fieldname": c["employee_field"]}, timeout=30)
    if r.ok:
        m = r.json().get("message") or {}
        return "ok", "", m.get("employee"), m.get("employee_name"), m.get("name")
    msg = r.text[:300]
    if "already has a log" in msg:
        return "duplicate", "already in ERPNext", None, None, None
    if r.status_code in (401, 403) or r.status_code >= 500:
        raise ERPDown(f"HTTP {r.status_code}: {msg}")
    return "error", msg, None, None, None


def push_row(c, row, run_id):
    """-> (status, erpnext_down)"""
    down = False
    try:
        res = push(c, row)  # network happens outside any DB transaction
    except (requests.RequestException, ERPDown) as e:
        res, down = ("error", str(e)[:300], None, None, None), True
    with db() as d:
        d.execute("""UPDATE log SET status=?, message=?, employee=COALESCE(?,employee),
                     employee_name=COALESCE(?,employee_name), checkin=COALESCE(?,checkin),
                     attempts=attempts+1, run_id=?, updated=? WHERE tx_id=?""",
                  (res[0], res[1], res[2], res[3], res[4], run_id, now(), row["tx_id"]))
    return res[0], down


def normalize(p):
    """Easy Time Pro punch (any key case) -> our row. Raises KeyError/ValueError when unusable."""
    p = {k.upper(): v for k, v in p.items()}
    raw = str(p.get("PUNCH_DATETIME") or p["PUNCH_TIME"]).strip()[:19]
    for f in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            t = datetime.strptime(raw, f).strftime("%Y-%m-%d %H:%M:%S")
            break
        except ValueError:
            pass
    else:
        raise ValueError(f"bad punch time {raw!r}")
    st = str(p.get("PUNCH_STATE", "")).strip().lower()  # "Check In", "Break Out", or "0".."5"
    typ = "IN" if st.endswith("in") or st in ("0", "3", "4") else "OUT"
    code = str(p["EMP_CODE"]).strip()
    return {"tx_id": f"{code}|{t}|{typ}", "emp_code": code, "punch_time": t, "log_type": typ,
            "device": p.get("TERMINAL_ALIAS") or p.get("TERMINAL_SN") or p.get("AREA_NAME") or "",
            "name": " ".join(filter(None, [p.get("FIRST_NAME"), p.get("LAST_NAME")])).strip()}


def ingest(punches, ts):
    """Store punches as pending; returns (new, unreadable). Same punch twice is ignored."""
    rows, bad = [], 0
    for p in punches:  # may page through the ETP API: do it before taking the DB write lock
        try:
            r = normalize(p)
        except (KeyError, ValueError, AttributeError):
            bad += 1
            continue
        rows.append((r["tx_id"], r["emp_code"], r["punch_time"], r["log_type"], r["device"], r["name"] or None, ts, ts))
    with db() as d:
        before = d.total_changes
        d.executemany(
            "INSERT OR IGNORE INTO log(tx_id, emp_code, punch_time, log_type, device, employee_name, status, message, attempts, created, updated)"
            " VALUES(?,?,?,?,?,?,'pending','',0,?,?)", rows)
        return d.total_changes - before, bad


def sync(trigger="manual", fetch=True, fetched=0):
    """Optionally pull new punches, then push every row not yet ok/duplicate (retries earlier errors)."""
    if not lock.acquire(blocking=False):
        return "Sync already running"
    try:
        c = cfg()
        started = now()
        with db() as d:
            run_id = d.execute("INSERT INTO sync_run(started, trigger) VALUES(?,?)", (started, trigger)).lastrowid
        n = {"ok": 0, "duplicate": 0, "error": 0}
        note, ok = "", True
        if fetch:
            try:
                since = c["start_date"] + " 00:00:00"
                if c["last_sync"]:  # also re-read a few days back: a punch that reaches Easy TimePro late still carries its old time
                    try:
                        days = max(int(c["lookback_days"]), 0)
                    except ValueError:
                        days = 1
                    since = (datetime.strptime(c["last_sync"], "%Y-%m-%d %H:%M:%S") - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                new, bad = ingest(fetch_punches(c, since, started), started)
                fetched += new
                set_cfg(last_sync=started)
                note = f"{bad} unreadable punches skipped" if bad else ""
            except Exception as e:
                note, ok = f"Fetch failed: {e}"[:300], False
        try:
            with db() as d:
                pending = d.execute("SELECT * FROM log WHERE status IN ('pending','error')").fetchall()
            fails = 0
            for i, row in enumerate(pending):
                st, down = push_row(c, row, run_id)
                n[st] += 1
                fails = fails + 1 if down else 0
                if fails >= 3:  # ERPNext is down: don't wait out a timeout per row; the rest stay queued
                    note = (note + f" ERPNext not responding, {len(pending) - i - 1} punches left queued").strip()
                    break
        except Exception as e:
            note, ok = (note + f" Push failed: {e}").strip()[:300], False
        if ok and not n["error"]:
            set_cfg(last_ok_sync=now())  # "last sync" on the dashboard: only when every punch got through
        with db() as d:
            d.execute("UPDATE sync_run SET finished=?, fetched=?, ok=?, duplicate=?, error=?, note=? WHERE id=?",
                      (now(), fetched, n["ok"], n["duplicate"], n["error"], note, run_id))
        return f"Fetched {fetched} new, pushed {n['ok']}, duplicate {n['duplicate']}, failed {n['error']}" + (f" ({note})" if note else "")
    finally:
        lock.release()


def scheduler():
    last = 0
    while True:
        time.sleep(15)
        c = cfg()
        if c["auto_sync"] == "1" and time.time() - last >= float(c["interval_min"] or 5) * 60:
            last = time.time()
            sync("auto")


def page_arg():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except ValueError:
        return 1


def query_logs(args, paged=True):
    where, p = [], []
    if args.get("status"):
        where.append("status=?"); p.append(args["status"])
    if args.get("q"):
        where.append("(emp_code LIKE ? OR employee LIKE ? OR employee_name LIKE ? OR device LIKE ?)")
        p += [f"%{args['q']}%"] * 4
    if args.get("date_from"):
        where.append("punch_time >= ?"); p.append(args["date_from"] + " 00:00:00")
    if args.get("date_to"):
        where.append("punch_time <= ?"); p.append(args["date_to"] + " 23:59:59")
    w = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as d:
        total = d.execute("SELECT COUNT(*) FROM log" + w, p).fetchone()[0]
        sql = "SELECT * FROM log" + w + " ORDER BY punch_time DESC, tx_id DESC"
        if paged:
            sql += f" LIMIT {PER_PAGE} OFFSET {(page_arg() - 1) * PER_PAGE}"
        return d.execute(sql, p).fetchall(), total


PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8><title>Attendance Sync</title>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=color-scheme content="light dark">
<link rel=icon href="{{url_for('static',filename='erpnext.svg')}}">
<style>
:root{--bg:#f4f5f6;--card:#fff;--ink:#1f272e;--mut:#6c7680;--line:#d9dee3;--soft:#f8f9fa;--blue:#0089ff;--green:#5fa32c;
--ok:#2f8f4e;--err:#d6382f;--warn:#b7791f;--wait:#7b8794}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#0c0c0c;--ink:#ececec;--mut:#9b9b9b;--line:#262626;--soft:#151515;--blue:#3aa0ff;--green:#8bc34a;
--ok:#5cc07c;--err:#ff7a70;--warn:#e0b050;--wait:#8c8c8c}}
*{box-sizing:border-box}body{margin:0;font:13px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--ink)}
a{color:var(--blue)}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:10}
.bar{max-width:1100px;margin:0 auto;padding:0 24px;height:56px;display:flex;align-items:center;gap:24px}
.brand{display:flex;align-items:center;gap:9px;text-decoration:none;color:var(--ink);font-size:15px}
.brand img{height:28px;width:28px;border-radius:6px;display:block}.brand .arr{color:var(--mut)}.brand b{margin-left:6px;font-weight:600}
nav{display:flex;gap:2px;height:100%;flex:1}nav a{display:flex;align-items:center;padding:0 12px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent}
nav a:hover{color:var(--ink)}nav a.on{color:var(--ink);border-color:var(--blue);font-weight:600}
.state{color:var(--mut);font-size:12px;white-space:nowrap}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);margin-right:6px;animation:p 1s infinite}@keyframes p{50%{opacity:.3}}
main{max-width:1100px;margin:0 auto;padding:28px 24px 56px}
.ph{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:20px}
h2{margin:0;font-size:20px;font-weight:600}h3{margin:0;font-size:14px;font-weight:600}.sub{color:var(--mut);margin:2px 0 0}
.sec{margin:32px 0 12px;display:flex;justify-content:space-between;align-items:baseline}
.nums{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.num{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:16px 18px;color:inherit;text-decoration:none;display:block}
a.num:hover{border-color:var(--mut)}.num span{color:var(--mut)}.num b{display:block;font-size:24px;font-weight:600;margin-top:2px}
.line{color:var(--mut);margin:16px 0 0}
.tw{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow-x:auto}
table{width:100%;border-collapse:collapse}th{color:var(--mut);font-weight:500;font-size:12px;background:var(--soft)}
td,th{padding:11px 16px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}tr:last-child td{border:0}
td.wrap{white-space:normal;max-width:330px;color:var(--mut);font-size:12px}td.n,th.n{text-align:right}.red{color:var(--err);font-weight:600}
.empty{padding:36px 16px;text-align:center;color:var(--mut)}
.ind{display:inline-flex;align-items:center;gap:6px}.ind:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--wait)}
.ind.ok:before{background:var(--ok)}.ind.error:before{background:var(--err)}.ind.duplicate:before{background:var(--warn)}.ind.run:before{background:var(--blue)}
.num.ok span:before,.num.error span:before,.num.duplicate span:before,.num.pending span:before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--wait)}
.num.ok span:before{background:var(--ok)}.num.error span:before{background:var(--err)}.num.duplicate span:before{background:var(--warn)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:20px 22px}
label{display:block;color:var(--mut);margin:14px 0 6px}label small{display:block;margin-top:3px;font-size:12px}
input,select{height:36px;padding:0 12px;border:1px solid var(--line);border-radius:6px;background:var(--soft);color:var(--ink);font:inherit;width:100%}
input:focus,select:focus{outline:0;border-color:var(--blue);background:var(--card)}input[readonly]{font-family:ui-monospace,Consolas,monospace;font-size:12px}
button,.btn{background:var(--blue);color:#fff;border:1px solid var(--blue);padding:7px 16px;border-radius:6px;cursor:pointer;font:inherit;text-decoration:none;display:inline-block;white-space:nowrap;line-height:20px}
button:hover,.btn:hover{opacity:.9}button:disabled{opacity:.55;cursor:default}button.sm{padding:3px 12px;font-size:12px;line-height:18px}
.btn.alt,button.alt{background:var(--card);color:var(--ink);border-color:var(--line)}
.fb{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px;align-items:end;margin-bottom:16px;padding:18px 22px}.fb label{margin:0}.fb .acts{display:flex;gap:8px;flex-wrap:wrap;grid-column:1/-1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px 20px}.two>label{margin:0}.panel>.two{margin-top:14px}.stack>*+*{margin-top:20px}
.hd{display:flex;align-items:center;gap:10px;margin-bottom:6px}.hd img{height:30px;width:30px;border-radius:6px}
.bar2{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}
.msg{border:1px solid var(--line);border-left:3px solid var(--blue);background:var(--card);padding:11px 16px;border-radius:6px;margin-bottom:20px}
.pg{display:flex;gap:12px 20px;align-items:center;justify-content:space-between;flex-wrap:wrap;color:var(--mut);margin-top:18px}.pgb{display:flex;gap:12px;align-items:center}
@media(max-width:760px){.bar{flex-wrap:wrap;height:auto;padding:10px 16px;gap:6px 16px}nav{order:3;flex-basis:100%;height:36px;overflow-x:auto}.state{margin-left:auto}
.nums{grid-template-columns:1fr 1fr}.two{grid-template-columns:1fr}main{padding:16px}}
</style></head><body>
<header><div class=bar>
<a class=brand href="{{url_for('dash')}}"><img src="{{url_for('static',filename='easytimepro.jpg')}}" alt="Easy TimePro" title="Easy TimePro"><span class=arr>→</span><img src="{{url_for('static',filename='erpnext.svg')}}" alt=ERPNext title=ERPNext><b>Attendance Sync</b></a>
<nav>{% for p,e,l in [('dash','dash','Dashboard'),('logs','logs','Punches'),('runs','runs_page','Sync history'),('cfg','cfg_page','Settings')] %}<a href="{{url_for(e)}}" class="{{'on' if page==p}}">{{l}}</a>{% endfor %}</nav>
<span class=state>{% if running %}<span class=dot></span>Sync running…{% elif c.auto_sync=='1' %}Auto sync every {{c.interval_min}} min{% else %}Auto sync is off{% endif %}</span></div></header>
<main>{% for m in get_flashed_messages() %}<div class=msg>{{m}}</div>{% endfor %}

{% macro pager(ep, a, n, pages, total) %}{% if total %}<div class=pg><span>Showing {{(n-1)*per+1}}–{{[n*per,total]|min}} of {{total}}</span>
<span class=pgb>{% if n>1 %}<a class="btn alt" href="{{url_for(ep,**dict(a,page=n-1))}}">Previous</a>{% endif %}<span>Page {{n}} of {{pages}}</span>{% if n<pages %}<a class="btn alt" href="{{url_for(ep,**dict(a,page=n+1))}}">Next</a>{% endif %}</span></div>{% endif %}{% endmacro %}
{% macro ind(s) %}<span class="ind {{s}}">{{L[s]}}</span>{% endmacro %}
{% macro logtable(rows, retry=True, empty='No punches.') %}<div class=tw><table>
<tr><th>Punch time<th>Code<th>Employee<th>In/Out<th>Device<th>Status<th class=n>Tries<th>Message<th></tr>
{% for r in rows %}<tr><td>{{r.punch_time}}<td>{{r.emp_code}}<td>{{r.employee_name or r.employee or '–'}}<td>{{r.log_type|title}}<td>{{r.device or '–'}}
<td>{{ind(r.status)}}<td class=n>{{r.attempts or 0}}<td class=wrap>{{r.message}}
<td>{% if retry and r.status in ('error','pending') %}<form method=post action="{{url_for('retry',tx_id=r.tx_id)}}"><button class="sm alt">Retry</button></form>{% endif %}</tr>
{% else %}<tr><td colspan=9 class=empty>{{empty}}</tr>{% endfor %}</table></div>{% endmacro %}

{% if page=='dash' %}<div class=ph><div><h2>Dashboard</h2><p class=sub>Punches are pulled from Easy TimePro and added to ERPNext as Employee Checkins.</p></div>
<form method=post action="{{url_for('do_sync')}}"><button {{'disabled' if running}}>{{'Syncing…' if running else 'Sync now'}}</button></form></div>
<div class=nums>{% for s in ['ok','duplicate','error','pending'] %}<a class="num {{s}}" href="{{url_for('logs',status=s)}}"><span>{{L[s]}}</span><b>{{counts[s]}}</b></a>{% endfor %}</div>
<p class=line>Last fully successful sync: <b>{{c.last_ok_sync or 'never'}}</b> &nbsp;·&nbsp; last pull from Easy TimePro: {{c.last_sync or 'never'}}</p>
<div class=sec><h3>Failed punches</h3></div>{{logtable(errors, empty='No failed punches.')}}
<div class=sec><h3>Latest syncs</h3><a href="{{url_for('runs_page')}}">See all</a></div>{{runtable(runs)}}

{% elif page=='logs' %}<div class=ph><div><h2>Punches</h2><p class=sub>{{total}} record{{'' if total==1 else 's'}}</p></div></div>
<form class="fb panel" method=get>
<label>Search<input name=q value="{{a.q or ''}}" placeholder="code, name or device"></label>
<label>Status<select name=status><option value="">All{% for s in ['ok','duplicate','error','pending'] %}<option value="{{s}}" {{'selected' if a.status==s}}>{{L[s]}}{% endfor %}</select></label>
<label>From<input type=date name=date_from value="{{a.date_from or ''}}"></label>
<label>To<input type=date name=date_to value="{{a.date_to or ''}}"></label>
<div class=acts><button>Apply</button><a class="btn alt" href="{{url_for('logs')}}">Clear</a><a class="btn alt" href="{{url_for('logs_csv',**a)}}">Download CSV</a></div></form>
{{logtable(rows, empty='No punches match these filters.')}}
{{pager('logs', a, page_no, pages, total)}}

{% elif page=='runs' %}<div class=ph><div><h2>Sync history</h2><p class=sub>Every time the app pulled and pushed punches.</p></div></div>{{runtable(runs)}}{{pager('runs_page', a, page_no, pages, total)}}

{% else %}<div class=ph><div><h2>Settings</h2><p class=sub>Connection details are stored in this app's local database.</p></div></div><form method=post class=stack>
<div class=two>
<section class=panel><div class=hd><img src="{{url_for('static',filename='easytimepro.jpg')}}" alt=""><h3>Easy TimePro</h3></div>
<label>Server address<input name=etp_url value="{{c.etp_url}}" placeholder="http://192.168.1.100:8081"></label>
<label>Username<input name=etp_user value="{{c.etp_user}}" autocomplete=off></label>
<label>Password<input type=password name=etp_pass autocomplete=new-password placeholder="{{'saved – leave blank to keep' if c.etp_pass}}"></label>
<label>Start syncing from<input type=date name=start_date value="{{c.start_date}}"><small>If you change this date, the next sync pulls everything since then again. Punches already in ERPNext are skipped.</small></label></section>
<section class=panel><div class=hd><img src="{{url_for('static',filename='erpnext.svg')}}" alt=""><h3>ERPNext</h3></div>
<label>Site address<input name=erp_url value="{{c.erp_url}}" placeholder="http://localhost:8000"></label>
<label>API key<input name=erp_key value="{{c.erp_key}}" autocomplete=off></label>
<label>API secret<input type=password name=erp_secret autocomplete=new-password placeholder="{{'saved – leave blank to keep' if c.erp_secret}}"></label>
<label>Employee field with the punch code<input name=employee_field value="{{c.employee_field}}"><small>The Easy TimePro employee code is matched to this field on the Employee.</small></label></section></div>
<section class=panel><h3>Schedule</h3><div class=two>
<label>Auto sync<select name=auto_sync><option value=1 {{'selected' if c.auto_sync=='1'}}>On<option value=0 {{'selected' if c.auto_sync!='1'}}>Off</select></label>
<label>Run every (minutes)<input type=number min=1 name=interval_min value="{{c.interval_min}}"></label>
<label>Re-read the last (days)<input type=number min=0 name=lookback_days value="{{c.lookback_days}}"><small>Each sync also re-reads this many days before the last sync, to catch punches that reached Easy TimePro late (for example from a machine that was offline). Punches already stored are skipped. Use 0 to switch it off.</small></label></div></section>
<section class=panel><h3>Receive punches from Easy TimePro</h3><p class=sub>If the server can push punches to a URL, point it here with a POST (click to select):</p>
<label style="margin-top:8px"><input readonly onclick="this.select()" value="{{request.host_url}}api/punches?token={{c.webhook_token}}"></label></section>
<div class=bar2><button>Save</button><button class=alt formaction="{{url_for('test')}}">Save and test connections</button></div></form>
{% endif %}
{% if running and page in ('dash','runs') %}<script>setTimeout(()=>location.reload(),3000)</script>{% endif %}
</main></body></html>"""

RUNS = """{% macro runtable(runs) %}<div class=tw><table><tr><th>#<th>Started<th>Finished<th>Started by<th class=n>New<th class=n>Pushed<th class=n>Duplicate<th class=n>Failed<th>Note</tr>
{% for r in runs %}<tr><td>{{r.id}}<td>{{r.started}}<td>{% if r.finished %}{{r.finished}}{% else %}<span class="ind run">running…</span>{% endif %}<td>{{r.trigger}}<td class=n>{{r.fetched}}<td class=n>{{r.ok}}<td class=n>{{r.duplicate}}<td class="n {{'red' if r.error}}">{{r.error}}<td class=wrap>{{r.note}}</tr>
{% else %}<tr><td colspan=9 class=empty>Nothing yet. Press “Sync now” on the dashboard.</tr>{% endfor %}</table></div>{% endmacro %}"""

TEMPLATE = PAGE.replace("{% macro ind", RUNS + "{% macro ind", 1)


def render(page, **kw):
    return render_template_string(TEMPLATE, page=page, c=cfg(), a=request.args.to_dict(), running=lock.locked(), L=LABEL, per=PER_PAGE, **kw)


def recent_runs(n):
    with db() as d:
        return d.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT ?", (n,)).fetchall()


@app.route("/")
def dash():
    with db() as d:
        counts = {s: 0 for s in ("ok", "duplicate", "error", "pending")}
        for r in d.execute("SELECT status, COUNT(*) n FROM log GROUP BY status"):
            counts[r["status"]] = r["n"]
        errors = d.execute("SELECT * FROM log WHERE status='error' ORDER BY updated DESC LIMIT 10").fetchall()
    return render("dash", counts=counts, errors=errors, runs=recent_runs(10))


@app.route("/logs")
def logs():
    rows, total = query_logs(request.args)
    return render("logs", rows=rows, total=total, page_no=page_arg(),
                  pages=max(-(-total // PER_PAGE), 1))


@app.route("/logs.csv")
def logs_csv():
    rows, _ = query_logs(request.args, paged=False)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["punch_time", "emp_code", "employee", "employee_name", "log_type", "device", "status", "attempts", "message", "checkin"])
    for r in rows:
        w.writerow([r["punch_time"], r["emp_code"], r["employee"], r["employee_name"], r["log_type"], r["device"],
                    r["status"], r["attempts"], r["message"], r["checkin"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=punch_logs.csv"})


@app.route("/runs")
def runs_page():
    n = page_arg()
    with db() as d:
        total = d.execute("SELECT COUNT(*) FROM sync_run").fetchone()[0]
        rows = d.execute("SELECT * FROM sync_run ORDER BY id DESC LIMIT ? OFFSET ?", (PER_PAGE, (n - 1) * PER_PAGE)).fetchall()
    return render("runs", runs=rows, total=total, page_no=n, pages=max(-(-total // PER_PAGE), 1))



@app.route("/api/punches", methods=["POST"])
def receive_punches():
    tok = request.headers.get("X-Token") or request.args.get("token", "")
    if not hmac.compare_digest(tok, cfg()["webhook_token"]):
        return jsonify(error="bad token"), 401
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        body = body.get("data", [body])
    if not isinstance(body, list):
        return jsonify(error="expected a JSON list of punches"), 400
    new, bad = ingest(body, now())
    sync("webhook", fetch=False, fetched=new)  # if a sync is already running, rows stay pending for the next one
    return jsonify(received=len(body), new=new, unreadable=bad)


@app.route("/sync", methods=["POST"])
def do_sync():
    if lock.locked():
        flash("A sync is already running")
    else:
        threading.Thread(target=sync, daemon=True).start()  # runs in background; the dashboard refreshes until it ends
        time.sleep(0.3)  # let the thread take the lock so the redirect already shows "Syncing…"
    return redirect(url_for("dash"))


@app.route("/retry/<tx_id>", methods=["POST"])
def retry(tx_id):
    with db() as d:
        row = d.execute("SELECT * FROM log WHERE tx_id=?", (tx_id,)).fetchone()
    flash(f"Retry: {push_row(cfg(), row, None)[0]}" if row else "Not found")
    return redirect(request.referrer or url_for("logs"))


def save_form():
    f = request.form.to_dict()
    for secret in ("etp_pass", "erp_secret"):
        if not f.get(secret):
            f.pop(secret, None)
    if f.get("start_date") and f["start_date"] != cfg()["start_date"]:
        f["last_sync"] = ""  # new start date -> next sync re-pulls from it (already-pushed punches are skipped)
    set_cfg(**{k: v.strip() for k, v in f.items() if k in DEFAULTS})


@app.route("/config", methods=["GET", "POST"])
def cfg_page():
    if request.method == "POST":
        save_form()
        flash("Saved")
        return redirect(url_for("cfg_page"))
    return render("cfg")



@app.route("/test", methods=["POST"])
def test():
    save_form()
    c = cfg()
    try:
        etp_token(c)
        flash("Easy Time Pro: login OK")
    except Exception as e:
        flash(f"Easy Time Pro failed: {e}")
    try:
        r = http.get(c["erp_url"].rstrip("/") + "/api/method/frappe.auth.get_logged_user",
                         headers={"Authorization": f"token {c['erp_key']}:{c['erp_secret']}"}, timeout=15)
        flash(f"ERPNext: OK as {r.json()['message']}" if r.ok else f"ERPNext failed: {r.status_code} {r.text[:150]}")
    except Exception as e:
        flash(f"ERPNext failed: {e}")
    return redirect(url_for("cfg_page"))


init()
if __name__ == "__main__":
    threading.Thread(target=scheduler, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=5000)
