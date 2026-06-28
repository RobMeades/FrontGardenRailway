# Introduction
These instructions describe how to set up a Wi-Fi access point on a headless Pi Zero W.  Note that, on the version of Raspbian I was using (Trixie), any attempt to set an access point with security failed, so these instructions set up an open Wi-Fi access point (security is provided later through [MAC address filtering](pi_wifi_dhcp_mac.md)).

NOTE: in all cases below, when pasting contents into a file, ensure there are no leading spaces.

# Preparation

## Installations
Since the Pi will lose connectivity to your Wi-Fi network (you do _not_ want an open access point on your Wi-Fi network) you must have a serial connection to a headless Pi Zero (e.g. using a 3V3 FTDI cable, black to GND, yellow (RXD) to GPIO14 (TXD), orange (TXD) to GPIO15 (RXD)), or an Ethernet connection to a bigger Pi.

- If you have hardened the Pi, enter `rw` to make the Pi writeable.

- The Pi will also lose connectivity to the internet, so install a few useful things first:

  - `sudo apt install git`: 'cos you'll need that for the next line,

  - `git clone https://github.com/RobMeades/FrontGardenRailway.git`: 'cos you will need the various Python scripts,

  - `sudo apt install python3-aiohttp`: which will be needed by `https_server.py`,

  - `sudo apt install python3-systemd`: which will be needed by `log_server.py`,

  - `sudo apt install minicom`: serial communications program,

  - `sudo apt install lrzsz`: this allows the `minicom` and `picocom` serial communications programs to perform file transfer,
  
  - `sudo apt install iptables iptables-persistent`: will be needed for MAC address filtering (save the current rules if it asks),

  - `sudo apt install tcpdump lsof jq`: can be handy for debugging,

  - `sudo apt install sqlite3`: may be needed later when you are debugging the database.

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

# AP Setup

## Pi Zero W Wifi Instability
I found the Pi Zero W on-board Wifi to be far too unstable in AP mode, see these posts for details:

[https://forums.raspberrypi.com/viewtopic.php?p=2374992](https://forums.raspberrypi.com/viewtopic.php?p=2374992)
[https://github.com/raspberrypi/firmware/issues/1768#issuecomment-4084988745](https://forums.raspberrypi.com/viewtopic.php?p=2374992)

Hence I switched to a Pi Zero I happened to have spare, later a Pi 5 (see note below) and plugged in a USB Wifi dongle: be careful which you choose!  the TPLink AC600 (`rtl8811au` chipset) looks good but only one of the three Linux drivers (which you must build yourself for Linux kernel versions > 6.14 (Trixie is 6.12)) I tried worked and the working one did not support transmission of TIM information elements which are required for a standards-compliant Wifi AP (ESP32 refused to connect).  The AR9271 dongle is huge but is known to work with Linux which has built-in drivers for it.

When you do this, assuming you do _not_ need the on-board Wifi on the Pi Zero W (or a bigger Pi) operating in client mode, `sudo nano /boot/firmware/config.txt` and add, near the top:

```
# Disable on-board Wifi
dtoverlay=disable-wifi
```

...then reboot.

If using a Pi 5 and supplying sufficient power for all things plugged into it (25 Watts), you may need to tell the Pi that this so.  If:

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

To make sure the Pi supplies the full 1.6 Amps to the USB peripherals, `sudo nano /boot/firmware/config.txt` and add to the end:

```
usb_max_current_enable=1
```

Reboot and hopefully all will be good.

## AR9271 USB Wifi Driver Instability
And, whaddaya know, the `AR9271` driver has known instabilities also, instabilities which can crash the Linux kernel (`ar9002_hw_calibrate` dereferencing a NULL pointer), bless its little cotton socks.  The only way out of _this_ is to rely on the watchdog which is already enabled on a Pi by default, though my experience is that the USB is left in a state where only a hard reboot will recover. Ugh.

## Setup
Connect to a Pi Zero using a serial terminal, or a bigger Pi using Ethernet, and set the AP up as follows:

- On the Pi, `sudo nano /etc/NetworkManager/NetworkManager.conf` and, if the `plugins` line has `ifupdown` in it, remove it (so it might become `plugins=keyfile`), otherwise you won't be able to create a new connection.

- On the Pi, create a Wi-Fi-specific NetworkManager configuration file with `sudo nano /etc/NetworkManager/conf.d/99-wifi-powersave.conf` and give it the contents:

  ```
  [connection]
  # Switch power saving off to avoid poll time-outs
  wifi.powersave = 2
  ``

- Restart NetworkManager with:

  `sudo systemctl restart NetworkManager`

- NOTE: originally, when using the Pi Zero W's own Wifi, I suffered occasional crashes of the Broadcomm Wi-Fi driver, apparently due to SDIO communication hanging, for which the suggested workaround was to create and populate a driver modification file with:

  `echo "options brcmfmac roamoff=1 feature_disable=0x82000" | sudo tee /etc/modprobe.d/brcmfmac.conf`

- Now you can create the access point with:

  `sudo nmcli connection add type wifi ifname wlan0 con-name FGR autoconnect yes connection.autoconnect-priority 1 ssid FGR`

- Set some properties for the access point with:

  `sudo nmcli connection modify FGR 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.10.3.1/24`

- Also set retries to zero to stop the Network Manager black-listing a device that repeatedly tries to connect:

  `sudo nmcli connection modify FGR connection.autoconnect-retries 0`

- If there is a pre-existing Wifi station configuration, make sure it does not auto-connect ever with:

  ```
  sudo nmcli connection modify <connection name> connection.autoconnect no
  sudo nmcli connection down <connection name>
  ```

- Finally, bring up the AP with:

  `sudo nmcli connection up FGR`

- You should now be able to connect to this open Wifi `FGR` access point from any device.

- If you want to bring the AP down, `sudo nmcli connection down FGR` and the Pi will return to having a connection to your Wi-Fi network.

## Ghosts And Broadcomm Driver Instability
There appears to be [a\[nother\] bug](https://github.com/raspberrypi/linux/issues/6975) in the `brcmfmac` driver, in that the driver holds onto a station that has disconnected without notice for anywhere from 27 to 90+ seconds. No matter how many times the device boots up within this time, if it sends an association frame while that stale kernel window is active, the Pi completely ignores it.  Because the Pi ignores the frames indefinitely while the old session decays, the device connection times out, resulting in a persistent Wifi 201 error.  More details here:

To fix this, and it might be a good idea to do this whether you are using the on-board Wifi or not Google Gemini wrote me a bash script `clear_node_ghosts.sh` which scans the output of `iw dev wlan0 station dump` every second and deletes any inactive MAC addresses.  Make this run with `sudo nano /etc/systemd/system/clear_node_ghosts.service`, pasting in the following:

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

...then:

```
sudo systemctl start clear_node_ghosts
sudo systemctl enable clear_node_ghosts
```

...to run it and have it start at boot.

