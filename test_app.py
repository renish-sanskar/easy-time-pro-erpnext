import os, tempfile
import app

app.DB = os.path.join(tempfile.mkdtemp(), "t.db")
app.init()

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
r = cl.post("/api/punches?token=" + tok, json=[punch('7', '26-11-2026 09:00:00', 'Check In'), punch('7', '26-11-2026 09:00:00', 'Check In')])
assert r.status_code == 200 and r.json == {"received": 2, "new": 1, "unreadable": 0}, r.json
assert cl.post("/api/punches", headers={"X-Token": tok}, json={"data": [punch('8', '26-11-2026 09:00:00', 'Check In')]}).json["new"] == 1
assert cl.post("/api/punches?token=" + tok, json="x").status_code == 400
for u in ("/", "/logs", "/logs?status=ok&q=Emp&date_from=2026-11-25", "/runs", "/config"):
    assert cl.get(u).status_code == 200, u
assert tok.encode() in cl.get("/config").data
assert cl.get("/logs.csv?status=ok").data.count(b"\n") == 6
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
