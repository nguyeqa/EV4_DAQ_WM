import can
import csv
from datetime import datetime, timezone
import signal
import sys

CSV_PATH = "can_log.csv"
CHANNEL = "can0"

bus = can.interface.Bus(channel=CHANNEL, bustype="socketcan")

f = open(CSV_PATH, "a", newline="")
writer = csv.writer(f)

# Write header only if file is empty
import os
if os.stat(CSV_PATH).st_size == 0:
    writer.writerow([
        "timestamp_iso",
        "timestamp_unix",
        "arbitration_id_hex",
        "is_extended",
        "dlc",
        "data_hex",
    ])

def shutdown(sig, frame):
    f.close()
    bus.shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)

print(f"Logging to {CSV_PATH}. Ctrl+C to stop.")

for msg in bus:
    # python-can gives msg.timestamp as a unix float already (from socketcan kernel TS)
    ts_unix = msg.timestamp
    ts_iso = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
    writer.writerow([
        ts_iso,
        f"{ts_unix:.6f}",
        hex(msg.arbitration_id),
        msg.is_extended_id,
        msg.dlc,
        msg.data.hex(),
    ])
    f.flush()  # so a crash/ctrl-C doesn't lose the tail
    