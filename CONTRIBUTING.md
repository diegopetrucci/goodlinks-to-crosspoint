# Contributing

Contributions are welcome, but this repository is a synthetic-only development
space. Read [SECURITY.md](SECURITY.md) before creating a fixture, issue, or
pull request.

## Non-negotiable privacy rules

Do not add, commit, attach, or paste any of the following, even temporarily in
a test, screenshot, log, issue, or pull-request comment:

- real GoodLinks databases or SQLite sidecars;
- real GoodLinks/API tokens, credentials, or secrets;
- real article bodies, article metadata, GoodLinks exports, or generated
  EPUBs;
- personal URLs, hostnames, IP addresses, or CrossPoint/device addresses; or
- personal CrossPoint directory listings, upload files, shell history, or
  terminal output.

Real databases, real tokens, real article bodies, real exports, and generated
EPUBs are prohibited. Use synthetic `example.com` values only. Committed
fixtures belong under an explicit path such as
`tests/fixtures/example.com/` or `examples/example.com/`, and should use URLs
such as `https://example.com/articles/synthetic-one`. Use fake tokens only as
non-sensitive test values, fake Pandoc executables, and local synthetic HTTP
servers. Tests must not contact a real GoodLinks app, CrossPoint device,
external service, or personal network.

Never add a token option, store a token in a URL/configuration file, or enable
shell tracing while `GOODLINKS_TOKEN` is set. Manual testing must read the token
silently from a prompt, keep it ephemeral, and run `unset GOODLINKS_TOKEN` when
done. Do not use real databases or exports to make a test more realistic.

## Development setup

The package supports Python 3.11 or newer and has no runtime Python
dependencies. From a clean checkout, on Apple Silicon macOS with Homebrew
Python, verify the absolute interpreter first (the output must be Python 3.11
or newer), then create the environment with it:

```console
/opt/homebrew/bin/python3 --version
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
.venv/bin/python -m unittest discover -s tests -v
```

On Intel Macs or other installations, substitute the absolute path to a
verified Python 3.11+ interpreter for `/opt/homebrew/bin/python3`; do not use
an aliased `python3`. The explicit `.venv/bin/...` paths bypass shell aliases
and functions (including ones that select an Xcode-provided Python), so local
checks use this checkout's interpreter without relying on activation.

The test suite uses synthetic `example.com` data, local in-process HTTP
fixtures, and fake Pandoc programs. It does not require a GoodLinks token,
Pandoc installation, Internet access, or a CrossPoint device. Do not add an
automatic Homebrew or network installation step; Pandoc, when needed for a
manual local workflow, is a separately managed prerequisite.

Before opening a change, also inspect the complete diff and status for private
or generated artifacts:

```console
git diff --check
git status --short
```

Do not stage or submit `.venv/`, output directories, EPUBs, manifests, lock
files, databases, exports, API files, credentials, logs, or other generated
state. Keep any locally needed real data outside the repository and outside
fixtures.

## Documentation and code changes

Keep README command examples copy-pasteable and synthetic. Document behavior
that exists in the current CLI; do not imply that the project flashes firmware,
uses USB/Calibre transfer, authenticates CrossPoint HTTP, or deletes remote
files when it does not. New behavior should include a focused synthetic test
and should preserve redacted diagnostics.

For a security concern, follow the responsible-disclosure process in
[SECURITY.md](SECURITY.md). Do not put vulnerability details or sensitive data
in a public pull request. This project does not publish a private email or
invented private contact; use the repository host's private security workflow
when available.
