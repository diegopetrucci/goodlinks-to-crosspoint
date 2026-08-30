"""Read-only client for the documented GoodLinks local API.

Only the official read endpoints are used.  The bearer token is loaded from
``GOODLINKS_TOKEN`` and is never put in a URL or copied into diagnostics.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_API_URL = "http://127.0.0.1:9428/api/v1"
DEFAULT_DELIVERY_TAG = "x3"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_ITEMS = 100_000
DEFAULT_MAX_PAGES = 1_000
MAX_PAGE_SIZE = 1_000
# Metadata responses are kept deliberately small; article HTML has a separate,
# practical allowance because it is expected to be substantially larger.
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HTML_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_RETAINED_METADATA_BYTES = 64 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 1024 * 1024


class GoodLinksError(Exception):
    """Base class for errors whose diagnostics contain no server response."""

    code = "goodlinks_error"
    default_message = "The GoodLinks request failed."

    def __init__(
        self,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.details = dict(details or {})
        if status is not None:
            self.details.setdefault("status", status)
        super().__init__(self.default_message)

    def as_dict(self) -> dict[str, Any]:
        """Return safe, structured diagnostics for a caller."""

        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.details:
            result["details"] = dict(self.details)
        return result


class ConfigurationError(GoodLinksError):
    """The client configuration is invalid or incomplete."""


class MissingTokenError(ConfigurationError):
    code = "missing_token"
    default_message = "GOODLINKS_TOKEN is not set."


class InvalidTokenError(ConfigurationError):
    code = "invalid_token"
    default_message = (
        "GOODLINKS_TOKEN is empty, non-ASCII, or contains control characters."
    )


class InvalidAPIURLError(ConfigurationError):
    code = "invalid_api_url"
    default_message = "The GoodLinks API URL must be an absolute HTTP or HTTPS URL."


class InsecureAPIURLError(ConfigurationError):
    code = "insecure_api_url"
    default_message = (
        "Plain HTTP is allowed only for a loopback GoodLinks API; "
        "use HTTPS for a remote host."
    )


class InvalidTimeoutError(ConfigurationError):
    code = "invalid_timeout"
    default_message = "The GoodLinks API timeout must be finite and greater than zero."


class InvalidPaginationError(ConfigurationError):
    code = "invalid_pagination"
    default_message = "GoodLinks pagination settings are outside their safe bounds."


class APIUnavailableError(GoodLinksError):
    code = "api_unavailable"
    default_message = "The GoodLinks API server is unavailable."


class AuthenticationError(GoodLinksError):
    code = "authentication_failed"
    default_message = "The GoodLinks API rejected the configured token."


class APIResponseError(GoodLinksError):
    code = "api_error"
    default_message = "The GoodLinks API returned an error response."


class ResponseTooLargeError(APIResponseError):
    code = "response_too_large"
    default_message = "The GoodLinks API response exceeds the client limit."


class MalformedResponseError(APIResponseError):
    code = "malformed_response"
    default_message = "The GoodLinks API returned an invalid response."


class PaginationError(APIResponseError):
    code = "pagination_error"
    default_message = "The GoodLinks API returned unsafe pagination data."


class _Transport(Protocol):
    """Minimal injectable transport used by the stdlib HTTP implementation."""

    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so the bearer header stays on the configured origin."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        file: Any,
        code: int,
        message: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del request, file, code, message, headers, newurl


class _URLTransport:
    """Stdlib transport with ambient proxies and redirects disabled."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirectHandler()
        )

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        return self._opener.open(request, timeout=timeout)


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_string_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_non_empty_string_or_none(value: Any) -> bool:
    return value is None or _is_non_empty_string(value)


def _is_string_list_or_none(value: Any) -> bool:
    return value is None or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _is_boolean(value: Any) -> bool:
    return type(value) is bool


def _is_integer_or_none(value: Any) -> bool:
    return value is None or type(value) is int


# These are the documented GoodLinks link fields. Unknown fields deliberately
# bypass this table so newer API fields remain usable.
_METADATA_FIELD_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "url": _is_non_empty_string,
    "title": _is_string_or_none,
    "author": _is_string_or_none,
    "summary": _is_string_or_none,
    "tags": _is_string_list_or_none,
    "starred": _is_boolean,
    "highlighted": _is_boolean,
    "wordCount": _is_integer_or_none,
    "addedAt": _is_non_empty_string,
    "modifiedAt": _is_non_empty_string,
    "readAt": _is_non_empty_string_or_none,
}


def _validate_article_metadata(payload: Any) -> dict[str, Any]:
    """Validate documented metadata while retaining unknown future fields."""

    if not isinstance(payload, dict):
        raise MalformedResponseError(details={"expected": "article metadata object"})
    article_id = payload.get("id")
    if not _is_non_empty_string(article_id):
        raise MalformedResponseError(details={"field": "id"})
    for field_name, validator in _METADATA_FIELD_VALIDATORS.items():
        if field_name in payload and not validator(payload[field_name]):
            raise MalformedResponseError(details={"field": field_name})
    return dict(payload)


def _response_header(response: Any, name: str) -> str | None:
    """Read one response header without allowing header diagnostics to escape."""

    try:
        headers = getattr(response, "headers", None)
        if headers is not None:
            get = getattr(headers, "get", None)
            if callable(get):
                value = get(name)
                if isinstance(value, str):
                    return value
            items = getattr(headers, "items", None)
            if callable(items):
                for key, value in items():
                    if isinstance(key, str) and key.lower() == name.lower():
                        return value if isinstance(value, str) else None
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            value = getheader(name)
            return value if isinstance(value, str) else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _media_type(content_type: str | None) -> str | None:
    if not isinstance(content_type, str):
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if "," in media_type:
        return None
    return media_type or None


@dataclass(frozen=True, slots=True)
class FetchedArticle:
    """GoodLinks metadata together with the cleaned article HTML."""

    metadata: dict[str, Any] = field(repr=False)
    html: str = field(repr=False)
    content_type: str = field(default="text/html", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _validate_article_metadata(self.metadata))
        if not isinstance(self.html, str):
            raise MalformedResponseError(details={"expected": "HTML text"})
        if _media_type(self.content_type) != "text/html":
            raise MalformedResponseError(details={"expected": "text/html content"})

    @property
    def id(self) -> str:
        return self.metadata["id"]

    @property
    def url(self) -> str | None:
        value = self.metadata.get("url")
        return value if isinstance(value, str) else None

    @property
    def title(self) -> str | None:
        value = self.metadata.get("title")
        return value if isinstance(value, str) else None

    def as_dict(self) -> dict[str, Any]:
        """Return a copy with HTML under the export-facing ``contentHtml`` key."""

        result = dict(self.metadata)
        result["contentHtml"] = self.html
        return result


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    body: bytes
    content_type: str | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Decoder errors can include response prefixes, so do not chain
            # them into the public exception.
            raise MalformedResponseError(
                status=self.status,
                details={"expected": "valid UTF-8 JSON"},
            ) from None

    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except UnicodeDecodeError:
            raise MalformedResponseError(
                status=self.status,
                details={"expected": "UTF-8 text"},
            ) from None


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_api_url(value: str) -> str:
    """Validate a base URL and add ``/api/v1`` when only an origin is given."""

    if not isinstance(value, str):
        raise InvalidAPIURLError()
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        raise InvalidAPIURLError()
    try:
        parsed = urllib.parse.urlsplit(candidate)
        # Accessing .port validates malformed and out-of-range ports.
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise InvalidAPIURLError() from None

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise InvalidAPIURLError()
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise InsecureAPIURLError()

    path = parsed.path.rstrip("/") or "/api/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001, S110
            # A close failure must not replace a safe API error.
            pass


def _read_response(response: Any, maximum: int, status: int) -> bytes:
    try:
        payload = response.read(maximum + 1)
    except Exception:  # noqa: BLE001
        # Transport/read diagnostics can include response fragments; expose
        # only the stable, generic client error.
        raise APIUnavailableError() from None
    finally:
        _close_response(response)
    if not isinstance(payload, bytes):
        raise MalformedResponseError(
            status=status,
            details={"expected": "a byte response body"},
        )
    if len(payload) > maximum:
        raise ResponseTooLargeError(
            status=status,
            details={"maximumBytes": maximum},
        )
    return payload


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    if isinstance(status, bool):
        raise MalformedResponseError(details={"expected": "HTTP status"})
    try:
        status = int(status)
    except (TypeError, ValueError):
        raise MalformedResponseError(details={"expected": "HTTP status"}) from None
    if not 100 <= status <= 599:
        raise MalformedResponseError(details={"expected": "HTTP status"})
    return status


def _http_error(status: int) -> GoodLinksError:
    if status == 401:
        return AuthenticationError(status=status)
    if status == 404:
        return APIResponseError(status=status, details={"resource": "not_found"})
    return APIResponseError(status=status)


class GoodLinksClient:
    """Read-only client for GoodLinks 3.2+'s documented local API."""

    def __init__(
        self,
        api_url: str | None = None,
        *,
        timeout: float = 15.0,
        transport: _Transport | None = None,
    ) -> None:
        self._base_url = _normalize_api_url(
            DEFAULT_API_URL if api_url is None else api_url
        )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise InvalidTimeoutError()
        if not math.isfinite(float(timeout)) or timeout <= 0:
            raise InvalidTimeoutError()
        self.timeout = float(timeout)
        self._token = self._load_token()
        self._transport = _URLTransport() if transport is None else transport

    @staticmethod
    def _load_token() -> str:
        token = os.environ.get("GOODLINKS_TOKEN")
        if token is None:
            raise MissingTokenError()
        if not token or token != token.strip():
            raise InvalidTokenError()
        try:
            # Keep the Authorization header strictly ASCII.  This rejects
            # values that urllib or an HTTP server could encode differently,
            # before any request or header construction occurs.
            token.encode("ascii")
        except UnicodeEncodeError:
            raise InvalidTokenError() from None
        if any(ord(character) < 32 or ord(character) == 127 for character in token):
            raise InvalidTokenError()
        return token

    @staticmethod
    def _encode_path_component(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def _build_url(
        self,
        path: str,
        query: Iterable[tuple[str, Any]] = (),
        *,
        encoded_path: bool = False,
    ) -> str:
        if not isinstance(path, str) or not path.startswith("/"):
            raise InvalidAPIURLError()
        path_value = (
            path
            if encoded_path
            else "/".join(
                self._encode_path_component(part) for part in path.split("/")
            )
        )
        encoded_query = urllib.parse.urlencode(
            [
                (
                    key,
                    str(value).lower() if isinstance(value, bool) else str(value),
                )
                for key, value in query
            ]
        )
        return f"{self._base_url}{path_value}" + (
            f"?{encoded_query}" if encoded_query else ""
        )

    def _request(
        self,
        path: str,
        query: Iterable[tuple[str, Any]] = (),
        *,
        encoded_path: bool = False,
        maximum: int = MAX_JSON_RESPONSE_BYTES,
        expected_content_type: str | None = None,
    ) -> _Response:
        request = urllib.request.Request(
            self._build_url(path, query, encoded_path=encoded_path),
            headers={
                "Accept": "application/json, text/html",
                "Authorization": f"Bearer {self._token}",
            },
            method="GET",
        )
        try:
            response = self._transport.open(request, self.timeout)
        except urllib.error.HTTPError as error:
            # Error bodies are bounded and discarded; they are never decoded.
            try:
                error.read(MAX_ERROR_RESPONSE_BYTES + 1)
            except Exception:  # noqa: BLE001, S110
                pass
            finally:
                _close_response(error)
            raise _http_error(int(error.code)) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise APIUnavailableError() from None
        except Exception:  # noqa: BLE001
            # Injectable transports may raise arbitrary exceptions whose text
            # could contain a token or response fragment.
            raise APIUnavailableError() from None

        try:
            status = _response_status(response)
        except GoodLinksError:
            _close_response(response)
            raise
        except Exception:  # noqa: BLE001
            _close_response(response)
            raise APIUnavailableError() from None
        if not 200 <= status < 300:
            try:
                _read_response(response, MAX_ERROR_RESPONSE_BYTES, status)
            except GoodLinksError:
                # HTTP status is enough to classify an error; the body is
                # intentionally not retained even when it is oversized.
                pass
            raise _http_error(status)
        content_type = _response_header(response, "Content-Type")
        if (
            expected_content_type is not None
            and _media_type(content_type) != expected_content_type.lower()
        ):
            _close_response(response)
            raise MalformedResponseError(
                status=status,
                details={"expectedContentType": expected_content_type},
            )
        return _Response(
            status, _read_response(response, maximum, status), content_type
        )

    def _request_json(
        self,
        path: str,
        query: Iterable[tuple[str, Any]] = (),
        *,
        encoded_path: bool = False,
    ) -> tuple[Any, int]:
        response = self._request(
            path,
            query,
            encoded_path=encoded_path,
            maximum=MAX_JSON_RESPONSE_BYTES,
        )
        return response.json(), len(response.body)

    @staticmethod
    def _validate_tag(delivery_tag: str) -> None:
        if not isinstance(delivery_tag, str) or not delivery_tag.strip():
            raise InvalidPaginationError(details={"setting": "delivery_tag"})

    @staticmethod
    def _validate_pagination(
        page_size: int,
        max_items: int,
        max_pages: int,
    ) -> None:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE
            or isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= DEFAULT_MAX_ITEMS
            or isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or not 1 <= max_pages <= DEFAULT_MAX_PAGES
        ):
            raise InvalidPaginationError()

    @staticmethod
    def _page(payload: Any) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(payload, dict):
            raise MalformedResponseError(
                details={"expected": ["data", "hasMore"]}
            )
        data = payload.get("data")
        has_more = payload.get("hasMore")
        if not isinstance(data, list) or type(has_more) is not bool:
            raise MalformedResponseError(
                details={"expected": ["data", "hasMore"]}
            )
        if any(not isinstance(item, dict) for item in data):
            raise MalformedResponseError(
                details={"expected": "article metadata objects"}
            )
        return [_validate_article_metadata(item) for item in data], has_more

    def list_articles(
        self,
        delivery_tag: str = DEFAULT_DELIVERY_TAG,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int = DEFAULT_MAX_ITEMS,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """List metadata matching one explicit delivery tag.

        The official API performs the ``tag`` filter.  Pages are bounded to the
        documented 1,000-item limit, offsets advance by the actual page size,
        and malformed/stalled or unexpectedly large collections fail safely.
        """

        self._validate_tag(delivery_tag)
        self._validate_pagination(page_size, max_items, max_pages)
        articles: list[dict[str, Any]] = []
        retained_bytes = 0
        offset = 0
        pages = 0
        while True:
            if pages >= max_pages:
                raise PaginationError(details={"setting": "max_pages"})
            payload, page_bytes = self._request_json(
                "/links",
                (
                    ("tag", delivery_tag),
                    ("limit", page_size),
                    ("offset", offset),
                ),
            )
            page, has_more = self._page(payload)
            pages += 1
            if len(page) > page_size:
                raise MalformedResponseError(details={"expected": "bounded page"})
            retained_bytes += page_bytes
            if retained_bytes > MAX_RETAINED_METADATA_BYTES:
                raise PaginationError(details={"setting": "max_retained_bytes"})
            if len(articles) + len(page) > max_items:
                raise PaginationError(details={"setting": "max_items"})
            articles.extend(page)
            if not has_more:
                return articles
            if not page:
                raise PaginationError(details={"setting": "non_empty_pages"})
            offset += len(page)

    def get_article_metadata(self, article_id: str) -> dict[str, Any]:
        """Retrieve metadata for one link through the official read endpoint."""

        self._validate_article_id(article_id)
        payload, _page_bytes = self._request_json(
            f"/links/{self._encode_path_component(article_id)}",
            encoded_path=True,
        )
        return _validate_article_metadata(payload)

    def fetch_html(self, article_id: str) -> str:
        """Fetch cleaned HTML with GoodLinks automatic download enabled."""

        self._validate_article_id(article_id)
        return self._fetch_html_response(article_id).text()

    def fetch_article(self, article: str | Mapping[str, Any]) -> FetchedArticle:
        """Fetch HTML for an ID or listed metadata object."""

        if isinstance(article, str):
            metadata = self.get_article_metadata(article)
        elif isinstance(article, Mapping):
            metadata = _validate_article_metadata(dict(article))
        else:
            raise MalformedResponseError(
                details={"expected": "article ID or metadata"}
            )
        article_id = metadata["id"]
        response = self._fetch_html_response(article_id)
        return FetchedArticle(
            metadata=metadata,
            html=response.text(),
            content_type=response.content_type or "",
        )

    def _fetch_html_response(self, article_id: str) -> _Response:
        self._validate_article_id(article_id)
        return self._request(
            f"/links/{self._encode_path_component(article_id)}/content",
            (("format", "html"), ("autoDownload", True)),
            encoded_path=True,
            maximum=MAX_HTML_RESPONSE_BYTES,
            expected_content_type="text/html",
        )

    @staticmethod
    def _validate_article_id(article_id: str) -> None:
        if not _is_non_empty_string(article_id):
            raise MalformedResponseError(details={"expected": "article id"})


__all__ = [
    "DEFAULT_API_URL",
    "DEFAULT_DELIVERY_TAG",
    "DEFAULT_MAX_ITEMS",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_PAGE_SIZE",
    "MAX_ERROR_RESPONSE_BYTES",
    "MAX_HTML_RESPONSE_BYTES",
    "MAX_JSON_RESPONSE_BYTES",
    "MAX_PAGE_SIZE",
    "MAX_RETAINED_METADATA_BYTES",
    "APIResponseError",
    "APIUnavailableError",
    "AuthenticationError",
    "ConfigurationError",
    "FetchedArticle",
    "GoodLinksClient",
    "GoodLinksError",
    "InsecureAPIURLError",
    "InvalidAPIURLError",
    "InvalidPaginationError",
    "InvalidTimeoutError",
    "InvalidTokenError",
    "MalformedResponseError",
    "MissingTokenError",
    "PaginationError",
    "ResponseTooLargeError",
]
