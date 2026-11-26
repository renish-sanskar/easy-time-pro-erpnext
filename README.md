# Easy TimePro → ERPNext attendance sync

A small Flask app that pulls attendance punches from **Easy TimePro** and adds them to **ERPNext** (HRMS) as Employee Checkins. Settings and sync history are kept in a local SQLite file, and a web page shows what was pushed and what failed.

## What it does

- Pulls punches from Easy TimePro, either on a schedule (auto sync), when you press **Sync now**, or when Easy TimePro pushes them to the app's `/api/punches` URL.
- Saves every punch in SQLite first, then pushes it to ERPNext. If ERPNext is down or rejects a punch, nothing is lost: failed punches are retried on every later sync, or one by one with the **Retry** button.
- Skips punches ERPNext already has, so pulling the same range twice is safe.
- Web pages: **Dashboard** (counts, failed punches, latest syncs), **Punches** (search, status and date filters, CSV download), **Sync history**, and **Settings**.

## Requirements

- Python 3 (tested on 3.14)
- ERPNext with the **HRMS** app (the app calls `hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field`)
- An ERPNext API key and secret for a user who can create Employee Checkins
- Each Employee has the Easy TimePro employee code in a field (default `attendance_device_id`)

## Run it

Linux or macOS:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python app.py

Windows:

    py -3 -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\python app.py

Then open http://localhost:5000 and fill in **Settings**:

| Setting | Meaning |
|---|---|
| Easy TimePro address, username, password | Where punches are pulled from |
| Start syncing from | First sync pulls from this date. Changing it re-pulls from the new date |
| ERPNext address, API key, API secret | Where Employee Checkins are created |
| Employee field | The Employee field that holds the Easy TimePro code |
| Auto sync, run every N minutes | Scheduled syncing |
| Re-read the last N days | Each sync also re-reads N days before the last sync (default 1), to catch punches that reached Easy TimePro late, for example from a machine that was offline. Set it to at least the number of days a machine can stay offline. Stored punches are skipped, so nothing is pushed twice. 0 turns it off |

Use **Save and test connections** to check both logins.

### Start with Windows

Right-click `install_windows.bat` and choose **Run as administrator**. It creates the virtual environment, adds a startup task (`AttendanceSync`) that runs `start_server.bat` at every boot, opens port 5000 in the firewall and starts the server. Server output goes to `app.log`. To remove it: `schtasks /Delete /TN AttendanceSync /F`.

## Easy TimePro format

The app reads punches in this shape (keys are case-insensitive):

    {"EMP_CODE": "1234", "FIRST_NAME": "XYZ", "PUNCH_DATETIME": "04-03-2024 09:00:00",
     "PUNCH_STATE": "Check In", "TERMINAL_ALIAS": "Gate-1"}

`PUNCH_DATETIME` is `DD-MM-YYYY HH:MM:SS`. States ending in "In" (Check In, Break In, Overtime In) become IN, the rest become OUT. A punch is identified by employee code, time and type, so the same punch is never stored twice.

To pull from the server, the app uses `POST /jwt-api-token-auth/` for a token and `GET /iclock/api/transactions/` with `start_time`, `end_time` and `page` parameters. These paths follow the ZKBio Time style API and may differ on your version. If so, change `fetch_punches` in `app.py`.

To receive punches instead, point Easy TimePro at the URL shown in **Settings** (`/api/punches?token=...`) and POST a JSON list.

## Tests

    python test_app.py

## Notes

- The site has no login. Run it on a trusted network only.
- API keys and passwords are saved in plain text in `attendance.db`, which is not part of the repository.
- The ERPNext and Easy TimePro logos in `static/` belong to their owners and are used only to label the two systems.

## Receive punches by POST
`POST /api/punches?token=<token>` (or header `X-Token`; the token is shown in Settings) with a JSON list, or `{"data": [...]}`, of punches in the Easy TimePro format. Keys are case-insensitive.

- Required: `EMP_CODE` and `PUNCH_DATETIME` (or `PUNCH_TIME`).
- `PUNCH_STATE` (or `LOG_TYPE`) must be `IN` or `OUT` here. Anything else makes the punch unreadable. The pull from Easy TimePro itself still understands its codes (`Check In`, `Break Out`, `0`..`5`).
- Optional: `LATITUDE` (`LAT`, -90 to 90) and `LONGITUDE` (`LNG`, `LON`, `LONG`, -180 to 180). They are stored, shown in the Punches list and CSV, and sent to ERPNext, which fills in the Geolocation and checks the Shift Location radius when "Allow geolocation tracking" is on in HR Settings.
- A punch is matched only to an Active Employee. Unknown or inactive codes show as Failed and are retried on every sync.
- The reply is `{"received": n, "new": n, "unreadable": n}`.

## License

[MIT](LICENSE)
