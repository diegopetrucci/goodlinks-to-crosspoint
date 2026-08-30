from __future__ import annotations

import json
import os
import socketserver
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import goodlinks_crosspoint as package
import goodlinks_crosspoint.crosspoint as crosspoint_module
from goodlinks_crosspoint.crosspoint import (
    DEFAULT_CROSSPOINT_URL,
    DEFAULT_REMOTE_DIRECTORY,
    CrossPointClient,
    CrossPointHTTPError,
    CrossPointInvalidTimeoutError,
    CrossPointMalformedResponseError,
    CrossPointRedirectError,
    CrossPointResponseTooLargeError,
    CrossPointStatus,
    CrossPointUnavailableError,
    CrossPointUploadIncompleteError,
    InvalidCrossPointURLError,
    InvalidEPUBError,
    InvalidRemotePathError,
    RemoteFileExistsError,
    WrongDeviceError,
    normalize_remote_path,
)


class CrossPointFixtureHandler(BaseHTTPRequestHandler):
    device = "X3"
    status_code = 200
    status_body: object = {"device": "X3"}
    directory_status = 200
    directory_entries: ClassVar[list[dict[str, object]]] = []
    upload_status = 200
    upload_body = b"File uploaded successfully: synthetic-book.epub"
    redirect_location: str | None = None
    requests: ClassVar[list[dict[str, object]]]

    def _record(self, body: bytes = b"") -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # A deliberate partial-upload test closes the client connection.
            pass

    def _request_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(length)

    def do_GET(self) -> None:
        body = b""
        self._record(body)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/status":
            if self.__class__.redirect_location is not None:
                self.send_response(302)
                self.send_header("Location", self.__class__.redirect_location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(
                self.__class__.status_code,
                json.dumps(self.__class__.status_body).encode("utf-8"),
                "application/json",
            )
            return
        if parsed.path == "/api/files":
            if self.__class__.directory_status != 200:
                self._send(
                    self.__class__.directory_status,
                    b"synthetic directory failure body",
                    "text/plain",
                )
                return
            self._send(
                200,
                json.dumps(self.__class__.directory_entries).encode("utf-8"),
                "application/json",
            )
            return
        self._send(404, b"synthetic not found body", "text/plain")

    def do_POST(self) -> None:
        body = self._request_body()
        self._record(body)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/mkdir":
            self._send(200, b"Folder created", "text/plain")
            return
        if parsed.path == "/upload":
            self._send(
                self.__class__.upload_status,
                self.__class__.upload_body,
                "text/plain",
            )
            return
        self._send(404, b"synthetic not found body", "text/plain")

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalHTTPServer(ThreadingHTTPServer):
    """Avoid reverse-DNS lookup in synthetic local HTTP tests."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class CrossPointFixture(TestCase):
    def setUp(self) -> None:
        CrossPointFixtureHandler.device = "X3"
        CrossPointFixtureHandler.status_code = 200
        CrossPointFixtureHandler.status_body = {"device": "X3"}
        CrossPointFixtureHandler.directory_status = 200
        CrossPointFixtureHandler.directory_entries = []
        CrossPointFixtureHandler.upload_status = 200
        CrossPointFixtureHandler.upload_body = (
            b"File uploaded successfully: synthetic-book.epub"
        )
        CrossPointFixtureHandler.redirect_location = None
        CrossPointFixtureHandler.requests = []
        self.server = LocalHTTPServer(("127.0.0.1", 0), CrossPointFixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def client(self) -> CrossPointClient:
        return CrossPointClient(self.base_url)


class CrossPointURLTests(TestCase):
    def test_default_url_and_restricted_url_parts(self) -> None:
        self.assertEqual(DEFAULT_CROSSPOINT_URL, "http://crosspoint.local")
        self.assertEqual(DEFAULT_REMOTE_DIRECTORY, "/GoodLinks")
        self.assertEqual(normalize_remote_path("/GoodLinks/"), "/GoodLinks")
        self.assertIs(package.normalize_remote_path, normalize_remote_path)
        self.assertIsInstance(CrossPointClient(), CrossPointClient)
        with self.assertRaises(CrossPointInvalidTimeoutError):
            CrossPointClient(timeout=0)
        self.assertIs(
            package.CrossPointMalformedResponseError,
            CrossPointMalformedResponseError,
        )
        self.assertEqual(
            CrossPointInvalidTimeoutError.code, "crosspoint_invalid_timeout"
        )
        self.assertEqual(
            CrossPointMalformedResponseError.code,
            "crosspoint_malformed_response",
        )
        self.assertEqual(
            CrossPointResponseTooLargeError.code,
            "crosspoint_response_too_large",
        )
        for old_name in (
            "InvalidTimeoutError",
            "MalformedResponseError",
            "ResponseTooLargeError",
        ):
            self.assertFalse(hasattr(package, old_name))
            self.assertFalse(hasattr(crosspoint_module, old_name))
        for value in (
            "http://user:synthetic-secret@crosspoint.local",
            "http://crosspoint.local?token=synthetic-secret",
            "http://crosspoint.local#synthetic-secret",
            "http://crosspoint.local?",
            "http://crosspoint.local#",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidCrossPointURLError) as raised:
                    CrossPointClient(value)
                self.assertNotIn("synthetic-secret", str(raised.exception))

    def test_redirect_is_rejected_without_following_location(self) -> None:
        fixture = CrossPointFixture()
        fixture.setUp()
        try:
            CrossPointFixtureHandler.redirect_location = (
                "http://synthetic-redirect.invalid/api/status"
            )
            with self.assertRaises(CrossPointRedirectError):
                fixture.client().get_status()
            self.assertEqual(len(CrossPointFixtureHandler.requests), 1)
        finally:
            fixture.tearDown()


class CrossPointHTTPTests(CrossPointFixture):
    def test_status_returns_validated_typed_status(self) -> None:
        CrossPointFixtureHandler.status_body = {
            "device": "X4",
            "version": "synthetic-version",
            "ip": "192.0.2.10",
            "mode": "AP",
            "rssi": -45,
            "freeHeap": 123,
            "uptime": 9,
            "ignored": "not retained",
        }

        status = self.client().get_status()

        self.assertIsInstance(status, CrossPointStatus)
        self.assertEqual(status.device, "X4")
        self.assertFalse(hasattr(status, "version"))
        self.assertFalse(hasattr(status, "ip"))
        self.assertFalse(hasattr(status, "mode"))
        self.assertFalse(hasattr(status, "ignored"))

    def test_wrong_device_status_is_rejected_without_echoing_body(self) -> None:
        CrossPointFixtureHandler.status_body = {
            "device": "synthetic-other-device",
            "private": "synthetic response body",
        }

        with self.assertRaises(WrongDeviceError) as raised:
            self.client().get_status()

        self.assertNotIn("synthetic-other-device", str(raised.exception))
        self.assertNotIn("synthetic response body", str(raised.exception))

    def test_list_directory_validates_documented_entries(self) -> None:
        CrossPointFixtureHandler.directory_entries = [
            {
                "name": "synthetic-book.epub",
                "size": 12,
                "isDirectory": False,
                "isEpub": True,
            },
            {"name": "Notes", "size": 0, "isDirectory": True, "isEpub": False},
        ]

        entries = self.client().list_directory()

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, "synthetic-book.epub")
        self.assertEqual(entries[0].size, 12)
        self.assertFalse(entries[0].is_directory)
        self.assertTrue(entries[0].is_epub)
        request = CrossPointFixtureHandler.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["path"], "/api/files")
        self.assertEqual(request["query"]["path"], ["/GoodLinks"])

    def test_upload_creates_missing_default_directory_and_uses_multipart_http(
        self,
    ) -> None:
        CrossPointFixtureHandler.directory_status = 404
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-book.epub"
            epub_bytes = b"synthetic EPUB bytes only for a local test"
            source.write_bytes(epub_bytes)

            remote_path = self.client().upload_epub(source)

        self.assertEqual(remote_path, "/GoodLinks/synthetic-book.epub")
        self.assertEqual(
            [request["method"] for request in CrossPointFixtureHandler.requests],
            ["GET", "GET", "POST", "POST"],
        )
        self.assertEqual(
            [request["path"] for request in CrossPointFixtureHandler.requests],
            ["/api/status", "/api/files", "/mkdir", "/upload"],
        )
        mkdir_request = CrossPointFixtureHandler.requests[2]
        mkdir_form = urllib.parse.parse_qs(
            bytes(mkdir_request["body"]).decode("utf-8"), keep_blank_values=True
        )
        self.assertEqual(mkdir_form, {"name": ["GoodLinks"], "path": ["/"]})
        upload_request = CrossPointFixtureHandler.requests[3]
        self.assertEqual(upload_request["query"]["path"], ["/GoodLinks"])
        headers = upload_request["headers"]
        self.assertEqual(
            headers["Content-Type"].split(";", 1)[0], "multipart/form-data"
        )
        upload_body = bytes(upload_request["body"])
        self.assertIn(b'name="file"', upload_body)
        self.assertIn(b'filename="synthetic-book.epub"', upload_body)
        self.assertIn(b"Content-Type: application/epub+zip", upload_body)
        self.assertIn(b"synthetic EPUB bytes only for a local test", upload_body)
        self.assertNotIn("Authorization", headers)

    def test_existing_destination_name_is_refused_before_upload(self) -> None:
        CrossPointFixtureHandler.directory_entries = [
            {
                "name": "synthetic-book.epub",
                "size": 99,
                "isDirectory": False,
                "isEpub": True,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-book.epub"
            source.write_bytes(b"synthetic local epub")

            with self.assertRaises(RemoteFileExistsError) as raised:
                self.client().upload_epub(source)

        self.assertNotIn("synthetic-book.epub", str(raised.exception))
        self.assertEqual(
            [request["path"] for request in CrossPointFixtureHandler.requests],
            ["/api/status", "/api/files"],
        )

    def test_explicit_overwrite_boolean_allows_documented_upload(self) -> None:
        CrossPointFixtureHandler.directory_entries = [
            {
                "name": "synthetic-book.epub",
                "size": 99,
                "isDirectory": False,
                "isEpub": True,
            }
        ]
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-book.epub"
            source.write_bytes(b"synthetic replacement epub")

            result = self.client().upload_epub(source, overwrite=True)

        self.assertEqual(result, "/GoodLinks/synthetic-book.epub")
        self.assertEqual(CrossPointFixtureHandler.requests[-1]["path"], "/upload")

    def test_upload_validates_documented_success_body_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-book.epub"
            source.write_bytes(b"synthetic local epub")
            CrossPointFixtureHandler.upload_body = b"synthetic unexpected body"

            with self.assertRaises(CrossPointMalformedResponseError) as raised:
                self.client().upload_epub(source)

            self.assertNotIn("synthetic unexpected body", str(raised.exception))

            CrossPointFixtureHandler.upload_body = (
                b"File uploaded successfully: synthetic-book.epub"
            )
            CrossPointFixtureHandler.upload_status = 201
            with self.assertRaises(CrossPointHTTPError):
                self.client().upload_epub(source, overwrite=True)

    def test_upload_rejects_zero_byte_epub_from_open_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-empty.epub"
            source.touch()

            with self.assertRaises(InvalidEPUBError):
                self.client().upload_epub(source)

        self.assertEqual(CrossPointFixtureHandler.requests, [])

    def test_upload_detects_source_shrink_as_possible_partial(self) -> None:
        original_read = crosspoint_module._MultipartBody.read
        calls = 0

        def shrink_after_prefix(
            body: crosspoint_module._MultipartBody, amount: int = -1
        ) -> bytes:
            nonlocal calls
            calls += 1
            result = original_read(body, amount)
            if calls == 1:
                source = body._source
                os.ftruncate(source.fileno(), 0)
            return result

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-shrink.epub"
            source.write_bytes(b"synthetic source bytes")
            with (
                mock.patch.object(
                    crosspoint_module._MultipartBody,
                    "read",
                    new=shrink_after_prefix,
                ),
                self.assertRaises(CrossPointUploadIncompleteError) as raised,
            ):
                self.client().upload_epub(source)

        self.assertIn("partial", str(raised.exception))
        self.assertNotIn("synthetic source bytes", str(raised.exception))
        self.assertNotIn(str(source), str(raised.exception))

    def test_upload_detects_source_growth_after_transmission(self) -> None:
        original_read = crosspoint_module._MultipartBody.read
        calls = 0

        def grow_after_prefix(
            body: crosspoint_module._MultipartBody, amount: int = -1
        ) -> bytes:
            nonlocal calls
            calls += 1
            result = original_read(body, amount)
            if calls == 1:
                source = body._source
                current = os.fstat(source.fileno()).st_size
                os.ftruncate(source.fileno(), current + 1)
            return result

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-growth.epub"
            source.write_bytes(b"synthetic source bytes")
            CrossPointFixtureHandler.upload_body = (
                b"File uploaded successfully: synthetic-growth.epub"
            )
            with (
                mock.patch.object(
                    crosspoint_module._MultipartBody,
                    "read",
                    new=grow_after_prefix,
                ),
                self.assertRaises(CrossPointUploadIncompleteError) as raised,
            ):
                self.client().upload_epub(source)

        self.assertIn("partial", str(raised.exception))
        self.assertNotIn("synthetic source bytes", str(raised.exception))

    def test_upload_detects_source_mtime_change_after_transmission(self) -> None:
        original_read = crosspoint_module._MultipartBody.read
        calls = 0

        def touch_after_prefix(
            body: crosspoint_module._MultipartBody, amount: int = -1
        ) -> bytes:
            nonlocal calls
            calls += 1
            result = original_read(body, amount)
            if calls == 1:
                source = body._source
                source_stat = os.fstat(source.fileno())
                os.utime(
                    source.fileno(),
                    ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000),
                )
            return result

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic-mtime.epub"
            source.write_bytes(b"synthetic source bytes")
            CrossPointFixtureHandler.upload_body = (
                b"File uploaded successfully: synthetic-mtime.epub"
            )
            with (
                mock.patch.object(
                    crosspoint_module._MultipartBody,
                    "read",
                    new=touch_after_prefix,
                ),
                self.assertRaises(CrossPointUploadIncompleteError),
            ):
                self.client().upload_epub(source)

    def test_http_failure_is_explicit_and_redacted(self) -> None:
        CrossPointFixtureHandler.status_code = 500
        CrossPointFixtureHandler.status_body = {
            "error": "synthetic server body",
            "bytes": "synthetic EPUB bytes",
        }

        with self.assertRaises(CrossPointHTTPError) as raised:
            self.client().get_status()

        self.assertEqual(raised.exception.status, 500)
        self.assertNotIn("synthetic server body", str(raised.exception))
        self.assertNotIn("synthetic EPUB bytes", str(raised.exception))
        self.assertNotIn(self.base_url, str(raised.exception))

    def test_offline_device_is_generic_and_redacted(self) -> None:
        base_url = self.base_url
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

        with self.assertRaises(CrossPointUnavailableError) as raised:
            CrossPointClient(base_url, timeout=0.2).get_status()

        self.assertNotIn(base_url, str(raised.exception))

    def test_bad_directory_response_does_not_echo_server_body(self) -> None:
        CrossPointFixtureHandler.directory_entries = [
            {"name": "synthetic/private/body", "size": 1, "isDirectory": False}
        ]

        with self.assertRaises(CrossPointMalformedResponseError) as raised:
            self.client().list_directory()

        self.assertNotIn("synthetic/private/body", str(raised.exception))


class CrossPointBoundsAndFilenameTests(CrossPointFixture):
    def test_oversized_success_response_is_rejected_without_body(self) -> None:
        CrossPointFixtureHandler.status_body = {"device": "X3"}
        huge = b"synthetic oversized response" * 10
        with mock.patch(
            "goodlinks_crosspoint.crosspoint.MAX_STATUS_RESPONSE_BYTES", 10
        ):
            CrossPointFixtureHandler.status_body = huge.decode("ascii")
            with self.assertRaises(CrossPointResponseTooLargeError) as raised:
                self.client().get_status()
        self.assertNotIn("synthetic oversized response", str(raised.exception))

    def test_filename_constraints_are_checked_before_any_request(self) -> None:
        invalid_names = (
            "synthetic,book.epub",
            "synthetic\x01book.epub",
            "synthetic\u200bbook.epub",
            "synthetic\\book.epub",
            "synthetic-book.pdf",
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, filename in enumerate(invalid_names):
                with self.subTest(filename=filename):
                    source = Path(temporary) / filename
                    source.write_bytes(b"synthetic local epub")
                    with self.assertRaises(InvalidEPUBError):
                        self.client().upload_epub(source)
        self.assertEqual(CrossPointFixtureHandler.requests, [])

    def test_remote_path_rejects_format_characters_without_request(self) -> None:
        with self.assertRaises(InvalidRemotePathError):
            self.client().list_directory("/Good\u200bLinks")
        self.assertEqual(CrossPointFixtureHandler.requests, [])

    def test_status_requires_a_device_marker_but_not_optional_fields(self) -> None:
        for payload in ({}, {"device": None}, {"device": 4}):
            with self.subTest(payload=payload):
                CrossPointFixtureHandler.status_body = payload
                with self.assertRaises(WrongDeviceError):
                    self.client().get_status()
                CrossPointFixtureHandler.requests.clear()


if __name__ == "__main__":
    import unittest

    unittest.main()
