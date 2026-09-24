# GoodLinks to CrossPoint

I really like tiny ereaders like the ones
[Xteink makes](https://www.xteink.com). I also really like
[GoodLinks, the read-later service](https://goodlinks.app).

So I made this to take selected articles from my GoodLinks queue, convert them
to ePubs, and send them to my tiny reader. Sync supports CrossPoint on the
Xteink X3, X4, and X4 Pro, with upload state tracked separately for each model.

For the full command, state, device, recovery, and undo reference, see the
[complete reference](docs/reference.md).

## One-time setup

1. Install pandoc, eg `brew install pandoc`.
2. Clone the project and install its CLI in a local virtual environment:

```console
git clone https://github.com/diegopetrucci/goodlinks-to-crosspoint.git
cd goodlinks-to-crosspoint
python3 -m venv .venv
.venv/bin/python -m pip install --no-deps .
.venv/bin/goodlinks-crosspoint --version
```

3. Open GoodLinks on the Mac, go to **Settings > API**, enable the API server,
and note down the token.
5. `cp .sync.env.example .sync.env` and change (if you want) the GoodLinks tag for the articles you want to sync.

### Syncing the selected articles

On the CrossPoint device, select **File Transfer** and join the Wi-Fi network as the Mac.

If you use `pass` as a secrets manager:

1. Save the GoodLinks token: `pass insert goodlinks-crosspoint/goodlinks-token`
2. Start the sync: `./sync.sh`.

If `export.manifest.json` was created by an older version of this project,
identify the reader that received those earlier uploads once. For example, if
that reader was an X3, use `./sync.sh --legacy-device x3` for the first sync.
Later X3 and X4 Pro syncs can both use the ordinary `./sync.sh` command; the
connected model is detected automatically.

Uploads go to the reader's root directory by default, so articles appear
without opening an extra folder. An X3 manifest created with the older
`/GoodLinks` default will upload the selected articles to root once; the CLI
does not delete the older folder copies.

If you don't use `pass`:

```console
printf 'GoodLinks API token: '
IFS= read -r -s GOODLINKS_TOKEN
printf '\n'
export GOODLINKS_TOKEN
.venv/bin/goodlinks-crosspoint sync
unset GOODLINKS_TOKEN
```
