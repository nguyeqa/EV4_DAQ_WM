#!/usr/bin/env python3
import pandas as pd
import argparse
from pathlib import Path
import re

def parse_decoded_signals(signal_str):
    """Parse the decoded signals string into a dictionary of signal names and values."""
    if pd.isna(signal_str):
        return {}
    
    # Split by comma and handle each signal
    signals = {}
    for signal in signal_str.split(','):
        # Split by equals sign
        parts = signal.strip().split('=')
        if len(parts) != 2:
            continue
            
        name, value = parts
        
        # Extract the numeric value and unit if present
        match = re.match(r'([-+]?\d*\.?\d+)\s*(.*)', value.strip())
        if match:
            numeric_value, unit = match.groups()
            # Store both the numeric value and the unit
            signals[name.strip()] = {
                'value': float(numeric_value),
                'unit': unit.strip() if unit else ''
            }
    
    return signals

def convert_to_wide_format(input_csv, output_csv=None):
    """Convert the CAN data CSV to wide format with separate columns for each signal and a flag for each message name."""
    # Read the CSV file
    df = pd.read_csv(input_csv)
    
    # Create a set to store all unique signal names
    all_signals = set()
    # Create a set to store all unique message names
    all_message_names = set(df['Message Name'].unique())
    
    # First pass: collect all unique signal names
    for signals_str in df['Decoded Signals']:
        signals = parse_decoded_signals(signals_str)
        all_signals.update(signals.keys())
    
    # Create new columns for each signal
    for signal_name in sorted(all_signals):
        # Create two columns: one for value and one for unit
        df[f'{signal_name}_value'] = None
        df[f'{signal_name}_unit'] = None
    
    # Create a flag column for each message name
    for message_name in sorted(all_message_names):
        col_name = f'{message_name}_present'
        df[col_name] = (df['Message Name'] == message_name).astype(int)
    
    # Second pass: fill in the values
    for idx, row in df.iterrows():
        signals = parse_decoded_signals(row['Decoded Signals'])
        for signal_name, signal_data in signals.items():
            df.at[idx, f'{signal_name}_value'] = signal_data['value']
            df.at[idx, f'{signal_name}_unit'] = signal_data['unit']
    
    # Drop the original 'Decoded Signals' column
    df = df.drop('Decoded Signals', axis=1)
    
    # If no output path specified, create one based on input
    if output_csv is None:
        input_path = Path(input_csv)
        output_csv = str(input_path.parent / f"{input_path.stem}_wide{input_path.suffix}")
    
    # Save to new CSV
    df.to_csv(output_csv, index=False)
    print(f"Converted CSV saved to: {output_csv}")
    print(f"Number of signals processed: {len(all_signals)}")
    print(f"Number of message names flagged: {len(all_message_names)}")
    print("\nSignal names:")
    for signal in sorted(all_signals):
        print(f"- {signal}")
    print("\nMessage name flags:")
    for message in sorted(all_message_names):
        print(f"- {message}_present")

def main():
    parser = argparse.ArgumentParser(description='Convert CAN data CSV to wide format')
    parser.add_argument('input_csv', help='Path to input CSV file')
    parser.add_argument('--output', '-o', help='Path to output CSV file (optional)')
    
    args = parser.parse_args()
    convert_to_wide_format(args.input_csv, args.output)

if __name__ == "__main__":
    main() 