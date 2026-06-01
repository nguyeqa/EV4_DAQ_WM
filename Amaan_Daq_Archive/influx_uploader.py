#!/usr/bin/env python3
import os
import pandas as pd
import cantools
from datetime import datetime
import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS, WriteOptions
import argparse
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('influx_uploader.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def load_dbc_files():
    """Load and parse DBC files from the DBC directory."""
    dbc_dir = "DBC"
    if not os.path.exists(dbc_dir):
        logger.error(f"DBC directory '{dbc_dir}' not found!")
        return None

    dbc_files = [f for f in os.listdir(dbc_dir) if f.endswith('.dbc')]
    if not dbc_files:
        logger.error(f"No DBC files found in '{dbc_dir}' directory!")
        return None

    # Create a new database
    db = cantools.database.Database()
    
    # Load each DBC file
    for dbc_file in dbc_files:
        try:
            file_path = os.path.join(dbc_dir, dbc_file)
            logger.info(f"Loading DBC file: {dbc_file}")
            temp_db = cantools.database.load_file(file_path)
            
            # Add each message to the main database
            for msg in temp_db.messages:
                existing_msg = next((m for m in db.messages if m.frame_id == msg.frame_id), None)
                if existing_msg is None:
                    db.messages.append(msg)
                    logger.info(f"Added message ID {msg.frame_id} (0x{msg.frame_id:X}) - {msg.name}")
                else:
                    for signal in msg.signals:
                        if signal.name not in [s.name for s in existing_msg.signals]:
                            existing_msg.signals.append(signal)
                            logger.info(f"Added signal {signal.name} to existing message ID {msg.frame_id}")
            
        except Exception as e:
            logger.error(f"Error loading {dbc_file}: {str(e)}")
            continue

    return db

def decode_can_data(db, message_id, data_hex):
    """Decode CAN data using the DBC database."""
    try:
        # Convert hex string to bytes
        data_bytes = bytes.fromhex(data_hex.replace(' ', ''))
        
        # Find message definition
        message = next((m for m in db.messages if m.frame_id == message_id), None)
        if message is None:
            return None

        # Decode the message
        decoded = {}
        for signal in message.signals:
            try:
                raw_value = db.decode_message(message_id, data_bytes)[signal.name]
                
                # Apply scaling and offset
                if hasattr(signal, 'scale') and signal.scale != 1:
                    value = raw_value * signal.scale
                else:
                    value = raw_value
                    
                if hasattr(signal, 'offset') and signal.offset != 0:
                    value += signal.offset
                
                decoded[signal.name] = value
            except Exception as e:
                logger.warning(f"Failed to decode signal {signal.name}: {e}")
                continue
                
        return decoded
    except Exception as e:
        logger.error(f"Error decoding message: {e}")
        return None

def process_csv_file(csv_file, db, influx_client, bucket, org):
    """Process CSV file and upload data to InfluxDB."""
    try:
        # Read CSV file
        df = pd.read_csv(csv_file)
        
        # Create write API
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        
        # Process each row
        for _, row in df.iterrows():
            try:
                # Parse timestamp
                timestamp = datetime.strptime(row['Timestamp'], '%Y-%m-%d %H:%M:%S.%f')
                
                # Convert message ID to integer
                message_id = int(row['ID(Dec)'])
                
                # Decode CAN data
                decoded_data = decode_can_data(db, message_id, row['Data'])
                
                if decoded_data:
                    # Create InfluxDB point
                    point = Point(row['Message Name'])
                    
                    # Add all decoded signals as fields
                    for signal_name, value in decoded_data.items():
                        point = point.field(signal_name, value)
                    
                    # Add message ID as tag
                    point = point.tag("message_id", str(message_id))
                    
                    # Set timestamp
                    point = point.time(timestamp)
                    
                    # Write to InfluxDB
                    write_api.write(bucket=bucket, org=org, record=point)
                    
            except Exception as e:
                logger.error(f"Error processing row: {e}")
                continue
                
    except Exception as e:
        logger.error(f"Error processing CSV file: {e}")

def main():
    parser = argparse.ArgumentParser(description='Upload CAN data to InfluxDB')
    parser.add_argument('--url', default='http://localhost:8086', help='InfluxDB URL')
    parser.add_argument('--token', required=True, help='InfluxDB token')
    parser.add_argument('--org', required=True, help='InfluxDB organization')
    parser.add_argument('--bucket', required=True, help='InfluxDB bucket')
    parser.add_argument('--csv', help='Specific CSV file to process (optional)')
    args = parser.parse_args()

    # Load DBC files
    db = load_dbc_files()
    if db is None:
        logger.error("Failed to load DBC files. Exiting.")
        return

    # Initialize InfluxDB client with custom WriteOptions
    try:
        write_options = WriteOptions(
            batch_size=1000,        # batch size of 1000 messages
            flush_interval=5000,    # flush every 5 seconds
            jitter_interval=1000,   # jitter interval of 1 second
            retry_interval=10000,   # retry every 10 seconds
            max_retries=10,         # maximum number of retries
            max_retry_delay=60000,  # maximum retry delay of 60 seconds
            exponential_base=2      # exponential backoff base
        )
        influx_client = InfluxDBClient(url=args.url, token=args.token, org=args.org, timeout=60000, write_options=write_options)
        logger.info("Connected to InfluxDB successfully")
    except Exception as e:
        logger.error(f"Failed to connect to InfluxDB: {e}")
        return

    # Process CSV files
    if args.csv:
        # Process specific CSV file
        process_csv_file(args.csv, db, influx_client, args.bucket, args.org)
    else:
        # Process all CSV files in can_logs directory
        log_dir = Path('can_logs')
        if not log_dir.exists():
            logger.error("can_logs directory not found!")
            return

        for csv_file in log_dir.glob('*.csv'):
            logger.info(f"Processing {csv_file}")
            process_csv_file(csv_file, db, influx_client, args.bucket, args.org)

    logger.info("Processing complete")

if __name__ == "__main__":
    main() 