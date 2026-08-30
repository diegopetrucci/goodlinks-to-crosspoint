"""Generate small CrossPoint-compatible EPUBs with an external Pandoc.

The exporter deliberately keeps Pandoc behind a small subprocess boundary.  It
never installs Pandoc, invokes a shell, or puts article content in diagnostics.
Article input, metadata, and the stylesheet live only in a private temporary
workspace while Pandoc runs.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import shutil
import subprocess
import tempfile
import unicodedata
import urllib.parse
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .api import FetchedArticle

DEFAULT_PANDOC_EXECUTABLE = "pandoc"
DEFAULT_PANDOC_TIMEOUT = 120.0

# Keep the stylesheet intentionally boring.  CrossPoint's EPUB renderer does
# not need browser-oriented layout, animations, scripts, or external assets.
CROSSPOINT_CSS = """body {
  color: #111;
  background: #fff;
  font-family: serif;
  line-height: 1.45;
  margin: 1em;
}

article {
  max-width: 42em;
  margin: 0 auto;
}

header {
  margin-bottom: 1.5em;
}

h1 {
  font-size: 1.6em;
}

main p {
  margin: 0 0 1em;
}

a {
  color: #0645ad;
  text-decoration: underline;
}

pre {
  white-space: pre-wrap;
  overflow-wrap: break-word;
}

code,
pre {
  font-family: monospace;
}

footer {
  border-top: 1px solid #bbb;
  color: #555;
  font-size: 0.85em;
  margin-top: 2em;
  padding-top: 0.5em;
}
"""


class EpubExportError(Exception):
    """Base class for safe, actionable EPUB export failures."""

    code = "epub_export_failed"
    default_message = "The EPUB could not be generated."

    def __init__(self, message: str | None = None) -> None:
        # Messages are selected by this module and never include article data,
        # command output, or filesystem paths supplied by a caller.
        super().__init__(self.default_message if message is None else message)

    def as_dict(self) -> dict[str, str]:
        """Return stable diagnostics suitable for a CLI or log."""

        return {"code": self.code, "message": str(self)}


class InvalidArticleError(EpubExportError):
    code = "invalid_article"
    default_message = "The exporter expected a fetched GoodLinks article."


class InvalidPandocConfigurationError(EpubExportError):
    code = "invalid_pandoc_configuration"
    default_message = "The Pandoc executable configuration is invalid."


class PandocNotFoundError(EpubExportError):
    code = "pandoc_not_found"
    default_message = (
        "Pandoc was not found. Install Pandoc separately or configure a valid "
        "pandoc_executable."
    )


class PandocVersionError(EpubExportError):
    code = "pandoc_version_failed"
    default_message = (
        "Pandoc could not report its version. Verify the executable and install "
        "a supported Pandoc release separately."
    )


class PandocInvocationError(EpubExportError):
    code = "pandoc_failed"
    default_message = (
        "Pandoc failed to generate the EPUB. Check the Pandoc installation and "
        "the destination permissions."
    )


class EpubOutputError(EpubExportError):
    code = "epub_output_failed"
    default_message = (
        "The EPUB output could not be written; check destination permissions."
    )


def _utf8_safe_text(value: str) -> bool:
    """Return whether ``value`` can be written as clean UTF-8 text."""

    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return all(unicodedata.category(character) != "Cc" for character in value)


def _without_controls(value: str, *, preserve_whitespace: bool = True) -> str:
    """Remove characters that cannot safely appear in generated text."""

    result: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category == "Cc":
            if preserve_whitespace and character in "\t\n\r":
                result.append(character)
            continue
        # Lone surrogate code points cannot be encoded into the UTF-8 files
        # passed to Pandoc.  They are not useful article text, so omit them.
        if category == "Cs":
            continue
        result.append(character)
    return "".join(result)


def _metadata_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _without_controls(value).strip()
    return cleaned or None


def _safe_source_url(value: Any) -> str | None:
    """Keep only ordinary HTTP(S) URLs for generated links and metadata."""

    if not isinstance(value, str) or not _utf8_safe_text(value):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except (ValueError, UnicodeError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _safe_link(value: str | None) -> str | None:
    """Allow links that cannot execute code or load an external resource type."""

    if not isinstance(value, str) or not _utf8_safe_text(value):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("#"):
        return candidate
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except (ValueError, UnicodeError):
        return None
    if parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return None
    if parsed.scheme.lower() in {"http", "https"} and not parsed.netloc:
        return None
    return candidate


class _SafeHTMLParser(HTMLParser):
    """Reduce article HTML to tags supported by a constrained EPUB reader."""

    _allowed_tags = frozenset(
        {
            "a",
            "abbr",
            "b",
            "blockquote",
            "br",
            "cite",
            "code",
            "dd",
            "del",
            "dfn",
            "div",
            "dl",
            "dt",
            "em",
            "figcaption",
            "figure",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "i",
            "kbd",
            "li",
            "mark",
            "ol",
            "p",
            "pre",
            "q",
            "s",
            "samp",
            "small",
            "span",
            "strong",
            "sub",
            "sup",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "u",
            "ul",
        }
    )
    _void_tags = frozenset({"br", "hr"})
    # These blocked media elements are void in HTML.  Treating them as
    # containers would leave the parser in blocked mode until a later,
    # unrelated closing tag (or forever when no closing tag is supplied).
    _blocked_void_tags = frozenset({"embed", "source", "track"})
    _blocked_tags = frozenset(
        {
            "audio",
            "canvas",
            "embed",
            "head",
            "iframe",
            "math",
            "noscript",
            "object",
            "picture",
            "script",
            "source",
            "style",
            "svg",
            "template",
            "title",
            "track",
            "video",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._blocked_depth = 0

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> str:
        safe: list[str] = []
        for name, value in attrs:
            name = name.lower()
            if value is None:
                continue
            if name == "href":
                href = _safe_link(value)
                if href is not None:
                    safe.append(f' href="{html.escape(href, quote=True)}"')
            elif name == "title":
                title = _without_controls(value).strip()
                if title:
                    safe.append(f' title="{html.escape(title, quote=True)}"')
            elif name in {"colspan", "rowspan"} and value.isdigit():
                safe.append(f' {name}="{value}"')
        return "".join(safe)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._blocked_depth:
            if tag in self._blocked_tags and tag not in self._blocked_void_tags:
                self._blocked_depth += 1
            return
        if tag in self._blocked_tags:
            if tag not in self._blocked_void_tags:
                self._blocked_depth = 1
            return
        if tag not in self._allowed_tags:
            return
        self._parts.append(f"<{tag}{self._attributes(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._blocked_depth or tag in self._blocked_tags:
            return
        if tag not in self._allowed_tags:
            return
        self._parts.append(f"<{tag}{self._attributes(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._blocked_depth:
            if tag in self._blocked_tags and tag not in self._blocked_void_tags:
                self._blocked_depth -= 1
            return
        if tag in self._allowed_tags and tag not in self._void_tags:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self._parts.append(html.escape(_without_controls(data), quote=False))

    def handle_comment(self, _data: str) -> None:
        return

    def handle_decl(self, _decl: str) -> None:
        return

    def handle_pi(self, _data: str) -> None:
        return

    def result(self) -> str:
        return "".join(self._parts)


def _sanitize_html(fragment: str) -> str:
    parser = _SafeHTMLParser()
    parser.feed(fragment)
    parser.close()
    return parser.result()


def _filename_slug(value: str | None, *, maximum: int) -> str:
    """Make an ASCII basename component without commas or path syntax."""

    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    try:
        normalized = normalized.encode("ascii", "ignore").decode("ascii")
    except UnicodeError:
        normalized = ""
    result: list[str] = []
    pending_separator = False
    for character in normalized:
        if character.isascii() and character.isalnum():
            if pending_separator and result:
                result.append("-")
            result.append(character)
            pending_separator = False
        elif (
            character in {"-", "_"}
            or character.isspace()
            or character
            in {
                ".",
                "/",
                "\\",
            }
        ):
            pending_separator = bool(result)
        else:
            # Punctuation (including commas) is treated as a separator rather
            # than copied into a name that may be consumed by a device parser.
            pending_separator = bool(result)
        if len(result) >= maximum:
            break
    return "".join(result).strip("-_")[:maximum]


def _stable_id_bytes(value: str) -> bytes:
    """Encode an ID deterministically, including lone surrogate code points."""

    # ``surrogatepass`` makes otherwise-invalid Python strings deterministic
    # without ever placing those raw code points in generated files.
    return value.encode("utf-8", "surrogatepass")


def safe_epub_filename(article: FetchedArticle) -> str:
    """Return a deterministic, basename-only filename for one article."""

    if not isinstance(article, FetchedArticle):
        raise InvalidArticleError()
    title = _filename_slug(article.title, maximum=72)
    identifier = _filename_slug(article.id, maximum=40)
    # The digest keeps distinct IDs distinct even when their safe slugs are
    # identical or contain only path/control characters.
    digest = hashlib.sha256(_stable_id_bytes(article.id)).hexdigest()[:12]
    title = title or "article"
    identifier = identifier or "id"
    return f"{title}-{identifier}-{digest}.epub"


def _identifier(article: FetchedArticle) -> str:
    value = _metadata_text(article.id)
    if value and _utf8_safe_text(article.id):
        return value
    digest = hashlib.sha256(_stable_id_bytes(article.id)).hexdigest()
    return f"goodlinks-{digest}"


def _document_for(article: FetchedArticle) -> tuple[str, dict[str, str]]:
    title = _metadata_text(article.title) or "Untitled article"
    author = _metadata_text(article.metadata.get("author"))
    source = _safe_source_url(article.url)
    identifier = _identifier(article)

    metadata: dict[str, str] = {
        "title": title,
        "identifier": identifier,
    }
    if author:
        metadata["author"] = author
    if source:
        # ``source`` is recognized by Pandoc's EPUB writer and is also shown
        # in the document footer below for readers that hide package metadata.
        metadata["source"] = source

    source_markup = ""
    if source:
        escaped_source = html.escape(source, quote=True)
        source_markup = (
            f'<p>Source: <a href="{escaped_source}">{escaped_source}</a></p>'
        )
    author_markup = f"<p>By {html.escape(author)}</p>" if author else ""
    document = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        "</head>\n"
        "<body>\n"
        "<article>\n"
        "<header>\n"
        f"<h1>{html.escape(title)}</h1>\n"
        f"{author_markup}"
        "</header>\n"
        "<main>\n"
        f"{_sanitize_html(article.html)}\n"
        "</main>\n"
        "<footer>\n"
        f"{source_markup}"
        f"<p>Identifier: <code>{html.escape(identifier)}</code></p>\n"
        "</footer>\n"
        "</article>\n"
        "</body>\n"
        "</html>\n"
    )
    return document, metadata


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise InvalidPandocConfigurationError()
    if not math.isfinite(float(timeout)) or timeout <= 0:
        raise InvalidPandocConfigurationError()
    return float(timeout)


class PandocEpubExporter:
    """Generate one EPUB per :class:`FetchedArticle` with external Pandoc."""

    def __init__(
        self,
        pandoc_executable: str | os.PathLike[str] = DEFAULT_PANDOC_EXECUTABLE,
        *,
        timeout: float = DEFAULT_PANDOC_TIMEOUT,
    ) -> None:
        try:
            executable = os.fspath(pandoc_executable)
        except (TypeError, ValueError):
            raise InvalidPandocConfigurationError() from None
        if not isinstance(executable, str) or not executable.strip():
            raise InvalidPandocConfigurationError()
        if any(
            ord(character) < 32 or ord(character) == 127 for character in executable
        ):
            raise InvalidPandocConfigurationError()
        self.pandoc_executable = executable
        self.timeout = _validate_timeout(timeout)
        self._resolved_executable: str | None = None
        self._version_checked = False

    def _resolve_executable(self) -> str:
        if self._resolved_executable is not None:
            return self._resolved_executable
        try:
            resolved = shutil.which(self.pandoc_executable)
        except (OSError, ValueError):
            resolved = None
        if not resolved:
            raise PandocNotFoundError()
        self._resolved_executable = resolved
        return resolved

    def _check_version(self) -> str:
        executable = self._resolve_executable()
        if self._version_checked:
            return executable
        try:
            completed = subprocess.run(
                [executable, "--version"],
                shell=False,
                check=False,
                capture_output=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            self._resolved_executable = None
            raise PandocNotFoundError() from None
        except (
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            subprocess.SubprocessError,
        ):
            raise PandocVersionError() from None
        except Exception:  # noqa: BLE001
            raise PandocVersionError() from None
        if getattr(completed, "returncode", None) != 0:
            raise PandocVersionError()
        self._version_checked = True
        return executable

    @staticmethod
    def _output_directory(output_dir: str | os.PathLike[str]) -> Path:
        try:
            directory = Path(output_dir)
            directory.mkdir(parents=True, exist_ok=True)
            if not directory.is_dir():
                raise EpubOutputError()
            return directory
        except EpubExportError:
            raise
        except (OSError, TypeError, ValueError):
            raise EpubOutputError() from None

    @staticmethod
    def _article_path(
        article: FetchedArticle,
        directory: Path,
        reserved: set[str],
    ) -> Path:
        filename = safe_epub_filename(article)
        stem = filename[: -len(".epub")]
        candidate = filename
        suffix = 2
        key = candidate.casefold()
        while key in reserved:
            candidate = f"{stem}-{suffix}.epub"
            suffix += 1
            key = candidate.casefold()
        reserved.add(key)
        return directory / candidate

    def _run_pandoc(
        self,
        executable: str,
        input_path: Path,
        metadata_path: Path,
        css_path: Path,
        output_path: Path,
    ) -> None:
        command = [
            executable,
            "--from=html",
            "--to=epub3",
            "--standalone",
            "--metadata-file",
            os.fspath(metadata_path),
            "--css",
            os.fspath(css_path),
            "--output",
            os.fspath(output_path),
            os.fspath(input_path),
        ]
        try:
            completed = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            self._resolved_executable = None
            raise PandocNotFoundError() from None
        except (
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            subprocess.SubprocessError,
        ):
            raise PandocInvocationError() from None
        except Exception:  # noqa: BLE001
            raise PandocInvocationError() from None
        if getattr(completed, "returncode", None) != 0:
            raise PandocInvocationError()
        if not output_path.is_file():
            raise PandocInvocationError()
        try:
            if output_path.stat().st_size <= 0:
                raise PandocInvocationError()
        except EpubExportError:
            raise
        except OSError:
            raise PandocInvocationError() from None

    @staticmethod
    def _staging_output(directory: Path) -> Path:
        """Create a restrictive temporary output on the destination filesystem."""

        descriptor = -1
        staging_path: Path | None = None
        try:
            descriptor, filename = tempfile.mkstemp(
                prefix=".goodlinks-crosspoint-",
                suffix=".epub.tmp",
                dir=os.fspath(directory),
            )
            staging_path = Path(filename)
            os.close(descriptor)
            descriptor = -1
            # mkstemp already requests 0600; make the invariant explicit in
            # case a platform applies an unusual umask or Pandoc replaces it.
            os.chmod(staging_path, 0o600)
            return staging_path
        except (OSError, TypeError, ValueError):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if staging_path is not None:
                try:
                    staging_path.unlink()
                except OSError:
                    pass
            raise EpubOutputError() from None

    @staticmethod
    def _cleanup_staging(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Preserve the useful Pandoc/output error if cleanup itself fails;
            # the cleanup is still attempted on every path through the method.
            pass

    def _export_one(
        self,
        article: FetchedArticle,
        directory: Path,
        reserved: set[str],
    ) -> Path:
        if not isinstance(article, FetchedArticle):
            raise InvalidArticleError()
        output_path = self._article_path(article, directory, reserved)
        staging_output = self._staging_output(directory)
        try:
            with tempfile.TemporaryDirectory(
                prefix="goodlinks-crosspoint-"
            ) as workspace:
                workspace_path = Path(workspace)
                input_path = workspace_path / "article.html"
                metadata_path = workspace_path / "metadata.json"
                css_path = workspace_path / "crosspoint.css"
                document, metadata = _document_for(article)
                input_path.write_text(document, encoding="utf-8")
                metadata_path.write_text(
                    json.dumps(metadata, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )
                css_path.write_text(CROSSPOINT_CSS, encoding="utf-8")
                self._run_pandoc(
                    self._resolved_executable or self._resolve_executable(),
                    input_path,
                    metadata_path,
                    css_path,
                    staging_output,
                )
                try:
                    os.chmod(staging_output, 0o600)
                    # Both paths are in ``directory``; this remains atomic
                    # even when the destination is on another filesystem.
                    os.replace(staging_output, output_path)
                except OSError:
                    raise EpubOutputError() from None
        except EpubExportError:
            raise
        except (OSError, TypeError, UnicodeError, ValueError):
            raise EpubOutputError() from None
        finally:
            self._cleanup_staging(staging_output)
        return output_path

    def export_article(
        self,
        article: FetchedArticle,
        output_dir: str | os.PathLike[str],
    ) -> Path:
        """Generate and return one EPUB path for ``article``."""

        self._check_version()
        directory = self._output_directory(output_dir)
        # Keep the reserved set local so repeated calls remain deterministic;
        # an existing output from an earlier run is intentionally replaceable.
        return self._export_one(article, directory, set())

    def export_articles(
        self,
        articles: Iterable[FetchedArticle],
        output_dir: str | os.PathLike[str],
    ) -> list[Path]:
        """Generate one EPUB for every article in the supplied iterable."""

        self._check_version()
        directory = self._output_directory(output_dir)
        try:
            article_list = list(articles)
        except Exception:  # noqa: BLE001
            raise InvalidArticleError() from None
        reserved: set[str] = set()
        return [
            self._export_one(article, directory, reserved) for article in article_list
        ]


def export_article(
    article: FetchedArticle,
    output_dir: str | os.PathLike[str],
    *,
    pandoc_executable: str | os.PathLike[str] = DEFAULT_PANDOC_EXECUTABLE,
    timeout: float = DEFAULT_PANDOC_TIMEOUT,
) -> Path:
    """Convenience wrapper for exporting one fetched article."""

    return PandocEpubExporter(pandoc_executable, timeout=timeout).export_article(
        article, output_dir
    )


def export_articles(
    articles: Iterable[FetchedArticle],
    output_dir: str | os.PathLike[str],
    *,
    pandoc_executable: str | os.PathLike[str] = DEFAULT_PANDOC_EXECUTABLE,
    timeout: float = DEFAULT_PANDOC_TIMEOUT,
) -> list[Path]:
    """Convenience wrapper for exporting multiple fetched articles."""

    return PandocEpubExporter(pandoc_executable, timeout=timeout).export_articles(
        articles, output_dir
    )


__all__ = [
    "CROSSPOINT_CSS",
    "DEFAULT_PANDOC_EXECUTABLE",
    "DEFAULT_PANDOC_TIMEOUT",
    "EpubExportError",
    "EpubOutputError",
    "InvalidArticleError",
    "InvalidPandocConfigurationError",
    "PandocEpubExporter",
    "PandocInvocationError",
    "PandocNotFoundError",
    "PandocVersionError",
    "export_article",
    "export_articles",
    "safe_epub_filename",
]
