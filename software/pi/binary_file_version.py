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

"""
Extract description from ESP32 binary file at fixed offset and length.
"""

import argparse
import sys
import re
import glob

# Defaults for ESP32
ESP32_DESCRIPTION_OFFSET = 30
ESP32_DESCRIPTION_LENGTH = 256

def extract_version_aggressive(file_path, offset, length):
    """
    Extremely aggressive filtering - only keep visible ASCII characters.
    """
    with open(file_path, 'rb') as f:
        f.seek(offset)
        data = f.read(length)
        
        text = data.decode('ascii', errors='replace')
        
        visible_chars = []
        for c in text:
            if 32 <= ord(c) <= 126:
                visible_chars.append(c)
        
        result = ''.join(visible_chars)
        result = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', result)
        
        return result.strip()


def main():
    parser = argparse.ArgumentParser(
        description='Extract version from binary files (supports wildcards)'
    )
    parser.add_argument('file_pattern', nargs='+',
                       help='File pattern(s) - quote wildcards to prevent shell expansion')
    parser.add_argument('-o', '--offset', type=int,
                        default=ESP32_DESCRIPTION_OFFSET,
                        help='Byte offset where version starts,'
                            f' default {ESP32_DESCRIPTION_OFFSET} for'
                             ' sizeof(esp_image_header_t) +'
                             ' sizeof(esp_image_segment_header_t)')
    parser.add_argument('-l', '--length', type=int,
                        default=ESP32_DESCRIPTION_LENGTH,
                        help='Length of version string in bytes,'
                            f' default {ESP32_DESCRIPTION_LENGTH}'
                             ' for sizeof(esp_app_desc_t)')
    parser.add_argument('-q', '--quiet', action='store_true',
                       help='Suppress error messages')
    parser.add_argument('--format', choices=['filename-version', 'version-only', 'csv'],
                       default='filename-version',
                       help='Output format (default: filename-version)')
    parser.add_argument('--delimiter', default=': ',
                       help='Delimiter for filename-version format (default: ": ")')
    
    args = parser.parse_args()
    
    # Collect all files from all patterns
    all_files = []
    for pattern in args.file_pattern:
        # Expand wildcards in each pattern
        expanded = glob.glob(pattern)
        if expanded:
            all_files.extend(expanded)
        elif not args.quiet:
            # If no files match and it's not a wildcard pattern, treat as literal filename
            if not any(c in pattern for c in '*?[]'):
                all_files.append(pattern)
            else:
                print(f"Warning: No files match pattern: {pattern}", file=sys.stderr)
    
    # Remove duplicates while preserving order
    seen = set()
    files = []
    for f in all_files:
        if f not in seen:
            seen.add(f)
            files.append(f)
    
    if not files:
        if not args.quiet:
            print("No files to process", file=sys.stderr)
        sys.exit(1)
    
    # Process each file
    results = []
    for file_path in sorted(files):
        try:
            version = extract_version_aggressive(file_path, args.offset, args.length)
            if version:
                results.append((file_path, version))
            else:
                results.append((file_path, None))
        except FileNotFoundError:
            if not args.quiet:
                print(f"{file_path}: File not found", file=sys.stderr)
            results.append((file_path, None))
        except Exception as e:
            if not args.quiet:
                print(f"{file_path}: Error - {e}", file=sys.stderr)
            results.append((file_path, None))
    
    # Output based on format
    if args.format == 'filename-version':
        for file_path, version in results:
            if version:
                print(f"{file_path}{args.delimiter}{version}")
            elif not args.quiet and version is None:
                print(f"{file_path}{args.delimiter}[NO VERSION]", file=sys.stderr)
    
    elif args.format == 'version-only':
        for _, version in results:
            if version:
                print(version)
    
    elif args.format == 'csv':
        print("filename,version")
        for file_path, version in results:
            print(f"{file_path},{version if version else ''}")


if __name__ == '__main__':
    main()
