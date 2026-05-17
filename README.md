# Introduction
This repo contains various files used in creating my front garden railway on a postage stamp.  The project is described here:

http://www.meades.org/railways/garden/garden.html

Please refer to that page for more information.

The [blender](/blender) contains the 3D printed parts (`.blend` files and exported `.stl` files), the [vcarve](/vcarve) directory contains the [VCarve](https://www.vectric.com/products/vcarve/) CNC files (used for milling) and the [software](/software) directory contains, you guessed it, software.

IMPORTANT: the software part of this project uses `git` sub-modules to bring in third-party components, so to clone this repo it is best to do:

```
git clone --recurse-submodules https://github.com/RobMeades/FrontGardenRailway.git
```

...and that will populate the sub-modules also.  If you have already cloned this repo without the `--recurse-submodules` bit, `cd` to this directory and do:

```
git submodule update --init --recursive
```

...to populate the sub-modules.
