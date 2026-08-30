"""Command-line entry point for the GoodLinks-to-CrossPoint workflow."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import NoReturn

from . import __version__
from .api import (
    DEFAULT_API_URL,
    DEFAULT_DELIVERY_TAG,
    GoodLinksClient,
    GoodLinksError,
)
from .crosspoint import (
    DEFAULT_CROSSPOINT_URL,
    DEFAULT_REMOTE_DIRECTORY,
    DEFAULT_TIMEOUT,
    CrossPointClient,
    CrossPointError,
)
from .epub import (
    DEFAULT_PANDOC_EXECUTABLE,
    DEFAULT_PANDOC_TIMEOUT,
    EpubExportError,
)
from .orchestration import (
    DEFAULT_OUTPUT_DIRECTORY,
    Orchestrator,
    WorkflowError,
    WorkflowResult,
)


class _ArgumentParser(argparse.ArgumentParser):
    """Keep invalid arguments from being echoed into command output."""

    def error(self, message: str) -> NoReturn:
        del message
        self.exit(
            2,
            f"{self.format_usage()}{self.prog}: error: invalid command-line arguments\n",
        )


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--api-url",
        default=None,
        metavar="URL",
        help=f"GoodLinks read API URL (default: {DEFAULT_API_URL}).",
    )
    parser.add_argument(
        "--tag",
        default=DEFAULT_DELIVERY_TAG,
        metavar="TAG",
        help=(
            "GoodLinks delivery tag to queue (default: "
            f"{DEFAULT_DELIVERY_TAG})."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIRECTORY,
        metavar="DIRECTORY",
        help=(
            "Directory for generated EPUBs (default: "
            f"{DEFAULT_OUTPUT_DIRECTORY})."
        ),
    )
    parser.add_argument(
        "--pandoc-executable",
        default=DEFAULT_PANDOC_EXECUTABLE,
        metavar="PATH",
        help="External Pandoc executable (default: pandoc).",
    )
    parser.add_argument(
        "--pandoc-timeout",
        default=DEFAULT_PANDOC_TIMEOUT,
        type=float,
        metavar="SECONDS",
        help="Pandoc timeout in seconds.",
    )
    parser.add_argument(
        "--api-timeout",
        default=15.0,
        type=float,
        metavar="SECONDS",
        help="GoodLinks API timeout in seconds.",
    )


def _add_sync_device_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device-url",
        default=DEFAULT_CROSSPOINT_URL,
        metavar="URL",
        help=f"CrossPoint URL (default: {DEFAULT_CROSSPOINT_URL}).",
    )
    parser.add_argument(
        "--destination",
        default=DEFAULT_REMOTE_DIRECTORY,
        metavar="PATH",
        help=f"CrossPoint destination (default: {DEFAULT_REMOTE_DIRECTORY}).",
    )
    parser.add_argument(
        "--device-timeout",
        default=DEFAULT_TIMEOUT,
        type=float,
        metavar="SECONDS",
        help="CrossPoint request timeout in seconds.",
    )


def _add_planning_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan reads and work without Pandoc, output, manifest, or device writes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate and (for sync) overwrite only with this explicit flag.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the export, send, and sync command parser."""

    parser = _ArgumentParser(
        prog="goodlinks-crosspoint",
        description=(
            "Privacy-safe GoodLinks-to-CrossPoint CLI foundation. "
            "Export tagged articles, send EPUBs, or sync to CrossPoint."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    export_parser = commands.add_parser(
        "export", help="Generate EPUBs for tagged GoodLinks articles."
    )
    _add_source_options(export_parser)
    _add_planning_options(export_parser)

    send_parser = commands.add_parser(
        "send", help="Upload one existing EPUB to CrossPoint."
    )
    send_parser.add_argument("epub", metavar="EPUB")
    send_parser.add_argument(
        "--device-url",
        default=DEFAULT_CROSSPOINT_URL,
        metavar="URL",
        help=f"CrossPoint URL (default: {DEFAULT_CROSSPOINT_URL}).",
    )
    send_parser.add_argument(
        "--destination",
        default=DEFAULT_REMOTE_DIRECTORY,
        metavar="PATH",
        help=f"CrossPoint destination (default: {DEFAULT_REMOTE_DIRECTORY}).",
    )
    send_parser.add_argument(
        "--device-timeout",
        default=DEFAULT_TIMEOUT,
        type=float,
        metavar="SECONDS",
        help="CrossPoint request timeout in seconds.",
    )
    send_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing remote file.",
    )

    sync_parser = commands.add_parser(
        "sync", help="Export tagged GoodLinks articles and upload them."
    )
    _add_source_options(sync_parser)
    _add_sync_device_options(sync_parser)
    _add_planning_options(sync_parser)
    return parser


def _print_result(result: WorkflowResult) -> None:
    prefix = f"{result.command}"
    if result.dry_run:
        prefix += " (dry-run)"
    values = [
        f"selected={result.selected}",
        f"generated={result.generated}",
        f"generation_skipped={result.generation_skipped}",
        f"uploaded={result.uploaded}",
        f"upload_skipped={result.upload_skipped}",
        f"planned_generation={result.planned_generation}",
        f"planned_upload={result.planned_upload}",
        f"failed={result.failed}",
    ]
    if result.error_codes:
        values.append(
            "errors="
            + ",".join(f"{code}:{count}" for code, count in result.error_codes)
        )
    print(f"{prefix}: " + " ".join(values))


def _print_error(error: BaseException) -> None:
    code = getattr(error, "code", "workflow_error")
    if (
        not isinstance(code, str)
        or not 0 < len(code) <= 64
        or not code.isascii()
        or not all(character.isalnum() or character in {"_", "-"} for character in code)
    ):
        code = "workflow_error"
    # Use the class-level static message rather than an instance message: a
    # dependency could otherwise attach a token, response body, or article
    # value to its exception text.
    message = getattr(type(error), "default_message", None)
    if not isinstance(message, str) or not message or len(message) > 300:
        message = "The command could not be completed."
    print(f"error: {code}: {message}", file=sys.stderr)


def _run_export(args: argparse.Namespace) -> int:
    goodlinks = GoodLinksClient(args.api_url, timeout=args.api_timeout)
    orchestrator = Orchestrator(
        goodlinks,
        args.output_dir,
        tag=args.tag,
        pandoc_executable=args.pandoc_executable,
        pandoc_timeout=args.pandoc_timeout,
        force=args.force,
        dry_run=args.dry_run,
    )
    result = orchestrator.run_export()
    _print_result(result)
    return 1 if result.failed else 0


def _run_sync(args: argparse.Namespace) -> int:
    goodlinks = GoodLinksClient(args.api_url, timeout=args.api_timeout)
    # Constructing the canonical client validates the configured endpoint but
    # performs no network I/O; the orchestrator never calls it in dry-run mode.
    device = CrossPointClient(args.device_url, timeout=args.device_timeout)
    orchestrator = Orchestrator(
        goodlinks,
        args.output_dir,
        tag=args.tag,
        pandoc_executable=args.pandoc_executable,
        pandoc_timeout=args.pandoc_timeout,
        remote_directory=args.destination,
        force=args.force,
        dry_run=args.dry_run,
        crosspoint=device,
    )
    result = orchestrator.run_sync()
    _print_result(result)
    return 1 if result.failed else 0


def _run_send(args: argparse.Namespace) -> int:
    device = CrossPointClient(args.device_url, timeout=args.device_timeout)
    device.upload_epub(
        args.epub,
        remote_directory=args.destination,
        overwrite=args.overwrite,
    )
    print("send: uploaded=1 failed=0")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one explicit workflow command and return its exit status."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "export":
            return _run_export(args)
        if args.command == "sync":
            return _run_sync(args)
        if args.command == "send":
            return _run_send(args)
    except (GoodLinksError, EpubExportError, CrossPointError, WorkflowError) as error:
        _print_error(error)
        return 1
    except Exception:  # noqa: BLE001
        # A dependency or filesystem implementation must not turn a command
        # line failure into a traceback containing article or server data.
        _print_error(WorkflowError())
        return 1
    return 2
