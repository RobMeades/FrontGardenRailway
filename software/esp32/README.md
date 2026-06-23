# Introduction
This directory contains a script, [nodes_esp32_deploy.py](nodes_esp32_deploy.py) and an associated JSON configuration file [nodes_esp32_deploy.json](nodes_esp32_deploy.json).  Together these manage the build and deployment of all ESP32 "nodes", meaning an application, along with all the necessary custom components to be a node on the front garden railway.

Note: the Pi-side HTTPS server script ([https_server.py](../pi/https_server.py)) also reads [nodes_esp32_deploy.json](nodes_esp32_deploy.json) so that it knows exactly which binary files to server to a node, effectively working with [nodes_esp32_deploy.py](nodes_esp32_deploy.py) (which builds the code and pushes it to the Raspberry Pi to be served).

By default, [nodes_esp32_deploy.py](nodes_esp32_deploy.py) creates development builds that are stored in the `beta` directory off wherever you have told it to stage files (e.g. `~/fw`).  These builds are only served to nodes that have been put into development mode (via the [https_server.py](../pi/https_server.py) dashboard).  If the flag `--production` is specified then the builds are considered production builds, put in the `production` sub-directory off wherever you have told [nodes_esp32_deploy.py](nodes_esp32_deploy.py) to stage files and served to all nodes.  All builds are also copied to an `archive` directory in a tree intended to make them easily searchable in order to decode back-traces and core-dumps.

The lot can then be `rsync`'ed to the Raspberry Pi (e.g. to `/mnt/ssd/fw`) by [nodes_esp32_deploy.py](nodes_esp32_deploy.py).

A good way to work during development is to use the [https_server.py](../pi/https_server.py) dashboard to switch a node into development mode and then create development builds just for it that IP address using the `--ip` flag to [nodes_esp32_deploy.py](nodes_esp32_deploy.py).

Some examples:

- This will build development versions of all nodes, put them in `~/fw`, then deploy them to `/mnt/fgr_data/fw` on the server machine (i.e. in the `beta` sub-directory):
 
  `python nodes_esp32_deploy.py --staging ~/fw --remote-target <your username>@<server IP>:/mnt/fgr_data/fw`

- This will do the same for production (i.e. in the `production` sub-directory); you will be prompted to make sure you have updated an relevant version numbers:
 
  `python nodes_esp32_deploy.py --staging ~/fw --production --remote-target <your username>@<server IP>:/mnt/fgr_data/fw`

- This will build and deploy to `/mnt/fgr_data/fw` on the server machine only the development version image necessary for the node with IP address 10.10.3.2:

  `python nodes_esp32_deploy.py --staging ~/fw --ip 10.10.3.2 --remote-target <your username>@<server IP>:/mnt/fgr_data/fw`

