# Introduction
These instructions described how to set up MAC address filtering of Wi-Fi connected devices on a Raspberry Pi and to fix the IP addresses allocated according to MAC address.

Note: see `Doing It Automatically` below!!!

This is done differently depending on whether you are using `nmcli` (the default for a Raspberry Pi) or `systemd-networkd` (the [nrmorrow](https://github.com/morrownr/USB-WiFi) approach).  The `nmcli` approach is kept below for historical interest, The Way is `systemd-networkd`.

# `systemd-networkd` Scenario: This Is The Way
## Fixed IP Address Allocation
This is done by modifying the configuration in `systemd-networkd`.  Note that it must be done _before_ the device first connects (i.e. before you let the client onto the system by adding it to the MAC address filter), otherwise `systemd-networkd` gets confused; should this happen, stop `systemd-networkd` and delete the lease file with `sudo rm /var/lib/systemd/network/dhcp-server-lease/wlan0` before restarting it.

- Edit your `wlan0` side network configuration file with `sudo nano /etc/systemd/network/20-wlan0.network` and add to it entries of the form:

  ```
  [DHCPServerStaticLease]
  # Node 1
  MACAddress=a1:81:5c:10:2e:f3
  Address=10.10.3.2

  [DHCPServerStaticLease]
  # Node 2
  MACAddress=84:d5:5c:63:51:4a
  Address=10.10.3.10
  ```

- Restart the `systemd-networkd` service to apply the changes with:

  ```
  sudo systemctl restart systemd-networkd
  ```

- Check the new IP address allocations with:

  ```
  sudo arp
  ```

## MAC Address Filtering
This is done by configuring `hostapd`.

- `sudo nano /etc/hostapd/hostapd.conf` and add to it:

  ```
  # Enable MAC address filtering
  macaddr_acl=1

  # Use an accept list (only these MACs can connect)
  accept_mac_file=/etc/hostapd/accept_mac.txt
  ```

- `sudo nano /etc/hostapd/accept_mac.txt` and populate it with the MAC addresses you want to allow to connect, e.g.:

  ```
  a1:81:5c:10:2e:f3
  84:d5:5c:63:51:4a
  ```

- `sudo systemctl restart hostapd` to apply the changes.

# `nmcli` Scenario: Historical Interest Only
## MAC Address Filtering, `nmcli` Scenario: Historical Interest Only
Since `nmcli` does not have MAC filtering, instead we use `iptables` to deny DHCP requests.

- Create a new chain in the `raw` table with:

  ```
  sudo iptables -t raw -N dhcp_clients
  ```

- Send all incoming DHCP requests to this table with:

  ```
  sudo iptables -t raw -A PREROUTING -p udp --dport 67 -j dhcp_clients
  ```

- Add `ACCEPT` rules for each MAC address you wish to allow:

  ```
  sudo iptables -t raw -A dhcp_clients -m mac --mac-source a1:81:5c:10:2e:f3 -j ACCEPT -m comment --comment "A comment that identifies the thing"
  sudo iptables -t raw -A dhcp_clients -m mac --mac-source 84:d5:5c:63:51:4a -j ACCEPT -m comment --comment "Another comment that identifies the thing"
  ```

  Note: it is not possible to add comments in the NetworkManager static address list (see below) so you might want to plan what IP address you will use and put that in the comment to tie the two together more obviously.

- Add a `DROP` rule to the end of the list for all other MAC addresses:

  ```
  sudo iptables -t raw -A dhcp_clients -j DROP
  ```

- Check that the list is as you like with:

  ```
  sudo iptables -t raw -L dhcp_clients
  ```

- Make the new rule persistent with:

  ```
  sudo netfilter-persistent save
  ```

- Try connecting to the Wi-Fi access point with a device whose MAC address is not in the list and it should not be allocated an IP address.

- Try connecting to the Wi-Fi acccess point with a device whose MAC address is in the list and it should be allocated an IP address as before.

- If, later, you need to temporarily remove MAC address filtering, do it with:

  ```
  sudo iptables -t raw -D PREROUTING -p udp --dport 67 -j dhcp_clients
  ```

  ...then later add it again with:

  ```
  sudo iptables -t raw -A PREROUTING -p udp --dport 67 -j dhcp_clients
  ```

  ...or simply reboot as we have not made the deletion persistent.

- If, later, you want to remove a MAC address from the list, find its entry number with:

  ```
  sudo iptables -t raw -L dhcp_clients --line-numbers
  ```

  ...then delete that line with:

  ```
  sudo iptables -t raw -D dhcp_clients <line_number>
  ```

  ...noting that the line number of subsequent entries in the table will change when one is deleted so you will need to re-issue the list command if deleting more than one line.  Don't forget to:

  ```
  sudo netfilter-persistent save
  ```

  ...afterwards to make the change persistent.

- If, later, you want to add a new MAC address to the list, add it at the start to make sure it is above the `DROP` rule with:

  ```
  sudo iptables -t raw -I dhcp_clients 1 -m mac --mac-source e3:b1:5d:31:66:c5 -j ACCEPT -m comment --comment "A comment that identifies the thing"
  ```

  ...then:

  ```
  sudo netfilter-persistent save
  ```

  ...to make the change persistent. Of course, you could always delete the `DROP` rule, append the new entry, then append the `DROP` rule once more.

## Fixed IP Address Allocation, `nmcli` Scenario: Historical Interest Only
`nmcli` uses `dnsmasq` under the hood and `dnsmasq` can be used to assign static IP addresses:

- Create a `dnsmasq` configuration file for static IP address allocation with:

  ```
  sudo nano /etc/NetworkManager/dnsmasq-shared.d/static-addresses
  ```

  ...and populate it with entries of the form:

  ```
  dhcp-host=a1:81:5c:10:2e:f3,10.10.3.2
  dhcp-host=84:d5:5c:63:51:4a,10.10.3.10
  ```

  ...i.e. the MAC address followed by the IP address.

- Restart the `NetworkManager` service to apply the changes with:

  ```
  sudo systemctl restart NetworkManager
  ```

- Check the new IP address allocations with:

  ```
  sudo arp
  ```

# Doing It Automatically
You can do all of the above steps automatically using the script `add_node.py`.  If you are doing this or the first time, make sure that `/etc/hostapd/hostapd.conf` has the lines:

  ```
  # Enable MAC address filtering
  macaddr_acl=1

  # Use an accept list (only these MACs can connect)
  accept_mac_file=/etc/hostapd/accept_mac.txt
  ```

You will need to know the Wi-Fi MAC address of the node you wish to add.  If, say, you wanted to add a client with MAC address `a1:81:5c:10:2e:f3`, you would type:

```
python add_node.py a1:81:5c:10:2e:f3
```

The node will be added and assigned the next available static IP address in the range `10.10.3.x` (or you can change it if you like).

Note: the script also supports the old `NetworkManager` mechanism with the command-line parameter `--nmcli` but, if you are doing that for the first time, you need to create and add one MAC address to `iptables` first.  For instance, usually you would test connectivity with your own phone so you would create the table and add it with something like:

```
sudo iptables -t raw -N dhcp_clients
sudo iptables -t raw -A PREROUTING -p udp --dport 67 -j dhcp_clients
sudo iptables -t raw -A dhcp_clients -m mac --mac-source <your phone's MAC address> -j ACCEPT -m comment --comment "Mobile phone"
sudo iptables -t raw -A dhcp_clients -j DROP
sudo netfilter-persistent save
```

Then you can add a node with:

```
python add_node.py --nmcli a1:81:5c:10:2e:f3
```

