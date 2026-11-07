"""The child process that decodes and re-encodes one uploaded image (plan §5, F54).

``processing`` runs it as ``python -I decoder.py SRC DST FORMAT MAX_PIXELS MAX_SIDE MAX_BYTES
MEMORY CPU``, never in the web process: a stuck or hostile decoder can only exhaust this
process, which runs under resource limits (address space, CPU time, file size, no core
dump) and is killed by the parent after a wall-clock deadline. ``-I`` keeps the current
directory and the environment out of its import path, so it imports only the standard
library and Pillow; it must not import the project.

The image is opened only as the format its magic bytes announced, refused above
``MAX_PIXELS`` before any pixel is decoded (decompression bombs), turned upright from its
EXIF orientation, reduced to at most ``MAX_SIDE`` pixels per side and written as WebP
without any metadata (EXIF, GPS, XMP and ICC are all dropped).

Exit status: ``EXIT_OK``, or the reason it refused the file (``processing`` maps them).
"""

import os
import sys
import warnings

EXIT_OK = 0
EXIT_FORMAT = 10  # not an image of the announced format
EXIT_TOO_MANY_PIXELS = 11  # above the pixel limit (a decompression bomb)
EXIT_TOO_LARGE = 12  # the re-encoded file is larger than allowed
EXIT_UNDECODABLE = 13  # truncated, corrupt or too demanding to decode
EXIT_TIMEOUT = 124  # killed at the deadline (the parent's status; this process never returns it)
EXIT_USAGE = 64

WEBP_QUALITY = 82
ARGUMENTS = 8


def limit_resources(memory_bytes: int, cpu_seconds: int, file_bytes: int) -> None:
    """Apply the child's limits to itself, before Pillow is imported.

    Without ``resource`` (not POSIX: a Windows development machine) only the parent's
    wall-clock kill applies.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX everywhere the app runs
        return
    for limit, value in (
        (resource.RLIMIT_AS, memory_bytes),
        (resource.RLIMIT_CPU, cpu_seconds),
        (resource.RLIMIT_FSIZE, file_bytes),
        (resource.RLIMIT_CORE, 0),
    ):
        _, hard = resource.getrlimit(limit)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)  # a limit can be lowered, never raised
        resource.setrlimit(limit, (value, value))


def convert(source: str, target: str, image_format: str, max_pixels: int, max_side: int) -> int:
    """Decode ``source`` as ``image_format`` and write it to ``target`` as WebP."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = max_pixels
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    try:
        with Image.open(source, formats=[image_format]) as image:
            if image.width * image.height > max_pixels:
                return EXIT_TOO_MANY_PIXELS
            image.load()
            picture = ImageOps.exif_transpose(image)
            alpha = picture.mode in {"RGBA", "LA", "PA"} or picture.has_transparency_data
            picture = picture.convert("RGBA" if alpha else "RGB")
            picture.thumbnail((max_side, max_side))
            picture.save(target, "WEBP", quality=WEBP_QUALITY, method=4)
    except Image.DecompressionBombError, Image.DecompressionBombWarning:
        return EXIT_TOO_MANY_PIXELS
    except UnidentifiedImageError:
        return EXIT_FORMAT
    except OSError, SyntaxError, ValueError, MemoryError, EOFError:
        return EXIT_UNDECODABLE
    return EXIT_OK


def main(argv: list[str]) -> int:
    if len(argv) != ARGUMENTS:
        return EXIT_USAGE
    source, target, image_format = argv[:3]
    max_pixels, max_side, max_bytes, memory, cpu = (int(value) for value in argv[3:])
    # The file-size limit stops a runaway encoder; the exact size is checked below.
    limit_resources(memory, cpu, 2 * max_bytes)
    status = convert(source, target, image_format, max_pixels, max_side)
    if status == EXIT_OK and os.path.getsize(target) > max_bytes:
        return EXIT_TOO_LARGE
    return status


if __name__ == "__main__":  # pragma: no cover - runs in the child process
    sys.exit(main(sys.argv[1:]))
