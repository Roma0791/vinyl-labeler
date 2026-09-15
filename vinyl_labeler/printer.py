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
