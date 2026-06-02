# Introduction
These instructions describe how to set up a Wi-Fi access point on a headless Pi Zero W.  Note that, on the version of Raspbian I was using (Trixie), any attempt to set an access point with security failed, so these instructions set up an open Wi-Fi access point (security is provided later through [MAC address filtering](pi_wifi_dhcp_mac.md)).

# Preparation
Since the Pi will lose connectivity to your Wi-Fi network (you do _not_ want an open access point on your Wi-Fi network) you must have a serial connection to the headless Pi (e.g. using a 3V3 FTDI cable, black to GND, yellow (RXD) to GPIO14 (TXD), orange (TXD) to GPIO15 (RXD)).

- If you have hardened the Pi, enter `rw` to make the Pi writeable.

- The Pi will also lose connectivity to the internet, so install a few useful things first:

  - `sudo apt install git`: 'cos you'll need that for the next line,

  - `git clone https://github.com/RobMeades/FrontGardenRailway.git`: 'cos you will need the `https_server.py` script,

  - `sudo apt install python3-aiohttp`: which will be needed by `https_server.py`,

  - `sudo apt install python3-systemd`: which will be needed by `log_server.py`,

  - `sudo apt install ntpsec-ntpdate`: useful if you get into a tangle with NTP time offsets later,

  - `sudo apt install minicom`: serial communications program,

  - `sudo apt install lrzsz`: this allows the `minicom` and `picocom` serial communications programs to perform file transfer,
  
  - `sudo apt install iptables iptables-persistent`: will be needed for MAC address filtering,

  - `sudo apt install tcpdump`: can be handy for debugging,

  - `sudo apt install sqlite3`: may be needed later when you are storing metrics from nodes in a database,

- Connect a PC to the Pi's serial port and log in to it, e.g. `minicom -D /dev/ttyUSB0` on Linux.

- Check that binary file uploads and downloads work, e.g. in `minicom` `CTRL-A`, `S`, `zmodem`, then find a binary file (let's call it `blah.bin`) and send it, rename the uploaded file to something like `blah_new.bin`, then in the `minicom` terminal type `sz blah_new.bin` to send the file back, leave `minicom` and finally, on Linux, `diff blah.bin blah_new.bin` should produce no output (i.e. the files are the same).

# AP Setup

*** SEE NOTE ABOUT PI ZERO W WIFI BELOW ***

Connect to the Pi using a serial terminal and set the AP up as follows:

- On the Pi, `sudo nano /etc/NetworkManager/NetworkManager.conf` and, in the section `[ifupdown]` change `managed` to `true` (otherwise you won't be able to create a new connection).

- On the Pi, create a Wi-Fi-specific NetworkManager configuration file with `sudo nano /etc/NetworkManager/conf.d/99-wifi-powersave.conf` and give it the contents:

  ```
  [connection]
  # Switch power saving off to avoid poll time-outs
  wifi.powersave = 2
  ``

- Restart NetworkManager with:

  `sudo systemctl restart NetworkManager`

- NOTE: I suffered occasional crashes of the Broadcomm Wi-Fi driver on the Raspberry Pi Zero W, apparently due to SDIO communication hanging, for which the suggested workaround was to create and populate a driver modification file with:

  `echo "options brcmfmac roamoff=1 feature_disable=0x82000" | sudo tee /etc/modprobe.d/brcmfmac.conf`

- Now you can create the access point with:

  `sudo nmcli connection add type wifi ifname wlan0 con-name FGR autoconnect yes connection.autoconnect-priority 1 ssid FGR`

- Set some properties for the access point with:

  `sudo nmcli connection modify FGR 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.10.3.1/24`

- Finally, bring up the AP with:

  `sudo nmcli connection up FGR`

- If you want to bring the AP down, `sudo nmcli connection down FGR` and the Pi will return to having a connection to your Wi-Fi network.

# Pi Zero W Wifi Instability
I found the Pi Zero W on-board Wifi to be far too unstable, see these posts for details:

[https://forums.raspberrypi.com/viewtopic.php?p=2374992](https://forums.raspberrypi.com/viewtopic.php?p=2374992)
[https://github.com/raspberrypi/firmware/issues/1768#issuecomment-4084988745](https://forums.raspberrypi.com/viewtopic.php?p=2374992)

Hence I switched to a Pi Zero I happened to have spare (could also use a Pi Zero W and switch to `wlan1`) and plugged in an AR9271 USB Wifi dongle: be careful which you choose!  the TPLink AC600 (`rtl8811au` chipset) looks good but only one of the three Linux drivers (which you must build yourself for Linux kernel versions > 6.14 (Trixie is 6.12)) I tried worked and the working one did not support transmission of TIM information elements which are required for a standards-compliant Wifi AP (ESP32 refused to connect).  The AR9271 dongle is huge but is known to work with Linux which has built-in drivers for it.  It _will_ extend the restart time of the Network Manager service to several minutes, but what can you do...

# Broadcomm Driver Instability
There appears to be [a\[nother\] bug](https://github.com/raspberrypi/linux/issues/6975) in the `brcmfmac` driver, in that the driver holds onto a station that has disconnected without notice for anywhere from 27 to 90+ seconds. No matter how many times the device boots up within this time, if it sends an association frame while that stale kernel window is active, the Pi completely ignores it.  Because the Pi ignores the frames indefinitely while the old session decays, the device connection times out, resulting in a persistent Wifi 201 error.  More details here:

To fix this, Google Gemini wrote me a bash script `clear_node_ghosts.sh` which scans the output of `iw dev wlan0 station dump` every second and deletes any inactive MAC addresses.  You will need to `sudo chmod +x clear_node_ghosts.sh` to make the script executable and then `sudo nano /etc/systemd/system/clear_node_ghosts.service`, paste the following in:

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
sudo systemctl daemon-reload
sudo systemctl start clear_node_ghosts
sudo systemctl enable clear_node_ghosts
```

...to run it and have it start at boot.

# HTTPS Server Setup
All of the ESP32 nodes will want to make an HTTPS connection to the access point to download updates to their programs; this is what the Python script `https_server.py` does.  To get it running with the ESP32s, connect a serial terminal to the Pi and do the following:

- Create a directory off your home directory named `fw`.

- `cd` to that directory and run SSL to create a key pair with:

  `openssl req -newkey rsa:2048 -x509 -days 36500 -nodes -out ca_cert.pem -keyout ca_key.pem`

  ...leaving all entries blank by entering `.` _except_ the Common Name entry, which *must* be set `10.10.3.1` (the IP address of the Pi as an access point).

- On a PC which has the ESP-IDF software environment installed on it, and has a clone of this repository, replace the file `FrontGardenRailway/software/server_certs/ca_cert.pem` with the `ca_cert.pem` you just generated.

- Build the ESP-IDF `test` application, e.g. by opening the workspace file `FrontGardenRailway/software/esp32/applications/test/test.code-workspace` in Visual Studio Code and pressing `CTRL-e` then `b`.

- Copy the newly created `test.bin` file to the `~/fw` directory on the Pi and rename it to `default.bin`.

- On the Pi, run the script:

  `python ~/FrontGardenRailway/software/pi/https_server.py ~/fw`

- Plug the same build PC into an ESP32, flash the newly created `test.bin` to the ESP32 and monitor the output of the ESP32.  You should see that the ESP connects to the Wi-Fi access point of the Pi, downloads at least the start of the file `default.bin` via HTTPS, realises it does not need to do an update and drops the HTTPS connection.

- If this all works, create `sudo nano /lib/systemd/system/https_server.service` with the following contents:

  ```
  [Unit]
  Description=HTTPS Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/fw
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/https_server.py
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start https_server`

  ...and make sure the ESP32 connects to the Wi-Fi AP and the HTTPS server to ensure all is good.

- To make the service run at boot:

  `sudo systemctl enable https_server`

  ...then take the power down and up again and repeat the check.

- If you had hardened the Pi, put it back into read-only mode with the command `ro`.

- NOTE: the way the HTTPS server works will change later (see "Proper OTA" in [`pi_installation.md`](pi_installation.md)) but for now this is good enough.

# Log Server Setup
The `log_server.py` script can be run on the Raspberry Pi to listen for log messages from all nodes and stuff the messages into the journal.  To get this script to run at boot, make sure port 5001 (the default port it will listen on) is open, then:

- `sudo nano /lib/systemd/system/log_server.service` with the following contents:

  ```
  [Unit]
  Description=Log Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/FrontGardenRailway/software/pi
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/log_server.py
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start log_server`

  ...and make sure the ESP32 connects to the Wi-Fi AP, the HTTPS server and then the log server.

- To view the log messages:
  
  `journalctl -t fgr-log-server`

  ...or to view the log messages from a particular IP address, updated in real time:

  `journalctl -f -t fgr-log-server SOURCE_IP=10.10.3.24`
  
- To make the service run at boot:

  `sudo systemctl enable log_server`
  
# Controller Setup
`controller.py` provides all of the main control logic for the nodes of the front garden railway, however it is not run directly, instead `web_controller.py` sub-classes it to provide a web interface.

Get `web_controller.py` to run at boot, using port 5000 for the connections to the nodes and port 8080 for the web interface by following the same pattern as above:

- `sudo nano /lib/systemd/system/web_controller.service` with the following contents:

  ```
  [Unit]
  Description=Web Controller
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/FrontGardenRailway/software/pi
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/web_controller.py
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start web_controller`

  ...and make sure that (a) an ESP32 test node running the test application, with a MAC address that gives it the static IP address 10.10.3.2, can connect to the controller script on the Raspberry Pi Wifi AP on port 5000 and (b) a PC that is able to connect to the Raspberry Pi Wifi AP can bring up the web controller interface on port 8080.
  
- When all is good, make the service run at boot with:

  `sudo systemctl enable web_controller`
