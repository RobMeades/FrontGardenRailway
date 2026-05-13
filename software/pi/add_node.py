#!/usr/bin/env python3

# Copyright 2026 Rob Meades
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#  Written by DeepSeek :-).

"""
Script to add a new node to the front garden railway.
"""

import argparse
import json
import re
import subprocess
import sys
import os
import time
import threading
from typing import Optional, Tuple, List, Dict, Any

def require_sudo():
    """Check if script is running with sudo privileges"""
    if os.geteuid() != 0:
        print("Error: This script requires sudo privileges.", file=sys.stderr)
        print("Please run with sudo", file=sys.stderr)
        sys.exit(1)

def validate_mac(mac: str) -> bool:
    """Validate MAC address format (case-insensitive)"""
    mac_pattern = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$')
    return bool(mac_pattern.match(mac))

def get_wlan0_ip() -> str:
    """Get IP address of wlan0 interface"""
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', 'wlan0'],
            capture_output=True,
            text=True,
            check=True
        )
        # Parse IP address from output
        match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', result.stdout)
        if match:
            return match.group(1)
        else:
            print("Error: Could not find IP address for wlan0", file=sys.stderr)
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error getting wlan0 IP: {e}", file=sys.stderr)
        sys.exit(1)

def parse_static_addresses(ip_range: str) -> List[Tuple[str, int]]:
    """Parse /etc/NetworkManager/dnsmasq-shared.d/static-addresses file"""
    filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
    addresses = []

    try:
        with open(filename, 'r') as f:
            for line in f:
                # Look for dhcp-host=MAC,IP
                match = re.search(r'dhcp-host=([0-9A-Fa-f:]+),(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    mac_addr = match.group(1)
                    ip_addr = match.group(2)
                    # Check if IP is in our range
                    if ip_addr.startswith(ip_range + '.'):
                        last_digit = int(ip_addr.split('.')[-1])
                        addresses.append((mac_addr, last_digit))
    except FileNotFoundError:
        print(f"Warning: {filename} not found. Starting with empty list.", file=sys.stderr)
    except PermissionError:
        print(f"Error: Permission denied reading {filename}. Run with sudo.", file=sys.stderr)
        sys.exit(1)

    return addresses

def check_mac_in_dnsmasq(mac: str, ip_range: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """Check if MAC already exists in dnsmasq static-addresses file
    Returns: (exists, ip_address, last_digit)
    """
    filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"

    try:
        with open(filename, 'r') as f:
            for line in f:
                # Look for dhcp-host=MAC,IP
                match = re.search(r'dhcp-host=([0-9A-Fa-f:]+),(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    mac_addr = match.group(1)
                    ip_addr = match.group(2)
                    if mac_addr.lower() == mac.lower():
                        # Check if IP is in our range
                        if ip_addr.startswith(ip_range + '.'):
                            last_digit = int(ip_addr.split('.')[-1])
                            return True, ip_addr, last_digit
                        else:
                            return True, ip_addr, None
        return False, None, None
    except FileNotFoundError:
        return False, None, None
    except Exception as e:
        print(f"Error checking dnsmasq file: {e}", file=sys.stderr)
        return False, None, None

def get_highest_ip(addresses: List[Tuple[str, int]], host_ip_last_digit: int) -> Tuple[Optional[str], Optional[int]]:
    """Get the highest IP address and its MAC from the list"""
    if not addresses:
        return None, None

    # Exclude host IP from consideration
    filtered = [(mac, digit) for mac, digit in addresses if digit != host_ip_last_digit]
    if not filtered:
        return None, None

    highest = max(filtered, key=lambda x: x[1])
    return highest

def check_full_range(addresses: List[Tuple[str, int]], host_ip_last_digit: int, ip_range: str) -> bool:
    """Check if all IPs in range 2-254 are used (excluding host)"""
    used_ips = {digit for mac, digit in addresses if digit != host_ip_last_digit}
    # IP range 2-254 (1 is usually gateway/host)
    all_ips = set(range(2, 255))
    return used_ips == all_ips

def get_iptables_line_number(mac: str) -> Optional[int]:
    """Get line number of MAC in iptables dhcp_clients chain"""
    try:
        result = subprocess.run(
            ['sudo', 'iptables', '-t', 'raw', '-L', 'dhcp_clients', '--line-numbers'],
            capture_output=True,
            text=True,
            check=True
        )

        lines = result.stdout.split('\n')
        for line in lines:
            # Look for MAC in column 7 (after MAC)
            match = re.search(r'^\s*(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+MAC\s+([0-9a-fA-F:]+)', line)
            if match:
                line_num = int(match.group(1))
                mac_in_line = match.group(2).lower()
                if mac_in_line == mac.lower():
                    return line_num
        return None
    except subprocess.CalledProcessError as e:
        print(f"Error reading iptables: {e}", file=sys.stderr)
        return None

def check_mac_in_iptables(mac: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Check if MAC already exists in iptables dhcp_clients chain
    Returns: (exists, line_number, comment)
    """
    try:
        result = subprocess.run(
            ['sudo', 'iptables', '-t', 'raw', '-L', 'dhcp_clients', '--line-numbers', '-n'],
            capture_output=True,
            text=True,
            check=True
        )

        lines = result.stdout.split('\n')
        for line in lines:
            # Look for MAC in the line
            # Format: num  target     prot opt source               destination
            # Then later: MAC XX:XX:XX:XX:XX:XX /* comment */
            match = re.search(r'^\s*(\d+)\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+\s+MAC\s+([0-9a-fA-F:]+)(?:\s+/\*\s*(.*?)\s*\*/)?', line)
            if match:
                line_num = int(match.group(1))
                mac_in_line = match.group(2).lower()
                comment = match.group(3) if match.group(3) else ""
                if mac_in_line == mac.lower():
                    return True, line_num, comment
        return False, None, None
    except subprocess.CalledProcessError as e:
        print(f"Error reading iptables: {e}", file=sys.stderr)
        return False, None, None

def show_iptables_line(line_num: int) -> None:
    """Show a specific line from iptables dhcp_clients chain"""
    try:
        result = subprocess.run(
            ['sudo', 'iptables', '-t', 'raw', '-L', 'dhcp_clients', '--line-numbers', '-n'],
            capture_output=True,
            text=True,
            check=True
        )

        lines = result.stdout.split('\n')
        for line in lines:
            if line.strip().startswith(str(line_num)):
                print(f"  {line}")
                break
    except subprocess.CalledProcessError as e:
        print(f"Error showing iptables line: {e}", file=sys.stderr)

def insert_iptables_rule(line_num: int, mac: str, ip_address: str) -> bool:
    """Insert new rule into iptables"""
    try:
        subprocess.run(
            ['sudo', 'iptables', '-t', 'raw', '-I', 'dhcp_clients', str(line_num),
             '-m', 'mac', '--mac-source', mac, '-j', 'ACCEPT',
             '-m', 'comment', '--comment', ip_address],
            check=True,
            capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error inserting iptables rule: {e}", file=sys.stderr)
        print(f"stderr: {e.stderr}", file=sys.stderr)
        return False

def insert_dnsmasq_entry(mac: str, ip_address: str, before_line: str) -> bool:
    """Insert new entry into dnsmasq static-addresses file"""
    filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
    new_line = f"dhcp-host={mac},{ip_address}\n"

    try:
        # Read existing file
        with open(filename, 'r') as f:
            lines = f.readlines()

        # Find the line to insert before
        insert_pos = -1
        for i, line in enumerate(lines):
            if line.strip() == before_line.strip():
                insert_pos = i
                break

        if insert_pos == -1:
            print(f"Error: Could not find line '{before_line}' in {filename}", file=sys.stderr)
            return False

        # Insert new line
        lines.insert(insert_pos, new_line)

        # Write back
        with open(filename, 'w') as f:
            f.writelines(''.join(lines))

        return True
    except Exception as e:
        print(f"Error editing {filename}: {e}", file=sys.stderr)
        return False

def append_dnsmasq_entry(mac: str, ip_address: str) -> bool:
    """Append new entry to dnsmasq static-addresses file"""
    filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
    new_line = f"dhcp-host={mac},{ip_address}\n"

    try:
        with open(filename, 'a') as f:
            f.write(new_line)
        return True
    except Exception as e:
        print(f"Error appending to {filename}: {e}", file=sys.stderr)
        return False

def save_netfilter() -> bool:
    """Save netfilter rules"""
    try:
        subprocess.run(['sudo', 'netfilter-persistent', 'save'], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error saving netfilter: {e}", file=sys.stderr)
        return False

def restart_networkmanager() -> bool:
    """Restart NetworkManager and wait for it to become fully operational"""
    print("Restarting NetworkManager (this may take 5-10 minutes due to Wi-Fi driver initialization)...")

    try:
        start_time = time.time()

        # Restart NetworkManager
        subprocess.run(['sudo', 'systemctl', 'restart', 'NetworkManager'], check=True, capture_output=True)

        # Wait a moment for the restart to actually begin
        time.sleep(2)

        # Wait for a real indicator that NetworkManager is ready
        while True:
            # Check if we can get NetworkManager status
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'RUNNING', 'general', 'status'],
                capture_output=True,
                text=True
            )

            if result.returncode == 0 and 'running' in result.stdout.lower():
                # Now wait for a network interface to be at least connecting
                dev_result = subprocess.run(
                    ['nmcli', '-t', '-f', 'DEVICE,STATE', 'device', 'status'],
                    capture_output=True,
                    text=True
                )

                if 'wlan0:connected' in dev_result.stdout or \
                   'wlan0:connecting' in dev_result.stdout or \
                   'eth0:connected' in dev_result.stdout or \
                   'eth0:connecting' in dev_result.stdout:
                    elapsed = int(time.time() - start_time)
                    minutes = elapsed // 60
                    seconds = elapsed % 60
                    if minutes > 0:
                        print(f"NetworkManager is operational after approximately {minutes}m {seconds}s")
                    else:
                        print(f"NetworkManager is operational after approximately {seconds} seconds")
                    return True

            # Timeout after 15 minutes
            if time.time() - start_time > 900:
                print("Warning: NetworkManager did not become operational within 15 minutes", file=sys.stderr)
                return False

            time.sleep(2)

    except subprocess.CalledProcessError as e:
        print(f"Error restarting NetworkManager: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Unexpected error during NetworkManager restart: {e}", file=sys.stderr)
        return False

def restart_web_controller() -> bool:
    """Restart web_controller service if it's running"""
    # Check if service exists and is running
    try:
        # Check if service is active
        result = subprocess.run(
            ['systemctl', 'is-active', 'web_controller'],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Service not running or doesn't exist
            return False

        # Service is running, ask user
        response = input("\nweb_controller service is running. Would you like to restart it to apply nodes.json changes? (y/n): ").lower()
        if response != 'y':
            print("Skipping web_controller restart.")
            return False

        print("Restarting web_controller (please wait up to a minute for it to restart)...")
        subprocess.run(['sudo', 'systemctl', 'restart', 'web_controller'], check=True, capture_output=True)
        print("web_controller restart initiated.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not restart web_controller: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        # systemctl not found or service doesn't exist
        return False

def handle_nodes_json(ip_address_mac: str) -> bool:
    """Check and update nodes.json if it exists in the current directory
    Returns: True if nodes.json was modified, False otherwise
    """
    nodes_json_path = "nodes.json"

    if not os.path.exists(nodes_json_path):
        return False

    print("\nFound nodes.json in current directory.")

    try:
        with open(nodes_json_path, 'r') as f:
            nodes = json.load(f)
    except Exception as e:
        print(f"Warning: Could not read nodes.json: {e}", file=sys.stderr)
        return False

    # Check if IP already exists in nodes.json
    existing_name = None
    for name, data in nodes.items():
        if data.get('ip') == ip_address_mac:
            existing_name = name
            break

    if existing_name:
        print(f"Note: nodes.json already contains entry '{existing_name}' for IP address {ip_address_mac}")
        return False

    # IP not found, look for test nodes
    test_nodes = []
    for name, data in nodes.items():
        if data.get('type') == 'test':
            test_nodes.append(name)

    if not test_nodes:
        print("No existing test nodes found in nodes.json. Skipping test node creation.")
        return False

    # Find highest numbered test node name (format: something_x or something_x with optional underscore)
    highest_num = -1
    highest_name = None
    pattern = re.compile(r'.*[_-]?(\d+)$')

    for name in test_nodes:
        match = pattern.search(name)
        if match:
            num = int(match.group(1))
            if num > highest_num:
                highest_num = num
                highest_name = name

    if highest_num == -1:
        # No numbered test nodes found, use base name "test_1"
        base_name = "test"
        next_num = 1
    else:
        # Extract base name without the number
        base_name = re.sub(r'[_-]?\d+$', '', highest_name)
        if not base_name:
            base_name = "test"
        next_num = highest_num + 1

    suggested_name = f"{base_name}_{next_num}" if base_name != "test" else f"test_{next_num}"

    response = input(f"\nWould you like to add a temporary test node for IP address {ip_address_mac}? (y/n): ").lower()
    if response != 'y':
        print("Leaving nodes.json alone.")
        return False

    response = input(f"Would you like to use the name '{suggested_name}'? (y/n): ").lower()
    if response == 'y':
        node_name = suggested_name
    else:
        node_name = input("Enter the name for the new node: ").strip()
        if not node_name:
            print("No name provided. Aborting nodes.json update.")
            return False

    # Create new entry
    new_entry = {
        node_name: {
            "ip": ip_address_mac,
            "type": "test",
            "essential": False,
            "heartbeat_timeout": 60
        }
    }

    print(f"\nProposed addition to nodes.json:")
    print(json.dumps(new_entry, indent=4))

    response = input("\nAdd this entry to nodes.json? (y/n): ").lower()
    if response != 'y':
        print("Leaving nodes.json alone.")
        return False

    # Insert the new entry after the highest numbered test node
    try:
        # Convert OrderedDict to list of items for insertion
        items = list(nodes.items())

        # Find position of the highest numbered test node
        insert_pos = -1
        for i, (name, _) in enumerate(items):
            if name == highest_name:
                insert_pos = i + 1  # Insert after it
                break

        if insert_pos == -1:
            # If not found, append at the end
            nodes.update(new_entry)
        else:
            # Insert at the found position
            items.insert(insert_pos, (node_name, new_entry[node_name]))
            nodes = dict(items)

        # Write back to file
        with open(nodes_json_path, 'w') as f:
            json.dump(nodes, f, indent=4)

        print(f"Successfully added '{node_name}' to nodes.json")
        return True

    except Exception as e:
        print(f"Error updating nodes.json: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description='Add a new node to fgr network')
    parser.add_argument('mac', help='MAC address of the new node')
    args = parser.parse_args()

    # Require sudo
    require_sudo()

    # Validate MAC
    mac = args.mac
    if not validate_mac(mac):
        print(f"Error: Invalid MAC address format: {mac}", file=sys.stderr)
        print("Expected format: XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX", file=sys.stderr)
        sys.exit(1)

    # Get wlan0 IP first (needed for range checks)
    host_ip = get_wlan0_ip()
    ip_parts = host_ip.split('.')
    ip_address_range = '.'.join(ip_parts[:3])
    host_ip_last_digit = int(ip_parts[3])
    print(f"Host IP: {host_ip}")
    print(f"IP Range: {ip_address_range}.x")

    # Check if MAC already exists in dnsmasq
    print("\nChecking if MAC address already exists in dnsmasq config...")
    dnsmasq_exists, existing_ip, existing_digit = check_mac_in_dnsmasq(mac, ip_address_range)
    if dnsmasq_exists:
        print(f"MAC address {mac} already exists in dnsmasq static addresses:")
        print(f"  {existing_ip}")
    else:
        print("MAC address not found in dnsmasq config.")

    # Check if MAC already exists in iptables
    print("\nChecking if MAC address already exists in firewall...")
    iptables_exists, line_num, comment = check_mac_in_iptables(mac)
    if iptables_exists:
        print(f"MAC address {mac} already exists in iptables:")
        show_iptables_line(line_num)
        if comment:
            print(f"  Comment: {comment}")
    else:
        print("MAC address not found in firewall.")

    # If MAC exists in both places, nothing to do for firewall/dnsmasq
    if dnsmasq_exists and iptables_exists:
        print("\nMAC address already configured in both firewall and dnsmasq.")
        # Still check nodes.json in case user forgot to add it
        if os.path.exists("nodes.json"):
            print("\nChecking nodes.json for IP address entry...")
            # Need the IP address from the existing dnsmasq entry
            if existing_ip:
                nodes_modified = handle_nodes_json(existing_ip)
                if nodes_modified:
                    # web_controller restart will be handled after any NetworkManager restart
                    # Since NetworkManager doesn't need restart, we can restart web_controller now
                    restart_web_controller()
            else:
                print("Warning: Could not determine existing IP address from dnsmasq config.")
        print("Nothing else to do, no changes made.")
        sys.exit(0)

    # If MAC exists only in one place, ask user if they want to continue
    if dnsmasq_exists:
        response = input("\nMAC address already in dnsmasq config. Would you like to continue? (y/n): ").lower()
        if response != 'y':
            print("Aborted, no changes made", file=sys.stderr)
            sys.exit(0)
        else:
            print("Continuing with existing dnsmasq entry...")

    if iptables_exists:
        response = input("\nMAC address already in firewall. Would you like to continue? (y/n): ").lower()
        if response != 'y':
            print("Aborted, no changes made", file=sys.stderr)
            sys.exit(0)
        else:
            print("Continuing with existing firewall entry...")

    # Parse static addresses (after checks, for IP allocation)
    addresses = parse_static_addresses(ip_address_range)
    print(f"\nFound {len(addresses)} existing static addresses in range")

    # Check if range is full
    if check_full_range(addresses, host_ip_last_digit, ip_address_range):
        print("Error: No unused IP addresses available in range", file=sys.stderr)
        sys.exit(1)

    # Get highest IP
    mac_highest, ip_highest_digit = get_highest_ip(addresses, host_ip_last_digit)
    if ip_highest_digit is None:
        # No existing addresses, start from 2
        next_digit = 2
    else:
        next_digit = ip_highest_digit + 1

    # Ask user for IP selection
    print(f"\nSuggested IP address: {ip_address_range}.{next_digit}")
    response = input("Would you like to allocate this IP? (y/n): ").lower()

    manual_entry = False
    if response == 'n':
        manual_entry = True
        while True:
            try:
                custom_digit = int(input(f"Enter the last digit (2-254, avoiding {host_ip_last_digit}): "))
                if custom_digit < 2 or custom_digit > 254:
                    print("Digit must be between 2 and 254")
                    continue
                if custom_digit == host_ip_last_digit:
                    print(f"Digit {host_ip_last_digit} is reserved for the host")
                    continue
                # Check if already in use
                if any(digit == custom_digit for mac, digit in addresses):
                    print(f"IP {ip_address_range}.{custom_digit} is already in use")
                    continue
                next_digit = custom_digit
                break
            except ValueError:
                print("Please enter a valid number")
    elif response != 'y':
        print("Aborted, no changes made")
        sys.exit(0)

    ip_address_mac = f"{ip_address_range}.{next_digit}"

    # Only ask for confirmation if user manually entered the IP
    if manual_entry:
        print(f"\nAllocate IP address {ip_address_mac}?")
        response = input("Confirm (y/n): ").lower()
        if response != 'y':
            print("Aborted, no changes made")
            sys.exit(0)
    else:
        print(f"Will allocate IP address {ip_address_mac}")

    # Apply changes (skip if already exist)
    iptables_modified = False
    dnsmasq_modified = False

    # Only insert iptables rule if MAC doesn't already exist
    if not iptables_exists:
        # Find MAC highest in iptables if we need line number
        if mac_highest:
            line_mac_highest = get_iptables_line_number(mac_highest)
            if line_mac_highest is None:
                print(f"Error: Could not find MAC {mac_highest} in iptables dhcp_clients chain", file=sys.stderr)
                sys.exit(1)

            # Insert iptables rule
            print(f"Inserting iptables rule before line {line_mac_highest}...")
            if not insert_iptables_rule(line_mac_highest, mac, ip_address_mac):
                sys.exit(1)
            iptables_modified = True
        else:
            # No existing entries, append at the end
            print("No existing entries found, appending to iptables...")
            try:
                subprocess.run(
                    ['sudo', 'iptables', '-t', 'raw', '-A', 'dhcp_clients',
                     '-m', 'mac', '--mac-source', mac, '-j', 'ACCEPT',
                     '-m', 'comment', '--comment', ip_address_mac],
                    check=True
                )
                iptables_modified = True
            except subprocess.CalledProcessError as e:
                print(f"Error appending iptables rule: {e}", file=sys.stderr)
                sys.exit(1)

        # Show updated iptables
        print("\nUpdated iptables rules:")
        subprocess.run(['sudo', 'iptables', '-t', 'raw', '-L', 'dhcp_clients', '--line-numbers'])

        # Confirm iptables changes
        response = input("\nAre the iptables changes correct? (y/n): ").lower()
        if response != 'y':
            print("Aborted, no persistent changes made, you may wish to reboot to remove new address from firewall")
            sys.exit(0)
    else:
        print("\nSkipping iptables insertion (MAC already exists)")
        # Ask user if they want to save existing netfilter rules
        response = input("\nSave the current netfilter rules anyway? (y/n): ").lower()
        if response == 'y':
            iptables_modified = True  # This will trigger netfilter save
            print("Will save existing netfilter rules.")
        else:
            print("Skipping netfilter save.")

    # Only insert dnsmasq entry if MAC doesn't already exist
    if not dnsmasq_exists:
        if mac_highest:
            dnsmasq_line = f"dhcp-host={mac_highest},{ip_address_range}.{ip_highest_digit}"
            print(f"Inserting entry into dnsmasq config before: {dnsmasq_line}")
            if not insert_dnsmasq_entry(mac, ip_address_mac, dnsmasq_line):
                sys.exit(1)
            dnsmasq_modified = True
        else:
            # Append to file
            filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
            print(f"Appending to {filename}")
            if not append_dnsmasq_entry(mac, ip_address_mac):
                sys.exit(1)
            dnsmasq_modified = True
    else:
        print("\nSkipping dnsmasq insertion (MAC already exists)")

    # Save netfilter only if iptables was modified (either by new insertion or by user choice)
    if iptables_modified:
        print("\nSaving netfilter rules...")
        if not save_netfilter():
            sys.exit(1)
    else:
        print("\nSkipping netfilter save (no iptables changes)")

    # Handle nodes.json if it exists (do this regardless of whether dnsmasq was modified)
    nodes_modified = handle_nodes_json(ip_address_mac)

    # Ask to restart NetworkManager (only needed if dnsmasq was modified)
    networkmanager_restarted = False
    if dnsmasq_modified:
        response = input("\nRestart NetworkManager to apply new static address? (y/n): ").lower()
        if response == 'y':
            if restart_networkmanager():
                networkmanager_restarted = True
                print("\nSuccess! NetworkManager restarted successfully.")
            else:
                print("\nError: NetworkManager restart may have failed. Please check status and restart manually if needed.", file=sys.stderr)
                # Continue anyway, we still might want to restart web_controller
        else:
            print("Success! Restart NetworkManager or reboot to apply new static address.")

    # Restart web_controller if nodes.json was modified (after NetworkManager restart)
    if nodes_modified:
        # Give NetworkManager a moment to settle if it was restarted
        if networkmanager_restarted:
            print("\nWaiting a few seconds for NetworkManager to settle...")
            time.sleep(3)
        restart_web_controller()

    # Final summary
    if not dnsmasq_modified and not iptables_modified and not nodes_modified:
        print("\nNo changes were made.")
    else:
        print("\nSummary of changes:")
        if dnsmasq_modified:
            print("  - Added static DHCP lease")
        if iptables_modified:
            print("  - Added/updated firewall rule")
        if nodes_modified:
            print("  - Updated nodes.json")

    sys.exit(0)

if __name__ == "__main__":
    main()