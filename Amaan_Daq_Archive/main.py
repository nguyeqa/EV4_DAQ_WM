#!/usr/bin/env python3
import can
import platform
import os
from datetime import datetime
import logging
from pathlib import Path
import cantools
import csv
import signal
import sys
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import queue
import threading
import time
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# InfluxDB Configuration
INFLUXDB_CONFIG = {
    'url': "https://us-east-1-1.aws.cloud2.influxdata.com",
    'token': "JMBZ_7392nKOjK3Q2ks0UiPFyU5cYrqWK_0W3FJRZcaZw7cUz9i99iZdCgB2d0IwTW4KhLuYjEUwsiX2miWpyw==",
    'org': "university_of_cincinnati",  # Changed to use underscores instead of spaces
    'bucket': "can_data"
}

# Global variables
db = None
running = True
message_queue = queue.Queue()
BATCH_SIZE = 1000  # Number of messages to batch before uploading

# Configure logging to file only
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('can_decoder.log'),
        logging.StreamHandler(sys.stdout)  # Keep console output for now
    ]
)
logger = logging.getLogger(__name__)

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    global running
    logger.info("Received exit signal. Shutting down...")
    running = False

def setup_can_interface():
    """Setup CAN interface based on the operating system"""
    system = platform.system()
    
    if system == 'Windows':
        # For Windows using CANKing simulation
        try:
            # Try using the virtual interface first
            return can.interface.Bus(channel=0, interface='kvaser')
        except Exception as e:
            logger.error(f"Failed to initialize CAN interface: {e}")
            logger.info("Please ensure CANKing is running and the virtual CAN interface is enabled")
            logger.info("You can simulate CAN messages using CANKing before running this script")
            raise
    elif system == 'Linux':
        # For Raspberry Pi with CAN HAT
        # Make sure to set up the CAN interface first:
        # sudo ip link set can0 type can bitrate 500000
        # sudo ip link set up can0
        try:
            return can.interface.Bus(channel='can0', interface='socketcan')
        except Exception as e:
            logger.error(f"Failed to initialize CAN interface: {e}")
            logger.info("Please ensure the CAN interface is properly set up:")
            logger.info("1. Run: sudo ip link set can0 type can bitrate 500000")
            logger.info("2. Run: sudo ip link set up can0")
            raise
    else:
        raise OSError(f"Unsupported operating system: {system}")

def create_log_directory():
    """Create a directory for CAN message logs"""
    log_dir = Path('can_logs')
    log_dir.mkdir(exist_ok=True)
    return log_dir

def create_dbc_directory():
    """Create DBC directory if it doesn't exist"""
    dbc_dir = Path('DBC')
    dbc_dir.mkdir(exist_ok=True)
    return dbc_dir

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
            
            # Load the DBC file
            temp_db = cantools.database.load_file(file_path)
            
            # Add each message to the main database
            for msg in temp_db.messages:
                # Check if message ID already exists
                existing_msg = next((m for m in db.messages if m.frame_id == msg.frame_id), None)
                if existing_msg is None:
                    db.messages.append(msg)
                    logger.info(f"Added message ID {msg.frame_id} (0x{msg.frame_id:X}) - {msg.name}")
                else:
                    # If message exists, merge signals that don't conflict
                    for signal in msg.signals:
                        if signal.name not in [s.name for s in existing_msg.signals]:
                            existing_msg.signals.append(signal)
                            logger.info(f"Added signal {signal.name} to existing message ID {msg.frame_id}")
            
            logger.info(f"Successfully loaded {dbc_file}")
            
        except Exception as e:
            logger.error(f"Error loading {dbc_file}: {str(e)}")
            continue

    if not db.messages:
        logger.error("No messages were loaded from DBC files!")
        return None

    # Log all known message IDs
    logger.info("\nKnown message IDs:")
    for msg in sorted(db.messages, key=lambda x: x.frame_id):
        logger.info(f"ID {msg.frame_id} (0x{msg.frame_id:X}) - {msg.name}")
        for signal in msg.signals:
            logger.info(f"  - {signal.name}")
    
    return db

def decode_message(db, msg):
    """Decode a CAN message using the database."""
    try:
        # Find the message definition - try both decimal and hex formats
        message = next((m for m in db.messages if m.frame_id == msg.arbitration_id), None)
        if message is None:
            # Try finding by hex string
            hex_id = f"0x{msg.arbitration_id:X}"
            message = next((m for m in db.messages if f"0x{m.frame_id:X}" == hex_id), None)
        
        if message is None:
            logger.debug(f"No message definition found for ID: 0x{msg.arbitration_id:X}")
            return {
                'name': 'Unknown',
                'signals': f"Unknown message (ID: Decimal={msg.arbitration_id}, Hex=0x{msg.arbitration_id:X})"
            }
        
        # Convert data to bytes if it's not already
        data = bytes(msg.data) if not isinstance(msg.data, bytes) else msg.data
        
        # Log message details for debugging
        logger.debug(f"Attempting to decode message {message.name} (ID: 0x{msg.arbitration_id:X})")
        logger.debug(f"Message length: {len(data)} bytes")
        logger.debug(f"Raw data: {' '.join([f'{b:02X}' for b in data])}")
        
        # Decode the message
        try:
            decoded = {}
            for signal in message.signals:
                try:
                    # Log signal details
                    logger.debug(f"Processing signal: {signal.name}")
                    logger.debug(f"Signal start bit: {signal.start}, length: {signal.length}")
                    
                    # Calculate byte position and bit position
                    start_byte = signal.start // 8
                    start_bit = signal.start % 8
                    
                    # Extract the raw value
                    if signal.length <= 8:
                        # For signals <= 8 bits
                        mask = (1 << signal.length) - 1
                        raw_value = (data[start_byte] >> start_bit) & mask
                    else:
                        # For signals > 8 bits
                        bytes_needed = (signal.length + 7) // 8
                        raw_value = 0
                        for i in range(bytes_needed):
                            if start_byte + i < len(data):
                                raw_value |= data[start_byte + i] << (i * 8)
                        raw_value = (raw_value >> start_bit) & ((1 << signal.length) - 1)
                    
                    # Handle signed values
                    if signal.is_signed and raw_value & (1 << (signal.length - 1)):
                        raw_value -= (1 << signal.length)
                    
                    logger.debug(f"Raw value for {signal.name}: {raw_value}")
                    
                    # Apply scaling and offset
                    if hasattr(signal, 'scale') and signal.scale != 1:
                        value = raw_value * signal.scale
                        logger.debug(f"After scaling: {value}")
                    else:
                        value = raw_value
                        
                    if hasattr(signal, 'offset') and signal.offset != 0:
                        value += signal.offset
                        logger.debug(f"After offset: {value}")
                    
                    # Format the value based on its type
                    if isinstance(value, (int, bool)):
                        decoded[signal.name] = value
                    else:
                        decoded[signal.name] = round(value, 2)
                        
                except Exception as signal_error:
                    logger.error(f"Failed to decode signal {signal.name} in message {message.name}: {str(signal_error)}")
                    logger.error(f"Signal details - Start bit: {signal.start}, Length: {signal.length}, Scale: {getattr(signal, 'scale', 1)}, Offset: {getattr(signal, 'offset', 0)}")
                    continue
            
            if not decoded:
                # If no signals were successfully decoded, show raw data
                data_hex = ' '.join([f'{b:02X}' for b in msg.data])
                logger.error(f"Failed to decode any signals for message {message.name} (ID: 0x{msg.arbitration_id:X}). Raw data: {data_hex}")
                return {
                    'name': message.name,
                    'signals': f"Raw data: {data_hex}"
                }
            
            # Format the decoded signals
            formatted_signals = []
            for name, value in decoded.items():
                signal = next((s for s in message.signals if s.name == name), None)
                if signal and hasattr(signal, 'unit') and signal.unit:
                    formatted_signals.append(f"{name}={value} {signal.unit}")
                else:
                    formatted_signals.append(f"{name}={value}")
            
            return {
                'name': message.name,
                'signals': ", ".join(formatted_signals),
                'decoded_values': decoded  # Add decoded values for InfluxDB
            }
            
        except Exception as decode_error:
            # Return raw data if decoding fails
            data_hex = ' '.join([f'{b:02X}' for b in msg.data])
            logger.error(f"Failed to decode message {message.name} (ID: 0x{msg.arbitration_id:X}): {str(decode_error)}")
            return {
                'name': message.name,
                'signals': f"Raw data: {data_hex}"
            }
        
    except Exception as e:
        logger.error(f"Error processing message (ID: 0x{msg.arbitration_id:X}): {str(e)}")
        return {
            'name': 'Unknown',
            'signals': f"Error processing message (ID: Decimal={msg.arbitration_id}, Hex=0x{msg.arbitration_id:X})"
        }

def influx_uploader_worker(influx_client, bucket, org):
    """Worker thread for uploading data to InfluxDB"""
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)
    batch = []
    
    while running:
        try:
            # Get message from queue with timeout
            try:
                message_data = message_queue.get(timeout=1.0)
            except queue.Empty:
                # If queue is empty and we have data, upload it
                if batch:
                    try:
                        write_api.write(bucket=bucket, org=org, record=batch)
                        logger.info(f"Uploaded batch of {len(batch)} messages to InfluxDB")
                        batch = []
                    except Exception as e:
                        logger.error(f"Error uploading batch to InfluxDB: {e}")
                continue
            
            # Create InfluxDB point
            point = Point(message_data['name'])
            
            # Add all decoded signals as fields
            if 'decoded_values' in message_data:
                for signal_name, value in message_data['decoded_values'].items():
                    # Ensure value is numeric and not None
                    if value is not None and isinstance(value, (int, float)):
                        point = point.field(signal_name, float(value))  # Convert to float for consistency
            
            # Add message ID as tag
            point = point.tag("message_id", str(message_data['message_id']))
            
            # Set timestamp
            point = point.time(message_data['timestamp'])
            
            # Only add point if it has at least one field
            if point._fields:  # Check if point has any fields
                batch.append(point)
            
            # If batch is full, upload it
            if len(batch) >= BATCH_SIZE:
                try:
                    if batch:  # Only try to write if we have points
                        write_api.write(bucket=bucket, org=org, record=batch)
                        logger.info(f"Uploaded batch of {len(batch)} messages to InfluxDB")
                        batch = []
                except Exception as e:
                    logger.error(f"Error uploading batch to InfluxDB: {e}")
                    logger.error(f"Batch content: {batch}")  # Log the batch content for debugging
            
            message_queue.task_done()
            
        except Exception as e:
            logger.error(f"Error in InfluxDB uploader worker: {e}")
            continue

def main():
    global running, db
    try:
        # Set up signal handlers for clean exit
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Load DBC files
        db = load_dbc_files()
        if db is None:
            logger.error("Failed to load DBC files. Please ensure valid DBC files are present in the DBC directory.")
            return
        
        # Create log directory
        log_dir = create_log_directory()
        
        # Create a new log file with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_file = log_dir / f'can_messages_{timestamp}.csv'
        
        # Setup CAN interface
        bus = setup_can_interface()
        logger.info(f"CAN interface initialized. Logging to {log_file}")
        
        # Initialize InfluxDB client
        try:
            influx_client = InfluxDBClient(
                url=INFLUXDB_CONFIG['url'],
                token=INFLUXDB_CONFIG['token'],
                org=INFLUXDB_CONFIG['org']
            )
            logger.info("Connected to InfluxDB successfully")
        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB: {e}")
            return

        # Start InfluxDB uploader thread
        uploader_thread = threading.Thread(
            target=influx_uploader_worker,
            args=(influx_client, INFLUXDB_CONFIG['bucket'], INFLUXDB_CONFIG['org']),
            daemon=True
        )
        uploader_thread.start()
        
        # Initialize CSV writer
        with open(log_file, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['Timestamp', 'ID(Hex)', 'ID(Dec)', 'Data', 'Message Name', 'Decoded Signals'])
            
            while running:
                try:
                    # Receive CAN message
                    msg = bus.recv(timeout=1.0)
                    
                    if msg:
                        # Decode the message
                        decoded = decode_message(db, msg)
                        
                        # Format the data bytes as a space-separated hex string
                        data_hex = ' '.join([f'{b:02X}' for b in msg.data])
                        
                        # Get current timestamp
                        current_time = datetime.now()
                        
                        # Write to CSV
                        writer.writerow([
                            current_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                            f'{msg.arbitration_id:X}',
                            str(msg.arbitration_id),
                            data_hex,
                            decoded['name'],
                            decoded['signals']
                        ])
                        csvfile.flush()
                        
                        # Add to InfluxDB queue
                        message_data = {
                            'timestamp': current_time,
                            'message_id': msg.arbitration_id,
                            'name': decoded['name'],
                            'signals': decoded['signals']
                        }
                        if 'decoded_values' in decoded:
                            message_data['decoded_values'] = decoded['decoded_values']
                        
                        message_queue.put(message_data)
                        
                        # Print to console
                        logger.info(f"ID: 0x{msg.arbitration_id:X} - {decoded['name']} - {decoded['signals']}")
                        
                except can.CanError as e:
                    logger.error(f"CAN Error: {e}")
                    continue
                    
    except Exception as e:
        logger.error(f"Error: {e}")
    finally:
        if 'bus' in locals():
            bus.shutdown()
        logger.info("CAN message logging stopped")

if __name__ == "__main__":
    main()


