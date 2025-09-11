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
A printable stone wall (plus `.stl` exports at 30.48 scale) that forms a filter for the water outlets of the lake.  This should be 3D printed in ASA for UV-safety, no supports required, for the filter wall components just 5% in-fill (they need to be flexible) and I chose relatively high detail, since this is not going to be moulded, it needs to look sufficiently realistic in its plastic form.  The chunky clip should also be printed in ASA; no detail required this time and 15% in-fill for a little more strength.  See [here](https://www.meades.org/railways/garden/garden.html#Filter_Wall_Version_Two) for the assembly process.

## `sump_cap_side_wall.blend`
This, with the four `.stl` exports at 30.48 scale, is a continuation of `viaduct_wall_*.stl` to be glued along the sides of the curved concrete board that carries the track across the sump cap.  `sump_cap_side_wall_end_?.stl` grade upwards, reducing in height, to match the height of `viaduct_wall_*.stl` (see (here)[https://www.meades.org/railways/garden/garden.html#Hiding_The_Sump_Cap] for assembly).  Should be printed in ASA, 10% in-fill, no supports required brim is advisable to stop the ASA warping away from the build plate.

## `paving.blend`
What it says really: paving, 450&nbsp;mm wide slabs, `.stl` export at 30.48 scale.  Should be printed in ASA, 10% in-fill, no supports required, brim advisable to prevent warping, no particular detail in the print so a fast mode should be fine.  See the finished version [here](https://www.meades.org/railways/garden/garden.html#paving).

## `dock_base.blend`
Components that form the edge of the base of the dock (`.stl` exported at 30.48 scale), similar to `sump_cap_side_wall.blend` but intended to represent wood panels that can be clipped to the side of a 3&nbsp;mm thick dock base.  Should be printed in ASA, 5% in-fill (nice and flexible), no supports required, brim advisable to prevent warping, fastest speed since there is little detail.  For assembly instructions see (here)[https://www.meades.org/railways/garden/garden.html#Dock_Base_Improvements].

## `track-side_path.blend`
A track-side path that finishes off where the waterfall concrete joins the track-bed concrete and provides a path to access the viewing platform for Sue Falls (plus `.stl` exports at 30.48 scale).  The "Rock Wall" part of this is intended to print a mould for filling with something like [Ultracrete High Flow Precision Grout](https://www.instarmac.co.uk/products/ultracrete/construction-chemicals/hf-high-flow-precision-grout/), in the same way as the viaduct, while the "Track Wall" part is intended for printing directly in ASA (for UV-safety).

The mould (which will initially be marked as not-visible in the Blender file) is a little different here: because the shape is complex the Boolean tools in Blender tend to misbehave so in this case the mould shape entirely encompasses the thing it is moulding, there is no hole.  Once the `rock_wall_mould.stl` file has been imported into your slicer program, cut off the bottom 2&nbsp;mm to make the hole, then of course cut the long mould N (probably six) times with a dovetailing tool in the slicer program to make it fit onto the print-plate.  Print the mould in the cheapest PLA you can find with supports everywhere (the dovetails will need them), no brim, 5% in-fill and the fastest possible printing time.  You will also find `rock_wall_mould_brace_*` objects and `rock_wall_mould_brace_*.stl` files: the dovetail joints in the mould may not be strong enough on their own, so these braces should printed, ideally with more like 15% in-fill, no need for supports this time, and pushed over the mould at the joints to prevent collapse/leakage during moulding.

See [here](https://www.meades.org/railways/garden/garden.html#Track-side_Path) for the moulding process.

The `track_wall.stl` can be printed directly in ASA, 15% in-fill, no supports required, brim advisable to stop the print leaving the print-bed, and go for relatively high detail since this wall will be visible.

## `bench.blend'
A long and relatively narrow wooden bench, with its own Curve modifier applied in Blender as the bench is intended to be placed against the rock-side wall of `track_side_path.blend` to hide the break that occurred when it was [moulded]((https://www.meades.org/railways/garden/garden.html#Track-side_Path)).  The `.stl` export should be printed in highest resolution in a brown ASA filament.  Since it is a rather detailed object with thin struts, the best way to print it is probably to split the model into two vertically in the printer's slicer program along the middle of the seat, place the two objects down on their cut faces, print them and then glue them together again afterwards; the prints will get a firmer base (less change of ASA warping) and there will be slightly less support material to remove that way.

## `steps.blend`
This Blender file is intended to create a flight of steps.  It is different to the other Blender files here in that the steps are created dynamically from a single step with three modifiers: array (to make 16 steps), solidify (to give the steps thickness) and then a curve modifier to make the steps run in any desired shape.  These modifiers are not "applied" in Blender, they remain dynamic, so the geometry of the steps can be changed at will.

In the same file, initial marked as not visible, is a mould object, which is intended to cover the steps and has a Boolean difference modifier with the steps.  Since one end (as well as the top) of the mould will be open, there is also a separate "mould end" that should be held to the mould with an elastic band or some such during moulding.  The intention is that latex is applied thickly and in several layers to the inside of the mould to form a flight of steps, and this can be done multiple times and the steps joined together to achieve the desired length.  If you change the shape of the steps you may need to change the shape/position of the mould object to match.

`.stl` exports of all components are provided but if you modify any of them you will need to re-export from Blender at a Blender scale factor of 30.48.  The mould should be printed with no supports, 5% in-fill and at maximum print speed in any filament you like (PLA will probably be cheapest).

See [here](https://www.meades.org/railways/garden/garden.html#Woodland_Path) for the moulding process.

## `road.blend`
The road down to the dock, including a bridge over the railway line, in two parts:

- an upper part (A) that has the bridge and rests on the track bed and the lower part,
- a lower part (B) that rests on the dock base.

The "Part A Bridge Spacer" is used during test fitting when only the bottom 2&nbsp;mm of Part A has been printed.

From this model will be formed a mould into which fibreglass will be layered.