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
Supports both NetworkManager (with --nmcli) and systemd-networkd (default).

The procedure for adding a new [ESP32S3] node becomes:

1.  Locally build and download the test application to the board, making
    sure to reset `sdkconfig` and do a full clean.

2.  Run the build and look for a line near the start, just before
    the output pauses for 5 seconds, of the form:

    debug: MAC address a1:81:5c:10:2e:f3

3.  SSH to the controller Raspberry Pi and run this script, supplying
    the MAC address from the "debug: MAC address" line when you do so.

4.  Add an entry to `inventory` in `nodes_esp32_deploy.json` for this
    new node, giving it the correct image type; `https_server.py` will
    re-read that file as it goes, it doesn't need to be restarted.
"""

import argparse
import json
import re
import subprocess
import sys
import os
import time
from typing import Optional, Tuple, List

# =============================================================================
# Constants
# =============================================================================

# NetworkManager backend file paths
NM_DNSMASQ_STATIC_FILE = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
NM_IPTABLES_CHAIN = "dhcp_clients"

# systemd-networkd backend file paths
SYSTEMD_NETWORK_FILE = "/etc/systemd/network/20-wlan0.network"
HOSTAPD_ACCEPT_FILE = "/etc/hostapd/accept_mac.txt"

# Service names
SERVICE_NETWORKMANAGER = "NetworkManager"
SERVICE_SYSTEMD_NETWORKD = "systemd-networkd"
SERVICE_HOSTAPD = "hostapd"
SERVICE_WEB_CONTROLLER = "web_controller"

# IP range configuration (will be detected from br0/wlan0)
IP_RANGE_START = 2
IP_RANGE_END = 254

# Timeouts
NETWORKMANAGER_RESTART_TIMEOUT = 900  # 15 minutes
SERVICE_RESTART_SLEEP = 2
SERVICE_SETTLE_SLEEP = 3

# =============================================================================
# Utility Functions
# =============================================================================

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

def get_ip(interface) -> str:
    """Get IP address of interface"""
    try:
        result = subprocess.run(
            ['ip', '-4', 'addr', 'show', interface],
            capture_output=True,
            text=True,
            check=True
        )
        # Parse IP address from output
        match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', result.stdout)
        if match:
            return match.group(1)
        else:
            print(f"Error: Could not find IP address for {interface}", file=sys.stderr)
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"Error getting {interface} IP: {e}", file=sys.stderr)
        sys.exit(1)

# =============================================================================
# NetworkManager Backend (original functionality)
# =============================================================================

def parse_static_addresses_nm(ip_range: str) -> List[Tuple[str, int]]:
    """Parse NetworkManager dnsmasq static-addresses file"""
    addresses = []

    try:
        with open(NM_DNSMASQ_STATIC_FILE, 'r') as f:
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
        print(f"Warning: {NM_DNSMASQ_STATIC_FILE} not found. Starting with empty list.", file=sys.stderr)
    except PermissionError:
        print(f"Error: Permission denied reading {NM_DNSMASQ_STATIC_FILE}. Run with sudo.", file=sys.stderr)
        sys.exit(1)

    return addresses

def check_mac_in_dnsmasq(mac: str, ip_range: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """Check if MAC already exists in dnsmasq static-addresses file
    Returns: (exists, ip_address, last_digit)
    """
    try:
        with open(NM_DNSMASQ_STATIC_FILE, 'r') as f:
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

def get_iptables_line_number(mac: str) -> Optional[int]:
    """Get line number of MAC in iptables dhcp_clients chain"""
    try:
        result = subprocess.run(
            ['sudo', 'iptables', '-t', 'raw', '-L', NM_IPTABLES_CHAIN, '--line-numbers'],
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
            ['sudo', 'iptables', '-t', 'raw', '-L', NM_IPTABLES_CHAIN, '--line-numbers', '-n'],
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
            ['sudo', 'iptables', '-t', 'raw', '-L', NM_IPTABLES_CHAIN, '--line-numbers', '-n'],
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
            ['sudo', 'iptables', '-t', 'raw', '-I', NM_IPTABLES_CHAIN, str(line_num),
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
    new_line = f"dhcp-host={mac},{ip_address}\n"

    try:
        # Read existing file
        with open(NM_DNSMASQ_STATIC_FILE, 'r') as f:
            lines = f.readlines()

        # Find the line to insert before
        insert_pos = -1
        for i, line in enumerate(lines):
            if line.strip() == before_line.strip():
                insert_pos = i
                break

        if insert_pos == -1:
            print(f"Error: Could not find line '{before_line}' in {NM_DNSMASQ_STATIC_FILE}", file=sys.stderr)
            return False

        # Insert new line
        lines.insert(insert_pos, new_line)

        # Write back
        with open(NM_DNSMASQ_STATIC_FILE, 'w') as f:
            f.writelines(''.join(lines))

        return True
    except Exception as e:
        print(f"Error editing {NM_DNSMASQ_STATIC_FILE}: {e}", file=sys.stderr)
        return False

def append_dnsmasq_entry(mac: str, ip_address: str) -> bool:
    """Append new entry to dnsmasq static-addresses file"""
    new_line = f"dhcp-host={mac},{ip_address}\n"

    try:
        with open(NM_DNSMASQ_STATIC_FILE, 'a') as f:
            f.write(new_line)
        return True
    except Exception as e:
        print(f"Error appending to {NM_DNSMASQ_STATIC_FILE}: {e}", file=sys.stderr)
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
    print(f"Restarting {SERVICE_NETWORKMANAGER} (this may take 5-10 minutes due to Wi-Fi driver initialization)...")

    try:
        start_time = time.time()

        # Restart NetworkManager
        subprocess.run(['sudo', 'systemctl', 'restart', SERVICE_NETWORKMANAGER], check=True, capture_output=True)

        # Wait a moment for the restart to actually begin
        time.sleep(SERVICE_RESTART_SLEEP)

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
                   'br0:connected' in dev_result.stdout or \
                   'br0:connecting' in dev_result.stdout or \
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

            # Timeout
            if time.time() - start_time > NETWORKMANAGER_RESTART_TIMEOUT:
                print(f"Warning: {SERVICE_NETWORKMANAGER} did not become operational within {NETWORKMANAGER_RESTART_TIMEOUT//60} minutes", file=sys.stderr)
                return False

            time.sleep(SERVICE_RESTART_SLEEP)

    except subprocess.CalledProcessError as e:
        print(f"Error restarting {SERVICE_NETWORKMANAGER}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Unexpected error during {SERVICE_NETWORKMANAGER} restart: {e}", file=sys.stderr)
        return False

# =============================================================================
# systemd-networkd Backend
# =============================================================================

def parse_static_addresses_systemd(ip_range: str) -> List[Tuple[str, int, str]]:
    """
    Parse /etc/systemd/network/30-config-bridge-br0.network for static DHCP leases
    Returns: List of (mac, last_digit, node_name)
    """
    addresses = []
    current_node_name = None
    in_static_lease = False

    try:
        with open(SYSTEMD_NETWORK_FILE, 'r') as f:
            lines = f.readlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Check for start of a static lease section
                if line == '[DHCPServerStaticLease]':
                    in_static_lease = True
                    current_node_name = None
                    i += 1
                    continue

                # If we're in a static lease section, look for the comment with node name
                if in_static_lease and line.startswith('#') and not line.startswith('# '):
                    node_name = line[1:].strip()
                    if node_name:
                        current_node_name = node_name
                    i += 1
                    continue

                # If we're in a static lease section, look for MACAddress
                if in_static_lease and line.startswith('MACAddress='):
                    mac = line.split('=', 1)[1].strip()
                    # Look ahead for Address line
                    j = i + 1
                    while j < len(lines) and lines[j].strip() == '':
                        j += 1
                    if j < len(lines):
                        addr_line = lines[j].strip()
                        if addr_line.startswith('Address='):
                            ip_addr = addr_line.split('=', 1)[1].strip()
                            if ip_addr.startswith(ip_range + '.'):
                                last_digit = int(ip_addr.split('.')[-1])
                                node = current_node_name if current_node_name else "unknown"
                                addresses.append((mac, last_digit, node))
                                current_node_name = None
                    in_static_lease = False
                    i += 1
                    continue

                # Reset if we hit another section
                if in_static_lease and line.startswith('[') and line != '[DHCPServerStaticLease]':
                    in_static_lease = False
                    current_node_name = None

                i += 1
    except FileNotFoundError:
        print(f"Warning: {SYSTEMD_NETWORK_FILE} not found. Starting with empty list.", file=sys.stderr)
    except PermissionError:
        print(f"Error: Permission denied reading {SYSTEMD_NETWORK_FILE}. Run with sudo.", file=sys.stderr)
        sys.exit(1)

    return addresses

def check_mac_in_hostapd(mac: str) -> bool:
    """Check if MAC already exists in /etc/hostapd/accept_mac.txt"""
    try:
        with open(HOSTAPD_ACCEPT_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    # Extract MAC (ignore VLAN ID if present)
                    mac_in_line = line.split()[0] if line.split() else line
                    if mac_in_line.lower() == mac.lower():
                        return True
        return False
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"Error checking {HOSTAPD_ACCEPT_FILE}: {e}", file=sys.stderr)
        return False

def check_mac_in_systemd(mac: str, ip_range: str) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """
    Check if MAC already exists in systemd-networkd static leases
    Returns: (exists, ip_address, last_digit, node_name)
    """
    addresses = parse_static_addresses_systemd(ip_range)
    for addr_mac, last_digit, node_name in addresses:
        if addr_mac.lower() == mac.lower():
            ip_addr = f"{ip_range}.{last_digit}"
            return True, ip_addr, last_digit, node_name
    return False, None, None, None

def append_hostapd_entry(mac: str) -> bool:
    """Append MAC address to /etc/hostapd/accept_mac.txt"""
    new_line = f"{mac}\n"

    try:
        with open(HOSTAPD_ACCEPT_FILE, 'a') as f:
            f.write(new_line)
        return True
    except Exception as e:
        print(f"Error appending to {HOSTAPD_ACCEPT_FILE}: {e}", file=sys.stderr)
        return False

def remove_hostapd_entry(mac: str) -> bool:
    """Remove MAC from /etc/hostapd/accept_mac.txt (for rollback)"""
    try:
        with open(HOSTAPD_ACCEPT_FILE, 'r') as f:
            lines = f.readlines()

        new_lines = []
        removed = False
        for line in lines:
            line_stripped = line.strip()
            if line_stripped and not line_stripped.startswith('#'):
                mac_in_line = line_stripped.split()[0] if line_stripped.split() else line_stripped
                if mac_in_line.lower() != mac.lower():
                    new_lines.append(line)
                else:
                    removed = True
            else:
                new_lines.append(line)

        if removed:
            with open(HOSTAPD_ACCEPT_FILE, 'w') as f:
                f.writelines(''.join(new_lines))
            return True
        return False
    except Exception as e:
        print(f"Error removing from {HOSTAPD_ACCEPT_FILE}: {e}", file=sys.stderr)
        return False

def append_systemd_lease(mac: str, ip_address: str, node_name: str) -> bool:
    """
    Append a static DHCP lease to systemd-networkd config
    """
    new_entry = f"""
[DHCPServerStaticLease]
# {node_name}
MACAddress={mac}
Address={ip_address}
"""

    try:
        with open(SYSTEMD_NETWORK_FILE, 'r') as f:
            lines = f.readlines()

        with open(SYSTEMD_NETWORK_FILE, 'a') as f:
            f.write(new_entry)

        return True
    except Exception as e:
        print(f"Error appending to {SYSTEMD_NETWORK_FILE}: {e}", file=sys.stderr)
        return False

def restart_hostapd() -> bool:
    """Restart hostapd service"""
    try:
        print(f"Restarting {SERVICE_HOSTAPD}...")
        subprocess.run(['sudo', 'systemctl', 'restart', SERVICE_HOSTAPD], check=True, capture_output=True)
        time.sleep(SERVICE_RESTART_SLEEP)
        result = subprocess.run(
            ['systemctl', 'is-active', SERVICE_HOSTAPD],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{SERVICE_HOSTAPD} restarted successfully.")
            return True
        else:
            print(f"Warning: {SERVICE_HOSTAPD} is not running after restart.", file=sys.stderr)
            return False
    except subprocess.CalledProcessError as e:
        print(f"Error restarting {SERVICE_HOSTAPD}: {e}", file=sys.stderr)
        print(f"stderr: {e.stderr}", file=sys.stderr)
        return False

def restart_systemd_networkd() -> bool:
    """Restart systemd-networkd service"""
    try:
        print(f"Restarting {SERVICE_SYSTEMD_NETWORKD}...")
        subprocess.run(['sudo', 'systemctl', 'restart', SERVICE_SYSTEMD_NETWORKD], check=True, capture_output=True)
        time.sleep(SERVICE_SETTLE_SLEEP)
        result = subprocess.run(
            ['systemctl', 'is-active', SERVICE_SYSTEMD_NETWORKD],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print(f"{SERVICE_SYSTEMD_NETWORKD} restarted successfully.")
            return True
        else:
            print(f"Warning: {SERVICE_SYSTEMD_NETWORKD} is not running after restart.", file=sys.stderr)
            return False
    except subprocess.CalledProcessError as e:
        print(f"Error restarting {SERVICE_SYSTEMD_NETWORKD}: {e}", file=sys.stderr)
        print(f"stderr: {e.stderr}", file=sys.stderr)
        return False

# =============================================================================
# Common Functions
# =============================================================================

def get_highest_ip(addresses: List[Tuple[str, int, Optional[str]]], host_ip_last_digit: int) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Get the highest IP address and its MAC from the list"""
    if not addresses:
        return None, None, None

    # Exclude host IP from consideration
    filtered = [(mac, digit, name) for mac, digit, name in addresses if digit != host_ip_last_digit]
    if not filtered:
        return None, None, None

    highest = max(filtered, key=lambda x: x[1])
    return highest

def check_full_range(addresses: List[Tuple[str, int, Optional[str]]], host_ip_last_digit: int) -> bool:
    """Check if all IPs in range are used (excluding host)"""
    used_ips = {digit for mac, digit, name in addresses if digit != host_ip_last_digit}
    all_ips = set(range(IP_RANGE_START, IP_RANGE_END + 1))
    return used_ips == all_ips

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

    # Parse all test nodes and extract numbers
    node_numbers = {}
    pattern = re.compile(r'^(.*?)([_-]?)(\d+)$')

    for name in test_nodes:
        match = pattern.search(name)
        if match:
            base = match.group(1)  # Everything before the number
            separator = match.group(2)  # _ or - or empty
            num = int(match.group(3))
            node_numbers[name] = num
            if num > highest_num:
                highest_num = num
                highest_name = name

    if highest_num == -1:
        # No numbered test nodes found, use base name "test_1"
        base_name = "test"
        next_num = 1
    else:
        # Extract base name without the number
        match = re.match(r'^(.*?)[_-]?\d+$', highest_name)
        if match:
            base_name = match.group(1)
            if not base_name:
                base_name = "test"
        else:
            base_name = "test"
        next_num = highest_num + 1

    suggested_name = f"{base_name}{next_num}" if base_name != "test" else f"test_{next_num}"

    # Clean up suggested name - ensure consistent formatting
    if '_' not in suggested_name and '-' not in suggested_name:
        suggested_name = f"{base_name}_{next_num}"

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

    # Insert the new entry in the correct sorted position based on node name
    try:
        # Get all test node names and their numeric parts for sorting
        test_node_info = []
        for name, data in nodes.items():
            if data.get('type') == 'test':
                # Extract number from name
                match = re.search(r'(\d+)$', name)
                if match:
                    num = int(match.group(1))
                    test_node_info.append((name, num))
                else:
                    test_node_info.append((name, 0))  # No number, treat as 0

        # Sort test nodes by their numeric part
        test_node_info.sort(key=lambda x: x[1])
        test_node_names = [info[0] for info in test_node_info]

        # Determine where to insert the new node
        # Extract the number from the new node name
        match = re.search(r'(\d+)$', node_name)
        if match:
            new_node_num = int(match.group(1))
            # Find the position where this should be inserted
            insert_pos = 0
            for i, (name, num) in enumerate(test_node_info):
                if new_node_num < num:
                    insert_pos = i
                    break
                elif new_node_num == num:
                    # Same number, insert after existing one
                    insert_pos = i + 1
                    break
                else:
                    insert_pos = i + 1

            # Also need to consider that we're inserting relative to all nodes, not just test nodes
            # Convert to list of items for insertion
            items = list(nodes.items())

            # Find the actual position in the full list
            actual_pos = 0
            test_node_idx = 0
            for i, (name, _) in enumerate(items):
                if name in test_node_names:
                    if test_node_idx == insert_pos:
                        actual_pos = i
                        break
                    test_node_idx += 1
            else:
                # If we didn't find the position, append at the end
                actual_pos = len(items)

            # Insert at the found position
            items.insert(actual_pos, (node_name, new_entry[node_name]))
            nodes = dict(items)
        else:
            # No number found, just append at the end
            nodes.update(new_entry)

        # Write back to file
        with open(nodes_json_path, 'w') as f:
            json.dump(nodes, f, indent=4)

        print(f"Successfully added '{node_name}' to nodes.json")
        return True

    except Exception as e:
        print(f"Error updating nodes.json: {e}", file=sys.stderr)
        return False

def restart_web_controller() -> bool:
    """Restart web_controller service if it's running"""
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', SERVICE_WEB_CONTROLLER],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return False

        response = input(f"\n{SERVICE_WEB_CONTROLLER} service is running. Would you like to restart it to apply nodes.json changes? (y/n): ").lower()
        if response != 'y':
            print(f"Skipping {SERVICE_WEB_CONTROLLER} restart.")
            return False

        print(f"Restarting {SERVICE_WEB_CONTROLLER} (please wait up to a minute for it to restart)...")
        subprocess.run(['sudo', 'systemctl', 'restart', SERVICE_WEB_CONTROLLER], check=True, capture_output=True)
        print(f"{SERVICE_WEB_CONTROLLER} restart initiated.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"Warning: Could not restart {SERVICE_WEB_CONTROLLER}: {e}", file=sys.stderr)
        return False
    except FileNotFoundError:
        return False

# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Add a new node to fgr network')
    parser.add_argument('mac', help='MAC address of the new node')
    parser.add_argument('--nmcli', action='store_true',
                        help='Use NetworkManager backend (default: systemd-networkd)')
    args = parser.parse_args()

    # Require sudo
    require_sudo()

    # Validate MAC
    mac = args.mac
    if not validate_mac(mac):
        print(f"Error: Invalid MAC address format: {mac}", file=sys.stderr)
        print("Expected format: XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX", file=sys.stderr)
        sys.exit(1)

    # Get IP first (needed for range checks)
    host_ip = get_ip("wlan0")
    ip_parts = host_ip.split('.')
    ip_address_range = '.'.join(ip_parts[:3])
    host_ip_last_digit = int(ip_parts[3])
    print(f"Host IP: {host_ip}")
    print(f"IP Range: {ip_address_range}.x")
    print(f"Backend: {'NetworkManager' if args.nmcli else 'systemd-networkd'}")

    if args.nmcli:
        # =====================================================================
        # NetworkManager Backend (original functionality)
        # =====================================================================

        # Parse static addresses
        addresses = parse_static_addresses_nm(ip_address_range)
        addresses_typed = [(mac, digit, "") for mac, digit in addresses]  # Convert to 3-tuple

        print(f"\nFound {len(addresses_typed)} existing static addresses in range")

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
                if existing_ip:
                    nodes_modified = handle_nodes_json(existing_ip)
                    if nodes_modified:
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

        # Check if range is full
        if check_full_range(addresses_typed, host_ip_last_digit):
            print("Error: No unused IP addresses available in range", file=sys.stderr)
            sys.exit(1)

        # Get highest IP
        mac_highest, ip_highest_digit, _ = get_highest_ip(addresses_typed, host_ip_last_digit)
        if ip_highest_digit is None:
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
                    if any(digit == custom_digit for mac, digit, name in addresses_typed):
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

        if manual_entry:
            print(f"\nAllocate IP address {ip_address_mac}?")
            response = input("Confirm (y/n): ").lower()
            if response != 'y':
                print("Aborted, no changes made")
                sys.exit(0)
        else:
            print(f"Will allocate IP address {ip_address_mac}")

        # Apply changes
        iptables_modified = False
        dnsmasq_modified = False

        if not iptables_exists:
            if mac_highest:
                line_mac_highest = get_iptables_line_number(mac_highest)
                if line_mac_highest is None:
                    print(f"Error: Could not find MAC {mac_highest} in iptables dhcp_clients chain", file=sys.stderr)
                    sys.exit(1)

                print(f"Inserting iptables rule before line {line_mac_highest}...")
                if not insert_iptables_rule(line_mac_highest, mac, ip_address_mac):
                    sys.exit(1)
                iptables_modified = True
            else:
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

            print("\nUpdated iptables rules:")
            subprocess.run(['sudo', 'iptables', '-t', 'raw', '-L', 'dhcp_clients', '--line-numbers'])

            response = input("\nAre the iptables changes correct? (y/n): ").lower()
            if response != 'y':
                print("Aborted, no persistent changes made, you may wish to reboot to remove new address from firewall")
                sys.exit(0)
        else:
            print("\nSkipping iptables insertion (MAC already exists)")
            response = input("\nSave the current netfilter rules anyway? (y/n): ").lower()
            if response == 'y':
                iptables_modified = True
                print("Will save existing netfilter rules.")
            else:
                print("Skipping netfilter save.")

        if not dnsmasq_exists:
            if mac_highest:
                dnsmasq_line = f"dhcp-host={mac_highest},{ip_address_range}.{ip_highest_digit}"
                print(f"Inserting entry into dnsmasq config before: {dnsmasq_line}")
                if not insert_dnsmasq_entry(mac, ip_address_mac, dnsmasq_line):
                    sys.exit(1)
                dnsmasq_modified = True
            else:
                filename = "/etc/NetworkManager/dnsmasq-shared.d/static-addresses"
                print(f"Appending to {filename}")
                if not append_dnsmasq_entry(mac, ip_address_mac):
                    sys.exit(1)
                dnsmasq_modified = True
        else:
            print("\nSkipping dnsmasq insertion (MAC already exists)")

        if iptables_modified:
            print("\nSaving netfilter rules...")
            if not save_netfilter():
                sys.exit(1)
        else:
            print("\nSkipping netfilter save (no iptables changes)")

        nodes_modified = handle_nodes_json(ip_address_mac)

        networkmanager_restarted = False
        if dnsmasq_modified:
            response = input("\nRestart NetworkManager to apply new static address? (y/n): ").lower()
            if response == 'y':
                if restart_networkmanager():
                    networkmanager_restarted = True
                    print("\nSuccess! NetworkManager restarted successfully.")
                else:
                    print("\nError: NetworkManager restart may have failed. Please check status and restart manually if needed.", file=sys.stderr)
            else:
                print("Success! Restart NetworkManager or reboot to apply new static address.")

        if nodes_modified:
            if networkmanager_restarted:
                print("\nWaiting a few seconds for NetworkManager to settle...")
                time.sleep(3)
            restart_web_controller()

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

    else:
        # =====================================================================
        # systemd-networkd Backend
        # =====================================================================

        # Parse static addresses from systemd-networkd config
        addresses = parse_static_addresses_systemd(ip_address_range)
        print(f"\nFound {len(addresses)} existing static leases in systemd config")

        # Check if MAC already exists in systemd config
        print("\nChecking if MAC address already exists in systemd-networkd config...")
        systemd_exists, existing_ip, existing_digit, existing_node = check_mac_in_systemd(mac, ip_address_range)
        if systemd_exists:
            print(f"MAC address {mac} already exists in systemd-networkd config:")
            print(f"  IP: {existing_ip}")
            if existing_node:
                print(f"  Node: {existing_node}")
        else:
            print("MAC address not found in systemd-networkd config.")

        # Check if MAC already exists in hostapd accept list
        print("\nChecking if MAC address already exists in hostapd accept list...")
        hostapd_exists = check_mac_in_hostapd(mac)
        if hostapd_exists:
            print(f"MAC address {mac} already exists in hostapd accept list")
        else:
            print("MAC address not found in hostapd accept list.")

        # If MAC exists in both places, nothing to do
        if systemd_exists and hostapd_exists:
            print("\nMAC address already configured in both systemd-networkd and hostapd.")
            if os.path.exists("nodes.json"):
                print("\nChecking nodes.json for IP address entry...")
                if existing_ip:
                    nodes_modified = handle_nodes_json(existing_ip)
                    if nodes_modified:
                        restart_web_controller()
                else:
                    print("Warning: Could not determine existing IP address.")
            print("Nothing else to do, no changes made.")
            sys.exit(0)

        # If MAC exists only in one place, ask user if they want to continue
        if systemd_exists:
            response = input("\nMAC address already in systemd config. Would you like to continue? (y/n): ").lower()
            if response != 'y':
                print("Aborted, no changes made", file=sys.stderr)
                sys.exit(0)
            else:
                print("Continuing with existing systemd entry...")

        if hostapd_exists:
            response = input("\nMAC address already in hostapd accept list. Would you like to continue? (y/n): ").lower()
            if response != 'y':
                print("Aborted, no changes made", file=sys.stderr)
                sys.exit(0)
            else:
                print("Continuing with existing hostapd entry...")

        # Check if range is full
        if check_full_range(addresses, host_ip_last_digit):
            print("Error: No unused IP addresses available in range", file=sys.stderr)
            sys.exit(1)

        # Get highest IP
        mac_highest, ip_highest_digit, name_highest = get_highest_ip(addresses, host_ip_last_digit)
        if ip_highest_digit is None:
            next_digit = 2
            mac_highest = None
            name_highest = None
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
                    if any(digit == custom_digit for mac, digit, name in addresses):
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

        if manual_entry:
            print(f"\nAllocate IP address {ip_address_mac}?")
            response = input("Confirm (y/n): ").lower()
            if response != 'y':
                print("Aborted, no changes made")
                sys.exit(0)
        else:
            print(f"Will allocate IP address {ip_address_mac}")

        # Get node name from user
        print("\nEnter a name for this node (or press Enter for a default name):")
        node_name = input("Node name: ").strip()
        if not node_name:
            if mac_highest and name_highest:
                # Try to increment the highest node name
                match = re.search(r'([_-]?)(\d+)$', name_highest)
                if match:
                    prefix = name_highest[:match.start(1)]
                    num = int(match.group(2)) + 1
                    node_name = f"{prefix}{match.group(1)}{num}"
                else:
                    node_name = f"{name_highest}_2"
            else:
                node_name = "node_1"

        # Apply changes - always append to maintain ascending order
        hostapd_modified = False
        systemd_modified = False

        # Add to hostapd accept list (append at end)
        if not hostapd_exists:
            print("Appending to hostapd accept list...")
            if not append_hostapd_entry(mac):
                print("Error: Failed to append to hostapd accept list", file=sys.stderr)
                sys.exit(1)
            hostapd_modified = True
        else:
            print("\nSkipping hostapd insertion (MAC already exists)")

        # Add to systemd-networkd config (append at end)
        if not systemd_exists:
            print("Appending to systemd-networkd config...")
            if not append_systemd_lease(mac, ip_address_mac, node_name):
                print("Error: Failed to append systemd lease", file=sys.stderr)
                sys.exit(1)
            systemd_modified = True
        else:
            print("\nSkipping systemd lease insertion (MAC already exists)")

        # Restart services
        restarted_hostapd = False
        restarted_networkd = False

        if systemd_modified:
            response = input("\nRestart systemd-networkd to apply static IP changes? (y/n): ").lower()
            if response == 'y':
                if restart_systemd_networkd():
                    restarted_networkd = True
                    # Wait for the bridge to get the new MAC if needed
                    time.sleep(2)
                else:
                    print("Warning: systemd-networkd restart may have failed.", file=sys.stderr)
                    # Offer to rollback
                    response = input("Rollback systemd-networkd changes? (y/n): ").lower()
                    if response == 'y':
                        # Remove the entry from systemd config
                        print("Rollback not implemented for systemd config. Please manually remove the entry.")
                        sys.exit(1)
            else:
                print("systemd-networkd not restarted. You will need to restart it manually or reboot.")

        if hostapd_modified:
            response = input("\nRestart hostapd to apply MAC filtering changes? (y/n): ").lower()
            if response == 'y':
                if restart_hostapd():
                    restarted_hostapd = True
                else:
                    print("Warning: hostapd restart may have failed.", file=sys.stderr)
                    # Offer to rollback
                    response = input("Rollback hostapd changes? (y/n): ").lower()
                    if response == 'y':
                        remove_hostapd_entry(mac)
                        print("Rollback complete.")
                        sys.exit(1)
            else:
                print("hostapd not restarted. You will need to restart it manually or reboot.")

        # Handle nodes.json
        nodes_modified = handle_nodes_json(ip_address_mac)
        if nodes_modified:
            if restarted_hostapd or restarted_networkd:
                print("\nWaiting a few seconds for services to settle...")
                time.sleep(3)
            restart_web_controller()

        # Final summary
        if not hostapd_modified and not systemd_modified and not nodes_modified:
            print("\nNo changes were made.")
        else:
            print("\nSummary of changes:")
            if hostapd_modified:
                print("  - Added MAC to hostapd accept list")
            if systemd_modified:
                print("  - Added static DHCP lease to systemd-networkd")
            if nodes_modified:
                print("  - Updated nodes.json")

    sys.exit(0)

if __name__ == "__main__":
    main()
