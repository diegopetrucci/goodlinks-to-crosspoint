from __future__ import annotations

import errno
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import goodlinks_crosspoint.epub as epub_module
from goodlinks_crosspoint.api import FetchedArticle
from goodlinks_crosspoint.epub import (
    CROSSPOINT_CSS,
    EpubOutputError,
    PandocEpubExporter,
    PandocInvocationError,
    PandocNotFoundError,
    PandocVersionError,
    _sanitize_html,
    safe_epub_filename,
)


class EpubTests(unittest.TestCase):
    def article(
        self,
        *,
        article_id: str = "synthetic-one",
        title: str | None = "Synthetic, title",
        author: str | None = "Synthetic author",
        url: str | None = "https://example.com/synthetic-one",
        html: str = "<p>Synthetic article body.</p>",
    ) -> FetchedArticle:
        metadata: dict[str, object] = {
            "id": article_id,
            "title": title,
            "author": author,
            "url": url,
        }
        return FetchedArticle(metadata=metadata, html=html)

    @staticmethod
    def fake_pandoc(directory: Path, *, failing: bool = False) -> Path:
        log_path = directory / "invocations.jsonl"
        mode = "fail" if failing else "ok"
        script = directory / "fake-pandoc.py"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import pathlib\n"
            "import sys\n"
            "import zipfile\n"
            f"log = pathlib.Path({str(log_path)!r})\n"
            "args = sys.argv[1:]\n"
            "with log.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({'args': args}) + '\\n')\n"
            "if args == ['--version']:\n"
            "    print('pandoc 0.0-fake')\n"
            "    raise SystemExit(0)\n"
            f"if {mode!r} == 'fail':\n"
            "    sys.stderr.write('synthetic article body and metadata\\n')\n"
            "    raise SystemExit(7)\n"
            "def option(name):\n"
            "    index = args.index(name)\n"
            "    return pathlib.Path(args[index + 1])\n"
            "metadata = option('--metadata-file').read_text(encoding='utf-8')\n"
            "source = option('--output')\n"
            "input_document = pathlib.Path(args[-1]).read_text(encoding='utf-8')\n"
            "stylesheet = option('--css').read_text(encoding='utf-8')\n"
            "with zipfile.ZipFile(source, 'w') as archive:\n"
            "    archive.writestr('mimetype', 'application/epub+zip')\n"
            "    archive.writestr('EPUB/content.xhtml', input_document)\n"
            "    archive.writestr('EPUB/metadata.json', metadata)\n"
            "    archive.writestr('EPUB/styles.css', stylesheet)\n",
            encoding="utf-8",
        )
        # The fake is intentionally an executable file so the test exercises
        # the same PATH/exec boundary as a real Pandoc binary.
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script

    def test_exports_one_minimal_epub_with_metadata_and_cleaned_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output_dir = root / "generated"
            article = self.article(
                article_id="synthetic/one,\x01",
                title="A, safe/filename\x02",
                html=(
                    "<p>Synthetic article body.</p>"
                    "<script>synthetic-javascript()</script>"
                    '<p><a href="javascript:synthetic()">Link</a></p>'
                ),
            )

            output = PandocEpubExporter(fake).export_article(article, output_dir)

            self.assertEqual(output.parent, output_dir)
            self.assertEqual(output.suffix, ".epub")
            self.assertEqual(output.name, safe_epub_filename(article))
            self.assertNotIn(",", output.name)
            self.assertNotIn("/", output.name)
            self.assertNotIn("\\", output.name)
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                content = archive.read("EPUB/content.xhtml").decode("utf-8")
                metadata = json.loads(
                    archive.read("EPUB/metadata.json").decode("utf-8")
                )
                stylesheet = archive.read("EPUB/styles.css").decode("utf-8")

            self.assertEqual(metadata["title"], "A, safe/filename")
            self.assertEqual(metadata["author"], "Synthetic author")
            self.assertEqual(metadata["source"], "https://example.com/synthetic-one")
            self.assertTrue(metadata["identifier"].startswith("goodlinks-"))
            self.assertNotIn("\x01", metadata["identifier"])
            self.assertIn("Synthetic article body.", content)
            self.assertIn("https://example.com/synthetic-one", content)
            self.assertNotIn("<script", content.lower())
            self.assertNotIn("javascript:", content.lower())
            self.assertNotIn("position:", stylesheet)
            self.assertNotIn("display: flex", stylesheet)
            self.assertNotIn("display: grid", stylesheet)

            invocations = [
                json.loads(line)
                for line in (root / "invocations.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(invocations), 2)  # version check + conversion
            conversion_args = invocations[1]["args"]
            self.assertIn("--from=html", conversion_args)
            self.assertIn("--to=epub3", conversion_args)
            self.assertEqual(conversion_args[-1].endswith("article.html"), True)
            self.assertTrue(
                all(isinstance(argument, str) for argument in conversion_args)
            )
            # The fake recorded the private workspace paths before cleanup.
            for option in ("--metadata-file", "--css", "--output"):
                path = Path(conversion_args[conversion_args.index(option) + 1])
                self.assertFalse(path.exists())

    def test_exports_multiple_articles_without_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            articles = [
                self.article(article_id="synthetic-one"),
                self.article(article_id="synthetic-two", title="Second synthetic"),
            ]

            outputs = PandocEpubExporter(fake).export_articles(
                articles, root / "output"
            )

            self.assertEqual(len(outputs), 2)
            self.assertEqual(len({path.name for path in outputs}), 2)
            self.assertTrue(all(path.is_file() for path in outputs))

    def test_staging_is_destination_local_and_cleaned_after_exdev(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output_dir = root / "output"
            article = self.article(article_id="synthetic-exdev")

            def cross_filesystem_replace(
                source: str | bytes, destination: str | bytes
            ) -> None:
                self.assertEqual(Path(source).parent, Path(destination).parent)
                raise OSError(errno.EXDEV, "synthetic cross-filesystem failure")

            with (
                mock.patch.object(
                    epub_module.os, "replace", side_effect=cross_filesystem_replace
                ),
                self.assertRaises(EpubOutputError),
            ):
                PandocEpubExporter(fake).export_article(article, output_dir)

            self.assertFalse(list(output_dir.glob("*.epub")))
            self.assertFalse(list(output_dir.glob("*.epub.tmp")))

    def test_original_ids_with_controls_or_surrogates_keep_distinct_safe_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            articles = [
                self.article(article_id="synthetic-id\x01", title="Same synthetic"),
                self.article(article_id="synthetic-id\x02", title="Same synthetic"),
                self.article(article_id="synthetic-id\ud800", title="Same synthetic"),
            ]

            outputs = PandocEpubExporter(fake).export_articles(
                articles, root / "output"
            )

            self.assertEqual(len({path.name for path in outputs}), len(outputs))
            identifiers: list[str] = []
            for output in outputs:
                with zipfile.ZipFile(output) as archive:
                    metadata = json.loads(
                        archive.read("EPUB/metadata.json").decode("utf-8")
                    )
                identifier = metadata["identifier"]
                identifiers.append(identifier)
                self.assertTrue(identifier.startswith("goodlinks-"))
                self.assertTrue(all(ord(character) >= 32 for character in identifier))
                self.assertNotIn("\ud800", identifier)
            self.assertEqual(len(set(identifiers)), len(identifiers))

    def test_void_blocked_media_tags_do_not_swallow_following_article_text(
        self,
    ) -> None:
        cases = (
            (
                (
                    "<video>synthetic video text<source src='synthetic-source'>"
                    "</video><p>after source start</p>"
                ),
                "after source start",
                "source",
            ),
            (
                (
                    "<video>synthetic video text<source src='synthetic-source' />"
                    "</video><p>after source self-closing</p>"
                ),
                "after source self-closing",
                "source",
            ),
            (
                (
                    "<video>synthetic video text<track src='synthetic-track'>"
                    "</video><p>after track start</p>"
                ),
                "after track start",
                "track",
            ),
            (
                (
                    "<video>synthetic video text<track src='synthetic-track' />"
                    "</video><p>after track self-closing</p>"
                ),
                "after track self-closing",
                "track",
            ),
            (
                "<embed src='synthetic-embed'><p>after embed</p>",
                "after embed",
                "embed",
            ),
            (
                "<embed src='synthetic-embed' /><p>after self-closing embed</p>",
                "after self-closing embed",
                "embed",
            ),
        )
        for fragment, following_text, blocked_tag in cases:
            with self.subTest(blocked_tag=blocked_tag, fragment=fragment):
                sanitized = _sanitize_html(fragment)
                self.assertIn(following_text, sanitized)
                self.assertNotIn("synthetic video text", sanitized)
                self.assertNotIn(f"<{blocked_tag}", sanitized.lower())
                self.assertNotIn(f"synthetic-{blocked_tag}", sanitized)

    def test_invalid_source_and_body_urls_are_omitted_without_write_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            article = self.article(
                url="https://example.com/synthetic-\ud800",
                html=(
                    '<p><a href="https://example.com/body-\ud800">'
                    "Synthetic link</a></p>"
                ),
            )

            output = PandocEpubExporter(fake).export_article(article, root / "output")

            with zipfile.ZipFile(output) as archive:
                content = archive.read("EPUB/content.xhtml").decode("utf-8")
                metadata = json.loads(
                    archive.read("EPUB/metadata.json").decode("utf-8")
                )
            self.assertNotIn("source", metadata)
            self.assertIn("Synthetic link", content)
            self.assertNotIn("href=", content)
            self.assertNotIn("\ud800", content)

    def test_batch_reservation_uses_casefolded_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            articles = [
                self.article(article_id="synthetic-case", title="Case title"),
                self.article(article_id="synthetic-case", title="case title"),
            ]

            outputs = PandocEpubExporter(fake).export_articles(
                articles, root / "output"
            )

            self.assertEqual(len(outputs), 2)
            self.assertNotEqual(outputs[0].name.casefold(), outputs[1].name.casefold())
            self.assertTrue(outputs[1].stem.endswith("-2"))

    def test_legacy_export_aliases_are_not_public(self) -> None:
        for name in ("EPUBError", "PandocError", "EPUBExporter", "EpubExporter"):
            self.assertFalse(hasattr(epub_module, name))
        self.assertFalse(hasattr(PandocEpubExporter, "export"))

    def test_pandoc_failure_is_actionable_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root, failing=True)
            article = self.article(
                article_id="synthetic-sensitive-id",
                title="synthetic-sensitive-title",
                author="synthetic-sensitive-author",
                html="<p>synthetic-sensitive-body</p>",
            )

            with self.assertRaises(PandocInvocationError) as raised:
                PandocEpubExporter(fake).export_article(article, root / "output")

            message = str(raised.exception)
            for value in (
                "synthetic-sensitive-id",
                "synthetic-sensitive-title",
                "synthetic-sensitive-author",
                "synthetic-sensitive-body",
            ):
                self.assertNotIn(value, message)
            self.assertIn("Pandoc", message)
            self.assertIn("permissions", message)
            self.assertFalse(list((root / "output").glob("*.epub")))

    def test_missing_and_bad_version_pandoc_are_actionable(self) -> None:
        article = self.article()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(PandocNotFoundError) as missing:
                PandocEpubExporter(root / "does-not-exist").export_article(
                    article, root / "output"
                )
            self.assertIn("Install Pandoc", str(missing.exception))

            bad_version = root / "bad-pandoc"
            bad_version.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('synthetic article body')\n"
                "raise SystemExit(2)\n",
                encoding="utf-8",
            )
            bad_version.chmod(bad_version.stat().st_mode | stat.S_IXUSR)
            with self.assertRaises(PandocVersionError) as version:
                PandocEpubExporter(bad_version).export_article(article, root / "output")
            self.assertNotIn("synthetic article body", str(version.exception))
            self.assertIn("version", str(version.exception))

    def test_output_directory_failures_do_not_echo_path_or_article(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = self.fake_pandoc(root)
            output_file = root / "not-a-directory"
            output_file.write_text("synthetic", encoding="utf-8")
            article = self.article(
                title="synthetic title", html="<p>synthetic body</p>"
            )
            with self.assertRaises(EpubOutputError) as raised:
                PandocEpubExporter(fake).export_article(article, output_file)
            self.assertNotIn(str(output_file), str(raised.exception))
            self.assertNotIn("synthetic body", str(raised.exception))

    def test_css_is_static_and_has_no_script_or_browser_layout(self) -> None:
        self.assertNotIn("<script", CROSSPOINT_CSS.lower())
        self.assertNotIn("javascript", CROSSPOINT_CSS.lower())
        self.assertNotIn("position:", CROSSPOINT_CSS.lower())
        self.assertNotIn("flex", CROSSPOINT_CSS.lower())
        self.assertNotIn("grid", CROSSPOINT_CSS.lower())


if __name__ == "__main__":
    unittest.main()
