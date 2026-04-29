# Introduction
This directory defines the communications protocol between the ESP32 devices on the front garden railway and the controlling Raspberry Pi:

- `fgr_protocol.h`: the master definition,
- `generate_python_protocol_module.py`: parses `fgr_protocol.h` and writes `fgr_protocol.py`,
- `fgr_protocol.py`: the output of `generate_python_protocol_module.py`, a Python module that can be used in the script running on the Raspberry Pi that controls everything.

The protocol is intended to be run over a lossless, ordered, bearer (e.g. a TCP socket).
