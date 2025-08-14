# Introduction

This repo contains various files used in creating my Front Garden Railway on a postage stamp.  The project is described here:

http://www.meades.org/railways/garden/garden.html

Please refer to that page for more information.

# 3D Printed Parts

## `plumbing.blend`
A small plastic part to fit under a bowl, catching rain water from a down pipe and providing a 3/4&nbsp;inch spigot; exported `.stl` file also included.  See [here](http://www.meades.org/railways/garden/garden.html#plumbing) for how it was used.  It was printed in ASA for UV hardness, 15% in-fill, 0.2&nbsp;mm "speed" resolution on my Prusa MK3 3D printer.

## `down_pipe_cap.blend`
A plug that can be fitted into the end of a normal-sized (65&nbsp;mm internal diameter) UK plastic rainwater down-pipe, as used with domestic guttering, to blank it off.  This was used with the lengths of drain-pipe that formed wiring channels through the concrete sections of the front garden railway, closing up the end of the channel for water-proofness (holes should be drilled in the cap as appropriate to let any wiring or conduit through).  It was printed in ASA for UV hardness, 15% in-fill, 0.2&nbsp;mm "speed" resolution on my Prusa MK4 3D printer.

## `radius_1121.crv`
[VCarve](https://www.vectric.com/products/vcarve/) file for cuttting the 1121&nbsp;mm, 45&nbsp;mm wide, radius curve on a CNC milling machine; see [here](https://www.meades.org/railways/garden/garden.html#curve) for how it was done.

## `viaduct_experiment_*.blend`
Blender files for the first experiment in 3D printing a viaduct for the railway.  See [here](https://www.meades.org/railways/garden/garden.html#viaduct_experiment) for how these files were printed.

## `conduit_connector.blend`
A set of small plastic parts that can be placed inside these 60&nbsp;cm diameter M20-threaded metal conduit fittings available from RS:

- RS PRO through box, conduit fitting, 20&nbsp;mm nominal, [228-895](https://uk.rs-online.com/web/p/conduit-fittings/0228895)
- RS PRO T-piece, conduit fitting, 20&nbsp;mm nominal, [228-873](https://uk.rs-online.com/web/p/conduit-fittings/0228873)
- RS PRO terminal box, conduit fitting, 20&nbsp;mm nominal, [228-889](https://uk.rs-online.com/web/p/conduit-fittings/0228889)

The parts hold standard mains 3&nbsp;Amp terminal blocks such as [these](https://www.amazon.co.uk/GTSE-Electrical-Connector-Blocks-Terminal/dp/B08LNWMMHQ) in sets of four (17&nbsp;mm x 30&nbsp;mm) so that cables can be connected together easily.  Export from Blender at a scale factor of 1 (exported `.stl` file included).  I printed them in ASA (with a brim to aid adhesion on everything but the clamps) at 0.1&nbsp;mm "detail" resolution on my Prusa MK4 3D printer with 10% in-fill.

The cable clamp is screwed to the body with something like a number&nbsp;4 1/4" self tapper, the base is not held in place at all except by the body placed on top of it and the body is held in place with a short (e.g. 15&nbsp;mm) M4 bolt screwed through the threaded hole in the bottom of the conduit fitting.  The terminal block can be tacked in place with a few spots of superglue but it will generally be held in place sufficently well by the clamped cables connected into it, no glue is really necessary, and it is quite nice for the terminal block to be removable in case easier access is required.

See [here](https://www.meades.org/railways/garden/garden.html#piping_and_wiring) for pictures of the finished/fitted items.

## `viaduct_final`
The contents of the [viaduct_final](/viaduct_final) directory are the final design of the viaduct; the moulds and related parts that were eventually printed.  Again, `.stl` exports are also included, with a Blender scale factor of 30.48.

The moulds are all printed in PVB with 10% in-fill: I used RepRapper PVB, which is priced very nicely (when you need 10&nbsp;kg of filament that is important) but PVB is highly hygroscopy, definitely needs a dehumidifier (I contained the spool in an [eSun eBox Lite](https://www.esun3d.com/ebox_lite-product/)).  RepRapper PVB should be printed at a relatively low nozzle temperature (195&nbsp;C first layer, then 185&nbsp;C) to avoid stringing, 75&nbsp;C on the heat bed.  A brim is advisable, to make sure the part doesn't come away from the heat-bed, and supports should be included everywhere on the upper moulds as those are awkward shapes (supports are not required on the lower moulds).  On the upper moulds some manual tweaking may be required to the mating faces, using a craft knife, as they are quite a tight fit.

I initially printed the joiners in ASA with 25% in-fill, intending to re-use them, but they proved quite difficult to extract from the set grout and so I reverted to printing them in PVB.  The braces and floors I did print in ASA though, the braces with 25% in-fill, the floors with just 10% in-fill (to reduce the chance of warping), though in a pinch PLA would do for both as ASA in such large, high-density, prints can warp quite badly.

The wall should be printed in ASA that, as near as possible, matches the colour of the grout being poured into the mould, or that painting it to do so is trivial.  The wall is in two parts so as to ease placement for printing; the outer part should be Araldited to the inner part once the inner part is in position, to lock any bend into place.  Use a 1.5&nbsp;mm drill in a small bit to clear out the 1.5&nbsp;mm alignment holes.

A resoluton of 0.2&nbsp;mm "speed" is fine for all parts, though for the walls it is useful to apply variable print height and increase the resolution to max for the layers of the wall pattern.

Summarizing:

- `viaduct_*_mould_lower.stl`, `viaduct_*_mould_upper_*.stl`: PVB, 10% in-fill, supports everywhere.
- `viaduct_*_mould_joiner*.stl`: PVB, 25% in-fill.
- `viaduct_*_mould_brace.stl`: ASA, 25% in-fill.
- `viaduct_*_mould_upper_floor.stl`: ASA, 10% in-fill.
- `viaduct_wall_*.stl`: ASA of the right colour, 10% in-fill.

The `viaduct_*_lower.stl` files are included for reference but don't need to be printed since they were the "positives" from which the moulds were created.

See [here](https://www.meades.org/railways/garden/garden.html#viaduct_manufacture_begins) for pictures of the moulds etc., in use.

## `supporting_wall.blend`
Blender file for a supporting wall, including a mould version of the same, `.stl` exports for both at a Blender scale factor of 30.48 also provided.  This provides a supporting wall along the back of the dock.  The mould sides should be printed in PLA or whatever, the mould main body in a flexible filament such as the amazing (Forward AM Ultrafuse TPU85A)[https://forward-am.com/material-portfolio/ultrafuse-filaments-for-fused-filaments-fabrication-fff/flexible-filaments/ultrafuse-tpu-85a/], no supports required (10% in-fill and 0.2&nbsp;mm "speed" resolution on my Prusa MK4 3D printer), then gently clamped together, silicon release agent sprayed in, liquid grout poured in, left for a day to harden and then the mould unclamped to allow the moulded walls to be removed and the moulded wall to be flexed free.  The finished wall may be cut to the correct height/width by carefully slicing off the bottom/right edge with an angle-grinder.

## `hydrofoil.blend`
A hydrofoil for the waterfall (plus `.stl` export of same at 1:1 scale) to pre-distort the water flow so as to create a straighter fall of water.  This should be 3D printed in ASA for UV-safety, supports everywhere as it is a complex shape, 10% in-fill and 0.2&nbsp;mm "speed" resolution on my Prusa MK4 3D printer.  You may need to split the object into two in your slicer program, e.g. with a dovetail joint, to fit it on your print-bed.  The printed hydrofoil should be glued on top of the front edge of the liner that forms the run-up to the waterfall-edge with a waterproof glue such as [Hutton Aquatic Products Gold Label Pond Aquarium Sealer](https://www.huttonaquaticproducts.co.uk/products/gold-label-pond-aquarium-sealer/).

## `filter_wall.blend`
A printable stone wall (plus `.stl` exports at 30.48 scale) that forms a filter for the water outlets of the lake.  This should be 3D printed in ASA for UV-safety, supports _are_ required but on the print bed only, for the filter wall components 10% in-fill and I chose relatively high detail, since this is not going to be moulded, it needs to look sufficiently realistic in its plastic form.  The truss will likely be too long for your print-bed: get your slicer program to cut it in two with a dovetail joint.  The clips should also be printed in ASA and be placed horizontally on the bed to get the strength of the filament in the best axis; 100% in-fill might be a good idea to be quite sure of their strength.  Multiple copies of the parts will need to be printed, the upper and lower portions glued together then heated and bent to form a wall of the right shape before being glued together, with the back-strips offset, to join them and keep that shape.  Probably six clips would be required (attaching at the top and the bottom of three legs).

## `sump_cap_side_wall.blend`
This, with the four `.stl` exports at 30.48 scale, is a continuation of `viaduct_wall_*.stl` to be glued along the sides of the curved concrete board that carries the track across the sump cap.  `sump_cap_side_wall_end_?.stl` grade upwards, reducing in height, to match the height of `viaduct_wall_*.stl`.  Should be printed in ASA, 10% in-fill, no supports required brim is advisable to stop the ASA warping away from the build plate.

## `paving.blend`
What it says really: paving, 450 mm wide slabs, `.stl` export at 30.48 scale.  Should be printed in ASA, 10% in-fill, supports on the build plate, brim advisable to prevent warping.