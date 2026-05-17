# Introduction
Having got everything installed, configured and running on a hardened Raspberry Pi with secured access, I needed to install it as a controller for the front garden railway.  The assumption here is that the Raspberry Pi is wired to the house network's router and acts as a Wi-Fi AP to the front garden railway: I used a PoE hat on a Raspberry Pi Zero and plugged the hat into the same PoE switch that serves the security cameras pointed at the front garden railway.

For isolation, the Raspberry Pi should be installed on a VLAN of the home network (i.e. plugged into an Ethernet port of the home router that is configured to behave in a particular way); it is then possible to restrict access on what will, after all, be an external Wi-Fi network.  In my case the PoE switch serving the security cameras was already on a restricted VLAN (only allowed out-bound connections to an NTP server).

# VLAN Setup
- Set up a VLAN on the home router: on mine this was in the address range `10.10.2.x` with the Pi given a static IP address of `10.10.2.10`.

- Configure a firewall for the VLAN that prevents outgoing connections with the exception of NTP service, which the Pi will need to establish time.  For instance, as of 18th March 2025 the IP address of `0.uk.pool.ntp.org` was `178.62.68.79`, so if your external network were named `br1` , internal network `br0` and WLAN interface `ppp0` then the firewall configuration for the VLAN on your router might look like this:

  ```
  # Allow established/related connections (MUST BE FIRST OF VLAN RULES)
  iptables -I FORWARD 1 -i br1 -o br0 -m state --state ESTABLISHED,RELATED -j ACCEPT

  # Allow new connections from main network to VLAN
  iptables -I FORWARD 2 -i br0 -o br1 -j ACCEPT

  # Allow NTP for all devices on VLAN2 to specific NTP server
  # As of 18th March 2025 the IP address of 0.uk.pool.ntp.org was 178.62.68.79
  iptables -I FORWARD 3 -i br1 -o ppp0 -d 178.62.68.79 -j ACCEPT

  # Allow Raspberry Pi to access router's dnsmasq (UDP 53)
  iptables -I FORWARD 4 -i br1 -o br0 -s 10.10.2.10 -d 10.10.2.1 -p udp --dport 53 -j ACCEPT

  # Development mode (commented by default): uncomment this to allow
  # Raspberry Pi to get to install applications and access Github
  # iptables -I FORWARD 5 -i br1 -o ppp0 -s 10.10.2.10 -p tcp -m multiport --dports 80,443 -j ACCEPT
  
  # Drop all other outbound traffic from VLAN (MUST BE LAST)
  iptables -I FORWARD 6 -i br1 -o ppp0 -j DROP
  iptables -I FORWARD 7 -i br1 -o br0 -j DROP
  ```

# Pi Configuration
- On the Pi, enter `rw` to make the file system writable.

- Make the NTP service on the Pi use the single IP address for NTP access by editing `sudo nano /etc/ntpsec/ntp.conf`, commenting out all of the entries beginning with `pool` and adding an entry `server 178.62.68.79 iburst` (or whatever IP address you allowed through the firewall for NTP service),

- Restart the NTP service  with `sudo systemctl restart ntpsec`.

- Verify that the change is working by running `ntpq -p`: you should see the configured NTP server IP address in the list, and it should eventually show a `*` next to it, indicating it is the source that NTP on the Pi is syncing to, but this might take many many minutes.

- If you intend to log node metrics to a database, plug an SSD into the Raspberry Pi, check with `lsblk` and, if it for instance appears as `/dev/sda`, mount it and check that it as mounted with:

  ```
  sudo mkdir -p /mnt/ssd
  sudo mount /dev/sda1 /mnt/ssd
  lsblk
  ```

  ...then make the mount persistent by getting the `PARTUUID` of the partition with `sudo blkid /dev/sda1` and then `sudo nano /etc/fstab` and add a line as follows, adding no spurious spaces at the start:
  
  ```
  PARTUUID=3e4d6a20-01 /mnt/ssd vfat defaults,auto,users,rw,nofail 0 0
  ```

 ...(obviously replacing `PARTUUID` with the `PARTUUID` for your SSD) then check that you got that write by confirming the mount with:
 
  ```
  sudo mount -a
  lsblk
  ```
- You can then `sudo nano /lib/systemd/system/log_server.service` and add to the end of the `ExecStart` line `--db-path /mnt/ssd/fgr_metrics.db`, do a `sudo systemctl daemon-reload` and then restart the `log_server` service  with `sudo systemctl estart log_server` and any metrics sent by nodes will be stored in the database.

- Enter `ro` again to make the file system of the Raspberry Pi read-only once more.
