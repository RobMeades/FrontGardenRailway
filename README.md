# Introduction

This repo contains various files used in creating my Front Garden Railway on a postage stamp.  The project is described here:

http://www.meades.org/railways/garden/garden.html

Please refer to that page for more information.

# 3D Printed Parts

## `plumbing.blend`
A small plastic part to fit under a bowl, catching rain water from a down pipe and providing a 3/4&nbsp;inch spigot; exported `.stl` file also included.  See [here](http://www.meades.org/railways/garden/garden.html#plumbing) for how it was used.  It was printed in ASA for UV hardness, 15% in-fill, 0.2&nbsp;mm "speed" resolution on my Prusa MK3 3D printer.

## `radius_1121.crv`
[VCarve](https://www.vectric.com/products/vcarve/) file for cuttting the 1121&nbsp;mm, 45&nbsp;mm wide, radius curve on a CNC milling machine; see [here](https://www.meades.org/railways/garden/garden.html#curve) for how it was done.

## `viaduct_experiment_*.blend`
Blender files for the first experiment in 3D printing a viaduct for the railway.  See [here](https://www.meades.org/railways/garden/garden.html#viaduct_experiment) for how these files were printed.

## `conduit_connector.blend`
A set of small plastic parts that can be placed inside these 60&nbsp;cm diameter M20-threaded metal conduit fittings available from RS:

- RS PRO through box, conduit fitting, 20&nbsp;mm nominal, [228-895](https://uk.rs-online.com/web/p/conduit-fittings/0228895)
- RS PRO T-piece, conduit fitting, 20&nbsp;mm nominal, [228-873](https://uk.rs-online.com/web/p/conduit-fittings/0228873)
- RS PRO terminal box, conduit fitting, 20&nbsp;mm nominal, [228-889](https://uk.rs-online.com/web/p/conduit-fittings/0228889)

The parts hold standard 3 Amp terminal blocks such as [these](https://www.amazon.co.uk/GTSE-Electrical-Connector-Blocks-Terminal/dp/B08LNWMMHQ) in sets of four (17&nbsp;mm x 30&nbsp;mm) so that cables can be connected together easily.  Export from Blender at a scale factor of 1 (exported `.stl` file included).  I printed them in ASA (with a brim to aid adhesion) at 0.1&nbsp;mm "detail" resolution on my Prusa MK4 3D printer with 10% in-fill.

The cable clamp is screwed to the body with something like a number&nbsp;4 1/4" self tapper, the base is not held in place at all except by the body placed on top of it and the body is held in place with a short (e.g. 15&nbsp;mm) M4 bolt screwed through the threaded hole in the bottom of the conduit fitting.  The terminal block can be tacked in place with a few spots of superglue but it will generally be held in place sufficently well by the clamped cables connected into it, no glue is really necessary, and it is quite nice for the terminal block to be removable in case easier access is required.