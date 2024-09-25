# Introduction

This repo contains various files used in creating my Front Garden Railway on a postage stamp.  The project is described here:

http://www.meades.org/railways/garden/garden.html

Please refer to that page for more information.

# 3D Printed Parts

## `track_bender.blend`
This Blender file contains one segment of the track blender jig; it should be exported scaled up by 1000.  The exported `.stl` file is also included.  I printed five of these in 0.2&nbsp;mm "fast" resolution on my Prusa MK3 3D printer with 20% in-fill.  See [here](http://www.meades.org/railways/garden/garden.html#rail_preparation) for further instructions on how I used this jig to bend track.

## `conduit_connector.blend`
A set of small plastic parts that can be placed inside these 60&nbsp;cm diameter M20-threaded metal conduit fittings available from RS:

- RS PRO through box, conduit fitting, 20&nbsp;mm nominal, [228-895](https://uk.rs-online.com/web/p/conduit-fittings/0228895)
- RS PRO T-piece, conduit fitting, 20&nbsp;mm nominal, [228-873](https://uk.rs-online.com/web/p/conduit-fittings/0228873)
- RS PRO terminal box, conduit fitting, 20&nbsp;mm nominal, [228-889](https://uk.rs-online.com/web/p/conduit-fittings/0228889)

The parts hold standard 3 Amp terminal blocks such as [these](https://www.amazon.co.uk/GTSE-Electrical-Connector-Blocks-Terminal/dp/B08LNWMMHQ) in sets of four (17&nbsp;mm x 30&nbsp;mm) so that cables can be connected together easily.  Export from Blender at a scale factor of 1 (exported `.stl` file included).  I printed them in ASA (with a brim to aid adhesion) at 0.1&nbsp;mm "detail" resolution on my Prusa MK4 3D printer with 10% in-fill.

The cable clamp is screwed to the body with something like a number&nbsp;4 1/4" self tapper, the base is not held in place at all except by the body placed on top of it and the body is held in place with a short (e.g. 15&nbsp;mm) M4 bolt screwed through the threaded hole in the bottom of the conduit fitting.  The terminal block can be tacked in place with a few spots of superglue but it will generally be held in place sufficently well by the clamped cables connected into it, no glue is really necessary, and it is quite nice for the terminal block to be removable in case easier access is required.