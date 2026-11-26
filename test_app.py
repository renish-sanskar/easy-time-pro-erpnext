import json, os, tempfile
import app

app.DB = os.path.join(tempfile.mkdtemp(), "t.db")
app.init()
real_push = app.push

def punch(code, t, state, **kw):
    return {'COMPANY_NAME': 'COMPANY', 'EMP_CODE': code, 'FIRST_NAME': 'XYZ', 'LAST_NAME': '', 'DEPT_NAME': 'Department',
            'PUNCH_DATETIME': t, 'PUNCH_TIME': t[11:], 'PUNCH_STATE': state, 'TERMINAL_SN': '', 'TERMINAL_ALIAS': 'Gate', **kw}

n = app.normalize(punch('1234', '04-03-2024 09:00:00', 'Check In'))
assert (n["tx_id"], n["punch_time"], n["log_type"], n["device"], n["name"]) == ("1234|2024-03-04 09:00:00|IN", "2024-03-04 09:00:00", "IN", "Gate", "XYZ"), n
assert app.normalize(punch('1', '04-03-2024 18:00:00', 'Check Out'))["log_type"] == "OUT"
assert app.normalize(punch('1', '04-03-2024 13:00:00', 'Break In'))["log_type"] == "IN"
assert app.normalize({"id": 5, "emp_code": "7", "punch_time": "2026-11-25 09:00:00", "punch_state": "1"})["log_type"] == "OUT"

punches = [punch('7', '25-11-2026 09:00:00', 'Check In'), punch('7', '25-11-2026 18:00:00', 'Check Out'),
           punch('9', '25-11-2026 09:05:00', 'Check In'), {"junk": 1}]
app.fetch_punches = lambda c, s, u: iter(punches)
calls = []
def fake_push(c, r):
    calls.append(r["tx_id"])
    if r["emp_code"] == "9" and len(calls) < 4:
        return "error", "no employee", None, None, None
    return "ok", "", "HR-EMP-0" + r["emp_code"], "Emp " + r["emp_code"], "CHKIN-" + r["emp_code"]
app.push = fake_push

print(app.sync("manual"))
app.fetch_punches = lambda c, s, u: iter(punches)  # same punches again: not re-pushed once ok
print(app.sync("auto"))
with app.db() as d:
    st = {r["emp_code"] + r["log_type"]: (r["status"], r["attempts"]) for r in d.execute("SELECT * FROM log")}
assert st == {"7IN": ("ok", 1), "7OUT": ("ok", 1), "9IN": ("ok", 2)}, st

cl = app.app.test_client()
tok = app.cfg()["webhook_token"]
assert cl.post("/api/punches", json=[punch('7', '26-11-2026 09:00:00', 'Check In')]).status_code == 401
r = cl.post("/api/punches?token=" + tok, json=[punch('7', '26-11-2026 09:00:00', 'IN'), punch('7', '26-11-2026 09:00:00', 'IN')])
assert r.status_code == 200 and r.json == {"received": 2, "new": 1, "unreadable": 0}, r.json
assert cl.post("/api/punches", headers={"X-Token": tok}, json={"data": [punch('8', '26-11-2026 09:00:00', 'in')]}).json["new"] == 1
assert cl.post("/api/punches?token=" + tok, json="x").status_code == 400
for u in ("/", "/logs", "/logs?status=ok&q=Emp&date_from=2026-11-25", "/runs", "/config"):
    assert cl.get(u).status_code == 200, u
assert tok.encode() in cl.get("/config").data
assert cl.get("/logs.csv?status=ok").data.count(b"\n") == 6

# the API takes only IN/OUT; the pull from Easy TimePro still understands its own codes
url = "/api/punches?token=" + tok
for state in ("Check In", "0", "", "BREAK"):
    assert cl.post(url, json=[punch('3', '26-11-2026 10:00:00', state)]).json["unreadable"] == 1, state
assert app.normalize(punch('3', '26-11-2026 10:00:00', 'Check In'))["log_type"] == "IN"
# latitude / longitude: optional, range-checked, stored
assert cl.post(url, json=[punch('3', '26-11-2026 10:00:00', 'OUT', LATITUDE=91, LONGITUDE=1),
                          punch('3', '26-11-2026 10:01:00', 'OUT', LAT='north', LNG=1)]).json["unreadable"] == 2
assert cl.post(url, json=[punch('3', '26-11-2026 10:02:00', 'out', lat='23.0225', LNG=72.5714)]).json["new"] == 1
with app.db() as d:
    assert tuple(d.execute("SELECT log_type, latitude, longitude FROM log WHERE emp_code='3'").fetchone()) == ("OUT", 23.0225, 72.5714)
assert app.normalize(punch('3', '04-03-2024 09:00:00', 'Check In'))["latitude"] is None
print("OK")

tx = "9|2026-11-25 09:05:00|IN"
assert cl.post("/retry/" + tx).status_code == 302
assert cl.post("/retry/nope").status_code == 302
import time
big = [punch(str(i), "27-11-2026 09:00:00", "Check In") for i in range(20000)]
t = time.time(); new, bad = app.ingest(big, app.now()); dt = time.time() - t
assert (new, bad) == (20000, 0) and dt < 3, (new, bad, dt)
print(f"ingest 20000 punches: {dt:.2f}s")

app.set_cfg(last_sync="2026-11-25 10:00:00", start_date="2026-11-25")
cl.post("/config", data={"start_date": "2026-11-01"})
c = app.cfg(); assert (c["start_date"], c["last_sync"]) == ("2026-11-01", ""), c
cl.post("/config", data={"start_date": "2026-11-01"}); app.set_cfg(last_sync="x")
cl.post("/config", data={"start_date": "2026-11-01"}); assert app.cfg()["last_sync"] == "x"  # unchanged date keeps last_sync
print("OK date")

app.DB = os.path.join(tempfile.mkdtemp(), "t2.db"); app.init()
import requests
app.ingest([punch(str(i), "28-11-2026 09:00:00", "Check In") for i in range(5)], app.now())
calls.clear()
def down_push(c, r):
    calls.append(r["tx_id"]); raise requests.ConnectionError("refused")
app.push = down_push
msg = app.sync(fetch=False)
assert len(calls) == 3 and "2 punches left queued" in msg, (calls, msg)
assert app.cfg()["last_ok_sync"] == ""  # failed push: "last successful sync" must not move
with app.db() as d:
    st = sorted(r["status"] for r in d.execute("SELECT status FROM log"))
assert st == ["error"] * 3 + ["pending"] * 2, st
app.push = fake_push; app.sync(fetch=False)
with app.db() as d:
    assert {r["status"] for r in d.execute("SELECT status FROM log")} == {"ok"}
assert app.cfg()["last_ok_sync"] != ""  # everything pushed: now it moves
print("OK erp down")

seen = []
def spy(c, since, until):
    seen.append(since); return iter([])
app.fetch_punches = spy
app.set_cfg(start_date="2026-11-01", last_sync="", lookback_days="2"); app.sync()
assert seen[-1] == "2026-11-01 00:00:00"  # first sync: from the start date
app.set_cfg(last_sync="2026-11-25 10:00:00"); app.sync()
assert seen[-1] == "2026-11-23 10:00:00"  # later syncs: 2 days before the last sync
app.set_cfg(lookback_days="0", last_sync="2026-11-25 10:00:00"); app.sync()
assert seen[-1] == "2026-11-25 10:00:00"
app.set_cfg(lookback_days="oops", last_sync="2026-11-25 10:00:00"); app.sync()
assert seen[-1] == "2026-11-24 10:00:00"  # unreadable value falls back to 1 day

late = punch("77", "25-11-2026 07:00:00", "Check In")  # punch time is before the last sync, but it only reached the server afterwards
app.fetch_punches = lambda c, since, until: iter([late] if "2026-11-25 07:00:00" >= since else [])
def late_rows():
    with app.db() as d:
        return d.execute("SELECT COUNT(*) FROM log WHERE emp_code='77'").fetchone()[0]
app.set_cfg(lookback_days="0", last_sync="2026-11-25 10:00:00"); app.sync()
assert late_rows() == 0  # missed without the look-back
app.set_cfg(lookback_days="1", last_sync="2026-11-25 10:00:00"); app.sync()
assert late_rows() == 1  # caught with it
app.set_cfg(last_sync="2026-11-25 10:00:00"); app.sync()
assert late_rows() == 1 and calls.count("77|2026-11-25 07:00:00|IN") == 1  # re-reading again stores and pushes nothing twice
cl.post("/config", data={"lookback_days": "3"}); assert app.cfg()["lookback_days"] == "3"
assert b"Re-read the last (days)" in cl.get("/config").data
print("OK lookback")

# --- Alternate IN / OUT checkbox + same-punch window
def day(code, ds="29-11-2026"):
    with app.db() as d:
        return [(r[0], r[1]) for r in d.execute("SELECT substr(punch_time,12,5), log_type FROM log WHERE emp_code=? ORDER BY punch_time", (code,))]

app.set_cfg(alt_inout="0")
mixed = lambda code: [punch(code, f"29-11-2026 {hm}:00", "Check In") for hm in ("09:00", "09:02", "09:04", "09:05", "09:08")]
app.ingest(mixed("A1"), app.now())
assert [x[1] for x in day("A1")] == ["IN"] * 5  # off: PUNCH_STATE is used
assert b'id=gapbox style="display:none"' in cl.get("/config").data
cl.post("/config", data={"alt_inout": ["1", "0"], "min_gap": "0"})  # a ticked box is sent before the hidden 0
assert app.cfg()["alt_inout"] == "1" and b'id=gapbox style=""' in cl.get("/config").data
app.ingest(mixed("A2"), app.now())
assert [x[1] for x in day("A2")] == ["IN", "OUT", "IN", "OUT", "IN"], day("A2")
assert app.ingest(mixed("A2"), app.now())[0] == 0  # the same punches again are not stored twice
cl.post("/config", data={"alt_inout": ["1", "0"], "min_gap": "3"})
app.ingest(mixed("A3"), app.now())
assert day("A3") == [("09:00", "IN"), ("09:02", "IN"), ("09:04", "OUT"), ("09:05", "OUT"), ("09:08", "IN")], day("A3")
cl.post("/config", data={"alt_inout": "0", "min_gap": "0"})

# --- console: read-only, punch tables only
r = cl.post("/console", data={"sql": "SELECT COUNT(*) AS n FROM log"})
assert b"<th>n" in r.data and b"1 row" in r.data
for bad in ("DELETE FROM log", "SELECT * FROM config", "SELECT 1; DROP TABLE log", "UPDATE log SET status='x'", "DROP TABLE log"):
    h = cl.post("/console", data={"sql": bad}).data
    assert b"class=msg" in h and tok.encode() not in h, bad
assert b"Console" in cl.get("/console").data
with app.db() as d:
    assert d.execute("SELECT COUNT(*) FROM log").fetchone()[0] > 0 and d.execute("SELECT COUNT(*) FROM config").fetchone()[0] > 0
print("OK alt + console")

started = []
app.sync = lambda *a, **k: started.append(1)
assert cl.post("/sync").status_code == 302
import time; time.sleep(0.5); assert started == [1]
for u in ("/", "/logs", "/runs", "/config"):
    h = cl.get(u).data
    assert b"Attendance Sync" in h and b"{%" not in h and b"{{" not in h, u
print("OK ui")

with app.db() as d:
    d.executemany("INSERT INTO sync_run(started, trigger) VALUES(?, 'auto')", [(f"2026-11-0{i%9+1} 10:00:00",) for i in range(120)])
r1, r3 = cl.get("/runs").data, cl.get("/runs?page=3").data
assert b"Showing 1" in r1 and b"Page 1 of 3" in r1 and b">Next<" in r1 and b">Previous<" not in r1
assert b"Page 3 of 3" in r3 and b">Previous<" in r3 and b">Next<" not in r3
assert cl.get("/runs?page=abc").status_code == 200 and cl.get("/logs?page=-4&status=ok").status_code == 200
print("OK paging")

# --- ERPNext lookup: Active employees only, one lookup per code, coordinates sent
class FakeHttp:
    def __init__(self, data):
        self.data, self.gets, self.posted = data, 0, []

    def get(self, url, **kw):
        self.gets += 1
        self.filters = json.loads(kw["params"]["filters"])
        return type("R", (), {"ok": True, "status_code": 200, "text": "", "json": lambda s: {"data": self.data}})()

    def post(self, url, **kw):
        self.posted.append((url, kw["json"]))
        return type("R", (), {"ok": True, "json": lambda s: {"data": {"employee": "HR-EMP-1", "employee_name": "A", "name": "C1"}}})()


c0 = {"erp_url": "http://x", "erp_key": "k", "erp_secret": "s", "employee_field": "attendance_device_id"}
row = {"emp_code": "55", "punch_time": "2026-11-26 09:00:00", "device": "G", "log_type": "IN", "latitude": 23.5, "longitude": 72.5}
app.http = FakeHttp([{"name": "HR-EMP-1"}])
assert real_push(c0, row)[0] == "ok" and real_push(c0, row)[0] == "ok" and app.http.gets == 1
assert app.http.filters == [["attendance_device_id", "=", "55"], ["status", "=", "Active"]]
url, p = app.http.posted[0]
assert url == "http://x/api/resource/Employee Checkin"  # same request as the old sync script
assert p == {"employee": "HR-EMP-1", "time": "2026-11-26 09:00:00", "log_type": "IN", "device_id": "G", "latitude": 23.5, "longitude": 72.5}, p
assert real_push(dict(c0), {**row, "punch_time": "2020-08-20 16:32:00", "latitude": None, "longitude": None})[0] == "ok"
assert app.http.posted[-1][1] == {"employee": "HR-EMP-1", "time": "2020-08-20 16:32:00", "log_type": "IN", "device_id": "G"}  # no coordinates: no keys
app.http = FakeHttp([])
assert real_push(dict(c0, _emp={}), row)[:2] == ("error", "No active Employee with attendance_device_id = 55")
print("OK active + geo")
