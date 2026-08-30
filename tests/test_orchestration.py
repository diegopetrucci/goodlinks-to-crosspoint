from __future__ import annotations

import fcntl
import json
import os
import socketserver
import stat
import sys
import tempfile
import threading
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from goodlinks_crosspoint.api import (
    APIUnavailableError,
    FetchedArticle,
    GoodLinksClient,
)
from goodlinks_crosspoint.crosspoint import CrossPointClient
from goodlinks_crosspoint.epub import safe_epub_filename
from goodlinks_crosspoint.orchestration import (
    ManifestLockError,
    Orchestrator,
    WorkflowInputError,
    article_content_hash,
    conversion_config_hash,
    manifest_lock_path,
    manifest_path,
)


class SyntheticGoodLinksHandler(BaseHTTPRequestHandler):
    token = "synthetic-orchestration-token"
    requests: ClassVar[list[tuple[str, str | None]]]

    @classmethod
    def metadata(cls, article_id: str) -> dict[str, object]:
        return {
            "id": article_id,
            "title": f"Synthetic {article_id}",
            "author": "Synthetic author",
            "url": f"https://example.com/{article_id}",
            "tags": ["x3"],
        }

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append((parsed.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._send(401, b'{"error":"synthetic unauthorized"}', "application/json")
            return
        if parsed.path == "/api/v1/links":
            body = json.dumps(
                {
                    "data": [self.metadata("one"), self.metadata("two")],
                    "hasMore": False,
                }
            ).encode("utf-8")
            self._send(200, body, "application/json")
            return
        if parsed.path.endswith("/content"):
            article_id = parsed.path.split("/")[-2]
            self._send(
                200,
                f"<p>synthetic body {article_id}</p>".encode(),
                "text/html; charset=utf-8",
            )
            return
        self._send(404, b'{"error":"synthetic not found"}', "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class SyntheticDeviceHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[str]]
    uploaded: ClassVar[list[str]]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        self.__class__.requests.append(self.command)
        if parsed.path == "/api/status":
            self._send(200, b'{"device":"X3"}', "application/json")
        elif parsed.path == "/api/files":
            entries = [
                {
                    "name": name,
                    "size": 1,
                    "isDirectory": False,
                    "isEpub": True,
                }
                for name in self.__class__.uploaded
            ]
            self._send(200, json.dumps(entries).encode("utf-8"), "application/json")
        else:
            self._send(404, b"synthetic missing", "text/plain")

    def do_POST(self) -> None:
        self.__class__.requests.append(self.command)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path.startswith("/upload"):
            marker = b'filename="'
            start = body.index(marker) + len(marker)
            end = body.index(b'"', start)
            name = body[start:end].decode("utf-8")
            self.__class__.uploaded.append(name)
            self._send(
                200,
                f"File uploaded successfully: {name}".encode(),
                "text/plain",
            )
        elif self.path == "/mkdir":
            self._send(200, b"Folder created", "text/plain")
        else:
            self._send(404, b"synthetic missing", "text/plain")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class FakeGoodLinks:
    def __init__(self, articles: list[FetchedArticle]) -> None:
        self.articles = articles
        self.list_tags: list[str] = []
        self.fetched: list[str] = []

    def list_articles(self, tag: str) -> list[dict[str, object]]:
        self.list_tags.append(tag)
        return [dict(article.metadata) for article in self.articles]

    def fetch_article(self, metadata: dict[str, object]) -> FetchedArticle:
        article_id = str(metadata["id"])
        self.fetched.append(article_id)
        return next(article for article in self.articles if article.id == article_id)


class FakeCrossPoint:
    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str, bool]] = []
        self.fail_uploads = 0

    def upload_epub(
        self,
        path: Path,
        *,
        remote_directory: str,
        overwrite: bool,
    ) -> str:
        self.uploads.append((path, remote_directory, overwrite))
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError()
        return f"{remote_directory}/{path.name}"


class OrchestrationTests(TestCase):
    @staticmethod
    def article(article_id: str, body: str = "synthetic body") -> FetchedArticle:
        return FetchedArticle(
            metadata={
                "id": article_id,
                "title": f"Synthetic {article_id}",
                "author": "Synthetic author",
                "url": f"https://example.com/{article_id}",
                "tags": ["x3"],
            },
            html=f"<p>{body}</p>",
        )

    @staticmethod
    def fake_pandoc(directory: Path) -> Path:
        script = directory / "fake-pandoc.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys, zipfile\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    raise SystemExit(0)\n"
            "output = pathlib.Path(args[args.index('--output') + 1])\n"
            "source = pathlib.Path(args[-1]).read_text(encoding='utf-8')\n"
            "if 'synthetic-failure-marker' in source:\n"
            "    raise SystemExit(7)\n"
            "with zipfile.ZipFile(output, 'w') as archive:\n"
            "    archive.writestr('mimetype', 'application/epub+zip')\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_dry_run_reads_articles_without_pandoc_output_or_manifest_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "not-created"
            goodlinks = FakeGoodLinks([self.article("one")])
            device = FakeCrossPoint()

            result = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,  # dry-run must not call it
                dry_run=True,
            ).run_sync()

            self.assertEqual(result.planned_generation, 1)
            self.assertEqual(result.planned_upload, 1)
            self.assertEqual(result.failed, 0)
            self.assertEqual(goodlinks.list_tags, ["x3"])
            self.assertEqual(device.uploads, [])
            self.assertFalse(output.exists())
            self.assertFalse(manifest_path(output).exists())

    def test_export_sync_deduplicate_and_force_controls_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            goodlinks = FakeGoodLinks([self.article("one")])
            device = FakeCrossPoint()

            exported = Orchestrator(
                goodlinks, output, pandoc_executable=fake
            ).run_export()
            custom_destination_export = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                remote_directory="/Other",
            ).run_export()
            synced = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            repeated = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            forced = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
                force=True,
            ).run_sync()

            self.assertEqual(exported.generated, 1)
            self.assertEqual(custom_destination_export.generation_skipped, 1)
            self.assertEqual(synced.generation_skipped, 1)
            self.assertEqual(synced.uploaded, 1)
            self.assertEqual(repeated.upload_skipped, 1)
            self.assertEqual(forced.generated, 1)
            self.assertEqual(forced.uploaded, 1)
            self.assertEqual(len(device.uploads), 2)
            self.assertFalse(device.uploads[0][2])
            self.assertTrue(device.uploads[1][2])

            manifest = json.loads(manifest_path(output).read_text(encoding="utf-8"))
            entry = manifest["articles"]["one"]
            self.assertTrue(entry["generated"])
            self.assertTrue(entry["uploaded"])
            self.assertEqual(set(entry), {
                "id",
                "content_hash",
                "config_hash",
                "filename",
                "output_hash",
                "generated",
                "uploaded",
                "remote_path",
            })
            manifest_text = manifest_path(output).read_text(encoding="utf-8")
            self.assertNotIn("synthetic body", manifest_text)
            self.assertNotIn("Synthetic author", manifest_text)
            self.assertNotIn("https://example.com/one", manifest_text)
            self.assertEqual(stat.S_IMODE(manifest_path(output).stat().st_mode), 0o600)
            with zipfile.ZipFile(output / entry["filename"]) as archive:
                self.assertEqual(archive.read("mimetype"), b"application/epub+zip")

    def test_bookkeeping_changes_do_not_regenerate_but_output_inputs_do(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            original = self.article("one")
            Orchestrator(
                FakeGoodLinks([original]), output, pandoc_executable=fake
            ).run_export()

            bookkeeping = self.article("one")
            bookkeeping.metadata.update(
                {
                    "tags": ["x3", "changed-bookkeeping"],
                    "readAt": "2025-01-01T00:00:00Z",
                    "modifiedAt": "2025-01-02T00:00:00Z",
                    "starred": True,
                }
            )
            self.assertEqual(article_content_hash(original), article_content_hash(bookkeeping))
            skipped = Orchestrator(
                FakeGoodLinks([bookkeeping]), output, pandoc_executable=fake
            ).run_export()
            self.assertEqual(skipped.generation_skipped, 1)
            self.assertEqual(
                conversion_config_hash(fake), conversion_config_hash(fake)
            )

            for field, value in (
                ("title", "Synthetic changed title"),
                ("author", "Changed synthetic author"),
                ("url", "https://example.com/changed"),
            ):
                changed = self.article("one")
                changed.metadata[field] = value
                regenerated = Orchestrator(
                    FakeGoodLinks([changed]), output, pandoc_executable=fake
                ).run_export()
                self.assertEqual(regenerated.generated, 1)

            changed_html = self.article("one", "changed synthetic body")
            regenerated = Orchestrator(
                FakeGoodLinks([changed_html]), output, pandoc_executable=fake
            ).run_export()
            self.assertEqual(regenerated.generated, 1)

    def test_fetch_failure_leaves_existing_manifest_entry_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            original = self.article("one")
            Orchestrator(
                FakeGoodLinks([original]), output, pandoc_executable=fake
            ).run_export()
            before = manifest_path(output).read_text(encoding="utf-8")

            class FailingFetch(FakeGoodLinks):
                def fetch_article(self, _metadata: dict[str, object]) -> FetchedArticle:
                    raise APIUnavailableError()

            result = Orchestrator(
                FailingFetch([original]), output, pandoc_executable=fake
            ).run_export()

            self.assertEqual(result.failed, 1)
            self.assertEqual(manifest_path(output).read_text(encoding="utf-8"), before)

    def test_changed_content_can_replace_a_remote_file_it_previously_owned(self) -> None:
        SyntheticDeviceHandler.requests = []
        SyntheticDeviceHandler.uploaded = []
        device_server = LocalServer(("127.0.0.1", 0), SyntheticDeviceHandler)
        device_thread = threading.Thread(
            target=device_server.serve_forever, daemon=True
        )
        device_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = self.fake_pandoc(root)
                output = root / "output"
                first = self.article("one", "first synthetic body")
                goodlinks = FakeGoodLinks([first])
                device = CrossPointClient(
                    f"http://127.0.0.1:{device_server.server_port}"
                )
                initial = Orchestrator(
                    goodlinks,
                    output,
                    pandoc_executable=fake,
                    crosspoint=device,
                ).run_sync()
                goodlinks.articles[0] = self.article("one", "changed synthetic body")
                changed = Orchestrator(
                    goodlinks,
                    output,
                    pandoc_executable=fake,
                    crosspoint=device,
                ).run_sync()

                self.assertEqual(initial.uploaded, 1)
                self.assertEqual(changed.generated, 1)
                self.assertEqual(changed.uploaded, 1)
                self.assertEqual(changed.failed, 0)
                self.assertEqual(len(SyntheticDeviceHandler.uploaded), 2)

                # A same-name remote file without a matching prior manifest
                # owner must retain CrossPoint's default refusal.
                foreign_output = root / "foreign-output"
                SyntheticDeviceHandler.uploaded = [safe_epub_filename(goodlinks.articles[0])]
                refused = Orchestrator(
                    goodlinks,
                    foreign_output,
                    pandoc_executable=fake,
                    crosspoint=device,
                ).run_sync()
                self.assertEqual(refused.generated, 1)
                self.assertEqual(refused.uploaded, 0)
                self.assertEqual(refused.failed, 1)
                self.assertEqual(len(SyntheticDeviceHandler.uploaded), 1)
                foreign_manifest = json.loads(
                    manifest_path(foreign_output).read_text(encoding="utf-8")
                )
                self.assertNotIn(
                    "owned_remote_path", foreign_manifest["articles"]["one"]
                )
        finally:
            device_server.shutdown()
            device_server.server_close()
            device_thread.join(timeout=5)

    def test_changed_content_pandoc_failure_can_retry_owned_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            goodlinks = FakeGoodLinks([self.article("one", "initial body")])
            device = FakeCrossPoint()
            initial = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            self.assertEqual(initial.uploaded, 1)

            goodlinks.articles[0] = self.article("one", "synthetic-failure-marker")
            failed = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            pending = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertEqual(failed.failed, 1)
            self.assertFalse(pending["generated"])
            self.assertFalse(pending["uploaded"])
            self.assertEqual(
                pending["owned_remote_path"], f"/GoodLinks/{pending['filename']}"
            )
            self.assertNotIn("remote_path", pending)

            goodlinks.articles[0] = self.article("one", "recovered body")
            retried = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            self.assertEqual(retried.generated, 1)
            self.assertEqual(retried.uploaded, 1)
            self.assertTrue(device.uploads[-1][2])
            complete = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertNotIn("owned_remote_path", complete)
            self.assertEqual(complete["remote_path"], f"/GoodLinks/{complete['filename']}")

    def test_changed_content_upload_failure_can_retry_owned_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            goodlinks = FakeGoodLinks([self.article("one", "initial body")])
            device = FakeCrossPoint()
            Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()

            goodlinks.articles[0] = self.article("one", "changed body")
            device.fail_uploads = 1
            failed = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            pending = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertEqual(failed.generated, 1)
            self.assertEqual(failed.uploaded, 0)
            self.assertTrue(pending["generated"])
            self.assertFalse(pending["uploaded"])
            self.assertEqual(
                pending["owned_remote_path"], f"/GoodLinks/{pending['filename']}"
            )
            self.assertNotIn("remote_path", pending)

            retried = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            self.assertEqual(retried.generated, 0)
            self.assertEqual(retried.uploaded, 1)
            self.assertTrue(device.uploads[-1][2])
            complete = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertNotIn("owned_remote_path", complete)
            self.assertTrue(complete["uploaded"])

    def test_changed_destination_upload_failure_preserves_owned_path_for_retry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            article = self.article("one")
            goodlinks = FakeGoodLinks([article])
            device = FakeCrossPoint()
            Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()

            filename = safe_epub_filename(article)
            device.fail_uploads = 1
            failed = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                remote_directory="/Other",
                crosspoint=device,
            ).run_sync()
            pending = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertEqual(failed.failed, 1)
            self.assertEqual(failed.generated, 0)
            self.assertFalse(pending["uploaded"])
            self.assertEqual(pending["owned_remote_path"], f"/GoodLinks/{filename}")
            self.assertNotIn("remote_path", pending)

            retried = Orchestrator(
                goodlinks,
                output,
                pandoc_executable=fake,
                remote_directory="/GoodLinks",
                crosspoint=device,
            ).run_sync()
            self.assertEqual(retried.failed, 0)
            self.assertEqual(retried.uploaded, 1)
            self.assertTrue(device.uploads[-1][2])
            complete = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertEqual(complete["remote_path"], f"/GoodLinks/{filename}")
            self.assertNotIn("owned_remote_path", complete)

    def test_deleted_owned_epub_regenerates_and_overwrites_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            article = self.article("one")
            device = FakeCrossPoint()
            Orchestrator(
                FakeGoodLinks([article]),
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            filename = safe_epub_filename(article)
            (output / filename).unlink()

            retried = Orchestrator(
                FakeGoodLinks([article]),
                output,
                pandoc_executable=fake,
                crosspoint=device,
            ).run_sync()
            self.assertEqual(retried.generated, 1)
            self.assertEqual(retried.uploaded, 1)
            self.assertTrue(device.uploads[-1][2])
            complete = json.loads(manifest_path(output).read_text(encoding="utf-8"))[
                "articles"
            ]["one"]
            self.assertNotIn("owned_remote_path", complete)

    def test_pandoc_timeout_is_validated_before_any_pandoc_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(
            WorkflowInputError
        ):
            Orchestrator(
                FakeGoodLinks([self.article("one")]),
                Path(temporary) / "output",
                pandoc_timeout=0,
            )

    def test_manifest_lock_is_nonblocking_and_released_after_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "output"
            lock_path = manifest_lock_path(output)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = lock_path.open("w+b")
            try:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(ManifestLockError):
                    Orchestrator(
                        FakeGoodLinks([self.article("one")]),
                        output,
                        pandoc_executable=root / "missing-pandoc",
                    ).run_export()
            finally:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
                descriptor.close()

            fake = self.fake_pandoc(root)
            result = Orchestrator(
                FakeGoodLinks([self.article("one")]),
                output,
                pandoc_executable=fake,
            ).run_export()
            self.assertEqual(result.generated, 1)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_failed_item_is_not_marked_complete_and_prior_item_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output = root / "output"
            goodlinks = FakeGoodLinks(
                [self.article("one"), self.article("two", "synthetic-failure-marker")]
            )

            result = Orchestrator(
                goodlinks, output, pandoc_executable=fake
            ).run_export()

            self.assertEqual(result.generated, 1)
            self.assertEqual(result.failed, 1)
            manifest = json.loads(manifest_path(output).read_text(encoding="utf-8"))
            self.assertTrue(manifest["articles"]["one"]["generated"])
            self.assertFalse(manifest["articles"]["two"]["generated"])
            self.assertFalse(manifest["articles"]["two"]["uploaded"])
            self.assertNotIn("synthetic-failure-marker", manifest_path(output).read_text())

    def test_end_to_end_uses_only_synthetic_servers_and_fake_pandoc(self) -> None:
        SyntheticGoodLinksHandler.requests = []
        SyntheticDeviceHandler.requests = []
        SyntheticDeviceHandler.uploaded = []
        api_server = LocalServer(("127.0.0.1", 0), SyntheticGoodLinksHandler)
        device_server = LocalServer(("127.0.0.1", 0), SyntheticDeviceHandler)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        device_thread = threading.Thread(
            target=device_server.serve_forever, daemon=True
        )
        api_thread.start()
        device_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake = self.fake_pandoc(root)
                output = root / "output"
                api_url = f"http://127.0.0.1:{api_server.server_port}/api/v1"
                device_url = f"http://127.0.0.1:{device_server.server_port}"
                with mock.patch.dict(
                    os.environ,
                    {"GOODLINKS_TOKEN": SyntheticGoodLinksHandler.token},
                    clear=False,
                ):
                    first = Orchestrator(
                        GoodLinksClient(api_url),
                        output,
                        pandoc_executable=fake,
                        crosspoint=CrossPointClient(device_url),
                    ).run_sync()
                    second = Orchestrator(
                        GoodLinksClient(api_url),
                        output,
                        pandoc_executable=fake,
                        crosspoint=CrossPointClient(device_url),
                    ).run_sync()

                self.assertEqual(first.generated, 2)
                self.assertEqual(first.uploaded, 2)
                self.assertEqual(second.generation_skipped, 2)
                self.assertEqual(second.upload_skipped, 2)
                self.assertEqual(len(SyntheticDeviceHandler.uploaded), 2)
                self.assertTrue(
                    all(path.startswith("/api/v1/") for path, _auth in SyntheticGoodLinksHandler.requests)
                )
                self.assertIn("POST", SyntheticDeviceHandler.requests)
                manifest_text = manifest_path(output).read_text(encoding="utf-8")
                self.assertNotIn("synthetic body", manifest_text)
                self.assertNotIn("Synthetic author", manifest_text)
        finally:
            api_server.shutdown()
            device_server.shutdown()
            api_server.server_close()
            device_server.server_close()
            api_thread.join(timeout=5)
            device_thread.join(timeout=5)


if __name__ == "__main__":
    import unittest

    unittest.main()
