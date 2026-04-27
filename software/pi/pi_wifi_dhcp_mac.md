# Introduction
These instructions described how to set up MAC address filtering of Wi-Fi connected devices on a Raspberry Pi and to fix the IP addresses allocated according to MAC address.

# MAC Address Filtering
This is done by configuring `iptables`.

- Create a new chain in the `raw` table with:

  `sudo iptables -t raw -N dhcp_clients`

- Send all incoming DHCP requests to this table with:

  `sudo iptables -t raw -A PREROUTING -p udp --dport 67 -j dhcp_clients`

- Add `ACCEPT` rules for each MAC address you wish to allow:

```
  sudo iptables -t raw -A dhcp_clients -m mac --mac-source a1:81:5c:10:2e:f3 -j ACCEPT
  sudo iptables -t raw -A dhcp_clients -m mac --mac-source 84:d5:5c:63:51:4a -j ACCEPT
```

- Add a `DROP` rule to the end of the list for all other MAC addresses:

  `sudo iptables -t raw -A dhcp_clients -j DROP`

- Check that the list is as you like with:

  `sudo iptables -t raw -L dhcp_clients`

- Make the new rule persistent with:

  `sudo netfilter-persistent save`

- Try connecting to the Wi-Fi access point with a device whose MAC address is not in the list and it should not be allocated an IP address.

- Try connecting to the Wi-Fi acccess point with a device whose MAC address is in the list and it should be allocated an IP address as before.

- If, later, you need to temporarily remove MAC address filtering, do it with:

  `sudo iptables -t raw -D PREROUTING -p udp --dport 67 -j dhcp_clients`

  ...then later add it again with:
  
  `sudo iptables -t raw -A PREROUTING -p udp --dport 67 -j dhcp_clients`
  
  ...or simply reboot as we have not made the deletion persistent.

- If, later, you want to remove a MAC address from the list, find its entry number with:

  `sudo iptables -t raw -L dhcp_clients --line-numbers`

  ...then delete that line with:
  
  `sudo iptables -t raw -D dhcp_clients <line_number>`

  ...noting that the line number of subsequent entries in the table will change when one is deleted so you will need to re-issue the list command if deleting more than one line.  Don't forget to:

  `sudo netfilter-persistent save`

  ...afterwards to make the change persistent.

- If, later, you want to add a new MAC address to the list, add it at the start to make sure it is above the `DROP` rule with:

  `sudo iptables -t raw -I dhcp_clients 1 -m mac --mac-source e3:b1:5d:31:66:c5 -j ACCEPT`

  ...then:

  `sudo netfilter-persistent save`

  ...to make the change persistent. Of course, you could always delete the `DROP` rule, append the new entry, then append the `DROP` rule once more.

# Fixed IP Address Allocation
This is done within `dnsmasq` which is used by `nmcli` under the hood.

- Create a `dnsmasq` configuration file for static IP address allocation with:

  `sudo nano /etc/NetworkManager/dnsmasq-shared.d/static-addresses`

  ...and populate it with entries of the form:

  ```
  dhcp-host=a1:81:5c:10:2e:f3,10.10.3.2
  dhcp-host=84:d5:5c:63:51:4a,10.10.3.10
  ```

  ...i.e. the MAC address followed by the IP address.

- Restart the `NetworkManager` service to apply the changes with:

  `sudo systemctl restart NetworkManager`
  
- Check the new IP address allocations with:

  `sudo arp`
