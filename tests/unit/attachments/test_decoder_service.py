"""The decoder as a service apart from ``web``: the same results, its protocol, and what comes
back treated as untrusted (M8.3, X11; ADR-0015). A real service and real children."""

import ast
import json
import socket
import struct
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

if not hasattr(socket, "AF_UNIX"):  # a Windows development machine
    pytest.skip("no unix sockets on this platform", allow_module_level=True)

from apps.attachments import decoder, decoder_client, decoder_service, processing
from apps.attachments.processing import Rejected
from tests.clamd import refused_socket
from tests.unit.attachments.conftest import jpeg, png


@pytest.fixture
def service(settings: Any) -> Iterator[str]:
    with tempfile.TemporaryDirectory(prefix="dec-") as directory:
        address = str(Path(directory) / "d.sock")
        server = decoder_service.DecoderServer(address)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        settings.ATTACHMENT_DECODER_SOCKET = address
        try:
            yield address
        finally:
            server.shutdown()
            server.server_close()


def run(tmp_path: Path, data: bytes) -> processing.Processed:
    source = tmp_path / "in"
    source.write_bytes(data)
    return processing.process(source, tmp_path / "out")


def rejected(tmp_path: Path, data: bytes) -> str:
    with pytest.raises(Rejected) as caught:
        run(tmp_path, data)
    return caught.value.code


def raw(address: str, payload: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        connection.connect(address)
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while data := connection.recv(4096):
            answer += data
    return answer


def header(**overrides: Any) -> bytes:
    request = {
        "format": "PNG",
        "max_pixels": 1000,
        "max_side": 100,
        "max_bytes": 1000,
        "memory": 512 * 1024 * 1024,
        "cpu": 10,
        "timeout": 10,
        "size": 1,
        **overrides,
    }
    return json.dumps(request).encode() + b"\n"


class TestResults:
    """What the child-process path promises, through the service."""

    def test_an_image_comes_back_as_webp_without_metadata(
        self, service: str, tmp_path: Path
    ) -> None:
        exif = Image.Exif()
        exif[0x010F] = "Camera maker"
        exif[0x0112] = 6
        done = run(tmp_path, jpeg(60, 20, exif=exif.tobytes()))
        assert done.content_type == processing.WEBP
        with Image.open(done.path) as stored:
            assert stored.format == "WEBP"
            assert not stored.getexif()
            assert stored.size == (20, 60)

    def test_an_undecodable_image(self, service: str, tmp_path: Path) -> None:
        assert rejected(tmp_path, jpeg(400, 400)[:200]) == "undecodable"

    def test_too_many_pixels(self, service: str, tmp_path: Path, settings: Any) -> None:
        settings.ATTACHMENT_MAX_PIXELS = 100
        assert rejected(tmp_path, png(50, 50)) == "too_many_pixels"

    def test_a_hung_decoder_is_killed_at_the_deadline(
        self, service: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        hung = tmp_path / "hung.py"
        hung.write_text("import time\ntime.sleep(30)\n")
        monkeypatch.setattr(decoder_service, "DECODER", hung)
        settings.ATTACHMENT_DECODE_TIMEOUT = 1
        assert rejected(tmp_path, png()) == "timeout"

    def test_a_child_killed_by_a_signal_is_undecodable(
        self, service: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        killed = tmp_path / "killed.py"
        killed.write_text("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
        monkeypatch.setattr(decoder_service, "DECODER", killed)
        assert rejected(tmp_path, png()) == "undecodable"


@contextmanager
def answering(settings: Any, answer: bytes) -> Iterator[None]:
    """A service that sends these bytes whatever it is asked, then closes the connection."""
    with tempfile.TemporaryDirectory(prefix="dec-") as directory:
        address = str(Path(directory) / "d.sock")
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(address)
            listener.listen(1)

            def reply() -> None:
                connection, _ = listener.accept()
                with connection, connection.makefile("rb") as stream:
                    # The whole request first: closing on a client still sending would break its
                    # pipe, and the answer under test would never be read.
                    stream.read(json.loads(stream.readline())["size"])
                    connection.sendall(answer)

            threading.Thread(target=reply, daemon=True).start()
            settings.ATTACHMENT_DECODER_SOCKET = address
            yield


class TestWhatComesBackIsUntrusted:
    def _lying_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
    ) -> None:
        def lie(request: dict[str, Any], source: Path, target: Path) -> int:
            target.write_bytes(payload)
            return decoder_service.EXIT_OK

        monkeypatch.setattr(decoder_service, "run_decoder", lie)

    def test_anything_but_webp_is_not_stored(
        self, service: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._lying_service(tmp_path, monkeypatch, b"<html><script>alert(1)</script></html>")
        assert rejected(tmp_path, png()) == "undecodable"

    def test_more_than_a_stored_file_may_be_is_cut(
        self, service: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
    ) -> None:
        self._lying_service(tmp_path, monkeypatch, b"RIFF\0\0\0\0WEBP" + b"x" * 2048)
        settings.ATTACHMENT_MAX_FILE_BYTES = 1024
        assert rejected(tmp_path, png()) == "too_large"

    def test_a_connection_that_drops_partway_is_not_a_whole_file(
        self, tmp_path: Path, settings: Any
    ) -> None:
        """The container killed mid-answer: not stored as a truncated image."""
        whole = b"RIFF\0\0\0\0WEBP" + b"x" * 500
        answer = bytes([decoder_service.EXIT_OK]) + struct.pack("!Q", len(whole)) + whole[:100]
        with answering(settings, answer):
            assert rejected(tmp_path, png()) == "unavailable"

    def test_a_connection_that_drops_before_the_size(self, tmp_path: Path, settings: Any) -> None:
        with answering(settings, bytes([decoder_service.EXIT_OK]) + b"\0\0"):
            assert rejected(tmp_path, png()) == "unavailable"

    def test_a_size_above_the_limit_is_not_read_at_all(self, tmp_path: Path, settings: Any) -> None:
        answer = bytes([decoder_service.EXIT_OK]) + struct.pack("!Q", 2**62)
        with answering(settings, answer):
            assert rejected(tmp_path, png()) == "too_large"

    def test_nothing_comes_back_at_all(
        self, service: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._lying_service(tmp_path, monkeypatch, b"")
        assert rejected(tmp_path, png()) == "empty"


class TestAvailability:
    def test_nothing_listening(self, tmp_path: Path, settings: Any) -> None:
        settings.ATTACHMENT_DECODER_SOCKET = refused_socket(tmp_path)
        assert rejected(tmp_path, png()) == "unavailable"

    def test_a_service_that_closes_without_an_answer(self, tmp_path: Path, settings: Any) -> None:
        with tempfile.TemporaryDirectory(prefix="dec-") as directory:
            address = str(Path(directory) / "d.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(address)
                listener.listen(1)

                def accept_and_close() -> None:
                    connection, _ = listener.accept()
                    connection.recv(65536)
                    connection.close()

                threading.Thread(target=accept_and_close, daemon=True).start()
                settings.ATTACHMENT_DECODER_SOCKET = address
                assert rejected(tmp_path, png()) == "unavailable"

    def test_a_service_that_never_answers(
        self, tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="dec-") as directory:
            address = str(Path(directory) / "d.sock")
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(address)
                listener.listen(1)
                settings.ATTACHMENT_DECODER_SOCKET = address
                settings.ATTACHMENT_DECODE_TIMEOUT = 0
                monkeypatch.setattr(decoder_client, "MARGIN", 0.2)
                source = tmp_path / "in"
                source.write_bytes(b"x")
                status = decoder_client.decode(source, tmp_path / "out", "PNG")
                assert status == decoder_service.EXIT_TIMEOUT

    def test_ping(self, service: str, settings: Any, tmp_path: Path) -> None:
        assert decoder_client.ping() is True
        assert decoder_service.main(["--ping", service]) == 0
        gone = refused_socket(tmp_path)
        assert decoder_service.main(["--ping", gone]) == 1
        settings.ATTACHMENT_DECODER_SOCKET = gone
        assert decoder_client.ping() is False
        settings.ATTACHMENT_DECODER_SOCKET = ""
        assert decoder_client.ping() is True

    def test_usage(self) -> None:
        assert decoder_service.main([]) == 64


class TestProtocol:
    @pytest.mark.parametrize(
        "payload",
        [
            b"not json\n",
            b"[1, 2]\n",
            b"x" * (decoder_service.MAX_HEADER + 10),
            b"no newline",
            header(format="SVG"),
            header(max_pixels=-1),
            header(cpu="10; rm -rf /"),
            header(memory=True),
            header(size=decoder_service.MAX_SOURCE + 1),
            header(timeout=decoder_service.MAX_TIMEOUT + 1),
        ],
    )
    def test_a_request_it_will_not_run_is_refused(self, service: str, payload: bytes) -> None:
        assert raw(service, payload) == bytes([decoder_service.EXIT_REFUSED])

    def test_a_body_shorter_than_it_said(self, service: str) -> None:
        assert raw(service, header(size=100) + b"short") == bytes([decoder_service.EXIT_REFUSED])

    def test_no_free_slot(self, tmp_path: Path) -> None:
        """No slot means the timeout's status, and nothing runs."""

        class NoSlots:
            class slots:
                @staticmethod
                def acquire(timeout: float | None = None) -> bool:
                    return False

        written: list[bytes] = []

        class Out:
            def write(self, data: bytes) -> None:
                written.append(data)

        handler = decoder_service.Handler.__new__(decoder_service.Handler)
        handler.server = NoSlots()  # type: ignore[assignment]
        handler.wfile = Out()  # type: ignore[assignment]
        handler._receive = lambda path, size: True  # type: ignore[method-assign]
        handler._convert({"size": 1}, tmp_path)
        assert written == [bytes([decoder_service.EXIT_TIMEOUT])]

    def test_a_stale_socket_is_replaced_and_anything_else_refused(self, tmp_path: Path) -> None:
        stale = refused_socket(tmp_path)
        server = decoder_service.DecoderServer(stale)
        server.server_close()
        other = tmp_path / "not-a-socket"
        other.write_text("")
        with pytest.raises(SystemExit):
            decoder_service.DecoderServer(str(other))

    def test_its_socket_is_open_to_the_other_uid(self, service: str) -> None:
        assert Path(service).stat().st_mode & 0o777 == 0o666

    def test_a_client_that_went_away_leaves_one_line(
        self, service: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = decoder_service.DecoderServer.__new__(decoder_service.DecoderServer)
        try:
            raise BrokenPipeError
        except BrokenPipeError:
            server.handle_error(None, None)
        assert "BrokenPipeError" in capsys.readouterr().err

    def test_the_two_sides_agree(self) -> None:
        """The service repeats these: run as ``python -I`` it cannot import the others."""
        assert decoder_service.EXIT_OK == decoder.EXIT_OK
        assert decoder_service.EXIT_UNDECODABLE == decoder.EXIT_UNDECODABLE
        assert decoder_service.EXIT_TIMEOUT == decoder.EXIT_TIMEOUT
        assert decoder_service.SIZE_FORMAT == decoder_client.SIZE_FORMAT
        assert decoder_service.CHUNK == decoder_client.CHUNK


def test_the_application_never_imports_the_service() -> None:
    """It needs unix sockets at import time: a Windows development machine has none."""
    package = Path(decoder_service.__file__).parent
    for path in package.glob("*.py"):
        if path.name == decoder_service.__file__.rsplit("/", 1)[-1]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for alias in node.names
        }
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert "decoder_service" not in names, path
        assert "apps.attachments.decoder_service" not in modules, path
