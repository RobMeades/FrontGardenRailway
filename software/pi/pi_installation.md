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

- If you intend to log to a database, plug an SSD that MUST HAVE BEEN EXT4 formatted (if you are to use it for `journal` storage, which is advisable into the Raspberry Pi, check with `lsblk` and, if it for instance appears as `/dev/sda`, mount it and check that it as mounted with:

  ```
  sudo mkdir -p /mnt/ssd
  sudo mount /dev/sda1 /mnt/ssd
  lsblk
  ```

  ...then make the mount persistent by getting the `PARTUUID` of the partition with `sudo blkid /dev/sda1` and then `sudo nano /etc/fstab` and add a line as follows, adding no spurious spaces at the start:
  
  ```
  PARTUUID=<PARTUUID> /mnt/ssd ext4 defaults,noatime,nofail 0 2
  ```

  ...(obviously replacing `<PARTUUID>` with the `PARTUUID` for your SSD) then check that you got that write by confirming the mount with:
 
  ```
  sudo mount -a
  lsblk
  ```

- You can then `sudo nano /lib/systemd/system/log_server.service` and add to the end of the `ExecStart` line `--db-path /mnt/ssd/logs.db`, do a `sudo systemctl daemon-reload` and then restart the `log_server` service  with `sudo systemctl restart log_server` and all logs sent by all nodes will be stored in the database.

- Note: later you can use `log_viewer.py` to query the database.

- You can do the same thing with `sudo nano /lib/systemd/system/web_controller.service`, i.e. add `--db-path /mnt/ssd/logs.db` to the end of the `ExecStart` line, do a `sudo systemctl daemon-reload` and then restart the `web_controller` service  with `sudo systemctl restart web_controller` and it will now plot graphs using data from the database.

- The graph feature does "click to time", which will take you to the log entry for a time on the graph, but this is only really useful with longer term journal storage than one gets with the journal files in RAM; since you now have that SSD, the journal can be moved there with:

  ```
  sudo systemctl stop systemd-journald
  sudo rm -rf /var/log/journal
  sudo mkdir -p /mnt/ssd/journal
  sudo chown root:systemd-journal /mnt/ssd/journal
  sudo chmod 2755 /mnt/ssd/journal
  sudo mkdir -p /var/log/journal
  sudo mount --bind /mnt/ssd/journal /var/log/journal
  ````

  Note: ignore the message about triggering units when you stop `system-journald`, it is harmless.

  ...then `sudo nano /etc/fstab`, remove the line referring to `var/log` (leave `/var/lib/logrotate` where it is) then add:
  
  `/mnt/ssd/journal /var/log/journal none bind 0 0`

  ...then `sudo nano /etc/systemd/journald.conf` and make it something like:
  
  ```
  [Journal]
    Storage=persistent
    SystemMaxUse=2G
    SystemMaxFileSize=100M
    MaxRetentionSec=7day
  ```

  ...then workaround the Trixie (in more ways than one) `40-rpi-volatile-storage.conf` `journald` configuration file with `sudo nano /usr/lib/systemd/journald.conf.d/90-rpi-persistent-storage.conf` and give it contents:
  
  ```
  [Journal]
  Storage=persistent
  ```

  ...then:

  ```
  sudo systemctl daemon-reload
  sudo systemctl start systemd-journald
  sudo journalctl --flush
  ```

  ...and finally check with:
  
  ```
  mount | grep journal
  df -h /var/log/journal
  systemctl status systemd-journald
  journalctl -n 5
  journalctl --disk-usage
  ```

  ...then you need to make sure that `systemd-journald` doesn't try to start logging until the SSD has been mounted, which you do by creating a drop-in directory with:
  
  ```
  sudo mkdir -p /etc/systemd/system/systemd-journald.service.d
  ```
  
  ...then `sudo nano /etc/systemd/system/systemd-journald.service.d/00-wait-for-ssd.conf` and paste into it:
  
  ```
  [Unit]
  After=var-log-journal.mount
  Requires=var-log-journal.mount
  ```

  ...and `sudo systemctl daemon-reload` to make it active, then, since `systemd-journald` can be a bit sensitive about having the disk switched to RO underneath it, and is now writing to external SSD so it matters a but more that it doesn't suddenly decide to not bother flushing stuff to disk, `sudo nano /etc/bash.bashrc` and add `sudo systemctl restart systemd-journald` to the alias lines:
  
  ```
  alias ro='sudo mount -o remount,ro / ; sudo mount -o remount,ro /boot/firmware ; sudo systemctl restart systemd-journald'
  alias rw='sudo mount -o remount,rw / ; sudo mount -o remount,rw /boot/firmware ; sudo systemctl restart systemd-journald'
  ```

  ...then, to monitor the situation and make sure stuff really is being written to the SSD journal file, run the `journal_ssd_check.sh` script every 5 minutes by doing `sudo crontab -e` and adding:
  
  ```
  */5 * * * * <path to cloned FGR repo>/FrontGardenRailway/software/pi/journal_ssd_check.sh
  ```

  ...which will insert a journal entry every 5 minutes telling you whether the SSD journal file is being written to. 

- Since you now have a large writable SSD, you might want to move the `~/fw` directory to it, `sudo nano /lib/systemd/system/https_server.service` to point the working directory to the new location and then `sudo systemctl deamon-reload`, `sudo systemctl restart https_server`.

- Now, assuming port 8060 is open, `log_server.py` can provide links that allow the local script `crash_decoder.py` to decode crash dumps (unfortunately there is no pre-compiled version of the Espressif tools for the Raspberry Pi Zero).  To enable this, `sudo nano /lib/systemd/system/log_server.service` and change the `ExecStart` line to something like:

  ```
  ExecStart=python log_server.py --web-port 8060 --web-bind 10.10.2.10 --port 5001 --db-path /mnt/ssd/logs.db --node-cfg ../esp32/nodes_esp32_deploy.json --staging /mnt/ssd/fw
  ```

  ...followed by `sudo systemctl deamon-reload`, `sudo systemctl restart log_server`.  There are then several details steps required to set up `crash_decoder.py`: see the top of that file for what they are, and, finally, set `crash_decoder.py` to run as a service on the local PC with `sudo nano /lib/systemd/system/fgr_crash_decoder.service` with the following contents:

  ```
  [Unit]
  Description=FGR crash decoder
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=<path to cloned FGR repo>/FrontGardenRailway/software/pi
  ExecStart=/usr/bin/python -u <path to cloned FGR repo>/FrontGardenRailway/software/pi/crash_decoder.py --daemon
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

  ...then, still on the local PC, `sudo systemctl start fgr_crash_decoder` and `sudo systemctl enable fgr_crash_decoder` to make it run at boot

- Enter `ro` again to make the file system of the Raspberry Pi read-only once more.

# Proper OTA
Once you have a number of nodes on the front garden railway, all doing different things, potentially with different ESP32 hardware variants, you will need more comprehensive OTA management.  For this, the same `https_server.py` as you set up earlier will be used in `differentiated` mode.  In this mode it does not simply give a node the file that node requested, it looks up what that node should do and what HW variant it is in a JSON configuration file and supplies the correct binary.

For details of the JSON configuration file, which is primarily used by [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py) see the [`README.md` in the ESP32 directory](../esp32/README.md)].  For the Raspberry Pi side, a couple of things are required:

- [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py) uses `rsync` to move the generated binary files to the Raspberry Pi (over SSH).  To avoid having to enter a password, on the development machine where you run [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py), generate an SSH key with:

  ```
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_fgr -N ""
  ```
  
- This will create two files inside your `.ssh` directory: leave the private key `id_ed25519_fgr` where it is and never share it.

- On the Pi, enter `rw` to make the file system writable.

- On the machine where you generated the key pair, copy the public key `id_ed25519_fgr.pub` to the Pi with:

  ```
  ssh-copy-id -i ~/.ssh/id_ed25519_fgr.pub username@ip
  ```

  ...where `username` is replaced with your username on the Pi and `ip` with the IP address of the Pi.
  
- Check that this has worked by logging in manually from that machine with:

   ```
   ssh -i ~/.ssh/id_ed25519_fgr username@ip
   ```

...where `username` is replaced with your username on the Pi and `ip` with the IP address of the Pi; you should end up logged in without being prompted for a password.

- [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py) will also need to be able to restart the `https_server` service: do that by, on the Raspberry Pi, entering `sudo visudo` and then adding, at the end:

```
<your username> ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart *
```

- The final step is to switch `https_server.py` into differentiated mode but BEFORE YOU DO THIS you need to have successfully run [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py), and at least once with the `--production` flag, to populate the `~/fw` directory on the Pi with a tree of properly named/versioned binary files.  Once you have done that, `sudo nano /lib/systemd/system/https_server.service` and change this line:

  ```
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/https_server.py
  ```
  
  ...to:
  
  ```
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/https_server.py . --node-cfg /home/<your home directory name>/FrontGardenRailway/software/esp32/nodes_esp32_deploy.json
  ```

  ...and reload the `https_service` with:
  
  ```
  sudo systemctl daemon-reload
  sudo systemctl start https_server
  ```

  The revised line points `https_server.py` at the `~/fw` directory as its working directory (you will have already populated this using [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py)), putting it into differentiated mode, and points it at [`nodes_esp32_deploy.json`](../esp32/nodes_esp32_deploy.json) for node configuration information.

- In this mode `https_server.py` has a dashboard running at the URL `https://<pi IP address>:8070/dashboard`: in order to stop your browser objecting that it uses a self-signed certificate, on Windows copy `ca_cert.pem` to your PC, rename it to `ca_cert.crt`, double-click on it and `Install Certificate...` -> `Local Machine`, browse to `Trusted Root Certification Authorities` and place the certificate there, or on Linux copy `ca_cert.pem` to `/usr/local/share/ca-certificates/ca_fgr.crt` and run `sudo update-ca-certificates` (if you ever find you need to update the certificate, replace the old with the new and add `--fresh` to the command-line).

- Enter `ro` again to make the file system of the Raspberry Pi read-only once more.
