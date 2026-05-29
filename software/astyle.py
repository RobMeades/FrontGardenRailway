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

'''Run AStyle on all code.'''

import subprocess
import sys

with subprocess.Popen(["astyle", "--options=astyle.cfg", "--suffix=none", "--verbose",
                      "--errors-to-stdout", "--recursive", "*.c,*.h,*.cpp,*.hpp"],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      universal_newlines=True) as astyle:
    fail = False
    output, _ = astyle.communicate()
    for line in output.splitlines():
        if line.startswith("Formatted"):
            fail = True
        print (line)
    if astyle.returncode != 0:
        fail = True
    sys.exit(1 if fail else 0)