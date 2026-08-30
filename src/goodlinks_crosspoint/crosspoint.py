"""Explicit HTTP client for the documented CrossPoint file-transfer API.

Endpoint reference (CrossPoint Reader v1.5.0):
https://github.com/crosspoint-reader/crosspoint-reader/blob/v1.5.0/docs/webserver-endpoints.md

The client deliberately uses only the HTTP status, directory listing/creation,
and multipart upload endpoints.  The CrossPoint HTTP server is available while
its user-selected File Transfer or Calibre Wireless mode is active.  The
status response's ``mode`` field is only the network mode (``STA`` or ``AP``),
not a file-transfer-mode gate; this client never changes or infers that mode.
It does not discover devices, retain credentials, or follow redirects.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import stat
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO

DEFAULT_CROSSPOINT_URL = "http://crosspoint.local"
DEFAULT_REMOTE_DIRECTORY = "/GoodLinks"
DEFAULT_TIMEOUT = 15.0

MAX_STATUS_RESPONSE_BYTES = 64 * 1024
MAX_DIRECTORY_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SUCCESS_RESPONSE_BYTES = 64 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_REMOTE_ENTRIES = 10_000
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_FILENAME_BYTES = 255
MAX_REMOTE_PATH_BYTES = 1_024
MAX_TIMEOUT = 300.0

_ALLOWED_DEVICES = frozenset({"X3", "X4"})
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class CrossPointError(Exception):
    """Base class for safe CrossPoint client errors."""

    code = "crosspoint_error"
    default_message = "The CrossPoint request failed."

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.details = dict(details or {})
        if status is not None:
            self.details.setdefault("status", status)
        # All default messages are static.  Callers in this module never pass
        # server bodies, EPUB bytes, or the configured host as ``message``.
        super().__init__(self.default_message if message is None else message)

    def as_dict(self) -> dict[str, Any]:
        """Return structured diagnostics without response content."""

        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            result["details"] = dict(self.details)
        return result


class CrossPointConfigurationError(CrossPointError):
    """The client configuration or an explicit local input is invalid."""

    code = "crosspoint_configuration_error"
    default_message = "The CrossPoint client configuration is invalid."


class InvalidCrossPointURLError(CrossPointConfigurationError):
    code = "invalid_crosspoint_url"
    default_message = (
        "The CrossPoint URL must be an absolute HTTP or HTTPS URL without "
        "credentials, a query, or a fragment."
    )


class CrossPointInvalidTimeoutError(CrossPointConfigurationError):
    code = "crosspoint_invalid_timeout"
    default_message = "The CrossPoint timeout must be finite and within safe bounds."


class InvalidRemotePathError(CrossPointConfigurationError):
    code = "invalid_remote_path"
    default_message = "The CrossPoint destination must be a safe absolute path."


class InvalidEPUBError(CrossPointConfigurationError):
    code = "invalid_epub"
    default_message = (
        "The upload must be a non-empty regular, basename-only .epub file "
        "without commas or control characters."
    )


class UploadTooLargeError(CrossPointConfigurationError):
    code = "upload_too_large"
    default_message = "The EPUB exceeds the client upload limit."


class CrossPointUnavailableError(CrossPointError):
    code = "crosspoint_unavailable"
    default_message = "The CrossPoint device is unavailable."


class CrossPointResponseError(CrossPointError):
    code = "crosspoint_response_error"
    default_message = "The CrossPoint device returned an invalid response."


class CrossPointHTTPError(CrossPointResponseError):
    code = "crosspoint_http_error"
    default_message = "The CrossPoint device returned an HTTP error."

    def __init__(self, status: int) -> None:
        super().__init__(status=status)


class CrossPointRedirectError(CrossPointHTTPError):
    code = "crosspoint_redirect"
    default_message = "CrossPoint redirects are not permitted."


class WrongDeviceError(CrossPointResponseError):
    code = "wrong_device"
    default_message = "The status endpoint is not an X3 or X4 CrossPoint device."


class CrossPointMalformedResponseError(CrossPointResponseError):
    code = "crosspoint_malformed_response"
    default_message = "The CrossPoint device returned an invalid response."


class CrossPointResponseTooLargeError(CrossPointResponseError):
    code = "crosspoint_response_too_large"
    default_message = "The CrossPoint response exceeds the client limit."


class CrossPointUploadIncompleteError(CrossPointError):
    code = "crosspoint_upload_incomplete"
    default_message = (
        "The CrossPoint upload may be partial; inspect and delete the remote "
        "file before retrying."
    )


class RemoteFileExistsError(CrossPointError):
    code = "remote_file_exists"
    default_message = (
        "The destination name already exists; explicit overwrite=True is required."
    )


@dataclass(frozen=True, slots=True)
class CrossPointStatus:
    """Validated fields from ``GET /api/status``.

    The documented ``device`` identity marker is the only status field needed
    by this client.  Network-mode and diagnostic fields are intentionally not
    retained as part of the small public API.
    """

    device: str


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    """One validated item returned by ``GET /api/files``."""

    name: str
    size: int
    is_directory: bool
    is_epub: bool


class _MultipartBody:
    """Small file-like multipart stream accepted by ``http.client``."""

    _READ_CHUNK = 64 * 1024

    def __init__(
        self,
        source: BinaryIO,
        *,
        filename: str,
        size: int,
        boundary: str,
    ) -> None:
        escaped_filename = filename.replace("\\", "\\\\").replace('"', '\\"')
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{escaped_filename}"\r\n'
            "Content-Type: application/epub+zip\r\n"
            "\r\n"
        ).encode()
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        self._source = source
        self._prefix = prefix
        self._suffix = suffix
        self._remaining = size
        self._length = len(prefix) + size + len(suffix)
        self._provided_bytes = 0

    @property
    def content_length(self) -> int:
        return self._length

    @property
    def provided_bytes(self) -> int:
        """Number of request bytes consumed from this stream so far."""

        return self._provided_bytes

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            amount = self._READ_CHUNK
        if amount == 0:
            return b""

        if self._prefix:
            result = self._prefix[:amount]
            self._prefix = self._prefix[len(result) :]
            self._provided_bytes += len(result)
            return result

        if self._remaining:
            try:
                chunk = self._source.read(min(amount, self._remaining))
            except Exception:  # noqa: BLE001
                raise CrossPointUploadIncompleteError() from None
            if not isinstance(chunk, bytes) or not chunk:
                raise CrossPointUploadIncompleteError()
            self._remaining -= len(chunk)
            self._provided_bytes += len(chunk)
            return chunk

        if self._suffix:
            result = self._suffix[:amount]
            self._suffix = self._suffix[len(result) :]
            self._provided_bytes += len(result)
            return result

        return b""


def _contains_controls(value: str) -> bool:
    return any(
        unicodedata.category(character) in _CONTROL_CATEGORIES for character in value
    )


def _validate_status(payload: Any) -> CrossPointStatus:
    if not isinstance(payload, dict):
        # The identity marker is absent, so this cannot be trusted as a
        # CrossPoint status response even if the JSON itself is well formed.
        raise WrongDeviceError()

    device = payload.get("device")
    if not isinstance(device, str) or device not in _ALLOWED_DEVICES:
        # Do not include an unknown value: it could be an arbitrary server
        # response and is not needed to explain the safe failure.
        raise WrongDeviceError()

    return CrossPointStatus(device=device)


def _validate_entry(payload: Any) -> RemoteEntry:
    if not isinstance(payload, dict):
        raise CrossPointMalformedResponseError(
            details={"expected": "directory entry object"}
        )
    name = payload.get("name")
    size = payload.get("size")
    is_directory = payload.get("isDirectory")
    is_epub = payload.get("isEpub")
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or _contains_controls(name)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or type(is_directory) is not bool
        or type(is_epub) is not bool
    ):
        raise CrossPointMalformedResponseError(
            details={"expected": "directory entry fields"}
        )
    return RemoteEntry(
        name=name,
        size=size,
        is_directory=is_directory,
        is_epub=is_epub,
    )


def normalize_remote_path(remote_path: str) -> str:
    """Validate and normalize one absolute CrossPoint destination path."""

    if not isinstance(remote_path, str) or not remote_path:
        raise InvalidRemotePathError()
    if _contains_controls(remote_path) or not remote_path.startswith("/"):
        raise InvalidRemotePathError()
    try:
        if len(remote_path.encode("utf-8")) > MAX_REMOTE_PATH_BYTES:
            raise InvalidRemotePathError()
    except UnicodeEncodeError:
        raise InvalidRemotePathError() from None

    segments = remote_path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        raise InvalidRemotePathError()
    # A trailing slash is harmless and normalized away, while empty interior
    # segments would make the server address a different path than intended.
    interior_segments = segments[1:-1] if remote_path.endswith("/") else segments[1:]
    if any(not segment for segment in interior_segments):
        raise InvalidRemotePathError()
    normalized = remote_path.rstrip("/") or "/"
    return normalized


def _validate_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "," in filename
        or _contains_controls(filename)
        or '"' in filename
        or not filename.lower().endswith(".epub")
    ):
        raise InvalidEPUBError()
    try:
        filename_bytes = filename.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidEPUBError() from None
    if len(filename_bytes) > MAX_FILENAME_BYTES:
        raise InvalidEPUBError()
    return filename


def _local_filename(local_path: str | os.PathLike[str]) -> tuple[str, str]:
    try:
        path_value = os.fspath(local_path)
    except (TypeError, ValueError):
        raise InvalidEPUBError() from None
    if not isinstance(path_value, str):
        raise InvalidEPUBError()
    try:
        filename = os.path.basename(path_value)
    except (TypeError, ValueError):
        raise InvalidEPUBError() from None
    return path_value, _validate_filename(filename)


def _parse_json(body: bytes, *, expected: str) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CrossPointMalformedResponseError(details={"expected": expected}) from None


class CrossPointClient:
    """Small, explicit client for CrossPoint's documented HTTP endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = self._normalize_base_url(
            DEFAULT_CROSSPOINT_URL if base_url is None else base_url
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= MAX_TIMEOUT
        ):
            raise CrossPointInvalidTimeoutError()
        self.timeout = float(timeout)

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidCrossPointURLError()
        candidate = value.strip()
        if not candidate or any(character.isspace() for character in candidate):
            raise InvalidCrossPointURLError()
        try:
            parsed = urllib.parse.urlsplit(candidate)
            _ = parsed.port
            hostname = parsed.hostname
        except (TypeError, ValueError):
            raise InvalidCrossPointURLError() from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in candidate
            or "#" in candidate
            or _contains_controls(candidate)
        ):
            raise InvalidCrossPointURLError()
        try:
            parsed.path.encode("ascii")
        except UnicodeEncodeError:
            # urllib/http.client would apply inconsistent IDNA/path handling;
            # endpoint paths themselves remain ASCII and query values are UTF-8
            # percent encoded below.
            raise InvalidCrossPointURLError() from None
        path = parsed.path.rstrip("/")
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), parsed.netloc, path, "", "")
        )

    def _endpoint(
        self,
        path: str,
        *,
        query: tuple[tuple[str, str], ...] = (),
    ) -> str:
        parsed = urllib.parse.urlsplit(self._base_url)
        target_path = f"{parsed.path}{path}"
        query_string = urllib.parse.urlencode(query)
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, target_path, query_string, "")
        )

    @staticmethod
    def _target(url: str) -> tuple[urllib.parse.SplitResult, str]:
        parsed = urllib.parse.urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        return parsed, target

    def _connection(
        self, parsed: urllib.parse.SplitResult
    ) -> http.client.HTTPConnection:
        port = parsed.port
        if parsed.scheme == "https":
            return http.client.HTTPSConnection(
                parsed.hostname, 443 if port is None else port, timeout=self.timeout
            )
        return http.client.HTTPConnection(
            parsed.hostname, 80 if port is None else port, timeout=self.timeout
        )

    @staticmethod
    def _read_response(
        response: http.client.HTTPResponse,
        maximum: int,
        *,
        status: int,
    ) -> bytes:
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = -1
            if declared_length < 0:
                raise CrossPointMalformedResponseError(
                    status=status, details={"expected": "valid content length"}
                )
            if declared_length > maximum:
                raise CrossPointResponseTooLargeError(
                    status=status, details={"maximumBytes": maximum}
                )
        try:
            body = response.read(maximum + 1)
        except Exception:  # noqa: BLE001
            raise CrossPointUnavailableError() from None
        if not isinstance(body, bytes):
            raise CrossPointMalformedResponseError(
                status=status, details={"expected": "byte response body"}
            )
        if len(body) > maximum:
            raise CrossPointResponseTooLargeError(
                status=status, details={"maximumBytes": maximum}
            )
        return body

    def _exchange(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | _MultipartBody | None = None,
        maximum: int,
    ) -> tuple[int, bytes]:
        connection: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            parsed, target = self._target(url)
            connection = self._connection(parsed)
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if isinstance(status, bool) or not isinstance(status, int):
                raise CrossPointMalformedResponseError(
                    details={"expected": "HTTP status"}
                )
            if not 100 <= status <= 599:
                raise CrossPointMalformedResponseError(
                    details={"expected": "HTTP status"}
                )
            response_limit = (
                maximum if 200 <= status < 300 else MAX_ERROR_RESPONSE_BYTES
            )
            try:
                response_body = self._read_response(
                    response, response_limit, status=status
                )
            except CrossPointError:
                if 200 <= status < 300:
                    raise
                # Status is sufficient to classify a failed request.  Do not
                # retain or expose an oversized/malformed server error body.
                response_body = b""
            return status, response_body
        except CrossPointError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException, ValueError):
            raise CrossPointUnavailableError() from None
        except Exception:  # noqa: BLE001
            raise CrossPointUnavailableError() from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001, S110
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | _MultipartBody | None = None,
        maximum: int,
        expected_status: tuple[int, ...] = (200,),
    ) -> bytes:
        status, response_body = self._exchange(
            method,
            url,
            headers=headers,
            body=body,
            maximum=maximum,
        )
        if 300 <= status < 400:
            raise CrossPointRedirectError(status)
        if status not in expected_status:
            raise CrossPointHTTPError(status)
        return response_body

    def get_status(self) -> CrossPointStatus:
        """Verify and return the documented CrossPoint device status."""

        body = self._request(
            "GET",
            self._endpoint("/api/status"),
            headers={"Accept": "application/json"},
            maximum=MAX_STATUS_RESPONSE_BYTES,
        )
        return _validate_status(_parse_json(body, expected="status JSON object"))

    def _list_directory(self, remote_path: str) -> tuple[RemoteEntry, ...]:
        body = self._request(
            "GET",
            self._endpoint("/api/files", query=(("path", remote_path),)),
            headers={"Accept": "application/json"},
            maximum=MAX_DIRECTORY_RESPONSE_BYTES,
        )
        payload = _parse_json(body, expected="directory JSON array")
        if not isinstance(payload, list) or len(payload) > MAX_REMOTE_ENTRIES:
            raise CrossPointMalformedResponseError(
                details={"expected": "bounded directory array"}
            )
        return tuple(_validate_entry(item) for item in payload)

    def list_directory(
        self, remote_path: str = DEFAULT_REMOTE_DIRECTORY
    ) -> tuple[RemoteEntry, ...]:
        """List one explicit CrossPoint destination directory."""

        return self._list_directory(normalize_remote_path(remote_path))

    def _create_directory(self, name: str, parent: str) -> None:
        form = urllib.parse.urlencode({"name": name, "path": parent}).encode("utf-8")
        self._request(
            "POST",
            self._endpoint("/mkdir"),
            headers={
                "Accept": "text/plain",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=form,
            maximum=MAX_SUCCESS_RESPONSE_BYTES,
        )

    def _ensure_directory_entries(self, remote_path: str) -> tuple[RemoteEntry, ...]:
        if remote_path == "/":
            return self._list_directory(remote_path)

        entries: tuple[RemoteEntry, ...] = ()
        current = ""
        for segment in remote_path.strip("/").split("/"):
            current = f"{current}/{segment}"
            try:
                entries = self._list_directory(current)
            except CrossPointHTTPError as error:
                if error.status != 404:
                    raise
                parent = current.rsplit("/", 1)[0] or "/"
                self._create_directory(segment, parent)
                entries = ()
        return entries

    def ensure_directory(self, remote_path: str = DEFAULT_REMOTE_DIRECTORY) -> None:
        """Create the explicit destination path when CrossPoint reports it absent."""

        self._ensure_directory_entries(normalize_remote_path(remote_path))

    @staticmethod
    def _source_stat(source: BinaryIO) -> os.stat_result:
        try:
            file_stat = os.fstat(source.fileno())
        except (OSError, TypeError, ValueError):
            raise InvalidEPUBError() from None
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
            raise InvalidEPUBError()
        if file_stat.st_size > MAX_UPLOAD_BYTES:
            raise UploadTooLargeError()
        return file_stat

    @staticmethod
    def _ensure_source_unchanged(
        source: BinaryIO, initial_stat: os.stat_result
    ) -> None:
        try:
            final_stat = os.fstat(source.fileno())
        except (OSError, TypeError, ValueError):
            raise CrossPointUploadIncompleteError() from None
        if (
            final_stat.st_size != initial_stat.st_size
            or final_stat.st_mtime_ns != initial_stat.st_mtime_ns
        ):
            raise CrossPointUploadIncompleteError()

    @staticmethod
    def _validate_upload_response(body: bytes, filename: str) -> None:
        try:
            message = body.decode("utf-8")
        except UnicodeDecodeError:
            raise CrossPointMalformedResponseError(
                details={"endpoint": "upload"}
            ) from None
        expected = f"File uploaded successfully: {filename}"
        if message != expected:
            raise CrossPointMalformedResponseError(details={"endpoint": "upload"})

    def upload_epub(
        self,
        local_path: str | os.PathLike[str],
        *,
        remote_directory: str = DEFAULT_REMOTE_DIRECTORY,
        overwrite: bool = False,
    ) -> str:
        """Upload one EPUB after status and destination checks.

        Existing destination names are refused by default because the device's
        documented HTTP upload endpoint overwrites matching names.  A later
        orchestration layer may pass ``overwrite=True`` explicitly.
        """

        if type(overwrite) is not bool:
            raise CrossPointConfigurationError(
                details={"setting": "overwrite", "expected": "boolean"}
            )
        normalized_directory = normalize_remote_path(remote_directory)
        path_value, filename = _local_filename(local_path)
        try:
            source = open(path_value, "rb")  # noqa: SIM115
        except (OSError, TypeError, ValueError):
            raise InvalidEPUBError() from None

        with source:
            initial_stat = self._source_stat(source)

            # This call is deliberately explicit and happens before any
            # destination mutation or upload bytes are sent.
            self.get_status()
            entries = self._ensure_directory_entries(normalized_directory)
            filename_key = filename.casefold()
            if (
                any(entry.name.casefold() == filename_key for entry in entries)
                and not overwrite
            ):
                raise RemoteFileExistsError()

            boundary = f"----goodlinks-crosspoint-{uuid.uuid4().hex}"
            body = _MultipartBody(
                source,
                filename=filename,
                size=initial_stat.st_size,
                boundary=boundary,
            )
            try:
                response_body = self._request(
                    "POST",
                    self._endpoint("/upload", query=(("path", normalized_directory),)),
                    headers={
                        "Accept": "text/plain",
                        "Content-Type": (f"multipart/form-data; boundary={boundary}"),
                        "Content-Length": str(body.content_length),
                    },
                    body=body,
                    maximum=MAX_SUCCESS_RESPONSE_BYTES,
                )
            except CrossPointUnavailableError:
                if body.provided_bytes:
                    raise CrossPointUploadIncompleteError() from None
                raise

            self._validate_upload_response(response_body, filename)
            self._ensure_source_unchanged(source, initial_stat)

        if normalized_directory == "/":
            return f"/{filename}"
        return f"{normalized_directory}/{filename}"


__all__ = [
    "DEFAULT_CROSSPOINT_URL",
    "DEFAULT_REMOTE_DIRECTORY",
    "CrossPointClient",
    "CrossPointConfigurationError",
    "CrossPointError",
    "CrossPointHTTPError",
    "CrossPointInvalidTimeoutError",
    "CrossPointMalformedResponseError",
    "CrossPointRedirectError",
    "CrossPointResponseTooLargeError",
    "CrossPointStatus",
    "CrossPointUnavailableError",
    "CrossPointUploadIncompleteError",
    "InvalidCrossPointURLError",
    "InvalidEPUBError",
    "InvalidRemotePathError",
    "RemoteEntry",
    "RemoteFileExistsError",
    "UploadTooLargeError",
    "WrongDeviceError",
    "normalize_remote_path",
]
