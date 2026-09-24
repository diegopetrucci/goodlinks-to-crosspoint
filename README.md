# GoodLinks to CrossPoint

Convert tagged articles from [GoodLinks](https://goodlinks.app) into EPUBs and
sync them wirelessly to small readers running
[CrossPoint](https://github.com/crosspoint-reader/crosspoint-reader).

The client supports Xteink X3, X4, and X4 Pro. It detects the connected model
and tracks upload completion separately, so the same GoodLinks queue can be
synced to an X3 and an X4 Pro without either reader replacing the other's
state.

| Reader | CrossPoint identity | Notes |
| --- | --- | --- |
| Xteink X3 | `X3` | Supported |
| Xteink X4 | `X4` | Supported |
| Xteink X4 Pro | `xteink_x4_pro` | Requires CrossPoint 1.6.0 or newer |

Uploads go to the reader's root directory by default, making articles visible
without opening an extra folder. The client does not delete remote files.

For every option, state format, recovery procedure, and security constraint,
see the [complete reference](docs/reference.md).

## Requirements

- macOS and Python 3.11 or newer;
- GoodLinks 3.2 or newer, open with **Settings > API** enabled;
- [Pandoc](https://pandoc.org) for EPUB generation; and
- a supported reader running CrossPoint in wireless **File Transfer** mode.

The repository's `sync.sh` convenience wrapper also requires
[`pass`](https://www.passwordstore.org). The Python CLI can be used without
`pass`.

## Setup

1. Install Pandoc and `pass`:

   ```console
   brew install pandoc pass
   ```

2. Clone the project and install it into a local virtual environment:

   ```console
   git clone https://github.com/diegopetrucci/goodlinks-to-crosspoint.git
   cd goodlinks-to-crosspoint
   python3 -m venv .venv
   .venv/bin/python -m pip install --no-deps .
   .venv/bin/goodlinks-crosspoint --version
   ```

3. In GoodLinks, open **Settings > API**, enable the API server, and copy its
   token.

4. Store that token for the wrapper:

   ```console
   pass insert goodlinks-crosspoint/goodlinks-token
   ```

5. Create the ignored local configuration and choose the GoodLinks tag to
   sync:

   ```console
   cp .sync.env.example .sync.env
   ```

   The default is `GOODLINKS_TAG=x3`. Despite the name, one tag can feed every
   supported reader.

## Sync a reader

On the reader, start wireless **File Transfer** and join the same trusted
private network as the Mac. Then run:

```console
./sync.sh --dry-run
./sync.sh
```

The dry-run reads GoodLinks and plans generation and upload work without
invoking Pandoc, changing the manifest, or contacting the reader's HTTP API.
The wrapper still resolves `crosspoint.local` before starting.

For an additional wrong-reader check, name the model you expect to be
connected:

```console
./sync.sh --device-model x3
./sync.sh --device-model x4
./sync.sh --device-model x4pro
```

The option does not select or configure a device. A real sync detects the
connected reader and fails safely if it does not match.

Run the same command again to verify idempotency. Current EPUBs should be
reported as `generation_skipped` and that model's completed uploads as
`upload_skipped`.

## Use X3 and X4 Pro together

Each real sync targets the reader currently serving `crosspoint.local`.
Connect one reader, run `./sync.sh`, then switch readers and run it again. Local
EPUB generation is shared, while upload completion and remote ownership are
stored separately for `X3`, `X4`, and `xteink_x4_pro`.

The manifest intentionally stores only the model identity, not a serial number
or another unique hardware identifier. Two physical readers of the same model
therefore share one completion record.

### Migrating an older manifest

A version-1 manifest recorded uploads without identifying the reader that
received them. Assign those records once with `--legacy-device`. For example,
if the existing files were sent to an X3 and the X4 Pro is connected now:

```console
./sync.sh --dry-run --device-model x4pro --legacy-device x3
./sync.sh --device-model x4pro --legacy-device x3
```

After the real migration, omit `--legacy-device` on subsequent runs.

Older X3 records may point to the former `/GoodLinks` default. The first sync
to the new root destination uploads the currently selected articles once and
records their root paths. Existing `/GoodLinks` copies remain untouched.

## Direct CLI without `pass`

Keep the GoodLinks token ephemeral and out of command arguments:

```console
printf 'GoodLinks API token: '
IFS= read -r -s GOODLINKS_TOKEN
printf '\n'
export GOODLINKS_TOKEN
.venv/bin/goodlinks-crosspoint sync --device-model x4pro
unset GOODLINKS_TOKEN
```

Use `goodlinks-crosspoint --help` and the subcommand help for the authoritative
syntax:

```console
.venv/bin/goodlinks-crosspoint export --help
.venv/bin/goodlinks-crosspoint send --help
.venv/bin/goodlinks-crosspoint sync --help
```

## Network and privacy

GoodLinks is read-only in this workflow. CrossPoint's File Transfer server is
unauthenticated, so use it only on a trusted private LAN or the reader's
temporary hotspot, and leave File Transfer mode when finished. Generated
EPUBs, manifests, locks, tokens, article data, device addresses, and local
configuration must not be committed.

## Development

The test suite uses synthetic local servers and a fake Pandoc executable; it
does not contact GoodLinks or a physical reader:

```console
.venv/bin/python -m unittest discover -s tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
submitting changes.
