"""Phase 2 of an upload (plan §5, F54): the type by magic bytes, the decoder child process
under its limits, re-encoding to WebP without metadata, and the refusals (an SVG or HTML
file disguised as an image, a 50 MP decompression bomb, a hung decoder, an oversized output).
"""

import io
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from apps.attachments import decoder, files, processing
from apps.attachments.processing import Rejected
from tests.unit.attachments.conftest import PDF_BYTES, jpeg, png

SVG = (
    b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
)
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"


def write(tmp_path: Path, data: bytes, name: str = "in") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def run(tmp_path: Path, data: bytes) -> processing.Processed:
    return processing.process(write(tmp_path, data), tmp_path / "out")


def rejected(tmp_path: Path, data: bytes) -> str:
    with pytest.raises(Rejected) as caught:
        run(tmp_path, data)
    return caught.value.code


class TestSniff:
    @pytest.mark.parametrize(
        ("data", "kind"),
        [
            (jpeg(), files.JPEG),
            (png(), files.PNG),
            (PDF_BYTES, files.PDF),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 ", files.WEBP),
            (SVG, None),
            (HTML, None),
            (b"GIF89a....", None),
            (b"", None),
        ],
    )
    def test_the_type_comes_from_the_first_bytes(
        self, tmp_path: Path, data: bytes, kind: str | None
    ) -> None:
        assert files.sniff(write(tmp_path, data)) == kind

    def test_a_storage_key_is_64_hex_characters(self) -> None:
        key = files.new_storage_key()
        assert len(key) == 64
        assert files.stored_path(key).name == key
        with pytest.raises(ValueError, match="storage key"):
            files.stored_path("../../etc/passwd")


class TestImages:
    def test_an_image_is_re_encoded_to_webp_without_metadata(self, tmp_path: Path) -> None:
        exif = Image.Exif()
        exif[0x010F] = "Camera maker"  # Make
        exif[0x0112] = 6  # Orientation: rotate 90°
        done = run(tmp_path, jpeg(60, 20, exif=exif.tobytes()))
        assert done.content_type == processing.WEBP
        assert done.sha256 == files.sha256_of(done.path)
        with Image.open(done.path) as stored:
            assert stored.format == "WEBP"
            assert not stored.getexif()
            assert stored.info.get("exif") is None
            assert stored.size == (20, 60)  # turned upright before the metadata was dropped

    def test_large_images_are_reduced_to_2560_pixels(self, tmp_path: Path) -> None:
        done = run(tmp_path, png(3000, 100))
        with Image.open(done.path) as stored:
            assert stored.size == (2560, 85)

    def test_transparency_is_kept(self, tmp_path: Path) -> None:
        done = run(tmp_path, png(10, 10, mode="RGBA"))
        with Image.open(done.path) as stored:
            assert stored.mode == "RGBA"

    def test_a_webp_is_accepted(self, tmp_path: Path) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(buffer, "WEBP")
        assert run(tmp_path, buffer.getvalue()).content_type == processing.WEBP

    def test_a_pdf_is_stored_as_it_arrived(self, tmp_path: Path) -> None:
        done = run(tmp_path, PDF_BYTES)
        assert done.content_type == processing.PDF
        assert done.path.read_bytes() == PDF_BYTES
        assert not (tmp_path / "in").exists()


class TestRefusals:
    @pytest.mark.parametrize("data", [SVG, HTML, b"GIF89a" + b"\x00" * 64])
    def test_a_file_that_is_not_an_accepted_type_is_rejected(
        self, tmp_path: Path, data: bytes
    ) -> None:
        assert rejected(tmp_path, data) == "type"

    def test_html_behind_an_image_signature_is_rejected(self, tmp_path: Path) -> None:
        # Pillow refuses them as not that format, or as corrupt: either way, never stored.
        assert rejected(tmp_path, b"\xff\xd8\xff" + HTML) in {"type", "undecodable"}
        assert rejected(tmp_path, b"\x89PNG\r\n\x1a\n" + SVG) in {"type", "undecodable"}

    def test_a_png_named_as_a_jpeg_is_read_as_what_it_is(self, tmp_path: Path) -> None:
        # The bytes decide: a PNG sent as photo.jpg is a PNG, never decoded as a JPEG.
        assert run(tmp_path, png()).content_type == processing.WEBP

    def test_a_50_megapixel_decompression_bomb_is_rejected(self, tmp_path: Path) -> None:
        bomb = png(7072, 7072, mode="1", optimize=True)  # 50 MP of one colour: a few kB
        assert len(bomb) < 100_000
        started = time.monotonic()
        assert rejected(tmp_path, bomb) == "too_many_pixels"
        assert time.monotonic() - started < 10  # refused from the header, never decoded

    def test_a_truncated_image_is_rejected(self, tmp_path: Path) -> None:
        assert rejected(tmp_path, jpeg(400, 400)[:200]) == "undecodable"

    def test_an_empty_file_or_output_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert rejected(tmp_path, b"") == "type"
        empty = [sys.executable, "-c", f"open({str(tmp_path / 'out')!r}, 'wb').close()"]
        monkeypatch.setattr(processing, "decoder_command", lambda *args: empty)
        assert rejected(tmp_path, png()) == "empty"

    def test_an_output_above_the_file_limit_is_rejected(
        self, tmp_path: Path, settings: Any
    ) -> None:
        settings.ATTACHMENT_MAX_FILE_BYTES = 100
        assert rejected(tmp_path, png(200, 200)) == "too_large"
        assert rejected(tmp_path, PDF_BYTES + b" " * 200) == "too_large"

    def test_a_hung_decoder_is_killed_at_its_deadline(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.ATTACHMENT_DECODE_TIMEOUT = 1
        hang = [sys.executable, "-c", "import time; time.sleep(60)"]
        monkeypatch.setattr(processing, "decoder_command", lambda *args: hang)
        started = time.monotonic()
        assert rejected(tmp_path, png()) == "timeout"
        assert time.monotonic() - started < 5

    def test_a_decoder_killed_by_a_signal_rejects_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crash = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
        monkeypatch.setattr(processing, "decoder_command", lambda *args: crash)
        assert rejected(tmp_path, png()) == "undecodable"

    def test_every_code_has_a_message(self) -> None:
        assert all(str(Rejected(code).message) for code in processing.REJECTIONS)


class TestDecoderChild:
    def test_the_child_runs_under_its_resource_limits(self, tmp_path: Path) -> None:
        script = (
            "import resource, sys; sys.path.insert(0, sys.argv[1]); import decoder; "
            "decoder.limit_resources(300 * 2**20, 7, 2**20); "
            "print(*(resource.getrlimit(getattr(resource, n))[0] for n in "
            "('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_CORE')))"
        )
        folder = str(Path(decoder.__file__).parent)
        result = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
            [sys.executable, "-I", "-c", script, folder],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.split() == [str(300 * 2**20), "7", str(2**20), "0"]

    def test_the_child_runs_isolated_with_the_configured_limits(self, tmp_path: Path) -> None:
        command = processing.decoder_command(tmp_path / "a", tmp_path / "b", files.PNG)
        assert command[:3] == [sys.executable, "-I", str(processing.DECODER)]
        assert command[5:] == ["PNG", "40000000", "2560", str(10 * 2**20), str(512 * 2**20), "60"]

    def test_the_child_exits_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(decoder, "limit_resources", lambda *args: None)
        source, target = write(tmp_path, png(30, 30)), tmp_path / "out.webp"
        common = ["1000000", "2560"]
        assert decoder.main(["only", "two"]) == decoder.EXIT_USAGE
        assert decoder.main([str(source), str(target), "PNG", *common, "100000", "1", "1"]) == 0
        assert decoder.main([str(source), str(target), "JPEG", *common, "100000", "1", "1"]) == (
            decoder.EXIT_FORMAT
        )
        assert decoder.main([str(source), str(target), "PNG", "100", "2560", "1", "1", "1"]) == (
            decoder.EXIT_TOO_MANY_PIXELS
        )
        assert decoder.main([str(source), str(target), "PNG", *common, "10", "1", "1"]) == (
            decoder.EXIT_TOO_LARGE
        )
