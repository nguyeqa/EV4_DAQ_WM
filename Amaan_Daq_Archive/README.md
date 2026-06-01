# CAN Message Listener

A cross-platform CAN message listener that works on both Windows and Raspberry Pi OS. The script logs CAN messages to a text file and displays them in the console.

## Prerequisites

- Python 3.7 or higher
- For Windows: CANKing software for CAN simulation
- For Raspberry Pi: CAN HAT with RS485 transceiver

## Installation

1. Create and activate a virtual environment:
```bash
# Windows
python -m venv venv
.\venv\Scripts\activate

# Raspberry Pi
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Setup

### Windows Setup
1. Install CANKing software
2. Configure CANKing to use the virtual CAN interface
3. Start CANKing and enable the CAN interface

### Raspberry Pi Setup
1. Enable CAN interface:
```bash
sudo ip link set can0 type can bitrate 500000
sudo ip link set up can0
```

## Usage

1. Activate the virtual environment if not already activated
2. Run the script:
```bash
python main.py
```

The script will:
- Create a `can_logs` directory if it doesn't exist
- Create a new log file with timestamp in the filename
- Listen for CAN messages and log them to both console and file
- Press Ctrl+C to stop the script

## Log File Format

The log file is in CSV format with the following columns:
- Timestamp: When the message was received
- ID: CAN message ID in hexadecimal
- Data: CAN message data in hexadecimal format

## Troubleshooting

1. If you get permission errors on Raspberry Pi:
```bash
sudo chmod 666 /dev/can0
```

2. If the CAN interface is not found:
- Check if the CAN interface is properly set up
- Verify the interface name (default is 'can0')
- Ensure the CAN HAT is properly connected
