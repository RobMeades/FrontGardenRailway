#!/usr/bin/env python

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

'''HTTPS server on a Raspberry Pi Wi-Fi access point to which clients can connect to download binary files for OTA updates.'''

import asyncio
import ssl
import logging
import os
import time
import argparse
from aiohttp import web
from aiohttp.web import HTTPException
from aiohttp.client_exceptions import ClientConnectorError
# These are the correct exceptions for server-side:
from aiohttp.web_exceptions import HTTPClientError
from aiohttp.web_ws import WebSocketError

# The default directory to serve files from
BASE_DIR_DEFAULT = '.'

# The default listening port
LISTENING_PORT_DEFAULT = 8070

# The certificate and key for HTTPS
CERTIFICATE_FILE = "ca_cert.pem"
CERTIFICATE_KEY_FILE = "ca_key.pem"

# Map IP addresses to the actual firmware file they should receive
# in differentiated mode.
# Format: {requested_filename: {ip_address: actual_filename}}
# This allows different IPs to get different files for the same request
IP_TO_FILE_MAP = {
    'stepper.bin': {
        "10.10.3.10": "stand.bin",
        "10.10.3.20": "lift.bin",
        "10.10.3.30": "plinky_plonky.bin",
        "10.10.3.40": "door.bin",
        "10.10.3.41": "door.bin",
        "10.10.3.42": "door.bin",
        "10.10.3.43": "door.bin",
        "10.10.3.44": "door.bin",
        "10.10.3.45": "door.bin"
    },
    # Add more mappings as needed
    # 'motor.bin': {
    #     "10.10.3.10": "rotation_motor.bin",
    #     "10.10.3.20": "lift_motor.bin",
    # }
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class RobustFileSender:
    """Helper class to handle file sending with error recovery"""
    
    def __init__(self, request, response, filepath, file_size):
        self.request = request
        self.response = response
        self.filepath = filepath
        self.file_size = file_size
        self.bytes_sent = 0
        self.start_time = time.time()
        
    async def send_file(self):
        try:
            with open(self.filepath, 'rb') as f:
                # Send first chunk (64KB)
                first_chunk = f.read(65536)
                if first_chunk and not await self.safe_write(first_chunk):
                    return False
                
                # Send remaining chunks
                while True:
                    chunk = f.read(16384)
                    if not chunk:
                        break
                    
                    if not await self.safe_write(chunk):
                        return False
                    
                    # Check transfer speed occasionally
                    if self.bytes_sent % (1024 * 1024) < 16384:  # Every ~1MB
                        elapsed = time.time() - self.start_time
                        if elapsed > 0:
                            speed = self.bytes_sent / elapsed / 1024  # KB/s
                            logger.debug(f"Transfer to {self.request.remote}: "
                                       f"{self.bytes_sent}/{self.file_size} bytes "
                                       f"({speed:.1f} KB/s)")
                
                logger.info(f"Complete transfer to {self.request.remote}: "
                          f"{self.bytes_sent} bytes in {time.time()-self.start_time:.1f}s")
                return True
                
        except FileNotFoundError:
            logger.error(f"File disappeared: {self.filepath}")
            return False
        except PermissionError:
            logger.error(f"Permission denied: {self.filepath}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in file send: {e}")
            return False
    
    async def safe_write(self, chunk):
        """Write chunk with error handling"""
        try:
            await self.response.write(chunk)
            self.bytes_sent += len(chunk)
            return True
        except ConnectionResetError as e:
            logger.info(f"Client {self.request.remote} connection reset: {e}")
            return False
        except ConnectionAbortedError as e:
            logger.info(f"Client {self.request.remote} connection aborted: {e}")
            return False
        except BrokenPipeError as e:
            logger.info(f"Client {self.request.remote} broken pipe: {e}")
            return False
        except asyncio.CancelledError:
            logger.info(f"Transfer cancelled for {self.request.remote}")
            return False
        except Exception as e:
            logger.error(f"Error writing to {self.request.remote}: {e}")
            return False

class HandleFirmware:
    def __init__(self, base_path, differentiated_mode):
        self.base_path = base_path
        self.differentiated_mode = differentiated_mode
    
    def map_filename(self, requested_file, client_ip):
        file = requested_file
        if self.differentiated_mode:
            if client_ip == '::1':
                client_ip = '127.0.0.1'
            # Check if we have a mapping for this filename
            if file in IP_TO_FILE_MAP:
                # Get IP-specific mapping, fallback to requested file if IP not found
                actual_file = IP_TO_FILE_MAP[file].get(client_ip, requested_file)
                if actual_file != file:
                    logger.info(f"IP {client_ip} requested '{file}' -> serving '{actual_file}'")
                file = actual_file
        return file

    def get_available_files(self):
        try:
            files = []
            for f in os.listdir(self.base_path):
                file_path = os.path.join(self.base_path, f)
                if os.path.isfile(file_path) and f.endswith('.bin'):
                    files.append(f)
            return sorted(files)
        except Exception:
            return []

    async def handle(self, request):
        # Get the filename from the URL path
        filename = request.match_info.get('filename', '')
        
        if not filename:
            return web.Response(status=400, text="No filename specified")
        
        # Security check
        if '..' in filename or filename.startswith('/'):
            logger.warning(f"Blocked directory traversal attempt: {filename} from {request.remote}")
            return web.Response(status=403, text="Invalid filename")
        
        #  Set the full file path
        filename_mapped = self.map_filename(filename, request.remote)
        filepath = os.path.join(self.base_path, filename_mapped)

        # Security check
        real_base = os.path.realpath(self.base_path)
        real_file = os.path.realpath(filepath)
        if not real_file.startswith(real_base):
            logger.warning(f"Blocked path escape attempt: {filename_mapped} resolves outside base directory")
            return web.Response(status=403, text="Invalid filename")

        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            logger.info(f"File not found: {filepath} (requested: {filename}) from {request.remote}")
            available_files = self.get_available_files()
            error_msg = f"File {filename} not found"
            if available_files:
                error_msg += f". Available files: {', '.join(available_files)}"
            return web.Response(status=404, text=error_msg)
        
        file_size = os.path.getsize(filepath)
        logger.info(f"Serving {filepath} ({file_size} bytes) to {request.remote} (requested: {filename})")
        
        # Create streaming response
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Length'] = str(file_size)
        response.headers['Cache-Control'] = 'no-cache'
        
        try:
            await response.prepare(request)
        except ConnectionResetError:
            logger.info(f"Client {request.remote} disconnected during prepare")
            return web.Response(status=499)
        except Exception as e:
            logger.error(f"Error preparing response for {request.remote}: {e}")
            return web.Response(status=500)
        
        # Use robust sender
        sender = RobustFileSender(request, response, filepath, file_size)
        success = await sender.send_file()
        
        if not success and sender.bytes_sent == 0:
            # Nothing was sent, return error
            return web.Response(status=500, text="Transfer failed")
        
        # Return the response (even if partial, aiohttp handles it)
        return response

async def main(base_dir, port, differentiated_mode):
    app = web.Application()

    handle_firmware = HandleFirmware(base_dir, differentiated_mode)
    app.router.add_get('/{filename:.*}', handle_firmware.handle)
    
    # Create SSL context
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    if not (os.path.exists(CERTIFICATE_FILE) and os.path.exists(CERTIFICATE_KEY_FILE)):
        logger.error(f"SSL certificates not found. Please ensure {CERTIFICATE_FILE} and {CERTIFICATE_KEY_FILE} exist.")
        return
    ssl_context.load_cert_chain(CERTIFICATE_FILE, CERTIFICATE_KEY_FILE)
    
    # Configure server with timeouts
    runner = web.AppRunner(app, keepalive_timeout=75, shutdown_timeout=60)
    await runner.setup()
    
    site = web.TCPSite(
        runner, 
        '0.0.0.0', 
        port, 
        ssl_context=ssl_context,
        reuse_address=True,
        reuse_port=True,
        backlog=128  # Connection queue size
    )
    await site.start()
    
    logger.info(f"Server running on https://localhost:{port}")
    logger.info(f"Serving files from '{base_dir}'")
    
    # List available .bin files in base directory
    try:
        bin_files = []
        for file in os.listdir(base_dir):
            file_path = os.path.join(base_dir, file)
            if os.path.isfile(file_path) and file.endswith('.bin'):
                bin_files.append((file, os.path.getsize(file_path)))
        
        if bin_files:
            logger.info(f"Available firmware files in '{base_dir}':")
            for file, size in sorted(bin_files):
                logger.info(f"  {file:30} {size:8} bytes")
        else:
            logger.warning(f"No .bin files found in '{base_dir}'")
        
        # Show IP to filename mappings if in differentiated mode
        if differentiated_mode and IP_TO_FILE_MAP:
            logger.info("Mappings:")
            for requested_file, ip_map in IP_TO_FILE_MAP.items():
                logger.info(f"  For requested file '{requested_file}':")
                # Group by actual file for cleaner output
                file_to_ips = {}
                for ip, actual_file in ip_map.items():
                    if actual_file not in file_to_ips:
                        file_to_ips[actual_file] = []
                    file_to_ips[actual_file].append(ip)
                
                for mapped_file, ips in sorted(file_to_ips.items()):
                    logger.info(f"     served file will be '{mapped_file}' for requesting IP address(es) {', '.join(sorted(ips))}")
    
    except FileNotFoundError:
        logger.error(f"Base directory {base_dir} not found!")
    except PermissionError:
        logger.error(f"Permission denied accessing {base_dir}")
    except Exception as e:
        logger.error(f"Error scanning directory: {e}")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await runner.cleanup()

def run(base_dir, port, differentiated_mode):
    if differentiated_mode:
        logger.warning("DIFFERENTIATED MODE: known IP addresses will be served specific binaries.")        
    try:
        asyncio.run(main(base_dir, port, differentiated_mode))
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=("A script to serve files over HTTPS, part of the musical box project."),
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('base_dir', nargs='?', default=BASE_DIR_DEFAULT, help=("directory to serve files from, default "
                                                                              f"'{BASE_DIR_DEFAULT}'."))
    parser.add_argument('-p', type=int, default=LISTENING_PORT_DEFAULT, help=("the listening port, default "
                                                                             f"{LISTENING_PORT_DEFAULT}."))
    parser.add_argument('-d', action='store_true', help=("switch to differentiated mode: in this"
                                                         " mode different files will be served"
                                                         " based on the source IP address of the"
                                                         " requesting client."))

    args = parser.parse_args()

    run(args.base_dir, args.p, args.d)
