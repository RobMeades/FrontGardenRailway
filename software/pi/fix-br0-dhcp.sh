#!/bin/bash
# Fix br0 MAC and block wireless DHCP

# Wait for br0 and eth0
for i in {1..50}; do
    if [ -d /sys/class/net/br0 ] && [ -d /sys/class/net/eth0 ]; then
        NEW_MAC=$(cat /sys/class/net/eth0/address)
        CURRENT_MAC=$(cat /sys/class/net/br0/address 2>/dev/null)
        MAC_WAS_FIXED=0

        # Fix MAC if needed
        if [ "$NEW_MAC" != "$CURRENT_MAC" ]; then
            ip link set br0 down 2>/dev/null
            ip link set dev br0 address $NEW_MAC
            ip link set br0 up 2>/dev/null
            logger -t fix-br0-dhcp "Fixed br0 MAC to $NEW_MAC"
            MAC_WAS_FIXED=1
            sleep 0.5
        fi

        # Block DHCP from wireless clients
        # Check if rules already exist to avoid duplicates
        if ! ebtables -L FORWARD | grep -q "wlan0.*dhcp"; then
            ebtables -A FORWARD -i wlan0 -p IPv4 --ip-proto udp --ip-dport 67 -j DROP
            ebtables -A FORWARD -i wlan0 -p IPv4 --ip-proto udp --ip-dport 68 -j DROP
            logger -t fix-br0-dhcp "Added ebtables rules to block wireless DHCP"
        fi

        # Check if dhcpcd is already running on br0
        if pgrep -f "dhcpcd.*br0" > /dev/null; then
            if [ $MAC_WAS_FIXED -eq 1 ]; then
                dhcpcd -n br0
                logger -t fix-br0-dhcp "Sent renew signal to dhcpcd on br0"
            else
                logger -t fix-br0-dhcp "dhcpcd already running on br0, no changes needed"
            fi
        else
            dhcpcd -b br0
            logger -t fix-br0-dhcp "Started dhcpcd on br0"
        fi

        exit 0
    fi
    sleep 0.2
done

logger -t fix-br0-dhcp "ERROR: Could not find br0 or eth0"
exit 1