# Introduction
These instructions describe how to set up a Wi-Fi access point on a headless Pi Zero W.  Note that, on the version of Raspbian I was using (Trixie), any attempt to set an access point with security failed, so these instructions set up an open Wi-Fi access point (security is provided later through [MAC address filtering](pi_wifi_dhcp_mac.md)).

# Preparation
Since the Pi will lose connectivity to your Wi-Fi network (you do _not_ want an open access point on your Wi-Fi network) you must have a serial connection to the headless Pi.

- If you have hardened the Pi, enter `rw` to make the Pi writeable.

- The Pi will also lose connectivity to the internet, so install a few useful things first:

  - `sudo apt install git`: 'cos you'll need that for the next line,

  - `git clone https://github.com/RobMeades/MusicalBox.git`: 'cos you will need the `https_server.py` script,

  - `sudo apt install python3-aiohttp`: which will be needed by `https_server.py`,

  - `sudo apt install python3-systemd`: which will be needed by `log_server.py`,

  - `sudo apt install python3-aiohttp-jinja2`: which will be needed by `web_server.py`,

  - `sudo apt install lrzsz`: this allows the `minicom` and `picocom` serial communications programs to perform file transfer,
  
  - `sudo apt install iptables iptables-persistent`: will be needed for MAC address filtering,

  - `sudo apt install tcpdump`: can be handy for debugging,

- Connect a PC to the Pi's serial port and log in to it, e.g. `minicom -D /dev/ttyUSB0` on Linux.

- Check that binary file uploads and downloads work, e.g. in `minicom` `CTRL-A`, `S`, `zmodem`, then find a binary file (e.g. the `stepper.bin` file that you will have built when testing the musical box) and send it, rename the uploaded file to something like `stepper_new.bin`, then in the `minicom` terminal type `sz stepper_new.bin` to send the file back, leave `minicom` and finally, on Linux, `diff stepper.bin stepper_new.bin` should produce no output (i.e. the files are the same).

# AP Setup
Connect to the Pi using a serial terminal and set the AP up as follows:

- On the Pi, `sudo nano /etc/NetworkManager/NetworkManager.conf` and:

  - In the section `[ifupdown]` change `managed` to `true` (otherwise you won't be able to create a new connection).

  - Add a section:
    ```
    [802-11-wireless]
    # Switch power saving off to avoid poll time-outs
    powersave=2
    ```

- Restart NetworkManager with:

  `sudo systemctl restart NetworkManager`

- Now you can create the access point with:

  `sudo nmcli connection add type wifi ifname wlan0 con-name MusicalBox autoconnect yes ssid MusicalBox`

- Set some properties for the access point with:

  `sudo nmcli connection modify MusicalBox 802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.10.3.1/24`

- Finally, bring up the AP with:

  `sudo nmcli connection up MusicalBox`

- If you want to bring the AP down, `sudo nmcli connection down MusicalBox` and the Pi will return to having a connection to your Wi-Fi network.

# HTTPS Server Setup
All of the ESP32 boards will want to make an HTTPS connection to the access point to download updates to their programs; this is what the Python script `https_server.py` does.  To get it running with the ESP32s, connect a serial terminal to the Pi and do the following:

- Create a directory off your home directory named `fw`.

- Copy the `https_server.py` script to this directory with:

  `cp ~/MusicalBox/software/pi/https_server.py ~/fw`

- `cd` to that directory and run SSL to create a key pair with:

  `openssl req -newkey rsa:2048 -x509 -days 36500 -nodes -out ca_cert.pem -keyout ca_key.pem`

  ...leaving all entries blank by entering `.` _except_ the Common Name entry, which *must* be set `10.10.3.1` (the IP address of the Pi as an access point).

- On a PC which has the ESP32 software environment installed on it, and has a clone of this repository, replace the file `MusicalBox/software/esp32/stepper/server_certs/ca_cert.pem` with the `ca_cert.pem` you just generated.

- Build the ESP-IDF `stepper` application with the flag `CONFIG_STEPPER_TEST_ROTATION` set to 1 (leave the flag `CONFIG_STEPPER_NO_WIFI` set to 0 so that the ESP32 _should_ contact the new server).

- Copy the newly created `stepper.bin` file to the `~/fw` directory on the Pi.

- On the Pi, run the script:

  `python https_server.py`

- Plug the same build PC into an ESP32 (likely the one that is inside the stand of the musical box), flash the newly created `stepper.bin` to the ESP32 and monitor the output of the ESP32.  You should see that the ESP connects to the Wi-Fi access point of the Pi, downloads at least the start of the file `stepper.bin` via HTTPS, realises it does not need to do an update, drops the HTTPS connection and continues to the motor rotation part of the test.

- If this all works, create `sudo nano /lib/systemd/system/https_server.service` with the following contents:

  ```
  [Unit]
  Description=HTTPS Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/fw/
  ExecStart=python /home/<your home directory name>/fw/https_server.py
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start https_server`

  ... and:

  `sudo systemctl status https_server`

  ...should show nice green things.  Maybe reboot the ESP32 and watch its output again as it connects to the Wi-Fi AP and the HTTPS server to ensure all is good.  You might also run:

  `journalctl -u https_server.service -f`

  ...on the Pi to live-monitor the output of the `https_server` service as the ESP32 is connecting.

- To make the service run at boot:

  `sudo systemctl enable https_server`

  ...then take the power down and up again; the motor attached to the ESP32 should rotate once everything has come up.

- If you had hardened the Pi, put it back into read-only mode with the command `ro`.

# Log Server Setup
If your \[ESP32\] connected devices are able to send their log messages to this server over TCP, `log_server.py` can be run to listen for them and stuff the messages into the journal.  To get this script to run at boot, making sure port 5001 (the default port it will listen on) and then:

- `sudo nano /lib/systemd/system/log_server.service` with the following contents:

  ```
  [Unit]
  Description=Log Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/MusicalBox/software/pi
  ExecStart=python /home/<your home directory name>/MusicalBox/software/pi/log_server.py
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start log_server`

  ... and:

  `sudo systemctl status log_server`

  ...should show nice green things.  To view the log messages:
  
  `journalctl -t esp32-device`
  
- To make the service run at boot:

  `sudo systemctl enable log_server`

# X Server Setup
Follow the pattern above to set up anything (e.g. `web_server.py` for the control interface to the musical box) as a run-from-boot service on the Pi.
