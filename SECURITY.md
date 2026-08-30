# Security and privacy

This project handles two sensitive boundaries: a GoodLinks bearer token and
article content sent to a local CrossPoint device. Treat both as private. The
CrossPoint File Transfer HTTP server is unauthenticated and has no TLS, so it
must be used only on a trusted private LAN or a temporary device hotspot.

## Prohibited data

Never commit, attach to an issue, put in a fixture, paste into a log, or share
in a screenshot any of the following:

- a real GoodLinks database or SQLite sidecar;
- a real API token, credential, secret, or personal configuration;
- real article bodies, article metadata, GoodLinks exports, or generated EPUBs;
- personal URLs, hostnames, IP addresses, or device addresses;
- CrossPoint directory listings or upload files that identify a person or
  device; or
- shell history, process output, crash output, or terminal captures containing
  any of the above.

Real databases, real tokens, real article bodies, real exports, and generated
EPUBs are prohibited. An ignore rule is not a security boundary. Check staged
and untracked files before sharing a patch. Do not ask a reporter to send
private data in order to reproduce a bug.

## Required synthetic fixtures

Tests and examples **must** use synthetic values only. Put committed fixtures
under an explicitly synthetic path such as
`tests/fixtures/example.com/` or `examples/example.com/`, and use URLs such as
`https://example.com/articles/synthetic-one`. Use fake Pandoc executables and
local synthetic HTTP servers instead of real GoodLinks, CrossPoint devices, or
external services. A fixture must not be made safe merely by replacing its
filename; its URL, hostname, IP, device address, token, article text, and
export content must all be synthetic.

Do not put `GOODLINKS_TOKEN` values in command arguments, URLs, source files,
`.env` files, shell startup files, or documentation. For a local manual test,
read it silently into the environment, run the command, and `unset
GOODLINKS_TOKEN` immediately afterward. Never enable shell tracing while it is
set.

## Reporting a vulnerability

Please practice responsible disclosure:

1. Do not open a public issue with a token, private article data, a device
   address, or an unredacted exploit.
2. Use the repository host's private **Report a vulnerability** or **Security
   Advisory** workflow from this repository's Security tab, when available.
   Include only a minimal synthetic reproduction until a maintainer requests
   more detail through that private channel.
3. If the host does not provide a private workflow, open a minimal public issue
   that contains no vulnerability details or sensitive data and asks for a
   private reporting channel. Do not invent or assume an email address or other
   private contact for this project.
4. Allow maintainers reasonable time to investigate and coordinate a fix before
   public disclosure. Do not test against anyone else's GoodLinks library,
   CrossPoint device, network, or token.

This repository does not claim a private security contact. The private workflow
provided by the repository host is the intended route when it exists.
