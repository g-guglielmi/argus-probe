Argus probe installer @VERSION@

This stick writes DietPi to a mini PC's internal disk, set up as an Argus probe.

What goes on it (at this top level):
  - DietPi's image for a mini PC, as downloaded (don't unpack it):
      DietPi_NativePC-UEFI-x86_64-Trixie.img.xz   from https://dietpi.com/
  - The probe's kit from Argus (Add probe, DietPi, Mini PC): open the zip and copy its "argus"
    folder here. A stick can carry the kits of several probes.

On the mini PC:
  1. Plug it into the network with a cable.
  2. Turn Secure Boot off in its firmware settings.
  3. Boot it from this stick (its boot menu key, often F7, F11 or F12).
  4. Check the disk the installer shows and press Enter: EVERYTHING on that disk is erased.
  5. Pull the stick out when it says so. The mini PC restarts into DietPi's first boot (10 to 15
     minutes), then the probe enrolls with Argus.

A kit is used once (its enrollment token is single use): the installer renames it used-<probe>.
