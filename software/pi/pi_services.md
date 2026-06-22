# Introduction
These instructions describe how to set up the various services that form the Front Garden Railway server, `https_server.py`, `log_server.py` and `web_controller.py`.

# HTTPS Server Setup
All of the ESP32 nodes will want to make an HTTPS connection to the access point to download updates to their programs; this is what the Python script `https_server.py` does.  To get it running with the ESP32s, connect a serial terminal to the Pi Zero, or Ethernet to a bigger Pi, and do the following:

- Create a directory off `/mnt/ssd` named `fw`.

- `cd` to that directory and run SSL to create a key pair with:

  `openssl req -newkey rsa:2048 -x509 -days 36500 -nodes -out ca_cert.pem -keyout ca_key.pem`

  ...leaving all entries blank by entering `.` _except_ the Common Name entry, which *must* be set `10.10.3.1` (the IP address of the Pi as an access point).

- On a PC which has the ESP-IDF software environment installed on it, and has a clone of this repository, replace the file `FrontGardenRailway/software/server_certs/ca_cert.pem` with the `ca_cert.pem` you just generated.

- Go take a look at the [`README.md`](../esp32) in the ESP32 directory: it will explain to you how to build the code for the ESP32 nodes using the script [`nodes_esp32_deploy.py`](../esp32/nodes_esp32_deploy.py).  Do what it says to populate the `/mnt/ssd/fw` directory with images.

- Create `sudo nano /lib/systemd/system/https_server.service` with the following contents:

  ```
  [Unit]
  Description=HTTPS Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/mnt/ssd/fw
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/https_server.py  . --node-cfg /home/<your home directory name>/FrontGardenRailway/software/esp32/nodes_esp32_deploy.json
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start https_server`

- In order to stop your browser objecting that it is using a self-signed certificate when talking to the `https_server.py` web interface:
  - on Windows copy `ca_cert.pem` to your PC, rename it to `ca_cert.crt`, double-click on it and `Install Certificate...` -> `Local Machine`, browse to `Trusted Root Certification Authorities` and place the certificate there, or...
  - on Linux copy `ca_cert.pem` to `/usr/local/share/ca-certificates/ca_fgr.crt` and run `sudo update-ca-certificates` (if you ever find you need to update the certificate, replace the old with the new and add `--fresh` to the command-line).

- Check that the `https_server.py` dashboard is visible from your PC at the URL `https://<pi IP address>:8070/dashboard`.

- To make the service run at boot:

  `sudo systemctl enable https_server`

  ...then take the power down and up again and repeat the check.

# Log Server Setup
The `log_server.py` script listens for log messages from all nodes and stuffs the messages into the journal and a database.  To get `log_server.py` to run at boot, make sure port 5001 (the default port it will listen for logs on) and port 8060 (where it offers a minimal web interface from which core dumps can be retrieved) are open, then:

- `sudo nano /lib/systemd/system/log_server.service` with the following contents:

  ```
  [Unit]
  Description=Log Server
  After=multi-user.target

  [Service]
  Type=simple
  WorkingDirectory=/home/<your home directory name>/FrontGardenRailway/software/pi
  ExecStart=python -u log_server.py --web-port 8060 --web-bind 10.10.2.10 --port 5001 --db-path /mnt/ssd/logs.db --node-cfg ../esp32/nodes_esp32_deploy.json
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start log_server`

  ...and make sure the ESP32's connect to the Wi-Fi AP, the HTTPS server and then the log server.

- To view the log messages:
  
  `journalctl -t fgr-log-server`

  ...or to view the log messages from a particular IP address, updated in real time:

  `journalctl -f -t fgr-log-server SOURCE_IP=10.10.3.24`
  
  ... or use `log_viewer.py` to query the database.
  
- To make the service run at boot:

  `sudo systemctl enable log_server`

- Decoding CORE DUMP messages sent by ESP32 nodes requires you to run [`crash_decoder.py`](crash_decoder.py) on the /[Linux/] PC on which you build the ESP-IDF code: see the top of that file for what they are.

- When those are done, set `crash_decoder.py` to run as a service on the local PC by doing `sudo nano /lib/systemd/system/fgr_crash_decoder.service` and pasting in the following contents:

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

- Still on the local PC, `sudo systemctl start fgr_crash_decoder` and `sudo systemctl enable fgr_crash_decoder` to make it run at boot.  Should you not get the nice URL thingy from `log_server.py` for some reason, you can query the database directly from the Pi with something like:

  ```
  sudo sqlite3 /mnt/ssd/logs.db "SELECT * FROM crash_dumps ORDER BY timestamp_utc DESC LIMIT 50;"
  ```

  ...then take the left-most field, form it into a URL something like:

  ```
  http://127.0.0.1:8080/1781624858_10.10.3.7
  ```

  ...paste it into your browser and it should be exactly like you clicked on the `log_server` link.

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
  ExecStart=python /home/<your home directory name>/FrontGardenRailway/software/pi/web_controller.py --db-path /mnt/ssd/logs.db
  KillSignal=SIGINT
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- Test that the service starts with:

  `sudo systemctl start web_controller`

  ...and make sure that the ESP32 test nodes running the test application, with a MAC address that gives them IP addresses, can connect to the controller script on the Raspberry Pi Wifi AP on port 5000 and (b) a PC that is able to connect to the Raspberry Pi Wifi AP can bring up the web controller interface on port 8080.
  
- When all is good, make the service run at boot with:

  `sudo systemctl enable web_controller`
