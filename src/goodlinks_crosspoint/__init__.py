"""Privacy-safe GoodLinks-to-CrossPoint clients and CLI foundation."""

import sys as _sys

_MINIMUM_PYTHON = (3, 11)


def _check_runtime_version(version_info=_sys.version_info):
    """Reject unsupported interpreters before importing package modules."""

    detected = (version_info[0], version_info[1])
    if detected < _MINIMUM_PYTHON:
        required_text = ".".join(map(str, _MINIMUM_PYTHON))
        detected_text = ".".join(map(str, detected))
        raise RuntimeError(
            "goodlinks-to-crosspoint requires Python "
            + required_text
            + " or newer; detected Python "
            + detected_text
            + "."
        )


_check_runtime_version()

from .api import (
    DEFAULT_API_URL,
    DEFAULT_DELIVERY_TAG,
    FetchedArticle,
    GoodLinksClient,
    GoodLinksError,
)
from .crosspoint import (
    DEFAULT_CROSSPOINT_URL,
    DEFAULT_REMOTE_DIRECTORY,
    CrossPointClient,
    CrossPointConfigurationError,
    CrossPointError,
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
    RemoteEntry,
    RemoteFileExistsError,
    UploadTooLargeError,
    WrongDeviceError,
    normalize_remote_path,
)
from .epub import (
    CROSSPOINT_CSS,
    DEFAULT_PANDOC_EXECUTABLE,
    DEFAULT_PANDOC_TIMEOUT,
    EpubExportError,
    EpubOutputError,
    InvalidArticleError,
    InvalidPandocConfigurationError,
    PandocEpubExporter,
    PandocInvocationError,
    PandocNotFoundError,
    PandocVersionError,
    export_article,
    export_articles,
    safe_epub_filename,
)
from .orchestration import (
    DEFAULT_OUTPUT_DIRECTORY,
    ManifestError,
    ManifestLockError,
    Orchestrator,
    WorkflowError,
    WorkflowInputError,
    WorkflowResult,
    article_content_hash,
    conversion_config_hash,
    manifest_lock_path,
    manifest_path,
)

__all__ = [
    "CROSSPOINT_CSS",
    "DEFAULT_API_URL",
    "DEFAULT_CROSSPOINT_URL",
    "DEFAULT_DELIVERY_TAG",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_PANDOC_EXECUTABLE",
    "DEFAULT_PANDOC_TIMEOUT",
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
    "EpubExportError",
    "EpubOutputError",
    "FetchedArticle",
    "GoodLinksClient",
    "GoodLinksError",
    "InvalidArticleError",
    "InvalidCrossPointURLError",
    "InvalidEPUBError",
    "InvalidPandocConfigurationError",
    "InvalidRemotePathError",
    "ManifestError",
    "ManifestLockError",
    "Orchestrator",
    "PandocEpubExporter",
    "PandocInvocationError",
    "PandocNotFoundError",
    "PandocVersionError",
    "RemoteEntry",
    "RemoteFileExistsError",
    "UploadTooLargeError",
    "WorkflowError",
    "WorkflowInputError",
    "WorkflowResult",
    "WrongDeviceError",
    "__version__",
    "article_content_hash",
    "conversion_config_hash",
    "export_article",
    "export_articles",
    "manifest_lock_path",
    "manifest_path",
    "normalize_remote_path",
    "safe_epub_filename",
]

__version__ = "0.1.0"
