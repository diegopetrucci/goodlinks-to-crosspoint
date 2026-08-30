from __future__ import annotations

import json
import os
import shutil
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]


class CliGoodLinksHandler(BaseHTTPRequestHandler):
    token = "synthetic-cli-token"
    requests: ClassVar[list[dict[str, str | None]]]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append(
            {
                "path": parsed.path,
                "method": self.command,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send(401, b'{"error":"synthetic-server-body"}', "application/json")
            return
        if parsed.path == "/api/v1/links":
            body = json.dumps(
                {
                    "data": [
                        {
                            "id": "one",
                            "title": "synthetic-cli-title",
                            "author": "synthetic-cli-author",
                            "url": "https://example.com/synthetic-cli",
                            "tags": ["x3"],
                        }
                    ],
                    "hasMore": False,
                }
            ).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if parsed.path == "/api/v1/links/one/content":
            self._send(
                200,
                b"<p>synthetic-cli-article-body</p>",
                "text/html; charset=utf-8",
            )
            return
        self._send(404, b'{"error":"synthetic-server-body"}', "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class CliDeviceHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]]
    uploads: ClassVar[list[str]]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append(self.command)
        if parsed.path == "/api/status":
            self._send(200, b'{"device":"X3"}', "application/json")
            return
        if parsed.path == "/api/files":
            entries = [
                {"name": name, "size": 1, "isDirectory": False, "isEpub": True}
                for name in self.__class__.uploads
            ]
            self._send(200, json.dumps(entries).encode("utf-8"), "application/json")
            return
        self._send(404, b"synthetic-device-body", "text/plain")

    def do_POST(self) -> None:
        self.__class__.requests.append(self.command)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if not self.path.startswith("/upload"):
            self._send(404, b"synthetic-device-body", "text/plain")
            return
        marker = b'filename="'
        start = body.index(marker) + len(marker)
        end = body.index(b'"', start)
        filename = body[start:end].decode("utf-8")
        self.__class__.uploads.append(filename)
        self._send(
            200,
            f"File uploaded successfully: {filename}".encode(),
            "text/plain",
        )

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class CliLocalServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class CliTests(unittest.TestCase):
    def run_cli(
        self,
        *args: str,
        environment_overrides: dict[str, str | None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (source, environment.get("PYTHONPATH", "")) if part
        )
        for key, value in (environment_overrides or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return subprocess.run(
            [sys.executable, "-m", "goodlinks_crosspoint", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_help_is_available(self) -> None:
        result = self.run_cli("--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "Privacy-safe GoodLinks-to-CrossPoint CLI foundation.", result.stdout
        )
        self.assertEqual(result.stderr, "")

    def test_version_is_available(self) -> None:
        result = self.run_cli("--version")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "goodlinks-crosspoint 0.1.0\n")
        self.assertEqual(result.stderr, "")

    def test_credential_like_arguments_are_rejected_without_echoing_values(
        self,
    ) -> None:
        result = self.run_cli("--token", "synthetic-example-token")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: goodlinks-crosspoint", result.stderr)
        self.assertIn("invalid command-line arguments", result.stderr)
        self.assertNotIn("synthetic-example-token", result.stdout + result.stderr)

    def test_private_artifact_paths_are_ignored(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git executable unavailable")

        ignored_paths = (
            "GoodLinks.sqlite",
            "GoodLinks.sqlite-wal",
            "GoodLinks.json",
            "GoodLinks.csv",
            "goodlinks-export.json",
            "goodlinks-export.csv",
            ".env.local",
            "api-keys.json",
            "article.epub",
            "exports/article.html",
            "article.manifest.json",
            "article.manifest.lock",
            "article.state.json",
            ".gnosis/entries.jsonl",
            ".tickets/gtc-l55l.md",
            "logs/run.log",
            "secrets/service.secret",
            "secret.key",
            "tokens/access.token",
            "tokens.json",
            "access_token.txt",
            "config.json",
            "config.local.json",
        )
        for path in ignored_paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    [
                        git,
                        "-C",
                        str(ROOT),
                        "check-ignore",
                        "--no-index",
                        "-q",
                        "--",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"expected {path!r} to be ignored: {result.stderr}",
                )

        safe_fixture_paths = (
            "tests/fixtures/example.com/goodlinks-fixture.json",
            "tests/fixtures/example.com/goodlinks-fixture.csv",
            "examples/example.com/config.json",
        )
        for path in safe_fixture_paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    [
                        git,
                        "-C",
                        str(ROOT),
                        "check-ignore",
                        "--no-index",
                        "-q",
                        "--",
                        path,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    1,
                    f"explicit synthetic fixture path unexpectedly ignored: {path!r}",
                )


class CliWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        CliGoodLinksHandler.requests = []
        CliDeviceHandler.requests = []
        CliDeviceHandler.uploads = []
        cls.api_server = CliLocalServer(("127.0.0.1", 0), CliGoodLinksHandler)
        cls.device_server = CliLocalServer(("127.0.0.1", 0), CliDeviceHandler)
        cls.api_thread = threading.Thread(
            target=cls.api_server.serve_forever, daemon=True
        )
        cls.device_thread = threading.Thread(
            target=cls.device_server.serve_forever, daemon=True
        )
        cls.api_thread.start()
        cls.device_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.api_server.shutdown()
        cls.device_server.shutdown()
        cls.api_server.server_close()
        cls.device_server.server_close()
        cls.api_thread.join(timeout=5)
        cls.device_thread.join(timeout=5)

    def setUp(self) -> None:
        CliGoodLinksHandler.requests.clear()
        CliDeviceHandler.requests.clear()
        CliDeviceHandler.uploads.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.fake_pandoc = self.root / "fake-pandoc.py"
        log = self.root / "pandoc-invocations.log"
        self.fake_pandoc.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys, zipfile\n"
            f"log = pathlib.Path({str(log)!r})\n"
            "args = sys.argv[1:]\n"
            "with log.open('a', encoding='utf-8') as stream:\n"
            "    stream.write('invocation\\n')\n"
            "if args == ['--version']:\n"
            "    raise SystemExit(0)\n"
            "output = pathlib.Path(args[args.index('--output') + 1])\n"
            "with zipfile.ZipFile(output, 'w') as archive:\n"
            "    archive.writestr('mimetype', 'application/epub+zip')\n",
            encoding="utf-8",
        )
        self.fake_pandoc.chmod(self.fake_pandoc.stat().st_mode | stat.S_IXUSR)
        self.api_url = f"http://127.0.0.1:{self.api_server.server_port}/api/v1"
        self.device_url = f"http://127.0.0.1:{self.device_server.server_port}"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(
        self, *args: str, token: str | None = CliGoodLinksHandler.token
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            part for part in (source, environment.get("PYTHONPATH", "")) if part
        )
        if token is None:
            environment.pop("GOODLINKS_TOKEN", None)
        else:
            environment["GOODLINKS_TOKEN"] = token
        return subprocess.run(
            [sys.executable, "-m", "goodlinks_crosspoint", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def source_args(self) -> tuple[str, ...]:
        return (
            "--api-url",
            self.api_url,
            "--output-dir",
            str(self.output),
            "--pandoc-executable",
            str(self.fake_pandoc),
        )

    @staticmethod
    def assert_redacted(result: subprocess.CompletedProcess[str]) -> None:
        combined = result.stdout + result.stderr
        for value in (
            "synthetic-cli-article-body",
            "synthetic-cli-author",
            "https://example.com/synthetic-cli",
            CliGoodLinksHandler.token,
            "synthetic-server-body",
            "synthetic-device-body",
        ):
            assert value not in combined

    def test_export_dry_run_has_no_unintended_side_effects(self) -> None:
        result = self.run_cli("export", "--dry-run", *self.source_args())

        self.assertEqual(result.returncode, 0)
        self.assertIn("planned_generation=1", result.stdout)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(f"{self.output}.manifest.json").exists())
        self.assertFalse((self.root / "pandoc-invocations.log").exists())
        self.assertEqual(CliDeviceHandler.requests, [])
        self.assert_redacted(result)

    def test_export_generates_without_contacting_device(self) -> None:
        result = self.run_cli("export", *self.source_args())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(list(self.output.glob("*.epub"))), 1)
        self.assertTrue(Path(f"{self.output}.manifest.json").is_file())
        self.assertEqual(CliDeviceHandler.requests, [])
        self.assertTrue(CliGoodLinksHandler.requests)
        self.assertTrue(
            all(request["method"] == "GET" for request in CliGoodLinksHandler.requests)
        )
        self.assert_redacted(result)

    def test_sync_dry_run_does_not_contact_device_or_pandoc(self) -> None:
        result = self.run_cli(
            "sync",
            "--dry-run",
            *self.source_args(),
            "--device-url",
            self.device_url,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("planned_generation=1", result.stdout)
        self.assertIn("planned_upload=1", result.stdout)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(f"{self.output}.manifest.json").exists())
        self.assertFalse((self.root / "pandoc-invocations.log").exists())
        self.assertEqual(CliDeviceHandler.requests, [])
        self.assert_redacted(result)

    def test_send_uploads_one_existing_epub_without_goodlinks(self) -> None:
        source = self.root / "existing.epub"
        source.write_bytes(b"synthetic-existing-epub")

        result = self.run_cli(
            "send", str(source), "--device-url", self.device_url, token=None
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "send: uploaded=1 failed=0\n")
        self.assertEqual(len(CliDeviceHandler.uploads), 1)
        self.assertEqual(CliGoodLinksHandler.requests, [])
        self.assertFalse(Path(f"{source}.manifest.json").exists())
        self.assert_redacted(result)

    def test_send_overwrite_is_explicit(self) -> None:
        source = self.root / "existing.epub"
        source.write_bytes(b"synthetic-existing-epub")
        first = self.run_cli(
            "send", str(source), "--device-url", self.device_url, token=None
        )
        refused = self.run_cli(
            "send", str(source), "--device-url", self.device_url, token=None
        )
        replaced = self.run_cli(
            "send",
            str(source),
            "--device-url",
            self.device_url,
            "--overwrite",
            token=None,
        )

        self.assertEqual(first.returncode, 0)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(replaced.returncode, 0)
        self.assertEqual(len(CliDeviceHandler.uploads), 2)
        self.assert_redacted(refused)
        self.assert_redacted(replaced)

    def test_sync_exports_and_uploads_then_deduplicates(self) -> None:
        first = self.run_cli(
            "sync", *self.source_args(), "--device-url", self.device_url
        )
        device_requests_after_first = len(CliDeviceHandler.requests)
        second = self.run_cli(
            "sync", *self.source_args(), "--device-url", self.device_url
        )

        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertIn("uploaded=1", first.stdout)
        self.assertIn("upload_skipped=1", second.stdout)
        self.assertEqual(len(CliDeviceHandler.uploads), 1)
        self.assertEqual(len(CliDeviceHandler.requests), device_requests_after_first)
        self.assertTrue(list(self.output.glob("*.epub")))
        self.assert_redacted(first)
        self.assert_redacted(second)
