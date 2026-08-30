from __future__ import annotations

import json
import os
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from goodlinks_crosspoint.api import (
    DEFAULT_API_URL,
    MAX_PAGE_SIZE,
    APIResponseError,
    APIUnavailableError,
    AuthenticationError,
    FetchedArticle,
    GoodLinksClient,
    InsecureAPIURLError,
    InvalidAPIURLError,
    InvalidPaginationError,
    InvalidTokenError,
    MalformedResponseError,
    PaginationError,
    ResponseTooLargeError,
)

SYNTHETIC_HTML = "<article><h1>Example</h1><p>Synthetic content.</p></article>"


class GoodLinksFixtureHandler(BaseHTTPRequestHandler):
    expected_token = "synthetic-server-token"
    malformed = False
    server_error = False
    over_page = False
    aggregate_pages = False
    content_type: str | None = "text/html; charset=UTF-8"
    metadata_override: dict[str, object] | None = None
    requests: ClassVar[list[tuple[str, dict[str, list[str]], str | None]]]
    methods: ClassVar[list[str]]

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        self.__class__.methods.append(self.command)
        self.__class__.requests.append(
            (parsed.path, query, self.headers.get("Authorization"))
        )
        if self.headers.get("Authorization") != f"Bearer {self.expected_token}":
            self._send(
                401,
                b'{"error":"Unauthorized","details":{"message":"redacted"}}',
                "application/json",
            )
            return

        if self.__class__.server_error:
            self._send(
                500,
                b'{"error":"synthetic-server-failure","body":"synthetic-body"}',
            )
            return

        if parsed.path == "/api/v1/links" and self.malformed:
            self._send(200, b'{"data":"synthetic-invalid-response"}')
            return

        if parsed.path == "/api/v1/links":
            offset = int(query.get("offset", ["0"])[0])
            if self.__class__.aggregate_pages:
                pages = {
                    0: ([self._metadata("one"), self._metadata("two")], True),
                    2: ([self._metadata("three")], False),
                }
            elif self.__class__.over_page:
                pages = {
                    0: (
                        [
                            self._metadata("one"),
                            self._metadata("two"),
                            self._metadata("three"),
                        ],
                        False,
                    )
                }
            else:
                pages = {
                    0: ([self._metadata("one"), self._metadata("two")], True),
                    2: ([self._metadata("three")], False),
                }
            data, has_more = pages.get(offset, ([], False))
            self._send(
                200,
                json.dumps({"data": data, "hasMore": has_more}).encode(),
            )
            return

        if parsed.path == "/api/v1/links/one":
            self._send(200, json.dumps(self._metadata("one")).encode())
            return

        if parsed.path == "/api/v1/links/one/content":
            self._send(200, SYNTHETIC_HTML.encode(), self.__class__.content_type)
            return

        self._send(404, b'{"error":"not found"}')

    @classmethod
    def _metadata(cls, article_id: str) -> dict[str, object]:
        metadata: dict[str, object] = {
            "id": article_id,
            "url": f"https://example.com/{article_id}",
            "title": f"Example {article_id}",
            "summary": "Synthetic summary.",
            "author": "Example Author",
            "tags": ["x3"],
            "wordCount": 2,
            "starred": False,
            "highlighted": False,
            "addedAt": "2025-01-15T10:30:00Z",
            "modifiedAt": "2025-01-15T10:30:00Z",
            "readAt": None,
        }
        if cls.metadata_override:
            metadata.update(cls.metadata_override)
        return metadata

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str | None = "application/json",
    ) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class LocalHTTPServer(ThreadingHTTPServer):
    """Avoid reverse-DNS lookup when binding synthetic local test servers."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class APIFixture(TestCase):
    def setUp(self) -> None:
        GoodLinksFixtureHandler.requests = []
        GoodLinksFixtureHandler.methods = []
        GoodLinksFixtureHandler.malformed = False
        GoodLinksFixtureHandler.server_error = False
        GoodLinksFixtureHandler.over_page = False
        GoodLinksFixtureHandler.aggregate_pages = False
        GoodLinksFixtureHandler.content_type = "text/html; charset=UTF-8"
        GoodLinksFixtureHandler.metadata_override = None
        self.server = LocalHTTPServer(("127.0.0.1", 0), GoodLinksFixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        self.environment = mock.patch.dict(
            os.environ,
            {"GOODLINKS_TOKEN": GoodLinksFixtureHandler.expected_token},
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class URLTests(TestCase):
    def test_default_url_and_loopback_policy(self) -> None:
        self.assertEqual(DEFAULT_API_URL, "http://127.0.0.1:9428/api/v1")
        with mock.patch.dict(os.environ, {"GOODLINKS_TOKEN": "synthetic-url-token"}):
            self.assertIsInstance(GoodLinksClient(), GoodLinksClient)
            self.assertIsInstance(
                GoodLinksClient("https://example.com/goodlinks/api"), GoodLinksClient
            )
            with self.assertRaises(InsecureAPIURLError):
                GoodLinksClient("http://example.com/api/v1")
            client = GoodLinksClient()
        self.assertFalse(hasattr(client, "base_url"))
        self.assertFalse(hasattr(client, "api_url"))
        self.assertFalse(hasattr(client, "build_url"))
        self.assertFalse(hasattr(client, "encode_path_component"))
        self.assertFalse(hasattr(client, "list_tagged_articles"))

    def test_url_credentials_and_queries_are_rejected(self) -> None:
        for value in (
            "http://user:secret@127.0.0.1:9428/api/v1",
            "http://127.0.0.1:9428/api/v1?token=secret",
            "http://127.0.0.1:9428/api/v1#secret",
        ):
            with self.subTest(value=value):
                with mock.patch.dict(
                    os.environ, {"GOODLINKS_TOKEN": "synthetic-url-token"}
                ), self.assertRaises(InvalidAPIURLError) as raised:
                    GoodLinksClient(value)
                self.assertNotIn("secret", str(raised.exception))

    def test_non_ascii_tokens_are_rejected_before_transport(self) -> None:
        for token in ("synthetic-tökən", "synthetic-\u200b-token"):
            with self.subTest(token=token):
                transport = mock.Mock()
                with mock.patch.dict(
                    os.environ, {"GOODLINKS_TOKEN": token}
                ), self.assertRaises(InvalidTokenError) as raised:
                    GoodLinksClient(transport=transport)
                transport.open.assert_not_called()
                self.assertNotIn(token, str(raised.exception))
                self.assertNotIn(token, str(raised.exception.as_dict()))


class ClientHTTPTests(APIFixture):
    def test_list_articles_filters_on_server_and_paginates_by_actual_page_size(
        self,
    ) -> None:
        client = GoodLinksClient(self.api_url)

        articles = client.list_articles(page_size=2)

        self.assertEqual(
            [article["id"] for article in articles], ["one", "two", "three"]
        )
        self.assertEqual(
            [request[1]["offset"][0] for request in GoodLinksFixtureHandler.requests],
            ["0", "2"],
        )
        for _path, query, authorization in GoodLinksFixtureHandler.requests:
            self.assertEqual(query["tag"], ["x3"])
            self.assertEqual(query["limit"], ["2"])
            self.assertEqual(authorization, "Bearer synthetic-server-token")

    def test_fetch_article_gets_metadata_and_cleaned_html(self) -> None:
        client = GoodLinksClient(self.api_url)

        article = client.fetch_article("one")

        self.assertIsInstance(article, FetchedArticle)
        self.assertEqual(article.metadata["url"], "https://example.com/one")
        self.assertEqual(article.html, SYNTHETIC_HTML)
        self.assertEqual(article.content_type, "text/html; charset=UTF-8")
        self.assertNotIn(SYNTHETIC_HTML, repr(article))
        content_requests = [
            request
            for request in GoodLinksFixtureHandler.requests
            if request[0].endswith("/content")
        ]
        self.assertEqual(len(content_requests), 1)
        self.assertEqual(content_requests[0][1]["format"], ["html"])
        self.assertEqual(content_requests[0][1]["autoDownload"], ["true"])

    def test_content_requires_html_media_type_without_echoing_body(self) -> None:
        client = GoodLinksClient(self.api_url)
        for content_type in ("application/json", "text/plain", None):
            with self.subTest(content_type=content_type):
                GoodLinksFixtureHandler.content_type = content_type
                with self.assertRaises(MalformedResponseError) as raised:
                    client.fetch_html("one")
                self.assertNotIn(SYNTHETIC_HTML, str(raised.exception))
        GoodLinksFixtureHandler.content_type = "TEXT/HTML; charset=utf-8"
        self.assertEqual(client.fetch_html("one"), SYNTHETIC_HTML)

    def test_all_requests_are_get_only(self) -> None:
        client = GoodLinksClient(self.api_url)
        client.list_articles(page_size=2, max_items=3)
        self.assertTrue(GoodLinksFixtureHandler.requests)
        # The fixture records each HTTP method; the client has no write method
        # and therefore cannot send a mutation request.
        self.assertTrue(
            all(
                request[0].startswith("/api/v1/")
                for request in GoodLinksFixtureHandler.requests
            )
        )
        self.assertEqual(GoodLinksFixtureHandler.methods, ["GET", "GET"])

    def test_authentication_error_does_not_echo_token_or_error_body(self) -> None:
        with mock.patch.dict(os.environ, {"GOODLINKS_TOKEN": "synthetic-client-token"}):
            client = GoodLinksClient(self.api_url)

        with self.assertRaises(AuthenticationError) as raised:
            client.list_articles()
        self.assertNotIn("synthetic-client-token", str(raised.exception))
        self.assertNotIn("redacted", str(raised.exception.as_dict()))

    def test_malformed_response_is_safe(self) -> None:
        GoodLinksFixtureHandler.malformed = True
        client = GoodLinksClient(self.api_url)

        with self.assertRaises(MalformedResponseError) as raised:
            client.list_articles()
        self.assertNotIn("synthetic-invalid-response", str(raised.exception))

    def test_api_error_does_not_echo_response_body(self) -> None:
        GoodLinksFixtureHandler.server_error = True
        client = GoodLinksClient(self.api_url)

        with self.assertRaises(APIResponseError) as raised:
            client.list_articles()
        self.assertNotIn("synthetic-body", str(raised.exception))

    def test_unavailable_server_is_reported_without_transport_details(self) -> None:
        client = GoodLinksClient(self.api_url)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

        with self.assertRaises(APIUnavailableError) as raised:
            client.list_articles()
        self.assertNotIn(self.api_url, str(raised.exception))


class MetadataValidationTests(APIFixture):
    def test_unknown_and_nullable_metadata_fields_are_preserved(self) -> None:
        GoodLinksFixtureHandler.metadata_override = {
            "futureField": {"enabled": True},
            "title": None,
            "author": None,
            "summary": None,
            "tags": None,
            "wordCount": None,
        }
        client = GoodLinksClient(self.api_url)

        articles = client.list_articles(page_size=2)
        self.assertEqual(articles[0]["futureField"], {"enabled": True})
        self.assertIsNone(articles[0]["title"])
        self.assertIsNone(client.get_article_metadata("one")["author"])
        fetched = client.fetch_article("one")
        self.assertEqual(fetched.metadata["futureField"], {"enabled": True})

    def test_documented_metadata_types_are_checked_at_each_boundary(self) -> None:
        invalid_fields = {
            "id": "",
            "url": 42,
            "title": False,
            "author": [],
            "tags": ["x3", 7],
            "starred": "false",
            "wordCount": False,
            "addedAt": None,
            "readAt": 123,
        }
        client = GoodLinksClient(self.api_url)
        for field_name, invalid_value in invalid_fields.items():
            with self.subTest(field=field_name):
                GoodLinksFixtureHandler.metadata_override = {
                    field_name: invalid_value
                }
                with self.assertRaises(MalformedResponseError):
                    client.list_articles(page_size=2)
                with self.assertRaises(MalformedResponseError):
                    client.get_article_metadata("one")
                with self.assertRaises(MalformedResponseError):
                    client.fetch_article("one")
                with self.assertRaises(MalformedResponseError):
                    client.fetch_article({"id": "one", field_name: invalid_value})
        GoodLinksFixtureHandler.metadata_override = None


class PaginationBoundTests(APIFixture):
    def test_over_page_response_is_rejected(self) -> None:
        GoodLinksFixtureHandler.over_page = True
        client = GoodLinksClient(self.api_url)
        with self.assertRaises(MalformedResponseError):
            client.list_articles(page_size=2)

    def test_json_page_and_aggregate_limits_are_bounded(self) -> None:
        GoodLinksFixtureHandler.metadata_override = {"futureField": "x" * 100}
        GoodLinksFixtureHandler.aggregate_pages = True
        client = GoodLinksClient(self.api_url)
        first_page = json.dumps(
            {
                "data": [
                    GoodLinksFixtureHandler._metadata("one"),
                    GoodLinksFixtureHandler._metadata("two"),
                ],
                "hasMore": True,
            }
        ).encode()
        final_page = json.dumps(
            {
                "data": [GoodLinksFixtureHandler._metadata("three")],
                "hasMore": False,
            }
        ).encode()
        aggregate_limit = len(first_page) + len(final_page) - 1
        with mock.patch(
            "goodlinks_crosspoint.api.MAX_RETAINED_METADATA_BYTES", aggregate_limit
        ), self.assertRaises(PaginationError):
            client.list_articles(page_size=2, max_items=3)

        GoodLinksFixtureHandler.aggregate_pages = False
        GoodLinksFixtureHandler.metadata_override = {
            "futureField": "x" * 300
        }
        with mock.patch(
            "goodlinks_crosspoint.api.MAX_JSON_RESPONSE_BYTES", 256
        ), self.assertRaises(ResponseTooLargeError):
            client.list_articles(page_size=2)

    def test_page_size_cannot_exceed_official_limit(self) -> None:
        client = GoodLinksClient(self.api_url)
        with self.assertRaises(InvalidPaginationError):
            client.list_articles(page_size=MAX_PAGE_SIZE + 1)

    def test_empty_page_claiming_more_is_rejected(self) -> None:
        class EmptyPageHandler(GoodLinksFixtureHandler):
            def do_GET(self) -> None:
                self._send(200, b'{"data":[],"hasMore":true}')

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = LocalHTTPServer(("127.0.0.1", 0), EmptyPageHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        client = GoodLinksClient(self.api_url)

        with self.assertRaises(PaginationError):
            client.list_articles()


if __name__ == "__main__":
    import unittest

    unittest.main()
