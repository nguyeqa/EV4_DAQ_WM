#!/usr/bin/env python3
"""
EV4 DAQ - Live CAN -> InfluxDB logger.

Reads CAN frames from a socketcan interface (e.g. can0) on the Raspberry Pi,
decodes them with the DBC files in ./DBC, writes every decoded signal to an
InfluxDB Cloud bucket in real time, and keeps a local CSV backup of every raw
frame so nothing is lost if the network drops.

Config comes from a .env file (see .env.example). Run with:
    python3 can_to_influx.py
Stop with Ctrl+C.
"""
import os
import sys
import csv
import signal
import logging
import platform
from pathlib import Path
from datetime import datetime, timezone

import can
import cantools
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteOptions

# ---------------------------------------------------------------------------
# Configuration (from .env)
# ---------------------------------------------------------------------------
load_dotenv()

INFLUX_URL = os.getenv("INFLUX_URL", "https://us-east-1-1.aws.cloud2.influxdata.com")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN")
INFLUX_ORG = os.getenv("INFLUX_ORG", "university_of_cincinnati")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "ev4_can_data")

CAN_CHANNEL = os.getenv("CAN_CHANNEL", "can0")
CAN_INTERFACE = os.getenv("CAN_INTERFACE", "socketcan")  # "socketcan" on the Pi

DBC_DIR = Path(os.getenv("DBC_DIR", "DBC"))
LOG_DIR = Path(os.getenv("LOG_DIR", "can_logs"))

# Optional tag applied to every point so you can filter runs in InfluxDB.
SESSION_TAG = os.getenv("SESSION_TAG", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ev4_daq")

running = True


def signal_handler(signum, frame):
    global running
    logger.info("Shutdown signal received, stopping...")
    running = False


def load_dbc() -> cantools.database.Database:
    """Load and merge every .dbc file in DBC_DIR into one database."""
    if not DBC_DIR.exists():
        logger.error("DBC directory '%s' not found.", DBC_DIR)
        sys.exit(1)

    dbc_files = sorted(DBC_DIR.glob("*.dbc"))
    if not dbc_files:
        logger.error("No .dbc files found in '%s'.", DBC_DIR)
        sys.exit(1)

    db = cantools.database.Database()
    for dbc_file in dbc_files:
        logger.info("Loading DBC: %s", dbc_file.name)
        temp = cantools.database.load_file(str(dbc_file))
        for msg in temp.messages:
            if not any(m.frame_id == msg.frame_id for m in db.messages):
                db.messages.append(msg)
    db.refresh()
    logger.info("Loaded %d CAN message definitions.", len(db.messages))
    return db


def setup_bus() -> can.BusABC:
    """Open the CAN bus. On the Pi this is socketcan/can0."""
    if CAN_INTERFACE == "socketcan" and platform.system() != "Linux":
        logger.warning(
            "socketcan only works on Linux (the Pi). Current OS is %s; "
            "set CAN_INTERFACE in .env for local testing.",
            platform.system(),
        )
    try:
        bus = can.interface.Bus(channel=CAN_CHANNEL, interface=CAN_INTERFACE)
        logger.info("CAN bus open: %s (%s)", CAN_CHANNEL, CAN_INTERFACE)
        return bus
    except Exception as e:
        logger.error("Failed to open CAN bus: %s", e)
        logger.error("On the Pi, bring the interface up first:")
        logger.error("  sudo ip link set can0 type can bitrate 500000")
        logger.error("  sudo ip link set up can0")
        sys.exit(1)


def make_influx_writer():
    """Return (client, write_api) using the batching (async) write API."""
    if not INFLUX_TOKEN:
        logger.error("INFLUX_TOKEN is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    # Batching write API: points are queued and flushed on a background thread,
    # with automatic retries if the upload fails (e.g. brief network loss).
    write_api = client.write_api(
        write_options=WriteOptions(
            batch_size=500,
            flush_interval=2_000,    # ms
            jitter_interval=500,
            retry_interval=5_000,
            max_retries=5,
            max_retry_delay=30_000,
            exponential_base=2,
        )
    )

    # Fail fast if we can't reach InfluxDB / auth is wrong.
    if not client.ping():
        logger.error("Could not reach InfluxDB at %s. Check URL/token/network.", INFLUX_URL)
        sys.exit(1)
    logger.info("Connected to InfluxDB, writing to bucket '%s'.", INFLUX_BUCKET)
    return client, write_api


def build_point(message, decoded: dict, raw_id: int, ts_unix: float) -> Point | None:
    """Turn a decoded CAN message into an InfluxDB Point. None if no usable fields."""
    point = Point(message.name).tag("raw_id", f"0x{raw_id:X}")
    if SESSION_TAG:
        point = point.tag("session", SESSION_TAG)

    has_field = False
    for name, value in decoded.items():
        # cantools returns numbers for normal signals and NamedSignalValue for
        # enum signals. Store numbers as floats, enums/strings as strings.
        if isinstance(value, bool):
            point = point.field(name, int(value))
            has_field = True
        elif isinstance(value, (int, float)):
            point = point.field(name, float(value))
            has_field = True
        else:
            point = point.field(name, str(value))
            has_field = True

    if not has_field:
        return None

    # Use the kernel hardware timestamp from socketcan for accuracy.
    point = point.time(int(ts_unix * 1e9), WritePrecision.NS)
    return point


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    db = load_dbc()
    bus = setup_bus()
    client, write_api = make_influx_writer()

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = LOG_DIR / f"can_{stamp}.csv"
    logger.info("CSV backup: %s", csv_path)

    decoded_count = 0
    unknown_ids = set()

    try:
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["timestamp_iso", "timestamp_unix", "id_hex", "dlc",
                 "data_hex", "message_name", "decoded_signals"]
            )

            while running:
                msg = bus.recv(timeout=1.0)
                if msg is None:
                    continue

                ts_unix = msg.timestamp  # unix float from socketcan
                ts_iso = datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat()
                data_hex = msg.data.hex()

                message = None
                decoded = None
                try:
                    message = db.get_message_by_frame_id(msg.arbitration_id)
                    decoded = db.decode_message(msg.arbitration_id, msg.data)
                except KeyError:
                    if msg.arbitration_id not in unknown_ids:
                        unknown_ids.add(msg.arbitration_id)
                        logger.debug("No DBC entry for ID 0x%X", msg.arbitration_id)
                except Exception as e:
                    logger.warning("Decode failed for 0x%X: %s", msg.arbitration_id, e)

                msg_name = message.name if message else "Unknown"
                decoded_str = (
                    ", ".join(f"{k}={v}" for k, v in decoded.items()) if decoded else ""
                )

                writer.writerow(
                    [ts_iso, f"{ts_unix:.6f}", f"0x{msg.arbitration_id:X}",
                     msg.dlc, data_hex, msg_name, decoded_str]
                )
                f.flush()

                if message and decoded:
                    point = build_point(message, decoded, msg.arbitration_id, ts_unix)
                    if point is not None:
                        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
                        decoded_count += 1
                        if decoded_count % 500 == 0:
                            logger.info("Uploaded %d decoded messages so far...", decoded_count)

    except Exception as e:
        logger.error("Fatal error in main loop: %s", e)
    finally:
        logger.info("Flushing remaining InfluxDB writes...")
        try:
            write_api.close()
            client.close()
        except Exception:
            pass
        bus.shutdown()
        logger.info("Done. %d messages uploaded. Unknown IDs seen: %d.",
                    decoded_count, len(unknown_ids))


if __name__ == "__main__":
    main()
