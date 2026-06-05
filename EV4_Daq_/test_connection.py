#!/usr/bin/env python3
"""
Quick InfluxDB v3 connection test for the EV4 DAQ.

Reads your .env, writes a single test point to the database, then tries to read
it back. Use this to confirm your token / host / database are correct BEFORE you
go run the real logger at the car. No CAN hardware required.

    python3 test_connection.py
"""
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client_3 import InfluxDBClient3, Point

load_dotenv()

HOST = os.getenv("INFLUX_HOST", "https://us-east-1-1.aws.cloud2.influxdata.com")
TOKEN = os.getenv("INFLUX_TOKEN")
ORG = os.getenv("INFLUX_ORG", "EV 4")
DATABASE = os.getenv("INFLUX_DATABASE", "ev4_can_data")

if not TOKEN:
    print("ERROR: INFLUX_TOKEN is not set. Copy .env.example to .env and fill it in.")
    sys.exit(1)

print(f"Host:     {HOST}")
print(f"Org:      {ORG}")
print(f"Database: {DATABASE}")
print("-" * 50)

# Default (no write_client_options) => synchronous writes, so a bad
# token/database raises right here instead of failing silently in the background.
client = InfluxDBClient3(host=HOST, token=TOKEN, org=ORG, database=DATABASE)

# 1. Write a test point ----------------------------------------------------
test_value = 42.0
point = (
    Point("daq_connection_test")
    .tag("source", "test_connection.py")
    .field("value", test_value)
    .time(datetime.now(timezone.utc))
)

try:
    client.write(record=point)
    print("WRITE OK  -> wrote 1 test point to measurement 'daq_connection_test'.")
except Exception as e:
    print(f"WRITE FAILED: {e}")
    print("\nCommon causes:")
    print("  - Token is wrong or lacks write access to this database")
    print("  - INFLUX_DATABASE name doesn't match what you created")
    print("  - No network / wrong INFLUX_HOST")
    client.close()
    sys.exit(1)

# 2. Read it back (gives data ~1s to be queryable) -------------------------
time.sleep(2)
try:
    query = (
        "SELECT * FROM daq_connection_test "
        "WHERE time > now() - INTERVAL '5 minutes'"
    )
    table = client.query(query=query, language="sql")
    rows = table.num_rows
    print(f"READ OK   -> query returned {rows} row(s) from the last 5 minutes.")
    if rows:
        print("\nMost recent test rows:")
        print(table.to_pandas().tail(3).to_string(index=False))
except Exception as e:
    # Write working is the important part; query can fail for unrelated reasons.
    print(f"READ check skipped/failed (write still succeeded): {e}")

client.close()
print("-" * 50)
print("Connection test complete. If WRITE OK, the logger will be able to upload.")
print("You can delete the 'daq_connection_test' rows later from the InfluxDB UI.")
