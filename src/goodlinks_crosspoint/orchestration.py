"""Tagged GoodLinks export and CrossPoint sync orchestration.

The orchestration layer composes the three deliberately small clients in this
package.  GoodLinks remains a read-only source, Pandoc is only invoked for a
real export, and CrossPoint is contacted only for a real sync or send.

A manifest is local workflow state, not an article archive.  It contains only
stable identifiers, hashes, safe filenames, boolean completion state, and the
remote path returned by the local CrossPoint client.  Writes are atomic and
restrictive so an interrupted run cannot leave a half-written state file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from .api import DEFAULT_DELIVERY_TAG, FetchedArticle, GoodLinksError
from .crosspoint import (
    DEFAULT_REMOTE_DIRECTORY,
    CrossPointClient,
    normalize_remote_path,
)
from .epub import (
    CROSSPOINT_CSS,
    DEFAULT_PANDOC_EXECUTABLE,
    DEFAULT_PANDOC_TIMEOUT,
    PandocEpubExporter,
    safe_epub_filename,
)

DEFAULT_OUTPUT_DIRECTORY = "export"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_HASH_LENGTH = hashlib.sha256().digest_size * 2
_CONTENT_ALGORITHM = "goodlinks-crosspoint-content-v1"
_CONFIG_ALGORITHM = "goodlinks-crosspoint-epub-config-v1"
_FILENAME_ALGORITHM = "title-id-sha256-12-v1"
_MAX_ERROR_CODE_LENGTH = 64
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class WorkflowError(Exception):
    """A safe orchestration or local manifest failure."""

    code = "workflow_error"
    default_message = "The GoodLinks-to-CrossPoint workflow failed."

    def __init__(self, message: str | None = None) -> None:
        # Callers deliberately select only static messages.  Article data,
        # server bodies, credentials, and local input paths never enter this
        # exception's text.
        super().__init__(self.default_message if message is None else message)


class ManifestError(WorkflowError):
    code = "manifest_error"
    default_message = "The local workflow manifest is invalid or unavailable."


class WorkflowInputError(WorkflowError):
    code = "workflow_input_error"
    default_message = "The workflow input is invalid."


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """Count-only result suitable for a CLI summary."""

    command: str
    dry_run: bool
    selected: int = 0
    generated: int = 0
    generation_skipped: int = 0
    uploaded: int = 0
    upload_skipped: int = 0
    planned_generation: int = 0
    planned_upload: int = 0
    failed: int = 0
    error_codes: tuple[tuple[str, int], ...] = ()


@dataclass(slots=True)
class _MutableResult:
    command: str
    dry_run: bool
    selected: int = 0
    generated: int = 0
    generation_skipped: int = 0
    uploaded: int = 0
    upload_skipped: int = 0
    planned_generation: int = 0
    planned_upload: int = 0
    failed: int = 0
    _errors: Counter[str] = field(default_factory=Counter)

    def failure(self, code: str) -> None:
        self.failed += 1
        self._errors[code] += 1

    def freeze(self) -> WorkflowResult:
        return WorkflowResult(
            command=self.command,
            dry_run=self.dry_run,
            selected=self.selected,
            generated=self.generated,
            generation_skipped=self.generation_skipped,
            uploaded=self.uploaded,
            upload_skipped=self.upload_skipped,
            planned_generation=self.planned_generation,
            planned_upload=self.planned_upload,
            failed=self.failed,
            error_codes=tuple(sorted(self._errors.items())),
        )


def manifest_path(output_dir: str | os.PathLike[str]) -> Path:
    """Return the ignored sibling manifest for one selected output directory."""

    try:
        directory = Path(output_dir)
    except (TypeError, ValueError):
        raise WorkflowInputError() from None
    # ``Path`` removes a trailing separator, so ``out/`` and ``out`` have one
    # stable sibling name.  The fallback covers the filesystem root and ``.``.
    name = directory.name or "output"
    return directory.parent / f"{name}.manifest.json"


def manifest_lock_path(output_dir: str | os.PathLike[str]) -> Path:
    """Return the ignored advisory-lock sibling for one workflow directory."""

    try:
        directory = Path(output_dir)
    except (TypeError, ValueError):
        raise WorkflowInputError() from None
    name = directory.name or "output"
    return directory.parent / f"{name}.manifest.lock"


class ManifestLockError(WorkflowError):
    code = "manifest_locked"
    default_message = (
        "Another workflow is using this output directory; wait for it to finish."
    )


class _ManifestLock:
    """Nonblocking advisory lock held for the complete real workflow run."""

    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        self.create = create
        self._descriptor = -1
        self._locked = False

    def __enter__(self) -> Self:
        if self.create:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ManifestLockError() from None
        flags = os.O_RDWR | (os.O_CREAT if self.create else 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            self._descriptor = os.open(self.path, flags, stat.S_IRUSR | stat.S_IWUSR)
        except FileNotFoundError:
            # A dry-run must not create output-adjacent state.  With no prior
            # lock file there is no shared state to contend over.
            if not self.create:
                return self
            raise ManifestLockError() from None
        except (OSError, TypeError, ValueError):
            raise ManifestLockError() from None
        try:
            if self.create:
                os.fchmod(self._descriptor, stat.S_IRUSR | stat.S_IWUSR)
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
        except (BlockingIOError, OSError):
            self._close()
            raise ManifestLockError() from None
        return self

    def _close(self) -> None:
        if self._descriptor >= 0:
            try:
                os.close(self._descriptor)
            except OSError:
                pass
            self._descriptor = -1

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._locked:
            try:
                fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            self._locked = False
        self._close()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        raise WorkflowInputError() from None


def article_content_hash(article: FetchedArticle) -> str:
    """Hash the metadata and HTML consumed by the EPUB exporter.

    GoodLinks bookkeeping such as tags, read state, and modification dates is
    intentionally excluded because it does not affect the generated EPUB.
    The algorithm identifier makes future input changes invalidate old state.
    """

    if not isinstance(article, FetchedArticle):
        raise WorkflowInputError()
    metadata = article.metadata
    return hashlib.sha256(
        _canonical_json(
            {
                "algorithm": _CONTENT_ALGORITHM,
                "id": article.id,
                "title": metadata.get("title"),
                "author": metadata.get("author"),
                "url": metadata.get("url"),
                "html": article.html,
            }
        )
    ).hexdigest()


def conversion_config_hash(
    pandoc_executable: str | os.PathLike[str],
) -> str:
    """Hash stable conversion configuration inputs."""

    try:
        executable = os.fspath(pandoc_executable)
    except (TypeError, ValueError):
        raise WorkflowInputError() from None
    if not isinstance(executable, str):
        raise WorkflowInputError()
    return hashlib.sha256(
        _canonical_json(
            {
                "algorithm": _CONFIG_ALGORITHM,
                "filename_algorithm": _FILENAME_ALGORITHM,
                "pandoc_executable": executable,
                "from": "html",
                "to": "epub3",
                "standalone": True,
                "stylesheet": CROSSPOINT_CSS,
            }
        )
    ).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == _HASH_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _contains_controls(value: str) -> bool:
    return any(unicodedata.category(character) in _CONTROL_CATEGORIES for character in value)


def _valid_manifest_filename(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.lower().endswith(".epub")
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "," not in value
        and '"' not in value
        and not _contains_controls(value)
    )


def _valid_remote_path(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return normalize_remote_path(value) == value
    except Exception:  # noqa: BLE001 - manifest validation stays generic
        return False


def _read_manifest(path: Path) -> dict[str, dict[str, Any]]:
    try:
        if not path.exists():
            return {}
        if path.is_symlink() or not path.is_file():
            raise ManifestError()
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestError()
        with path.open("rb") as stream:
            payload_bytes = stream.read(MAX_MANIFEST_BYTES + 1)
    except ManifestError:
        raise
    except (OSError, TypeError, ValueError):
        raise ManifestError() from None
    if len(payload_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestError()
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ManifestError() from None
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise ManifestError()
    articles = payload.get("articles")
    if not isinstance(articles, dict):
        raise ManifestError()

    result: dict[str, dict[str, Any]] = {}
    allowed_fields = {
        "id",
        "content_hash",
        "config_hash",
        "filename",
        "output_hash",
        "generated",
        "uploaded",
        "remote_path",
        "owned_remote_path",
    }
    for key, raw_entry in articles.items():
        if not isinstance(key, str) or not key or not isinstance(raw_entry, dict):
            raise ManifestError()
        if set(raw_entry) - allowed_fields:
            # Reject and do not round-trip unknown fields; this prevents a
            # previously contaminated manifest from being preserved.
            raise ManifestError()
        article_id = raw_entry.get("id")
        if article_id != key or not isinstance(article_id, str) or not article_id:
            raise ManifestError()
        generated = raw_entry.get("generated")
        uploaded = raw_entry.get("uploaded")
        if type(generated) is not bool or type(uploaded) is not bool:
            raise ManifestError()
        if uploaded and not generated:
            raise ManifestError()
        for field_name in ("content_hash", "config_hash", "output_hash"):
            if field_name in raw_entry and not _is_hash(raw_entry[field_name]):
                raise ManifestError()
        if "filename" in raw_entry and not _valid_manifest_filename(
            raw_entry["filename"]
        ):
            raise ManifestError()
        if "remote_path" in raw_entry and not _valid_remote_path(
            raw_entry["remote_path"]
        ):
            raise ManifestError()
        if "owned_remote_path" in raw_entry:
            if not _valid_remote_path(raw_entry["owned_remote_path"]):
                raise ManifestError()
            # The durable ownership marker is only valid on an incomplete
            # entry, before a successful upload restores remote_path.
            if uploaded or "remote_path" in raw_entry:
                raise ManifestError()
        if generated and not all(
            field_name in raw_entry
            for field_name in (
                "content_hash",
                "config_hash",
                "filename",
                "output_hash",
            )
        ):
            raise ManifestError()
        if uploaded and "remote_path" not in raw_entry:
            raise ManifestError()
        if not uploaded and "remote_path" in raw_entry:
            raise ManifestError()
        result[article_id] = dict(raw_entry)
    return result


def _manifest_payload(articles: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    # Only the explicitly supported fields are serialized.  This is a second
    # boundary in addition to load-time validation.
    return {
        "version": MANIFEST_VERSION,
        "articles": {
            article_id: {
                key: entry[key]
                for key in (
                    "id",
                    "content_hash",
                    "config_hash",
                    "filename",
                    "output_hash",
                    "generated",
                    "uploaded",
                    "remote_path",
                    "owned_remote_path",
                )
                if key in entry
            }
            for article_id, entry in sorted(articles.items())
        },
    }


def _write_manifest(path: Path, articles: Mapping[str, Mapping[str, Any]]) -> None:
    payload = _manifest_payload(articles)
    encoded = _canonical_json(payload) + b"\n"
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ManifestError()

    descriptor = -1
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=os.fspath(path.parent)
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            os.fchmod(stream.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        # Keep the permission invariant explicit even on platforms with an
        # unusual temporary-file implementation.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = -1
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    except (OSError, TypeError, ValueError):
        raise ManifestError() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _file_hash(path: Path) -> str:
    try:
        if path.is_symlink():
            raise WorkflowInputError()
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
            raise WorkflowInputError()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except WorkflowInputError:
        raise
    except (OSError, TypeError, ValueError):
        raise WorkflowInputError() from None


def _remote_path(remote_directory: str, filename: str) -> str:
    normalized = normalize_remote_path(remote_directory)
    if normalized == "/":
        return f"/{filename}"
    return f"{normalized}/{filename}"


def _error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if (
        isinstance(code, str)
        and 0 < len(code) <= _MAX_ERROR_CODE_LENGTH
        and code.isascii()
        and all(character.isalnum() or character in {"_", "-"} for character in code)
    ):
        return code
    return "item_failed"


def _validate_pandoc_timeout(timeout: Any) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise WorkflowInputError()
    try:
        normalized = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise WorkflowInputError() from None
    if not math.isfinite(normalized) or normalized <= 0:
        raise WorkflowInputError()
    return normalized


class _Manifest:
    """Mutable, validated state held for one workflow invocation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.articles = _read_manifest(path)

    def save(self) -> None:
        _write_manifest(self.path, self.articles)


class Orchestrator:
    """Run tagged export or sync while retaining only safe local state."""

    def __init__(
        self,
        goodlinks: Any,
        output_dir: str | os.PathLike[str],
        *,
        tag: str = DEFAULT_DELIVERY_TAG,
        pandoc_executable: str | os.PathLike[str] = DEFAULT_PANDOC_EXECUTABLE,
        pandoc_timeout: float = DEFAULT_PANDOC_TIMEOUT,
        remote_directory: str = DEFAULT_REMOTE_DIRECTORY,
        force: bool = False,
        dry_run: bool = False,
        crosspoint: CrossPointClient | None = None,
    ) -> None:
        if not callable(getattr(goodlinks, "list_articles", None)) or not callable(
            getattr(goodlinks, "fetch_article", None)
        ):
            raise WorkflowInputError()
        try:
            directory = Path(output_dir)
            executable = os.fspath(pandoc_executable)
        except (TypeError, ValueError):
            raise WorkflowInputError() from None
        if not isinstance(executable, str) or not executable.strip():
            raise WorkflowInputError()
        pandoc_timeout = _validate_pandoc_timeout(pandoc_timeout)
        if type(force) is not bool or type(dry_run) is not bool:
            raise WorkflowInputError()
        if not isinstance(tag, str) or not tag.strip():
            raise WorkflowInputError()
        try:
            normalized_remote = normalize_remote_path(remote_directory)
        except Exception:  # noqa: BLE001
            raise WorkflowInputError() from None
        self.goodlinks = goodlinks
        self.output_dir = directory
        self.tag = tag
        self.pandoc_executable = executable
        self.pandoc_timeout = pandoc_timeout
        self.remote_directory = normalized_remote
        self.force = force
        self.dry_run = dry_run
        self.crosspoint = crosspoint
        self._exporter: PandocEpubExporter | None = None

    def _exporter_for_run(self) -> PandocEpubExporter:
        if self._exporter is None:
            self._exporter = PandocEpubExporter(
                self.pandoc_executable, timeout=self.pandoc_timeout
            )
        return self._exporter

    def _crosspoint_for_run(self) -> CrossPointClient:
        if self.crosspoint is None:
            raise WorkflowInputError()
        return self.crosspoint

    @staticmethod
    def _new_entry(
        article: FetchedArticle,
        content_hash: str,
        config_hash: str,
        filename: str,
    ) -> dict[str, Any]:
        return {
            "id": article.id,
            "content_hash": content_hash,
            "config_hash": config_hash,
            "filename": filename,
            "generated": False,
            "uploaded": False,
        }

    def _generation_current(
        self,
        entry: Mapping[str, Any] | None,
        *,
        article: FetchedArticle,
        content_hash: str,
        config_hash: str,
        filename: str,
    ) -> bool:
        if not isinstance(entry, Mapping):
            return False
        if (
            entry.get("id") != article.id
            or entry.get("content_hash") != content_hash
            or entry.get("config_hash") != config_hash
            or entry.get("filename") != filename
            or entry.get("generated") is not True
            or not _is_hash(entry.get("output_hash"))
        ):
            return False
        try:
            output_path = self.output_dir / filename
            return _file_hash(output_path) == entry["output_hash"]
        except WorkflowInputError:
            return False

    @staticmethod
    def _owned_remote_path(entry: Mapping[str, Any] | None) -> str | None:
        """Return a validated remote path this entry proves it owns."""

        if not (
            isinstance(entry, Mapping)
            and _is_hash(entry.get("content_hash"))
            and _is_hash(entry.get("config_hash"))
            and _valid_manifest_filename(entry.get("filename"))
        ):
            return None
        filename = entry["filename"]
        owned_remote = entry.get("owned_remote_path")
        if (
            _valid_remote_path(owned_remote)
            and owned_remote.rsplit("/", 1)[-1] == filename
        ):
            return owned_remote
        if entry.get("uploaded") is True:
            remote_path = entry.get("remote_path")
            if (
                _valid_remote_path(remote_path)
                and remote_path.rsplit("/", 1)[-1] == filename
            ):
                return remote_path
        return None

    @staticmethod
    def _owns_remote(entry: Mapping[str, Any] | None, expected_remote: str) -> bool:
        """Return whether validated state proves ownership of one path."""

        return Orchestrator._owned_remote_path(entry) == expected_remote

    def _upload_current(
        self,
        entry: Mapping[str, Any] | None,
        *,
        article: FetchedArticle,
        content_hash: str,
        config_hash: str,
        filename: str,
    ) -> bool:
        if not self._generation_current(
            entry,
            article=article,
            content_hash=content_hash,
            config_hash=config_hash,
            filename=filename,
        ):
            return False
        expected_remote = _remote_path(self.remote_directory, filename)
        return (
            entry.get("uploaded") is True
            and entry.get("remote_path") == expected_remote
        )

    @staticmethod
    def _article_ids(metadata: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in metadata:
            article_id = item.get("id") if isinstance(item, Mapping) else None
            if not isinstance(article_id, str) or not article_id:
                # The canonical GoodLinks client validates this boundary.  A
                # test double or future client cannot make us log its value.
                selected.append({})
                continue
            if article_id in seen:
                continue
            seen.add(article_id)
            selected.append(dict(item))
        return selected

    def run_export(self) -> WorkflowResult:
        return self._run(sync=False)

    def run_sync(self) -> WorkflowResult:
        if self.crosspoint is None and not self.dry_run:
            raise WorkflowInputError()
        return self._run(sync=True)

    def _run(self, *, sync: bool) -> WorkflowResult:
        lock = _ManifestLock(
            manifest_lock_path(self.output_dir), create=not self.dry_run
        )
        with lock:
            return self._run_locked(sync=sync)

    def _run_locked(self, *, sync: bool) -> WorkflowResult:
        manifest = _Manifest(manifest_path(self.output_dir))
        result = _MutableResult(
            command="sync" if sync else "export", dry_run=self.dry_run
        )
        try:
            listed = self.goodlinks.list_articles(self.tag)
        except GoodLinksError:
            raise
        except Exception:  # noqa: BLE001
            raise WorkflowError() from None
        if not isinstance(listed, list):
            raise WorkflowError()
        selected = self._article_ids(listed)
        result.selected = len(selected)
        config_hash = conversion_config_hash(self.pandoc_executable)

        for metadata in selected:
            article_id = metadata.get("id") if metadata else None
            if not isinstance(article_id, str) or not article_id:
                result.failure("invalid_article")
                continue
            try:
                article = self.goodlinks.fetch_article(metadata)
            except Exception as error:  # noqa: BLE001
                # A source fetch failure does not establish a new content
                # hash, so leave an existing manifest entry untouched.
                result.failure(_error_code(error))
                continue

            try:
                content_hash = article_content_hash(article)
                filename = safe_epub_filename(article)
                entry = manifest.articles.get(article_id)
                expected_remote = _remote_path(self.remote_directory, filename)
                prior_owned_path = self._owned_remote_path(entry)
                prior_owned_remote = prior_owned_path == expected_remote
                generation_current = self._generation_current(
                    entry,
                    article=article,
                    content_hash=content_hash,
                    config_hash=config_hash,
                    filename=filename,
                )
                upload_current = sync and self._upload_current(
                    entry,
                    article=article,
                    content_hash=content_hash,
                    config_hash=config_hash,
                    filename=filename,
                )

                if self.dry_run:
                    if generation_current and not self.force:
                        result.generation_skipped += 1
                    else:
                        result.planned_generation += 1
                    if sync:
                        if upload_current and not self.force:
                            result.upload_skipped += 1
                        else:
                            result.planned_upload += 1
                    continue

                needs_generation = self.force or not generation_current
                if needs_generation:
                    # Invalidate any earlier completion before invoking an
                    # operation.  ``prior_owned_path`` was captured before
                    # this replacement, so regeneration may replace the same
                    # remote file without authorizing a name it did not own.
                    pending = self._new_entry(
                        article, content_hash, config_hash, filename
                    )
                    if prior_owned_path is not None:
                        pending["owned_remote_path"] = prior_owned_path
                    manifest.articles[article_id] = pending
                    manifest.save()
                    generated_path = self._exporter_for_run().export_article(
                        article, self.output_dir
                    )
                    if not isinstance(generated_path, (str, os.PathLike)):
                        raise WorkflowError()
                    generated_path = Path(generated_path)
                    if generated_path.parent != self.output_dir or generated_path.name != filename:
                        raise WorkflowError()
                    output_hash = _file_hash(generated_path)
                    pending["output_hash"] = output_hash
                    pending["generated"] = True
                    # ``_new_entry`` already cleared upload state; this is
                    # intentional when --force regenerates an EPUB.
                    manifest.save()
                    result.generated += 1
                    entry = pending
                else:
                    result.generation_skipped += 1
                    entry = manifest.articles[article_id]

                if sync:
                    upload_current = self._upload_current(
                        entry,
                        article=article,
                        content_hash=content_hash,
                        config_hash=config_hash,
                        filename=filename,
                    )
                    needs_upload = self.force or not upload_current
                    if needs_upload:
                        # If a destination changed, atomically retain the
                        # validated old path before clearing completion.  This
                        # lets a later retry replace that known path without
                        # authorizing an unrelated remote file.
                        if entry.get("uploaded") is True:
                            if prior_owned_path is not None:
                                entry["owned_remote_path"] = prior_owned_path
                            entry["uploaded"] = False
                            entry.pop("remote_path", None)
                            manifest.save()
                        uploaded_path = self._crosspoint_for_run().upload_epub(
                            self.output_dir / filename,
                            remote_directory=self.remote_directory,
                            overwrite=self.force or prior_owned_remote,
                        )
                        if uploaded_path != expected_remote:
                            raise WorkflowError()
                        entry["uploaded"] = True
                        entry["remote_path"] = expected_remote
                        entry.pop("owned_remote_path", None)
                        manifest.save()
                        result.uploaded += 1
                    else:
                        result.upload_skipped += 1
            except Exception as error:  # noqa: BLE001
                # Earlier successful entries have already been persisted.  The
                # current entry is either pending/false or has only the
                # operation that actually succeeded marked true.
                result.failure(_error_code(error))
                continue

        return result.freeze()


__all__ = [
    "DEFAULT_OUTPUT_DIRECTORY",
    "MANIFEST_VERSION",
    "ManifestError",
    "ManifestLockError",
    "Orchestrator",
    "WorkflowError",
    "WorkflowInputError",
    "WorkflowResult",
    "article_content_hash",
    "conversion_config_hash",
    "manifest_lock_path",
    "manifest_path",
]
