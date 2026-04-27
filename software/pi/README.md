# Introduction
This folder contains the Python scripts that run on the Raspberry Pi server of the front garden railway, plus explanatory `README.md`s describing how to set it up.

- `pi_read_only_file_system.md`: how to set up the Raspberry Pi to have a read only SD card, for robustness to power just going away,
- `pi_wifi_ap.md`: how to set up the Pi as a Wi-Fi access point,
- `pi_wifi_dhcp_mac.md`: how to set up the Pi to do DHCP with static IP addresses for known things, and only allow known MAC addresses to connect,
- `https_server.py`: the HTTPS server that provides OTA updates to the connected ESP32s,
- `binary_file_version.py`: a utility that extracts the version information from an ESP32 compiled binary file.