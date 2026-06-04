#!/usr/bin/env python3

# Copyright 2026 Rob Meades
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Written by Google Gemini :-)

"""
Crash decode for FGR Log Server

This script is to be run locally on a PC that has the ESP-IDF
build environment installed on it and has access to an archive of
the ELF files built for all nodes types of the front garden railway,
likely built by node_esp32_deploy.py.  It acts as a crash decoder
for log_server.py, likely running on the controller Raspberry Pi of
the front garden railway.

When a crash occurs, a node will reboot and emit a backtrace and a
core dump, along with the hash of the running image when the
crash occurred.  `log_server.py` captures this and inserts a URL
style link into the log stream after it, of the form:

CRASH! Decode: http://127.0.0.1:8080/1780542538_10.10.3.8

...i.e. a clickable local URL containing an ID for the crash that
includes a unique number and the IP address of the node that emitted
the crash data.

With this script running as a service on the PC [requiring some
set-up, see below] clicking on that link will run this script,
which will talk to the web port of `log_server.py`, obtain
the crash meta-data and then run the necessary ESP-IDF tools with
it and the .ELF image to print a detailed decoded crash dump.

In detail, the URL will hit this server, which will strip out
the crash ID and send a 302 redirect with header
"Location: fgr-crash-decoder://{crash_id}", that will hit the
Mime handler you have set up (below) and trigger this script
to run _again_ (but not in deamon mode this time), causing it to
contact log_server.py and, finally, load the ESP-IDF environment
and do the decoding using esp-coredump.

Setup (for Linux - also possible on Windows somehow but I happen
to have the ESP-IDF tools installed on Linux as they compile
much more quickly there):

For this to run sweetly, create the following file:

~/.local/share/applications/fgr-crash-decoder.desktop

...with contents of the following form:

[Desktop Entry]
Name=FGR Crash Decoder
Type=Application
Exec=bash -c "source ~/.bashrc && export FGR_LOG_SERVER_ADDRESS=<ip> && export FGR_LOG_SERVER_WEB_PORT=<port> && export FGR_ARCHIVE_PATH=/home/<your local home directory>/<node_esp32_deploy.py staging directory>/archive && /usr/bin/python <path to cloned FGR repo>/FrontGardenRailway/software/pi/crash_decoder.py %u"
Terminal=false
MimeType=x-scheme-handler/fgr-crash-decoder;

...where:
  - <ip> is replaced with the IP address of the machine
    that log_server.py is running on (e.g. 10.10.2.10),
  - <port> is the port that the log_server.py's web
    interface is listening on (e.g. 8060)
  - <your local home directory> is replaced with the
    home directory on this PC,
  - <node_esp32_deploy.py staging directory> is the name
    of the staging directory on this PC as given to
    node_esp32_deploy.py.
  - <path to cloned FGR repo> is the directory into
    which this repo has been cloned.

...then register this file as a MIME handler with:

update-desktop-database ~/.local/share/applications/
xdg-mime default fgr-crash-decoder.desktop x-scheme-handler/fgr-crash-decoder

...and ensure that local port 8080 (or whatever you
supply as '--port') is available.

You will also need to set up three environment
variables (so nano ~/.bashrc and add, at the end,
"export blah") with some of the values from above:

FGR_LOG_SERVER_ADDRESS=<ip>
FGR_LOG_SERVER_WEB_PORT=<port>
FGR_ARCHIVE_PATH=<node_esp32_deploy.py staging directory>

You also, of course, need to have get_idf in there
as an alias, e.g.

alias get_idf=". $HOME/.espressif/v5.5.2/esp-idf/export.sh"

"""

import sys
import os
import requests
import subprocess
import argparse
import platform
import signal
import traceback
import webbrowser
import tempfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        crash_id = self.path.lstrip('/')
        self.send_response(302)
        self.send_header('Location', f"fgr-crash-decoder://{crash_id}")
        self.end_headers()
    def log_message(self, format, *args):
        return

class CrashDecoderServer:
    def __init__(self, port):
        self.port = port
        self.server = HTTPServer(('127.0.0.1', self.port), RedirectHandler)
        self.running = True

        # Register signals
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, signum, frame):
        print(f"\nSignal {signum} received. Shutting down...")
        # Force-close the socket first to unstick any blocking 'accept'
        try:
            self.server.socket.close()
        except:
            pass

        # Use os.kill to send SIGKILL to this process (PID is os.getpid())
        # SIGKILL (9) is handled by the kernel, not the Python interpreter.
        os.kill(os.getpid(), signal.SIGKILL)

    def run(self):
        print(f"FGR crash decoder server running on http://127.0.0.1:{self.port}...")
        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            pass
        print("Server stopped.")

def early_exit(error_message="Unknown error"):
    # Just show the error in the browser instead of popping a terminal
    show_in_browser("error", f"ERROR: {error_message}", is_error=True)
    sys.exit(1)

def show_in_browser(node_ip, content, is_error=False):
    """Writes content to a temp HTML file and opens it in the browser."""
    color = "red" if is_error else "black"
    warning_msg = (
        "Note: ignore the two bash errors below, they are harmless artifacts of the non-interactive shell environment.\n"
    )
    final_content = warning_msg + content
    html_content = f"<html><head><title>FGR crash {node_ip}</title></head><body><pre style='color:{color};'>{final_content}</pre></body></html>"
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix=".html") as tmp_file:
        tmp_file.write(html_content)
        tmp_path = tmp_file.name
    webbrowser.open(f"file://{tmp_path}", new=2)

def main():
    parser = argparse.ArgumentParser(description="FGR crash decoder")
    parser.add_argument("--daemon", action='store_true', help="Run in daemon mode")
    parser.add_argument("--port", type=int, default=8080, help="Listening port")
    parser.add_argument("--server_address", default=os.getenv("FGR_LOG_SERVER_ADDRESS"),
                        help="The IP address of the machine where log_server.py is running")
    parser.add_argument("--server_web_port", default=os.getenv("FGR_LOG_SERVER_WEB_PORT"),
                        help="The port on which log_server.py's web interface is available")
    parser.add_argument("--archive", default=os.getenv("FGR_ARCHIVE_PATH"),
                        help="The archive directory containing .ELF files for nodes, likely managed by nodes_esp32_deploy.py")
    args, _ = parser.parse_known_args()

    try:
        if args.daemon:
            print(f"Attempting to start server on port {args.port}...")
            try:
                decoder_server = CrashDecoderServer(args.port)
                decoder_server.run()
            except OSError as e:
                if e.errno == 98:
                    print(f"ERROR: Port {args.port} is already in use.", file=sys.stderr)
                else:
                    print(f"ERROR: Server failed to start: {e}", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"ERROR: Unexpected server failure: {e}", file=sys.stderr)
                sys.exit(1)
            return

        # Direct Analysis Mode
        if not args.server_address:
            early_exit(f"missing log server IP address")
        if not args.server_web_port:
            early_exit(f"missing log server port")
        if not args.archive:
            early_exit(f"missing archive location")

        print(f"Log server {args.server_address}:{args.server_web_port}, archived ELF files at {args.archive}")

        uri = sys.argv[-1]
        if "://" not in uri:
            early_exit(f"invalid URL '{uri}'")

        crash_id = uri.split("://")[-1].rstrip('/')
        node_ip = crash_id.split("_")[1]

        try:
            # Fetch Core Dump
            core_url = f"http://{args.server_address}:{args.server_web_port}/data/{crash_id}"
            core_data = requests.get(core_url, timeout=10).content
            core_file = Path(f"/tmp/{crash_id}.core")
            core_file.write_bytes(core_data)

            # Fetch Metadata
            meta_url = f"http://{args.server_address}:{args.server_web_port}/meta/{crash_id}"
            fw_hash = requests.get(meta_url, timeout=5).json()['fw_hash']
        except Exception as e:
            early_exit(e)

        elf_candidates = list(Path(args.archive).rglob(f"{fw_hash}/test.elf"))
        if not elf_candidates:
            early_exit("ELF file not found")

        cmd = f"get_idf && esp-coredump info_corefile -c {core_file} -t raw {elf_candidates[0]}"

        if platform.system() == "Windows":
            # NOTE: not tested
            result = subprocess.run(["start", "cmd", "/k", cmd],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True)
        else:
            result = subprocess.run(["bash", "-ic", cmd],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True)

        # Display the output (or error if non-zero exit code)
        if result.returncode != 0:
            show_in_browser(node_ip, result.stdout, is_error=True)
        else:
            show_in_browser(node_ip, result.stdout)


    except Exception:
        error_details = traceback.format_exc()
        early_exit(error_details)

if __name__ == "__main__":
    main()