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

# Refactored by Google Gemini.

import os
import sys
import time
import json
import shutil
import subprocess
import tempfile
import argparse
import re
import hashlib

def parse_args(script_dir):
    default_config = os.path.join(script_dir, "nodes_esp32_deploy.json")

    parser = argparse.ArgumentParser(
        description="Front Garden Railway Node Deployment Automation Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    parser.add_argument("--node-cfg", default=default_config, help="Path to the node configuration JSON file")
    parser.add_argument("--staging", default="staging", help="Path to the local staging and archive folder")
    parser.add_argument("--variant", help="Force compilation of a single specific hardware variant only")
    parser.add_argument("--app", help="Force compilation of a single specific application only")
    parser.add_argument("--ip", help="Target a specific node by its IP address to automatically determine app and variant")
    parser.add_argument("--incremental", action="store_true", help="Skip fullclean pass for faster debugging builds")
    parser.add_argument("--production", action="store_true",
                        help="Build a clean production release (disables ephemeral .dev suffix and prompts for version checks).")
    parser.add_argument('--remote-target', metavar='USER@IP/DIR',
                        help="Remote target destination relative to home directory on remote system (e.g., pi@10.10.3.1/fw)")
    parser.add_argument('--reset-server', action='store_true',
                        help="Force the remote HTTPS server to restart, clearing all RAM telemetry and beta overrides.")

    return parser.parse_args()

def run_command(cmd, cwd=None):
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd)
    if result.returncode != 0:
        print(f"\n[ERROR] Command failed with exit code {result.returncode}: {cmd}")
        sys.exit(result.returncode)

def get_git_hash():
    try:
        return subprocess.check_output("git rev-parse --short HEAD", shell=True).decode("utf-8").strip()
    except Exception:
        return "unknown"

def read_version_file(filepath):
    if os.path.exists(filepath):
        with open(filepath, "r") as f:
            return f.read().strip()
    return "1.0"

def parse_remote_target(target_str):
    match = re.match(r"^([^@]+)@([^/:]+)[:/](.+)$", target_str)
    if not match:
        print(f"Error: Invalid remote target format '{target_str}'.")
        print("Expected format: user@ip:/directory")
        sys.exit(1)
    return {
        "user": match.group(1),
        "host": match.group(2),
        "directory": match.group(3)
    }

def prune_archive(staging_dir, max_to_keep=10):
    """
    Scans the local archive directories and keeps only the 'max_to_keep' most recent
    build tracking subfolders per app/variant, deleting the oldest ones.
    """
    archive_root = os.path.join(staging_dir, "archive")
    if not os.path.exists(archive_root):
        return

    print(f"\n[INFO] Sweeping staging archive (retaining top {max_to_keep} entries per variant)...")

    # Drill down: staging/archive -> application -> variant -> [timestamped version folders]
    for app in os.listdir(archive_root):
        app_path = os.path.join(archive_root, app)
        if not os.path.isdir(app_path):
            continue

        for variant in os.listdir(app_path):
            variant_path = os.path.join(app_path, variant)
            if not os.path.isdir(variant_path):
                continue

            # Gather all subdirectories inside this variant track
            subdirs = [
                os.path.join(variant_path, d)
                for d in os.listdir(variant_path)
                if os.path.isdir(os.path.join(variant_path, d))
            ]

            # Sort them by creation/modification time (oldest first)
            subdirs.sort(key=os.path.getmtime)

            # If we exceed our retention budget, wipe the oldest out entirely
            if len(subdirs) > max_to_keep:
                to_delete = subdirs[:-max_to_keep]
                for old_dir in to_delete:
                    print(f"  -> Pruning old archive footprint: {os.path.basename(old_dir)}")
                    shutil.rmtree(old_dir)

def main():
    script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(script_path)
    args = parse_args(script_dir)

    # Ensure production versions are intentionally bumped
    if args.production:
        print("\n⚠️  PRODUCTION RELEASE BUILD TARGETED ⚠️")
        response = input("Has the system version and any relevant application version numbers been incremented? (y/N): ")
        if response.strip().lower() not in ['y', 'yes']:
            print("[ABORT] Canceling build pass. Please verify version numbers before cutting a production release.")
            sys.exit(0)

    if args.remote_target:
        remote = parse_remote_target(args.remote_target)
    else:
        remote = None

    if not os.path.exists(args.node_cfg):
        print(f"[ERROR] Could not find node configuration file at: {args.node_cfg}")
        sys.exit(1)

    print(f"Using node configuration: {os.path.abspath(args.node_cfg)}")
    with open(args.node_cfg, "r") as f:
        node_cfg = json.load(f)

    # =========================================================================
    # IP RESOLUTION LOGIC
    # =========================================================================
    target_app = args.app
    target_variant = args.variant

    if args.ip:
        print(f"Resolving application and variant configuration for IP: {args.ip}")
        inventory = node_cfg.get("inventory", {})

        if args.ip in inventory:
            target_app = inventory[args.ip].get("app")
            target_variant = inventory[args.ip].get("variant")
            print(f"[INFO] IP Matched -> Application: '{target_app}', Variant: '{target_variant}'")
        else:
            print(f"[ERROR] IP address '{args.ip}' could not be found in the configuration inventory.")
            print(f"Available IPs: {', '.join(inventory.keys())}")
            sys.exit(1)

    workspace_root = script_dir
    git_hash = get_git_hash()

    system_version_path = os.path.join(workspace_root, node_cfg.get("system_version_file", "sdkconfig/system_version.txt"))
    system_version = read_version_file(system_version_path)

    apps_to_build = [target_app] if target_app else node_cfg["applications"].keys()

    for app in apps_to_build:
        app_path = os.path.join(workspace_root, "applications", app)
        if not os.path.exists(app_path):
            print(f"[WARN] Application directory not found, skipping: {app_path}")
            continue

        app_version = read_version_file(os.path.join(app_path, "version.txt"))
        app_meta = node_cfg["applications"][app]
        supported_variants = app_meta.get("supported_variants", node_cfg["global_variants"])

        for variant in supported_variants:
            if target_variant and variant != target_variant:
                continue

            print(f"\n=======================================================")
            print(f" Component Pipeline: {app} ({variant})")
            print(f" Track Channel: {'PRODUCTION' if args.production else 'BETA (Development)'}")
            print(f" Mode: {'Incremental' if args.incremental else 'Full Clean'}")
            print(f"=======================================================")

            # Determine explicit track destination targets
            track_dir_name = "production" if args.production else "beta"
            target_output_dir = os.path.join(workspace_root, args.staging, track_dir_name)

            # Define the command-line arguments string base
            variant_meta = node_cfg["variants_matrix"][variant]
            traits = variant_meta.get("traits", [])
            part_file = variant_meta.get("partition_table")

            if not part_file:
                print(f"[ERROR] No 'partition_table' defined for variant '{variant}' in configuration file.")
                sys.exit(1)

            sdkconfig_layers = [os.path.join(workspace_root, "sdkconfig_fragments", "defaults")]
            for t in traits:
                sdkconfig_layers.append(os.path.join(workspace_root, t))

            abs_partition_csv = os.path.abspath(os.path.join(workspace_root, "sdkconfig_fragments", part_file))

            with tempfile.NamedTemporaryFile(mode='w', suffix='.defaults', delete=False) as temp_cfg:
                temp_cfg.write(f'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="{abs_partition_csv}"\n')
                temp_cfg.write(f'CONFIG_PARTITION_TABLE_FILENAME="{abs_partition_csv}"\n')
                temp_cfg_path = temp_cfg.name

            try:
                sdkconfig_layers.append(temp_cfg_path)
                sdkconfig_defaults_arg = ";".join(sdkconfig_layers)

                # =========================================================================
                # VERSION INJECTION CONTROL LAYOUT
                # =========================================================================
                build_cmd = f'idf.py -DSDKCONFIG_DEFAULTS="{sdkconfig_defaults_arg}"'

                if args.production:
                    # Production Release: Pass the custom clean variable to override CMake defaults
                    version_tag = f"v{app_version}.{system_version}_{git_hash}"
                    build_cmd += f' -DPROJECT_VER="{version_tag}" build'
                else:
                    # Beta/Development: Do NOT pass a variable. Let CMake automatically generate the .dev suffix.
                    build_cmd += ' build'
                    # Mirror the naming format locally so our staging script can name the archive path safely
                    time_struct = time.localtime()
                    min_sec_nonce = f"{time_struct.tm_min:02d}{time_struct.tm_sec:02d}"
                    version_tag = f"v{app_version}.{system_version}.d{min_sec_nonce}_{git_hash}"

                # Create the unique subfolder
                os.makedirs(target_output_dir, exist_ok=True)

                if not args.incremental:
                    run_command("idf.py fullclean", cwd=app_path)

                local_config_file = os.path.join(app_path, "sdkconfig")
                if os.path.exists(local_config_file):
                    print(f"Clearing old configuration file for clean variant generation: {local_config_file}")
                    os.remove(local_config_file)

                cmake_cache_file = os.path.join(app_path, "build", "CMakeCache.txt")
                if os.path.exists(cmake_cache_file):
                    print(f"Clearing old CMake cache to force configuration fresh evaluation...")
                    os.remove(cmake_cache_file)

                # Execute compilation build pass
                run_command(build_cmd, cwd=app_path)

                target_bin = os.path.join(app_path, "build", f"{app}.bin")
                target_elf = os.path.join(app_path, "build", f"{app}.elf")

                if os.path.exists(target_bin):
                    fw_build_hash = "unknown_sha"
                    try:
                        # Read the ESP application descriptor header from the binary file
                        # The app_desc structure starts after the image header (24 bytes) + segment header (8 bytes)
                        with open(target_bin, "rb") as f:
                            f.seek(24 + 8)
                            # Read the esp_app_desc_t structural fields
                            # Magic word (4 bytes), Secure version (4 bytes), App version (32 bytes), Project name (32 bytes)
                            # Compile time (16 bytes), Compile date (16 bytes), ESP-IDF version (32 bytes), ELF SHA256 (32 bytes)
                            app_desc_data = f.read(256)

                            # The SHA256 array sits precisely at offset 144 inside the description struct
                            sha256_bytes = app_desc_data[144:176]

                            # Convert the raw bytes into a clean lowercase hex string
                            fw_build_hash = sha256_bytes.hex()
                    except Exception as e:
                        print(f"[WARN] Failed to read ELF SHA256 directly from binary header structure: {e}")
                        # Fallback safe measurement if header structure parsing encounters read boundary bugs
                        hasher = hashlib.sha256()
                        with open(target_bin, "rb") as f:
                            for chunk in iter(lambda: f.read(4096), b""):
                                hasher.update(chunk)
                        fw_build_hash = hasher.hexdigest()

                    # Create the unique subfolder named strictly by its true ELF runtime hash string
                    archive_dir = os.path.join(workspace_root, args.staging, "archive", app, variant, fw_build_hash)
                    os.makedirs(archive_dir, exist_ok=True)
                    os.makedirs(target_output_dir, exist_ok=True)

                    # Save version-labeled .elf file directly into safety archive folder (if it exists)
                    if os.path.exists(target_elf):
                        archive_elf_dest = os.path.join(archive_dir, f"{app}.elf")
                        shutil.copy2(target_elf, archive_elf_dest)
                        print(f"[SUCCESS] Archived debug symbols blueprint: {archive_elf_dest}")

                    # Construct structural naming pattern: e.g., test_esp32s3-rgb-4mbyte.bin
                    track_filename = f"{app}_{variant}.bin"
                    track_dest_path = os.path.join(target_output_dir, track_filename)

                    shutil.copy2(target_bin, track_dest_path)
                    print(f"[SUCCESS] Promoted release payload to active track channel: {track_dest_path}")

            finally:
                if os.path.exists(temp_cfg_path):
                    os.remove(temp_cfg_path)

    # Clean up the local archive folder before syncing everything over to the Pi
    prune_archive(os.path.join(workspace_root, args.staging), max_to_keep=50)

    # =========================================================================
    # OPTIONAL REMOTE DEPLOYMENT STEP
    # =========================================================================
    if remote and remote['host'] and remote['directory']:
        print("\n=======================================================")
        print(" Synchronizing Local Staging to Remote Destination Machine")
        print("=======================================================")

        local_staging_path = os.path.join(workspace_root, args.staging) + "/"
        user_prefix = f"{remote['user']}@" if remote['user'] else ""

        # Using a single ':' lets rsync differentiate:
        # host:fw       -> relative to home (/home/pi/fw)
        # host:/mnt/ssd -> absolute path (/mnt/ssd)
        remote_destination = f"{user_prefix}{remote['host']}:{remote['directory']}"

        # Keep server credentials untouched
        exclude_certs = "--exclude='/*.pem'"

        deploy_cmd = (
            f'rsync -rltvz --no-perms --no-owner --no-group --inplace --delete {exclude_certs} '
            f'"{local_staging_path}" "{remote_destination}"'
        )

        run_command(deploy_cmd)
        print("[SUCCESS] Remote synchronization complete!")

        # Remote daemon service restart handling
        if args.reset_server:
            print("\nTriggering remote HTTPS OTA server daemon reload...")
            service_name = "https_server.service"
            ssh_cmd = f'ssh {user_prefix}{remote["host"]} "sudo systemctl restart {service_name}"'

            try:
                run_command(ssh_cmd)
                print(f"[SUCCESS] Remote service '{service_name}' restarted smoothly! All volatile tracks neutralized.")
            except Exception as e:
                print(f"[ERROR] Failed to restart remote service over SSH link: {e}")
                sys.exit(1)
        else:
            print("\n[INFO] Skipped server restart. Active dashboard tracks and telemetry preserved in Pi RAM.")

    print("\n=======================================================")
    print(" Node Deployment Compile Engine Finished Successfully! ")
    print("=======================================================")

if __name__ == "__main__":
    main()