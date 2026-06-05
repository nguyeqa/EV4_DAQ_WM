# EV4 DAQ — Live CAN → InfluxDB

Logs CAN bus traffic on a Raspberry Pi, decodes it with the team DBC files, and
streams the decoded signals into an InfluxDB Cloud bucket in real time. Every
raw frame is also saved to a local CSV as a backup.

```
EV4_Daq_/
├── can_to_influx.py     # main live logger (CAN -> decode -> InfluxDB + CSV)
├── CAN_Timestamp_CSV.py # simple raw-frame CSV logger (no Influx) — kept as a reference
├── DBC/                 # CAN message definitions (.dbc)
├── .env.example         # config template — copy to .env and fill in
├── requirements.txt
└── can_logs/            # CSV backups are written here (git-ignored)
```

---

## 1. One-time InfluxDB setup

1. Log in to InfluxDB Cloud (the org is `university_of_cincinnati`).
2. **Create the bucket** `ev4_can_data`:
   *Load Data → Buckets → Create Bucket → name it `ev4_can_data`*, set a
   retention period (e.g. 30 days, or "never" if you want to keep everything).
3. **Create an API token** scoped to write that bucket:
   *Load Data → API Tokens → Generate API Token → Custom*, give it **Write**
   (and Read) access to `ev4_can_data`. Copy the token — you only see it once.

> ⚠️ Last year's token was hardcoded in the old `main.py` and committed to git,
> so it is public. **Regenerate it** and only ever put the new one in `.env`
> (which is git-ignored). Never paste a token into a `.py` file.

---

## 2. One-time Raspberry Pi setup

Assumes a CAN HAT (MCP2515 / RS485-CAN style) is installed.

### a. Enable the CAN hardware
Add to `/boot/firmware/config.txt` (older Pi OS: `/boot/config.txt`):

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

Check `oscillator=` and `interrupt=` against your HAT's documentation, then
reboot (`sudo reboot`).

### b. Bring the CAN interface up
Set the bitrate to match the car's bus (the EV3/EV4 bus is **500000**):

```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set up can0
```

Verify it's up and seeing traffic:

```bash
ip -details link show can0      # should say state UP
candump can0                    # should print frames when the car is powered
```

(`candump` comes from `sudo apt install can-utils` — very handy for debugging.)

### c. Install the project
```bash
cd ~/EV4_DAQ_WM/EV4_Daq_
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### d. Configure secrets
```bash
cp .env.example .env
nano .env        # paste your INFLUX_TOKEN and confirm the bucket/org
```

---

## 3. Running the logger

With the CAN interface up and `.env` filled in:

```bash
source venv/bin/activate
python3 can_to_influx.py
```

You'll see it connect to InfluxDB, open `can0`, and start uploading. It prints
a progress line every 500 messages. Press **Ctrl+C** to stop — it flushes any
queued writes and closes cleanly.

Then open InfluxDB → Data Explorer → bucket `ev4_can_data` and you should see
measurements named after each CAN message (e.g. `APPS_Info`, `Sensors_Info`)
with the decoded signals as fields.

---

## 4. Running automatically on boot (optional)

Once it's working, you usually want it to start by itself when the car powers on.
Create `/etc/systemd/system/ev4-daq.service`:

```ini
[Unit]
Description=EV4 CAN to InfluxDB logger
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/EV4_DAQ_WM/EV4_Daq_
# Bring up CAN before starting (ignore error if already up)
ExecStartPre=/bin/bash -c '/sbin/ip link set can0 type can bitrate 500000 || true; /sbin/ip link set up can0 || true'
ExecStart=/home/pi/EV4_DAQ_WM/EV4_Daq_/venv/bin/python3 can_to_influx.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ev4-daq.service
journalctl -u ev4-daq.service -f      # watch the logs
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Failed to open CAN bus` | Interface isn't up — rerun the `ip link set` commands from step 2b. |
| `Could not reach InfluxDB` | Check WiFi/cellular, the `INFLUX_URL`, and that the token is valid. |
| Runs but **0 messages uploaded** | Car/bus not actually sending, or bitrate mismatch. Confirm with `candump can0`. |
| Lots of `Unknown` IDs in the CSV | Those frame IDs aren't in the DBC files. Add/update the `.dbc` in `DBC/`. |
| Permission error on `can0` | `sudo` the `ip link` commands; the logger itself doesn't need root. |

## How it works
- **Read:** `python-can` receives frames from `can0` (socketcan).
- **Decode:** `cantools` loads the DBC files and decodes each frame into named
  signals with scaling/offset applied (this is the reliable path the old
  `main.py` hand-rolled and mostly got wrong).
- **Upload:** the InfluxDB batching write API queues points and flushes them on
  a background thread with automatic retries, so a brief network drop won't lose
  data or block logging.
- **Backup:** every raw frame is appended to a timestamped CSV in `can_logs/`.
