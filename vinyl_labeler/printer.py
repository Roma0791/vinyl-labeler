"""
Stage 6: print, or in --dry-run mode, just save the PNG so you can eyeball
layout before committing tape to it.

Uses brother_ql's documented API (BrotherQLRaster -> convert -> send). This
has been checked against the real installed brother_ql 0.9.4 package
(model list, label-size codes, function signatures all confirmed present)
but NOT against an actual QL-570 over USB -- that can only happen on your
machine. Run `vinyl-label discover-printer` first and confirm you can print
a single test label before wiring this into the full pipeline.
"""
from pathlib import Path
from PIL import Image

from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send, discover


# Raster command to set the QL-570's Auto Power-Off timeout, reverse-
# engineered from a genuine USB capture of Brother's own Printer Setting
# Tool (see https://github.com/pklaus/brother_ql/issues/50) -- not part of
# brother_ql's own documented API, so sent as raw bytes via the same
# backend/send() helper print_label() uses, rather than through
# BrotherQLRaster. Confirmed by multiple users there on QL-700/QL-800
# (same USB-only architecture as the 570); not separately confirmed on a
# QL-570 specifically, but low-risk to try -- it's a one-byte settings
# write, not a firmware change, and the print pipeline is unaffected
# either way.
#
# This exists because Auto Power-Off is a hard shutdown, not a sleep
# state -- once it fires, the printer disconnects from USB entirely and
# nothing sent over the wire can wake it, only the physical button. The
# practical fix is avoiding the state altogether.
_AUTO_POWER_OFF_MINUTE_STEPS = {0: 0x00, 10: 0x01, 20: 0x02, 30: 0x03, 40: 0x04, 50: 0x05, 60: 0x06}


def set_auto_power_off(minutes: int, printer_identifier: str, backend: str = "pyusb") -> None:
    """minutes=0 disables Auto Power-Off entirely; otherwise must be one of
    10/20/30/40/50/60. Setting is stored in the printer's own memory, so
    this is a one-time action, not something to resend per print job --
    and idempotent to resend anyway (it's the same single-byte settings
    write every time, not a cumulative operation, so sending it twice by
    mistake does nothing worse than sending it once)."""
    if minutes not in _AUTO_POWER_OFF_MINUTE_STEPS:
        raise ValueError(f"minutes must be one of {sorted(_AUTO_POWER_OFF_MINUTE_STEPS)}, got {minutes}")
    command = bytes([0x1B, 0x69, 0x55, 0x41, 0x00, _AUTO_POWER_OFF_MINUTE_STEPS[minutes]])
    # blocking=True (used for real print jobs elsewhere in this module)
    # polls for a "printing completed" status response for up to 10s --
    # this isn't a print job, so that status never arrives, and it was
    # producing a spurious "Printing potentially not successful?" warning
    # on a command that had, in fact, succeeded. blocking=False just
    # confirms the bytes were written to the device and returns.
    status = send(instructions=command, printer_identifier=printer_identifier,
                   backend_identifier=backend, blocking=False)
    if not status.get("instructions_sent"):
        raise RuntimeError(f"Printer did not acknowledge the command: {status}")


def discover_printers(backend: str = "pyusb") -> list:
    """Lists connected printers for this backend. Run this once to find your
    printer_identifier for config.yaml -- e.g. 'usb://0x04f9:0x2028'.
    Requires libusb: `brew install libusb` on macOS."""
    return discover(backend_identifier=backend)


def print_label(image: Image.Image, model: str, label_size: str,
                 printer_identifier: str, backend: str = "pyusb",
                 cut: bool = True, dry_run: bool = False,
                 dry_run_path: Path = None) -> None:
    if dry_run:
        out = dry_run_path or Path("label_preview.png")
        image.save(out)
        print(f"[dry-run] label saved to {out} -- nothing sent to the printer")
        return

    if not printer_identifier:
        raise ValueError(
            "No printer_identifier set in config.yaml. Run "
            "`vinyl-label discover-printer` to find it first."
        )

    qlr = BrotherQLRaster(model)
    qlr.exception_on_warning = True
    instructions = convert(
        qlr=qlr,
        images=[image],
        label=label_size,
        rotate="0",
        threshold=70.0,
        dither=False,
        compress=False,
        red=False,
        dpi_600=False,
        hq=True,
        cut=cut,
    )
    send(instructions=instructions, printer_identifier=printer_identifier,
         backend_identifier=backend, blocking=True)
