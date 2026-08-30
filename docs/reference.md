# GoodLinks to CrossPoint reference

This is the comprehensive reference for the current `0.1.0` command. It
covers the direct CLI, the pass-specific `sync.sh` wrapper, local state,
CrossPoint behavior, recovery, and undo instructions.

The short, personal setup walkthrough remains in the
[README](../README.md). Use it for the one-time setup; this page avoids
repeating those setup steps and is the detailed behavior reference. Every
example below uses repository-relative paths and synthetic values only. Do not
replace them with a real token, article, URL, hostname, address, or file.

## Overview

The workflow has three explicit operations:

- `export` reads articles selected by a GoodLinks tag and creates local EPUBs.
- `send` uploads one existing local EPUB to CrossPoint.
- `sync` combines `export` and `send` with local idempotency state.

GoodLinks is a read-only source. The client uses its read API to list metadata
and fetch cleaned HTML; it does not change tags, read state, or article data.
Pandoc is a separately installed executable. CrossPoint is contacted only by a
real `send` or `sync`, never by `export`.

The package requires Python 3.11 or newer, has no runtime Python dependencies,
and exposes the `goodlinks-crosspoint` console script. The project-local
wrapper is a macOS convenience command; the direct Python CLI has no wrapper's
macOS-only guard.

## Safety boundaries

GoodLinks access uses a bearer token. CrossPoint's wireless File Transfer HTTP
server is unauthenticated and has no TLS. Use it only on a trusted private LAN
or the device's temporary private hotspot. Do not expose it to a shared or
public network, the Internet, or a port forward.

The repository ignores local environments, GoodLinks databases and exports,
credentials, generated EPUBs, output, manifests, locks, and logs. An ignore
rule is not a privacy boundary: keep those files local and inspect status
before sharing a patch. `.sync.env` is ignored, while
`.sync.env.example` is intentionally tracked.

Examples and tests use synthetic `example.com` values such as
`https://example.com/articles/synthetic-one`. Never put real article content,
metadata, URLs, tokens, databases, exports, device addresses, or directory
listings in this repository. See [SECURITY.md](../SECURITY.md) and
[CONTRIBUTING.md](../CONTRIBUTING.md) for the repository-wide rules.

## Prerequisites and operating modes

The one-time installation commands are intentionally kept in the README. The
behavioral prerequisites are:

- Python 3.11 or newer, as declared by `pyproject.toml`.
- GoodLinks 3.2 or newer for the documented local read API. Keep GoodLinks
  open with **Settings > API** enabled while running `export` or `sync`.
- Pandoc available as a separate executable for a real `export` or `sync`.
  This project does not install Pandoc. `send` and dry-runs do not invoke it.
- A CrossPoint device that reports `X3` or `X4` from its status endpoint. Put
  it in wireless **File Transfer** mode before a real upload and leave that
  mode active until the transfer is complete.

The device may join the same trusted private network as the Mac or provide a
private temporary hotspot for the Mac. The CLI does not enter or leave device
modes, discover devices, or verify the user's network choice. The default
CrossPoint URL is `http://crosspoint.local` and the default remote directory is
`/GoodLinks`.

## Credentials: direct CLI versus wrapper

These two invocation paths are deliberately different. Do not mix their
credential sources.

### Direct CLI: ephemeral `GOODLINKS_TOKEN`

`export` and `sync` construct a GoodLinks client and therefore require
`GOODLINKS_TOKEN` in the process environment, including for `--dry-run`.
`send` does not construct a GoodLinks client, so it does not require GoodLinks,
the API server, or `GOODLINKS_TOKEN`.

Read a token silently and remove it after the command. This synthetic-safe
pattern does not put a credential in argv or shell history:

```console
printf 'GoodLinks API token: '
IFS= read -r -s GOODLINKS_TOKEN
printf '\n'
export GOODLINKS_TOKEN
.venv/bin/goodlinks-crosspoint sync --dry-run
unset GOODLINKS_TOKEN
```

Use the same pattern for `export` or a real `sync`. Do not write a token after
`GOODLINKS_TOKEN=`, put it in a URL, config file, script, manifest, or log, or
enable shell tracing while it is set. There is no supported `--token` option;
unknown options are rejected without echoing their values.

The GoodLinks client rejects a missing token and rejects an empty, non-ASCII,
control-character, or leading/trailing-whitespace value. It sends the token
only as a bearer header to the configured GoodLinks endpoint. API redirects
are not followed.

The effective default GoodLinks API URL is
`http://127.0.0.1:9428/api/v1`. `--api-url` may select another absolute HTTP or
HTTPS endpoint, subject to the URL rules in [GoodLinks API](#goodlinks-api).

### Pass-specific `sync.sh` wrapper

`sync.sh` is only for the normal repository-local `sync` flow on macOS. It
reads the token from the password-store entry
`goodlinks-crosspoint/goodlinks-token`; it does not use an inherited
`GOODLINKS_TOKEN`, put the token in argv, or print it. The setup walkthrough in
the README shows how to create the ignored `.sync.env` and the pass entry.

The wrapper parses `.sync.env` as data rather than sourcing it. The file must
be a readable regular file, not a symlink, and must contain exactly one
non-blank setting with this form:

```text
GOODLINKS_TAG=x3
```

Blank lines and comments (including indented comments) are allowed. Tag values
may contain spaces and Unicode. A blank value, surrounding whitespace, control
character, leading `-`, duplicate setting, missing setting, or unsupported
setting is rejected. In particular, `.sync.env` is not a place for a token or
any other credential.

Before it invokes Python, the wrapper checks its repository `.venv`, source
tree, macOS platform, `pass`, and macOS `dscacheutil`. It resolves the fixed
host `crosspoint.local` and selects the first canonical private RFC1918 IPv4
answer (`10/8`, `172.16/12`, or `192.168/16`). It never reports the resolved
address. Resolution happens even for `./sync.sh --dry-run`, so a dry-run
through the wrapper still needs that host to resolve; this is additional to
the direct Python dry-run behavior.

The wrapper appends and therefore pins these three values after caller
arguments:

- `--tag` from `GOODLINKS_TAG`;
- `--output-dir` to the checkout's absolute `export/` directory; and
- `--device-url` to the resolved `http://` address.

It forwards other supported `sync` flags, including `--api-url`,
`--api-timeout`, `--pandoc-executable`, `--pandoc-timeout`,
`--device-timeout`, `--dry-run`, `--force`, and `--destination`. Since the
fixed flags are last, attempts to override the tag, output directory, or
device URL lose to the wrapper's values. `sync` still does not accept
`--overwrite`.

Typical wrapper invocations are:

```console
./sync.sh --dry-run
./sync.sh
./sync.sh --force
```

A wrapper dry-run still reads GoodLinks and requires the pass entry and token,
but the Python workflow does not invoke Pandoc, write output or manifest state,
or make a CrossPoint HTTP request.

## Direct CLI

Use the installed console script from the checkout's virtual environment. The
live parser is authoritative for syntax:

```console
.venv/bin/goodlinks-crosspoint --version
.venv/bin/goodlinks-crosspoint --help
.venv/bin/goodlinks-crosspoint export --help
.venv/bin/goodlinks-crosspoint send --help
.venv/bin/goodlinks-crosspoint sync --help
```

The current version is `0.1.0`. `--help` is available at the root and for each
subcommand. Invoking the program without a command prints root help and exits
successfully. Invalid or unsupported arguments exit with status 2 and use a
generic diagnostic that does not echo the rejected value.

### Option defaults

The following are the parser's current options and effective defaults. A flag
not listed for a command is not accepted by that command.

#### `export` and `sync` source options

- `--api-url URL` — GoodLinks API base; default
  `http://127.0.0.1:9428/api/v1`.
- `--tag TAG` — GoodLinks delivery tag; default `x3`.
- `--output-dir DIRECTORY` — generated EPUB directory; default `export`.
- `--pandoc-executable PATH` — external executable; default `pandoc`.
- `--pandoc-timeout SECONDS` — Pandoc timeout; default `120.0` seconds.
- `--api-timeout SECONDS` — GoodLinks request timeout; default `15.0` seconds.

`--api-timeout` and `--pandoc-timeout` must be finite, positive numbers. Leading
and trailing URL whitespace is stripped; remaining whitespace is invalid. The
GoodLinks URL must be absolute HTTP or HTTPS without credentials, a query, or a
fragment. Plain HTTP is allowed only for a loopback GoodLinks host; use HTTPS
for a non-loopback host.

#### Planning options for `export` and `sync`

- `--dry-run` — read and plan work without Pandoc, output, manifest, or device
  writes. It is off by default.
- `--force` — regenerate selected EPUBs and, for `sync`, explicitly permit
  remote replacement. It is off by default.

#### `send` options

- `EPUB` — one local EPUB path; this positional argument is required.
- `--device-url URL` — CrossPoint base URL; default
  `http://crosspoint.local`.
- `--destination PATH` — absolute CrossPoint directory; default `/GoodLinks`.
- `--device-timeout SECONDS` — CrossPoint request timeout; default `15.0`
  seconds.
- `--overwrite` — explicitly allow replacing an existing remote basename. It
  is off by default.

The CrossPoint URL is trimmed at its surrounding whitespace and must then be
absolute HTTP or HTTPS without credentials, a query, or a fragment. Its base
path must be ASCII, and trailing base-path slashes are removed. The timeout
must be finite, greater than zero, and no more than `300.0` seconds. A
destination must be a safe absolute path no longer than 1,024 UTF-8 bytes;
Unicode control, format, and surrogate characters, `.`/`..` segments, and
empty interior segments are rejected. A trailing destination slash is
normalized away.

#### Additional `sync` options

`sync` accepts all `export` source and planning options, plus:

- `--device-url URL` — same default and validation as `send`.
- `--destination PATH` — same default and validation as `send`.
- `--device-timeout SECONDS` — same default and validation as `send`.

`sync` has no `--overwrite` option. Its `--force` flag is the explicit remote
overwrite control.

## GoodLinks API

The CLI uses only the documented read operations. It asks GoodLinks for
metadata matching the selected tag, then fetches each selected article's
cleaned HTML with the equivalent of `format=html` and `autoDownload=true`.
The article source URL used in examples must remain synthetic, for example
`https://example.com/articles/synthetic-one`.

The default API URL is the loopback URL listed above. Surrounding whitespace
is stripped from a supplied value, and an origin-only GoodLinks base gains the
`/api/v1` path; trailing path slashes are removed. A supplied API base may
include a path, but cannot contain remaining whitespace, credentials, a query,
or a fragment. The client rejects insecure plain HTTP to non-loopback hosts.
It bounds API responses and pagination rather than following unbounded data:
metadata responses are limited to 4 MiB, HTML responses to 64 MiB, and retained
metadata to 64 MiB. The non-CLI API defaults to 100 items per page, at most
100,000 items and 1,000 pages; a page size cannot exceed 1,000.

GoodLinks pagination and malformed responses fail safely. A source fetch failure
marks that item failed and leaves an existing manifest entry untouched; other
selected items can still be processed. Duplicate article IDs in a returned
page set are processed once. GoodLinks tags and read state are never written
back by this project.

## `export`: generate local EPUBs

`export` selects the GoodLinks delivery tag, fetches the selected articles, and
runs Pandoc once per EPUB that needs generation. It never contacts CrossPoint.
A real export creates the output directory and local manifest state as needed.
A generated document is text-first: the exporter sanitizes HTML, keeps safe
text and links, and removes images, media, scripts, styles, and other blocked
content instead of fetching or embedding those assets.

Pandoc is run as a non-shell subprocess with HTML input, EPUB3 output,
standalone mode, a generated metadata file, and the CrossPoint stylesheet. The
input HTML, metadata, stylesheet, and staging EPUB live in private temporary
locations while conversion runs. A Pandoc version check runs before conversion.
The exporter does not install Pandoc or include command output in diagnostics.

Generated basenames are deterministic and basename-only. They use a sanitized
title and article ID plus a short SHA-256 identifier digest; punctuation such
as commas is not copied into the name. A safe source URL may appear in EPUB
metadata and its footer, so real article URLs must never be used in committed
examples.

A synthetic export command is:

```console
.venv/bin/goodlinks-crosspoint export \
  --tag x3 \
  --output-dir ./export \
  --pandoc-executable pandoc
```

## `send`: upload one existing EPUB

`send` is intentionally outside manifest bookkeeping. It does not create or
read a GoodLinks client, require `GOODLINKS_TOKEN`, invoke Pandoc, or read or
write an output manifest or lock. It opens one existing local file, verifies
CrossPoint status, creates the destination directory when needed, checks for a
case-insensitive duplicate basename, and uploads exactly one file.

The source must resolve to a non-empty regular file whose basename ends in
`.epub` (case-insensitive), is at most 255 UTF-8 bytes, and contains no comma,
double quote, path separator, or Unicode control, format, or surrogate
character. The upload is capped at 512 MiB. The client does not validate the
EPUB archive itself. A synthetic command is:

```console
.venv/bin/goodlinks-crosspoint send ./export/synthetic-article.epub \
  --device-url http://crosspoint.local \
  --destination /GoodLinks
```

A matching remote basename is refused by default. Add `--overwrite` only when
replacing that known remote file is intentional:

```console
.venv/bin/goodlinks-crosspoint send ./export/synthetic-article.epub \
  --device-url http://crosspoint.local \
  --destination /GoodLinks \
  --overwrite
```

The upload checks status before creating a missing destination or sending file
bytes. A status response must identify an `X3` or `X4` device. The client uses
the documented status, directory, directory-creation, and multipart-upload
endpoints; it does not authenticate to CrossPoint, follow redirects, delete
remote files, or change device mode.

The CrossPoint v1.5.0 web uploader has a filename-comma problem. Generated
basenames avoid commas, and `send` rejects one before any network request. Do
not bypass that validation with an unsafe filename.

## `sync`: export and upload with idempotency

`sync` performs the `export` work and then uploads each generated EPUB to the
configured CrossPoint directory. Its defaults are the `x3` GoodLinks tag,
`export` output directory, `http://crosspoint.local` device URL,
`/GoodLinks` destination, `pandoc` executable, and the timeouts listed above.
A real sync requires both a valid GoodLinks token and an available CrossPoint
File Transfer server.

For each selected article, the workflow:

1. lists and fetches the article through GoodLinks;
2. computes content and conversion-configuration hashes;
3. reuses a current local EPUB when its manifest entry and file hash match;
4. generates a missing or changed EPUB with Pandoc; and
5. uploads it when the manifest does not prove the expected remote path is
   complete.

The GoodLinks client is read-only. CrossPoint is contacted only for an actual
upload, and the CLI performs no background upload.

### Dry-run behavior

`export --dry-run` and `sync --dry-run` still construct the GoodLinks client,
read the selected metadata, and fetch each selected article's HTML. Therefore
they still require a valid direct token and a running GoodLinks API. They
compute the plan but do not invoke Pandoc, create the output directory, write a
manifest, create a missing lock file, or send CrossPoint requests.

A sync dry-run constructs and validates the configured CrossPoint client but
never contacts the device. It can therefore validate a URL and timeout without
the device being available. If an existing manifest or lock is present, the
workflow may read or contend with that existing state; it does not mutate it.
The wrapper has the additional `crosspoint.local` resolution step described
above.

The result line reports `planned_generation` and `planned_upload` counts for
work that would occur. It reports current generation or upload skips when
state says those operations are already complete.

### Force, deduplication, and remote replacement

Without `--force`, generation is current only when all of the following match:
the article ID, content hash, conversion-configuration hash, safe filename,
generation state, and SHA-256 hash of the local EPUB. A changed title, author,
source URL, article HTML, executable path, or conversion configuration causes
regeneration. A missing, empty, changed, or replaced local output also causes
regeneration.

Bookkeeping fields such as GoodLinks tags, read state, and modification dates
do not enter the content hash. The tag still controls which articles are
selected. The `pandoc-timeout`, API timeout, and device timeout control a run
but are not conversion-content inputs in the manifest hash.

- `export --force` regenerates every selected EPUB and clears its upload
  completion in the manifest. It never uploads.
- `sync --force` regenerates every selected EPUB and uploads it, explicitly
  permitting replacement of the destination basename. Review the selected
  queue and remote names first; this can replace a file not proven to belong to
  this manifest.
- `send --overwrite` is the separate one-file replacement permission.
- `send` has no `--force`, and `sync` has no `--overwrite`.

For a normal sync, an existing remote basename is refused unless the manifest
proves that exact remote path was previously owned by this workflow. That
ownership evidence also lets a changed article or failed retry replace the
known path without `--force`. An unrelated same-named remote file remains
protected. A completed manifest entry is trusted for an upload skip; the CLI
does not re-list the device to prove that a previously uploaded file still
exists. Use `--force` after reviewing the device if it was removed or changed.

The manifest hashes the executable path and selected conversion configuration,
not the installed Pandoc binary's version. After upgrading Pandoc in place,
use `export --force` or `sync --force` if existing EPUBs should be regenerated.

## Local state and remote state

The direct CLI resolves a relative `--output-dir` from its process working
directory. Thus the default `export` output is `./export` under the directory
from which the command is launched, with sibling state beside it. `sync.sh`
passes the absolute `export/` directory belonging to its checkout instead.
Generated article content and state remain local unless an explicit upload is
run.

- **Generated EPUBs:** `./export/*.epub`, one safe-named file per generated
  article. They are ignored and are not automatically deleted.
- **Manifest:** `./export.manifest.json`, a version-1 JSON file containing
  article IDs, hashes, safe filenames, generation/upload booleans, and
  conditional remote paths. Workflow-created entries start with `id`,
  `content_hash`, `config_hash`, `filename`, `generated`, and `uploaded`.
  Successful generation adds `output_hash`; successful upload adds
  `remote_path`; an incomplete retry may instead retain `owned_remote_path`.
  A manifest generated by this workflow contains no article HTML or token, but
  IDs, names, and remote paths can still be sensitive.
- **Advisory lock:** `./export.manifest.lock`, a mode `0600` sibling lock file
  for a real `export` or `sync`. The lock is held for the complete run and the
  OS lock is released on exit; the file itself can remain afterward.
- **Temporary conversion data:** a private temporary workspace and a
  destination-local `.goodlinks-crosspoint-*.epub.tmp` staging file. HTML,
  metadata, CSS, and staging output are cleaned up after conversion is
  complete or fails; remove a leftover staging file only after no workflow is
  running.
- **CrossPoint files:** remote
  `/GoodLinks/<safe-basename>.epub` files deliberately sent to the device.
  The CLI does not delete them, and `send` does not add them to the manifest.

For `--output-dir ./work/epubs`, the sibling state files are
`./work/epubs.manifest.json` and `./work/epubs.manifest.lock`; they are not
inside `./work/epubs`. The same sibling naming rule applies to any custom
output path. Keep these paths out of reports and screenshots.

A real run writes manifest files atomically with mode `0600`. The manifest is
bounded to 4 MiB and is validated before use. Unknown article-entry fields,
invalid hashes, unsafe names or paths, inconsistent completion flags, truncated
JSON, and wrong versions are rejected as `manifest_error` rather than silently
repaired. Unknown top-level fields are currently accepted. A real workflow
uses a nonblocking advisory lock and fails with `manifest_locked` if another
workflow owns it. A dry-run does not create a missing lock file.

## Results and failures

A successful `export` or `sync` prints a count-only line with the command and
these fields: `selected`, `generated`, `generation_skipped`, `uploaded`,
`upload_skipped`, `planned_generation`, `planned_upload`, and `failed`. When
items fail, it also reports safe aggregate error-code counts. A workflow exits
1 when any item fails or a command-level error is caught. `send` prints
`send: uploaded=1 failed=0` on success and exits 1 on a caught error.

Diagnostics use stable generic messages and do not include article bodies,
server response bodies, tokens, or configured private addresses. Common codes
include:

- `missing_token` or `invalid_token` — correct the direct environment path;
  the wrapper's pass entry is a separate path.
- `api_unavailable` or `authentication_failed` — keep GoodLinks open with its
  API enabled and check the ephemeral credential.
- `pandoc_not_found`, `pandoc_version_failed`, or `pandoc_failed` — check the
  separately installed executable and its permissions.
- `wrong_device`, `crosspoint_unavailable`, or
  `crosspoint_upload_incomplete` — keep the device in File Transfer mode and
  follow the recovery steps below.
- `remote_file_exists` — review the remote basename; use an explicit overwrite
  control only when replacement is intended.
- `manifest_error` or `manifest_locked` — follow the local-state recovery
  steps; do not hand-edit state or remove a live lock.

## Recovery and cleanup

### Invalid-manifest recovery

Manifest validation is structural, not tamper detection. Malformed, truncated,
oversized, wrong-version, or otherwise structurally invalid state is rejected
before use; valid-looking ownership or completion fields can still be accepted,
and unknown top-level fields are accepted. If tampering is suspected, discard
and rebuild the manifest even if it validates. Do not hand-edit it or paste it
into a report; it can contain private article IDs and remote paths.

1. Stop other CLI workflows and confirm no process is using the advisory lock.
2. If useful, preserve the invalid or suspect manifest privately for diagnosis.
   Do not commit or share it.
3. Remove the invalid or suspect sibling manifest. For the default output:

   ```console
   rm -- ./export.manifest.json
   ```

4. If the sibling lock is stale and no workflow is running, remove it too:

   ```console
   rm -- ./export.manifest.lock
   ```

5. Inspect local EPUBs and the CrossPoint destination in the device's File
   Transfer UI. A fresh manifest cannot prove ownership of old remote files.
   Keep files that are wanted; delete unwanted or partial files in the UI.
   If replacement is intentional, review names and use `sync --force` or
   `send --overwrite` as appropriate.
6. Rerun a real `export` or `sync` with a token supplied through its safe
   credential path. A dry-run does not rebuild the manifest.

Deleting a manifest does not delete local EPUBs or device files. It only
removes local idempotency and remote-ownership evidence.

### Title changes and orphaned files

The safe filename includes a sanitized title, article ID, and digest. A title
change can therefore produce a new basename. The CLI deliberately does not
delete the old local EPUB or remote file because it cannot safely infer that a
same-named file is disposable.

After a title change, identify the old name from private local or device
listings, run `export` or `sync` to create/send the new name, and verify that
the old name is the article version you own before deleting it. Delete the old
local file and the old remote file manually through the CrossPoint File
Transfer UI. There is no remote-delete CLI command. If the old remote file is
retained, later syncs do not remove it. Never publish private listings.

### Partial-upload recovery

`crosspoint_upload_incomplete` means request bytes may have reached the device,
and a lost response can have the same practical result. Do not blindly retry:

1. Stay on the trusted private network or private hotspot and keep CrossPoint
   in File Transfer mode.
2. Inspect the destination in the CrossPoint File Transfer file UI and identify
   the exact basename privately. Real generated names can include sanitized
   title and ID components; treat the basename and listing as sensitive and do
   not share them.
3. Delete the partial or unwanted duplicate in that UI, then retry. This CLI
   has no unauthenticated remote-delete operation.
4. For `sync`, the manifest leaves the item incomplete and may retain an
   ownership marker. Retry after cleanup; use `sync --force` only when replacing
   a known existing file is intentional.
5. For `send`, retry the same existing EPUB after cleanup; use `--overwrite`
   only for an intentional replacement of a known complete file.

If a failure happened before any request bytes were sent, the client may report
`crosspoint_unavailable` instead. Inspecting the destination before retrying is
still the safe choice.

## CrossPoint, X3, and firmware caveats

The current client targets the documented CrossPoint status, directory,
directory-creation, and multipart-upload HTTP endpoints. It supports status
identities `X3` and `X4`; it does not implement USB transfer, Calibre transfer,
firmware flashing, device unlocking, or remote deletion.

For CrossPoint Reader v1.5.0, project guidance is to treat X3 USB/Calibre
transfer as a corruption risk and use wireless File Transfer for this workflow
instead. Keep backups and verify the device model and firmware before any
transfer. The filename-comma mitigation above is a client-side safety measure,
not a firmware fix.

Firmware flashing or unlocking is a separate device-changing operation. Follow
only official instructions for the exact hardware revision and release, verify
published checksums, back up first, and understand that an error can erase data,
void support, or leave the reader unusable. The official CrossPoint README warns
that some third-party units may be USB-locked and that unsupported firmware can
permanently brick a device or leave no recovery path. Its official unlock-tool
guidance supports only CrossPoint and CrossInk. Verify those statements and the
current procedure for the exact hardware before proceeding. This project cannot
validate a flash or unlock, recover corrupted storage, or provide a recovery
path. This CLI does not require unlocking, and this reference is not a flashing
procedure. Stop rather than guessing if official instructions do not cover the
device revision.

Consult the official sources before any device-changing operation:

- [CrossPoint Reader repository and README](https://github.com/crosspoint-reader/crosspoint-reader)
- [Official CrossPoint Xteink Unlocker](https://crosspointreader.com/#unlock-tool)
- [Official CrossPoint web flash tools](https://crosspointreader.com/#flash-tools)
- [CrossPoint Reader v1.5.0 release](https://github.com/crosspoint-reader/crosspoint-reader/releases/tag/v1.5.0)
- [v1.5.0 web-server endpoints](https://github.com/crosspoint-reader/crosspoint-reader/blob/v1.5.0/docs/webserver-endpoints.md)

## Undo everything

Undo each layer explicitly when the workflow is no longer needed. The removal
commands below are destructive: stop all workflows first, verify the intended
checkout and targets with `pwd` and `git rev-parse --show-toplevel`, and use
verified absolute paths when you are not in that checkout. Do not run them
based only on an assumed shell working directory.

1. **GoodLinks:** disable the API server in **Settings > API**. Revoke or
   regenerate the token if it may have been exposed.
2. **Shell and password store:** run `unset GOODLINKS_TOKEN`, remove accidental
   token copies from shell startup files, `.env` files, logs, clipboard history,
   or scripts, and remove the `goodlinks-crosspoint/goodlinks-token` pass entry
   if the wrapper is no longer wanted.
3. **Local wrapper configuration:** stop workflows and remove the ignored
   `.sync.env` if it is no longer needed.
4. **CLI environment:** optionally uninstall the local package, then remove
   the virtual environment:

   ```console
   .venv/bin/python -m pip uninstall -y goodlinks-to-crosspoint
   rm -rf -- .venv
   ```

5. **Local output and state:** after all workflows stop, remove the output
   directory and its sibling manifest and lock. For the default paths:

   ```console
   rm -rf -- ./export
   rm -f -- ./export.manifest.json ./export.manifest.lock
   ```

   For a custom `./work/epubs` output, remove `./work/epubs` together with
   `./work/epubs.manifest.json` and `./work/epubs.manifest.lock`. Never remove
   a lock while a workflow is running.
6. **CrossPoint:** while still on the trusted File Transfer connection, inspect
   `/GoodLinks` and delete the EPUBs and partial uploads you sent in the
   device UI. The CLI does not delete device files. Then exit File Transfer,
   disable the temporary hotspot if used, and disconnect from that network.
7. **Pandoc:** if it was installed with Homebrew and is no longer wanted, run
   this manually after the workflow is stopped:

   ```console
   brew uninstall pandoc
   ```

   Use the package manager that actually installed Pandoc. This reference does
   not run an uninstall command automatically.

Do not commit removed artifacts, generated EPUBs, manifests, locks, databases,
exports, credentials, or other ignored workflow state.
