# Introduction
This folder contains the Python scripts that run on the Raspberry Pi server of the front garden railway (plus one that runs on the build PC), plus explanatory `README.md`s describing how to set it up.

- [`pi_read_only_file_system.md`](pi_read_only_file_system): how to set up the Raspberry Pi to have a read only SD card, for robustness to power just going away; DO THIS FIRST,
- [`pi_wifi_ap.md`](pi_wifi_ap.md): how to set up the Pi as a Wi-Fi access point; DO THIS SECOND,
- [`pi_wifi_dhcp_mac.md`](pi_wifi_dhcp_mac.md): how to set up the Pi to do DHCP with static IP addresses for known things, and only allow known MAC addresses to connect; DO THIS, UMMH, THIRDLY,
- [`pi_services.md`](pi_services.md): how to install all of the services for the Front Garden Railway; DO THIS FOURTHRIGHTLY.
- [`pi_installation.md`](pi_installation.md): how to install all of this properly on your network; DO THIS LAST.

- `https_server.py`: the HTTPS server that provides OTA updates to the connected ESP32s and a small web dashboard to monitor what version they are running and switch nodes to development mode,
- `binary_file_version.py`: a utility that extracts the version information from an ESP32 compiled binary file,
- `log_server.py`: captures logs from all nodes and writes to journal/database, also offers access to crash-dump information, used by `crash_decoder.py`,
- `log_viewer.py`: view logs extracted from the database, rather than from the journal,
- `controller.py`: main script controlling all nodes, sub-classed by `web_controller.py`,
- `web_controller.py`: main web interface viewing and controlling all nodes,
- `nodes.json`: configuration file for `controller.py`,
- `add_node.py`: script to automate adding a new node to the system,
- `clear_node_ghosts.sh`: script to try to make Wifi on the Pi clean-up better.

- `crash_decoder.py`: script to be run locally on the build PC that bridges to `log_server.py` and can decode crash dumps with one click.

The `nodes` sub-directory contains the node-specific code that forms part of `controller.py`.
