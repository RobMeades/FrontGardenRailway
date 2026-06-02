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

'''HTTPS server on a Raspberry Pi Wi-Fi access point to dynamically route, track, and stream OTA update binaries to garden nodes.'''

import asyncio
import ssl
import logging
import os
import time
import argparse
import json
import random
from aiohttp import web

# Default configuration profiles
BASE_DIR_DEFAULT = '.'
LISTENING_PORT_DEFAULT = 8070
CERTIFICATE_FILE = "ca_cert.pem"
CERTIFICATE_KEY_FILE = "ca_key.pem"

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)


class RobustFileSender:
    """Handles low-level block-by-block file streaming with drop-out error catchments."""
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
                # Initial 64KB burst header buffer allocation pass
                first_chunk = f.read(65536)
                if first_chunk and not await self.safe_write(first_chunk):
                    return False

                # Stream remaining elements via standard 16KB data blocks
                while True:
                    chunk = f.read(16384)
                    if not chunk:
                        break

                    if not await self.safe_write(chunk):
                        return False

                    # Monitor connection tracking states across 1MB transfer blocks
                    if self.bytes_sent % (1024 * 1024) < 16384:
                        elapsed = time.time() - self.start_time
                        if elapsed > 0:
                            speed = self.bytes_sent / elapsed / 1024
                            logger.debug(f"Transfer to {self.request.remote}: "
                                         f"{self.bytes_sent}/{self.file_size} bytes ({speed:.1f} KB/s)")

                logger.info(f"Complete transfer to {self.request.remote}: {self.bytes_sent} bytes "
                            f"delivered in {time.time()-self.start_time:.1f}s")
                return True

        except FileNotFoundError:
            logger.error(f"Target binary asset vanished during transmission: {self.filepath}")
            return False
        except Exception as e:
            logger.error(f"Unexpected fault during block transmission pipeline: {e}")
            return False

    async def safe_write(self, chunk):
        try:
            await self.response.write(chunk)
            self.bytes_sent += len(chunk)
            return True
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
            logger.info(f"Client {self.request.remote} dropped connection layout: {e}")
            return False
        except asyncio.CancelledError:
            logger.info(f"Transmission routine cancelled for client target: {self.request.remote}")
            return False
        except Exception as e:
            logger.error(f"Socket transmission fault encountered on {self.request.remote}: {e}")
            return False


class HandleFirmware:
    def __init__(self, base_path, node_cfg_data=None):
        self.base_path = base_path
        self.node_cfg = node_cfg_data if node_cfg_data else {"inventory": {}}

        # Volatile runtime state tracking (Kept strictly in RAM)
        self.telemetry = {}  # Format: { "10.10.2.XX": {"version": "v1.0", "last_seen": timestamp} }

    def resolve_node_binary(self, client_ip, requested_filename):
        """Resolves identity paths dynamically based on a node's inventory mode classification."""
        if client_ip in ('::1', 'localhost'):
            client_ip = '127.0.0.1'

        inventory = self.node_cfg.get("inventory", {})
        if client_ip in inventory:
            node_mapping = inventory[client_ip]
            app = node_mapping.get('app')
            variant = node_mapping.get('variant')
            mode = node_mapping.get('mode', 'stable').lower()

            target_filename = f"{app}_{variant}.bin"

            # Route tracks explicitly based on active RAM configurations
            if mode == "beta":
                resolved_bin = os.path.join("beta", target_filename)
            else:
                resolved_bin = os.path.join("production", target_filename)  # Matches new script folder layout!

            if requested_filename != resolved_bin and requested_filename != "generic_update_query":
                logger.info(f"Inventory Match [IP {client_ip}] | Mode: {mode.upper()} -> "
                            f"Routing request '{requested_filename}' to asset '{resolved_bin}'")
            return resolved_bin

        logger.warning(f"IP {client_ip} missing from inventory. Serving raw requested asset from root: '{requested_filename}'")
        return requested_filename

    async def handle_update(self, request):
        """Dedicated route checking firmware state. Parses '?version=vX.Y' for runtime telemetry."""
        client_ip = request.remote
        if client_ip in ('::1', 'localhost'):
            client_ip = '127.0.0.1'

        # Harvest query-string version telemetry pushed from the ESP32
        reported_version = request.query.get('version', 'Unknown')

        # Log the status update in the volatile RAM telemetry dictionary
        self.telemetry[client_ip] = {
            "version": reported_version,
            "last_seen": time.time()
        }

        filename_resolved = self.resolve_node_binary(client_ip, "generic_update_query")

        inventory = self.node_cfg.get("inventory", {})
        if client_ip not in inventory:
            logger.error(f"Generic update check failed for unmapped hardware client node: {client_ip}")
            return web.Response(status=404, text="Error: Node not found in network inventory mapping matrix.")

        return await self.process_file_delivery(request, filename_resolved)

    async def handle_telemetry_api(self, request):
        """Returns the volatile RAM telemetry data as a clean JSON payload."""
        return web.json_response(self.telemetry)

    async def handle_direct_file(self, request):
        """Standard routing endpoint allowing direct file matching or local fallback calls."""
        filename = request.match_info.get('filename', '')
        if not filename:
            return web.Response(status=400, text="Bad Request: Missing targeting asset filename target.")

        if '..' in filename or filename.startswith('/'):
            logger.warning(f"Blocked directory traversal intrusion trace attempt: {filename} from {request.remote}")
            return web.Response(status=403, text="Access Denied.")

        filename_resolved = self.resolve_node_binary(request.remote, filename)
        return await self.process_file_delivery(request, filename_resolved)

    async def process_file_delivery(self, request, filename):
        client_ip = request.remote

        await self.enforce_rate_limits(request, client_ip)

        filepath = os.path.normpath(os.path.join(self.base_path, filename))
        real_base = os.path.realpath(self.base_path)
        real_file = os.path.realpath(filepath)

        if not real_file.startswith(real_base):
            logger.warning(f"Blocked asset escape tracing path verification bounds: {filename} from {client_ip}")
            return web.Response(status=403, text="Access Denied.")

        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            logger.info(f"Target file path missing or offline: {filepath} requested by target {client_ip}")
            return web.Response(status=404, text=f"Error: Firmware payload footprint target '{filename}' is offline.")

        file_size = os.path.getsize(filepath)
        logger.info(f"Streaming target binary tracking block: {filename} ({file_size} bytes) -> To client destination: {client_ip}")

        response = web.StreamResponse()
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Length'] = str(file_size)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'

        try:
            await response.prepare(request)
        except Exception as e:
            logger.info(f"Socket interface tracking error context during setup phase with {client_ip}: {e}")
            raise

        sender = RobustFileSender(request, response, filepath, file_size)
        success = await sender.send_file()

        if not success and sender.bytes_sent == 0:
            return web.Response(status=500, text="Internal transport streaming fault.")

        return response

    async def handle_dashboard(self, request):
        """Serves the control dashboard UI shell with client-side live rendering."""
        inventory = self.node_cfg.get("inventory", {})

        # Inject the inventory as a JavaScript object so the browser knows our master node list
        inventory_json = json.dumps(inventory)

        table_rows = ""
        for ip, meta in sorted(inventory.items()):
            table_rows += f"""
            <tr id="row-{ip.replace('.', '-')}">
                <td><strong>{ip}</strong></td>
                <td>{meta.get('app')} ({meta.get('variant')})</td>
                <td><span class="track-label {meta.get('mode', 'stable').lower()}">{meta.get('mode', 'stable').upper()}</span></td>
                <td class="col-version"><code>Syncing...</code></td>
                <td class="col-status"><span class="badge gray">Connecting...</span></td>
                <td class="col-timer">N/A</td>
                <td>
                    <form action="/toggle-mode" method="post" style="margin:0;">
                        <input type="hidden" name="ip" value="{ip}">
                        <input type="hidden" name="mode" value="{'beta' if meta.get('mode', 'stable').lower() == 'stable' else 'stable'}">
                        <button type="submit" class="btn {meta.get('mode', 'stable').lower()}">
                            {"Switch to Dev Track" if meta.get('mode', 'stable').lower() == 'stable' else 'Switch to Stable Release'}
                        </button>
                    </form>
                </td>
            </tr>
            """

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>FGR OTA Dashboard</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f4f7f6; color: #333; }}
                h1 {{ color: #1a3a2a; margin-bottom: 5px; }}
                p.sub {{ color: #666; margin-top: 0; margin-bottom: 30px; }}
                table {{ width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border-radius: 8px; overflow: hidden; }}
                th, td {{ padding: 15px; text-align: left; border-bottom: 1px solid #eee; }}
                th {{ background: #1a3a2a; color: white; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }}
                tr:hover {{ background-color: #f9fbf9; }}
                .badge {{ padding: 4px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; display: inline-block; }}
                .badge.green {{ background: #e2f7ed; color: #157347; }}
                .badge.orange {{ background: #fff3cd; color: #664d03; }}
                .badge.blue {{ background: #cfe2ff; color: #084298; }}
                .badge.gray {{ background: #e2e3e5; color: #41464b; }}
                .track-label {{ padding: 2px 6px; border-radius: 4px; font-size: 11px; font-weight: bold; color: white; }}
                .track-label.stable {{ background: #2b5c43; }}
                .track-label.beta {{ background: #7b2cbf; }}
                .btn {{ padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: bold; color: white; transition: background 0.2s; }}
                .btn.beta {{ background: #2b5c43; }} /* Button styling fixes to match current mode target toggle action */
                .btn.beta:hover {{ background: #1f4431; }}
                .btn.stable {{ background: #7b2cbf; }}
                .btn.stable:hover {{ background: #62219b; }}
                .alert-info {{ background: #e2f0d9; border: 1px solid #bcdca7; color: #385723; padding: 12px; border-radius: 4px; margin-bottom: 20px; font-size: 13px; }}
            </style>
        </head>
        <body>
            <h1>FGT OTA Dashboard</h1>
            <p class="sub">Deployment tracking dashboard.</p>
            <table>
                <thead>
                    <tr>
                        <th>Node Network IP</th>
                        <th>Target Identity Allocation</th>
                        <th>Target Track</th>
                        <th>Reporting Boot Version</th>
                        <th>Deployment Status Flags</th>
                        <th>Last Vital Signal</th>
                        <th>Operational Dispatch Action</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>

            <script>
                const masterInventory = {inventory_json};

                async function syncTelemetry() {{
                    try {{
                        const response = await fetch('/api/telemetry');
                        const telemetry = await response.json();
                        const currentTime = Math.floor(Date.now() / 1000);

                        for (const [ip, meta] of Object.entries(masterInventory)) {{
                            const rowId = "row-" + ip.replace(/\./g, '-');
                            const row = document.getElementById(rowId);
                            if (!row) continue;

                            const nodeData = telemetry[ip] || {{ "version": "Never Checked In", "last_seen": null }};
                            const versionCell = row.querySelector('.col-version code');
                            const statusCell = row.querySelector('.col-status');
                            const timerCell = row.querySelector('.col-timer');

                            // 1. Update Version Code Text
                            versionCell.textContent = nodeData.version;

                            // 2. Update Live Elapsed Timer
                            if (nodeData.last_seen) {{
                                const elapsed = currentTime - Math.floor(nodeData.last_seen);
                                timerCell.textContent = elapsed < 60 ? `${{elapsed}}s ago` : `${{Math.floor(elapsed / 60)}}m ago`;
                            }} else {{
                                timerCell.textContent = "N/A";
                            }}

                            // 3. Re-calculate status color badges fluidly
                            const currentMode = (meta.mode || 'stable').toLowerCase();
                            if (nodeData.version.includes("Never")) {{
                                statusCell.innerHTML = '<span class="badge gray">Offline</span>';
                            }} else if (nodeData.version.includes("-d") && currentMode === "beta") {{
                                statusCell.innerHTML = '<span class="badge green">Dev Tracking Live</span>';
                            }} else if (nodeData.version.includes("-d") && currentMode === "stable") {{
                                statusCell.innerHTML = '<span class="badge orange">Dev Build (Pending Stable)</span>';
                            }} else if (currentMode === "beta") {{
                                statusCell.innerHTML = '<span class="badge blue">Update Pending</span>';
                            }} else {{
                                statusCell.innerHTML = '<span class="badge green">Stable Release Running</span>';
                            }}
                        }}
                    }} catch (err) {{
                        console.error("Telemetry fetch fault:", err);
                    }}
                }}

                // Run sync immediately on window load, then refresh fluidly every 1 second
                syncTelemetry();
                setInterval(syncTelemetry, 1000);
            </script>
        </body>
        </html>
        """
        return web.Response(text=html_content, content_type='text/html')

    async def handle_toggle_mode(self, request):
        """Processes form postings from the dashboard to modify node tracks strictly in memory."""
        data = await request.post()
        target_ip = data.get('ip')
        target_mode = data.get('mode')

        inventory = self.node_cfg.get("inventory", {})
        if target_ip in inventory:
            inventory[target_ip]['mode'] = target_mode
            logger.info(f"[RAM OVERRIDE CHANGE] Shifted target node {target_ip} dynamically into track allocation: {target_mode.upper()}")

        # Bounce the web browser user context straight back to the refreshed control interface view
        return web.HTTPFound(location='/dashboard')

    async def enforce_rate_limits(self, request, client_ip):
        limiter = request.app.rate_limiter
        wait_time = 0.0

        async with limiter['lock']:
            current_time = time.time()

            time_since_last_global = current_time - limiter['last_global_accept']
            if time_since_last_global < limiter['min_global_interval'] and limiter['last_global_accept'] > 0:
                wait_time = max(wait_time, limiter['min_global_interval'] - time_since_last_global)

            last_time = limiter['last_accept_per_ip'].get(client_ip, 0)
            if last_time > 0 and current_time - last_time < limiter['min_per_ip_interval']:
                wait_time = max(wait_time, limiter['min_per_ip_interval'] - (current_time - last_time))

            if client_ip not in limiter['last_accept_per_ip']:
                wait_time = max(wait_time, random.uniform(0.0, 0.4))

            limiter['last_global_accept'] = current_time + wait_time
            limiter['last_accept_per_ip'][client_ip] = current_time + wait_time

        if wait_time > 0:
            logger.info(f"Throttling connection trace for client node {client_ip}: enforcing {wait_time:.2f}s delay window...")
            await asyncio.sleep(wait_time)


def load_master_node_cfg(config_path):
    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
        if "inventory" not in data:
            logger.warning(f"Configuration loaded from '{config_path}' does not contain an 'inventory' dictionary mapping profile block.")
        else:
            # Set default execution tracks inside inventory entries upon starting the service
            for ip, meta in data["inventory"].items():
                if "mode" not in meta:
                    meta["mode"] = "stable"
            logger.info(f"[SUCCESS] Parsed master node map matrix index network footprint from: '{config_path}'")
        return data
    except Exception as e:
        logger.error(f"Could not load master inventory deployment target mapping configuration: {e}")
        return {"inventory": {}}


async def main(base_dir, port, node_cfg_path=None):
    app = web.Application()

    app.rate_limiter = {
        'last_accept_per_ip': {},
        'last_global_accept': 0.0,
        'min_global_interval': 0.25,
        'min_per_ip_interval': 2.0,
        'lock': asyncio.Lock()
    }

    node_cfg_data = load_master_node_cfg(node_cfg_path) if node_cfg_path else {"inventory": {}}
    handler = HandleFirmware(base_dir, node_cfg_data)

    # Core URL Routing Topology Configurations
    app.router.add_get('/dashboard', handler.handle_dashboard)
    app.router.add_get('/toggle-mode', handler.handle_dashboard)  # Fallback bounce route
    app.router.add_post('/toggle-mode', handler.handle_toggle_mode)
    app.router.add_get('/update', handler.handle_update)
    app.router.add_get('/api/telemetry', handler.handle_telemetry_api)
    app.router.add_get('/{filename:.*}', handler.handle_direct_file)

    cert_path = os.path.normpath(os.path.join(base_dir, CERTIFICATE_FILE))
    key_path = os.path.normpath(os.path.join(base_dir, CERTIFICATE_KEY_FILE))

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        logger.critical("SSL certificates missing relative to base_dir!")
        logger.critical(f" -> Looking for cert at: {os.path.abspath(cert_path)}")
        logger.critical(f" -> Looking for key at:  {os.path.abspath(key_path)}")
        return

    ssl_context.load_cert_chain(cert_path, key_path)

    runner = web.AppRunner(app, keepalive_timeout=75, shutdown_timeout=30)
    await runner.setup()

    site = web.TCPSite(runner, '0.0.0.0', port, ssl_context=ssl_context, reuse_address=True, reuse_port=True)
    await site.start()

    logger.info(f"Server engine listening online at: https://0.0.0.0:{port}")
    logger.info(f"Open dashboard matrix visualization interface tool at: https://<your_pi_ip>:{port}/dashboard")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Initiating server shutdown sequence protocols...")
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="OTA Firmware Server for Front Garden Railway",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('base_dir', nargs='?', default=BASE_DIR_DEFAULT, help="Directory for binary payloads and server certificates")
    parser.add_argument('-p', '--port', type=int, default=LISTENING_PORT_DEFAULT, help="Network listener execution port binding")
    parser.add_argument('--node-cfg', metavar='JSON_FILE', help="Path to common configuration file (enables Differentiated Mode)")

    args = parser.parse_args()

    try:
        asyncio.run(main(args.base_dir, args.port, args.node_cfg))
    except KeyboardInterrupt:
        print("\nProcess terminated clean by system handler sequence.")