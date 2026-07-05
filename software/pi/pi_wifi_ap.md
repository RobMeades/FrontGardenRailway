# Introduction
These instructions describe how to set up a Wi-Fi access point on a headless Pi.  Note that, on the version of Raspbian I was using (Trixie), any attempt to set an access point with security failed, so these instructions set up an open Wi-Fi access point (security is provided later through [MAC address filtering](pi_wifi_dhcp_mac.md)).

NOTE: in all cases below, when pasting contents into a file, ensure there are no leading spaces.

# Preparation

## Installations
Since the Pi will lose connectivity to your Wi-Fi network (you do _not_ want an open access point on your Wi-Fi network) you must have a serial connection to a headless Pi Zero (e.g. using a 3V3 FTDI cable, black to GND, yellow (RXD) to GPIO14 (TXD), orange (TXD) to GPIO15 (RXD)), or an Ethernet connection to a bigger Pi.

- The Pi will also lose connectivity to the internet, so install a few useful things first:

  - `sudo apt install git`: 'cos you'll need that for the next line,

  - `git clone https://github.com/RobMeades/FrontGardenRailway.git`: 'cos you will need the various Python scripts,

  - `sudo apt install python3-aiohttp`: which will be needed by `https_server.py`,

  - `sudo apt install python3-systemd`: which will be needed by `log_server.py`,

  - `sudo apt install minicom`: serial communications program,

  - `sudo apt install lrzsz`: this allows the `minicom` and `picocom` serial communications programs to perform file transfer,

  - `sudo apt install hostapd systemd-resolved ebtables`: needed for networking with the [nrmorrow](https://github.com/morrownr/USB-WiFi) approach, which is The Way,

  - `sudo apt install tcpdump lsof jq`: can be handy for debugging,

  - `sudo apt install sqlite3`: may be needed later when you are debugging the database,

  - History: `sudo apt install iptables iptables-persistent` was needed for MAC address filtering, but only in ye old `nmcli` scenario, no longer required.

- If you are using a Pi Zero, with no Ethernet port, make sure you have serial access to it as follows:

  - Connect a PC to the Pi's serial port and log in to it, e.g. `minicom -D /dev/ttyUSB0` on Linux.

  - Check that binary file uploads and downloads work, e.g. in `minicom` `CTRL-A`, `S`, `zmodem`, then find a binary file (let's call it `blah.bin`) and send it, rename the uploaded file to something like `blah_new.bin`, then in the `minicom` terminal type `sz blah_new.bin` to send the file back, leave `minicom` and finally, on Linux, `diff blah.bin blah_new.bin` should produce no output (i.e. the files are the same).

## Easier SSH Access
To avoid having to enter a password all the time, and so that [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py) can restart `https_server` should it need to, on the /[Linux, 'cos building is way faster on Linux /] development machine where you are building the ESP-IDF FW, generate an SSH key with:

  ```
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_fgr -N ""
  ```

- This will create two files inside your `.ssh` directory: leave the private key `id_ed25519_fgr` where it is and never share it.

- On the machine where you generated the key pair, copy the public key `id_ed25519_fgr.pub` to the Pi with:

  ```
  ssh-copy-id -i ~/.ssh/id_ed25519_fgr.pub username@ip
  ```

  ...where `username` is replaced with your username on the Pi and `ip` with the IP address of the Pi.

- Check that this has worked by logging in manually from that machine with:

  ```
  ssh username@ip
  ```

  ...where `username` is replaced with your username on the Pi and `ip` with the IP address of the Pi; you should end up logged in without being prompted for a password.

# AP Setup: History
I spent quite a while stabilizing the AP behaviour of the Pi, please refer to the section `AP Setup: The Way` below for how it should be done; these notes kept for historical interest only.

## Wi-Fi Hardware History
I found the Pi Zero W on-board Wi-Fi to be far too unstable in AP mode, see these posts for details:

[https://forums.raspberrypi.com/viewtopic.php?p=2374992](https://forums.raspberrypi.com/viewtopic.php?p=2374992)
[https://github.com/raspberrypi/firmware/issues/1768#issuecomment-4084988745](https://forums.raspberrypi.com/viewtopic.php?p=2374992)

Hence I switched to a Pi Zero I happened to have spare, later a Pi 5 and plugged in a USB Wi-Fi dongle: be careful which you choose!  The  TP-link Archer T2U AC600 Nano Wi-Fi adapter (`rtl8811au` chipset) looked good initially, but on a Pi Zero which at the time had only `6.12.75+rpt-rpi-v6`, the drivers were not good: only one of the three Linux drivers (which you must build yourself for Linux kernel versions > 6.14) I tried worked and the working one did not support transmission of TIM information elements which are required for a standards-compliant Wi-Fi AP (ESP32 refused to connect).

So I switched to an Atheros AR9271 USB Wi-Fi adapter, which is huge but is known to work with Linux and there are built-in drivers for it that have been around for a decade.  And, whaddaya know, the `AR9271` driver has known instabilities also, instabilities which can crash the Linux kernel (`ar9002_hw_calibrate()` dereferencing a NULL pointer), bless its little cotton socks.  The only way out of _this_ was to rely on the watchdog which is already enabled on a Pi by default, though my experience is that the USB is left in a state where only a hard reboot will recover. Ugh.

And then I discovered that the Atheros AR9271 has a limit of just seven clients in AP mode.  Double ugh.

Finally, I was pointed at [nrmorrow on Github](https://github.com/morrownr/USB-WiFi) as the authority on Wi-Fi on Linux and followed their lead.

## Networking Software History
I originally ran networking using `nmcli`, since that is the default on a Pi, however since being introduced to [nrmorrow on Github](https://github.com/morrownr/USB-WiFi) I moved to using `systemd-resolved` and `systed-networkd`.  The `nmcli` method is kept here in case if it is of interest.

Connect to a Pi Zero using a serial terminal, or a bigger Pi using Ethernet, and set the AP up as follows:

- On the Pi, `sudo nano /etc/NetworkManager/NetworkManager.conf` and, if the `plugins` line has `ifupdown` in it, remove it (so it might become `plugins=keyfile`), otherwise you won't be able to create a new connection.

- On the Pi, create a Wi-Fi-specific NetworkManager configuration file with `sudo nano /etc/NetworkManager/conf.d/99-wifi-powersave.conf` and give it the contents:

  ```
  [connection]
  # Switch power saving off to avoid poll time-outs
  wifi.powersave = 2
  ```

- Restart NetworkManager with:

  ```
  sudo systemctl restart NetworkManager
  ```

- NOTE: originally, when using the Pi Zero W's own Wi-Fi, I suffered occasional crashes of the Broadcomm Wi-Fi driver, apparently due to SDIO communication hanging, for which the suggested workaround was to create and populate a driver modification file with:

  ```
  echo "options brcmfmac roamoff=1 feature_disable=0x82000" | sudo tee /etc/modprobe.d/brcmfmac.conf
  ```

- Now you can create the access point with:

  ```
  sudo nmcli connection add type wifi ifname wlan0 con-name FGR autoconnect yes connection.autoconnect-priority 1 ssid FGR
  ```

- Set some properties for the access point with:

  ```
  sudo nmcli connection modify FGR 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.10.3.1/24
  ```

- Also set retries to zero to stop the Network Manager black-listing a device that repeatedly tries to connect:

  ```
  sudo nmcli connection modify FGR connection.autoconnect-retries 0
  ```

- If there is a pre-existing Wi-Fi station configuration, make sure it does not auto-connect ever with:

  ```
  sudo nmcli connection modify <connection name> connection.autoconnect no
  sudo nmcli connection down <connection name>
  ```

- Finally, bring up the AP with:

  ```
  sudo nmcli connection up FGR
  ```

- You should now be able to connect to this open Wi-Fi `FGR` access point from any device.

- If you want to bring the AP down, `sudo nmcli connection down FGR` and the Pi will return to having a connection to your Wi-Fi network.

# AP Setup: The Way
After being introduced to [nrmorrow on Github](https://github.com/morrownr/USB-WiFi) I followed their lead on choice of HW and software.  This is The Way.

## Wi-Fi Hardware
In HW terms, it turns out that the TP-link Archer T2U AC600 Nano Wi-Fi adapter (`rtl8811au` chipset) is able to handle more clients and, once I had switched to a 64-bit Pi 5, the Linux version became `6.18.34+rpt-rpi-2712` and that _does_ have capable drivers, with the TIM IE, in fact drivers written by [dubhater on Github](https://github.com/dubhater) who supports [nrmorrow on Github](https://github.com/morrownr/USB-WiFi).  So I switched back to the nice neat TP-link Archer T2U AC600 Nano.

## Pi Configuration
Connect to a Pi Zero using a serial terminal, or a bigger Pi using Ethernet, and set the Pi up as follows.

- Assuming you do _not_ need the on-board Wi-Fi on the Pi Zero W (or a bigger Pi) operating in client mode, `sudo nano /boot/firmware/config.txt` and add, near the top:

  ```
  # Disable on-board Wi-Fi
  dtoverlay=disable-wifi
  ```

  ...then reboot.

- If using a Pi 5 and supplying sufficient power for all things plugged into it (25 Watts), you may need to tell the Pi that this so.  If:

  ```
  od --endian=big -i /sys/firmware/devicetree/base/chosen/power/max_current
  ```

  ...does not produce a response with `5000` on the first line (i.e. 5 Amps has been negotiated), you can tell the Pi that it really has got enough power with

  ```
  sudo rpi-eeprom-config -e
  ```

  ...and adding on the end:

  ```
  PSU_MAX_CURRENT=5000
  ```

- To make sure the Pi supplies the full 1.6 Amps to the USB peripherals, `sudo nano /boot/firmware/config.txt` and add to the end:

  ```
  usb_max_current_enable=1
  ```

- Reboot and hopefully all will be good.

## Networking Software
In SW terms, [nrmorrow](https://github.com/morrownr/USB-WiFi) uses `hostapd` directly and `systemd-networkd`, which seems more sensible for a server anyway, so the software AP setup here follows [that pattern](https://github.com/morrownr/USB-WiFi/blob/main/home/AP_Mode/Bridged_Wireless_Access_Point.md).

- To stop `nmcli` trying to fight for control of the W-Fi hardware, `sudo nano /etc/NetworkManager/NetworkManager.conf` and add:

  ```
  [keyfile]
  unmanaged-devices=interface-name:wlan0
  ```

  ...then `sudo systemctl restart NetworkManager` for the change to take effect.

- Start and enable `systemd-resolved` with:

  ```
  sudo systemctl start systemd-resolved
  sudo systemctl enable systemd-resolved
  ```

- For backwards compatibility, `symlink` the configuration file that `systemd-resolved` will have created to `/etc/resolv.conf` with:

  ```
  sudo rm /etc/resolv.conf
  sudo ln -s /run/systemd/resolve/resolv.conf /etc/resolv.conf
  ```

- `systemd-networkd` expects to be able to write lease durations to disk; since our microSDHC card will be read only, put the location it uses for that in a RAM disk with `sudo nano /etc/fstab` and adding at the end:

  ```
  tmpfs   /var/lib/systemd/network   tmpfs   defaults,size=1M,mode=0755,uid=systemd-network,gid=systemd-network   0   0
  ```

  ...making sure there is no white space at the start of the line, then `sudo reboot`.

 - `dhcpcd`, which we need for the  next step, does the same thing, and though it only bleats without causing a real problem, it is cleaner to fix it by `symlink`ing the relevant directory to `/run`:

    ```
    sudo ln -s /run/dhcpcd /var/lib/dhcpcd
    sudo mkdir -p /run/dhcpcd
    ```

- Before doing anything else, the creation of the `br0` interface below will replace your `eth0` interface with `br0`.  For some wacky reason, `systemd-networkd` does not have a way to adopt the MAC address of the Ethernet adapter for the `br0` interface, so the MAC address the Ethernet interface appears as back on the router will change and any static IP address assignment the router does for the Raspberry Pi will stop working.  To fix this we don't let `systemd-networkd` use DHCP to get an IP address by itself, instead we have a service that runs and updates the MAC address using a script the moment `systemd-networkd` creates `br0` and then we make sure that `dhcpcd` is running and _that_ gets the IP address for `br0`.  To do this, `sudo nano /etc/systemd/system/fix-br0-dhcp.service` and paste in the following:

  ```
  [Unit]
  Description=Fix br0 MAC and ensure dhcpcd is running
  After=systemd-networkd.service
  Wants=systemd-networkd.service
  PartOf=systemd-networkd.service

  [Service]
  Type=oneshot
  ExecStart=/home/<your home directory name>/FrontGardenRailway/software/pi/fix-br0-dhcp.sh
  RemainAfterExit=yes

  [Install]
  WantedBy=multi-user.target
  ```

  ...replacing `<your home directory name>` with your user name, then `sudo nano /etc/dhcpcd-br0.conf` and give the file contents:

  ```
  # DHCP client configuration for br0
  # Only manage br0
  interface br0

  # Set hostname from DHCP
  hostname

  # Wait for interface to be ready
  waitip 4

  # Client identifier
  clientid

  # Lease time options
  option dhcp_lease_time
  ```

  ...and finally:


  ```
  sudo systemctl start fix-br0-dhcp.service
  sudo systemctl enable fix-br0-dhcp.service
  ```

- Enable `systemd-networkd` with:

  ```
  sudo systemctl enable systemd-networkd
  ```

- Create the wireless bridge interface with `sudo nano /etc/systemd/network/10-create-bridge-br0.netdev`, pasting in the contents:

  ```
  [NetDev]
  Name=br0
  Kind=bridge
  ```

- Bind the bridge to the Ethernet interface with `sudo nano /etc/systemd/network/20-bind-ethernet-with-bridge-br0.network`, pasting in the contents:

  ```
  [Match]
  Name=eth0

  [Network]
  Bridge=br0
  ```

- Configure the bridge interface with `sudo nano /etc/systemd/network/30-config-bridge-br0.network`, pasting in the contents:

  ```
  [Match]
  Name=br0

  [Network]
  Address=10.10.3.1/24
  Gateway=10.10.2.1
  DHCPServer=yes

  [DHCPServer]
  # Start from 10.10.3.2
  PoolOffset=2
  PoolSize=253
  ```

- Enable the Wi-Fi access point to start at boot with:

  ```
  sudo systemctl unmask hostapd
  sudo systemctl enable hostapd
  ```

- Copy the contents of `hostapd.conf` to `/etc/hostapd/hostapd.conf`.

- Make a copy of the `hostapd` service file then edit it with:

  ```
  sudo cp /usr/lib/systemd/system/hostapd.service /etc/systemd/system/hostapd.service
  sudo nano /etc/systemd/system/hostapd.service
  ```

  ...and in it:

    - change `RestartSec` to 3,
    - if there is an `EnvironmentFile=` line, comment it out,
    - add the line `Environment="DAEMON_OPTS=-d -K -f /mnt/fgr_data/hostapd.log"`,
    - change the `Environment=DAEMON_CONF=` line to be `Environment="DAEMON_CONF=/etc/hostapd/hostapd.conf"`,
    - add `ExecStartPre=/bin/sleep 6` before the `ExecStart=` line,
    - change the `ExecStart=` line to be `ExecStart=/usr/sbin/hostapd -B -P /run/hostapd.pid $DAEMON_OPTS $DAEMON_CONF`.

- `sudo reboot` and Bob might be your mother's brother.

# Ghosts And Broadcomm Driver Instability
There appears to be [a\[nother\] bug](https://github.com/raspberrypi/linux/issues/6975) in the `brcmfmac` driver, in that the driver holds onto a station that has disconnected without notice for anywhere from 27 to 90+ seconds. No matter how many times the device boots up within this time, if it sends an association frame while that stale kernel window is active, the Pi completely ignores it.  Because the Pi ignores the frames indefinitely while the old session decays, the device connection times out, resulting in a persistent Wi-Fi 201 error.

To fix this, and it might be a good idea to do this whether you are using the on-board Pi Wi-Fi or not, Google Gemini wrote me a bash script `clear_node_ghosts.sh` which scans the output of `iw dev wlan0 station dump` every second and deletes any inactive MAC addresses.  Make this run with `sudo nano /etc/systemd/system/clear_node_ghosts.service`, pasting in the following:

```
[Unit]
Description=Force-Clear Ghost Node Connections from Station Table
After=NetworkManager.service

[Service]
Type=simple
ExecStart=/home/<your home directory name>/FrontGardenRailway/software/pi/clear_node_ghosts.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

...replacing `<your home directory name>` with your user name, then:

```
sudo systemctl start clear_node_ghosts
sudo systemctl enable clear_node_ghosts
```

...to run it and have it start at boot.

