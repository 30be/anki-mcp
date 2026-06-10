# anki-mcp

A small [MCP](https://modelcontextprotocol.io) server that adds flashcards to
[Anki](https://apps.ankiweb.net/) through the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on.

Built on [FastMCP](https://github.com/modelcontextprotocol/python-sdk) with the
streamable-HTTP transport. It exposes two MCP tools:

- **`add_cards`** — insert notes into the `Mixed` deck and sync. Each card has
  `front`, `back`, `notes`, `context`, and optional `image`/`image_field`.
  Duplicates don't fail the batch: notes are screened with
  `canAddNotesWithErrorDetail`, the addable ones are inserted, and the rest are
  reported in a `skipped` list. Returns the created note ids.
- **`delete_cards`** — delete by note id. Only the exact ids from a creation are
  removed, never a search, so deletion stays scoped to what you added.

### Images

A card's `image` may be a single source or a list, placed in `image_field`
(default `notes`). Sources auto-detect: a `data:` URI or `http(s)` URL is stored
as an Anki media file; raw inline markup (e.g. a full `<svg>…</svg>` or `<img>`
string) is dropped straight into the field. To avoid inlining a large `data:`
URI, `POST` an image to the `/upload` endpoint and pass back the `/tmp` path it
returns as the card's `image`.

## Run

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

The server listens on `0.0.0.0:3457`. Anki must be running with AnkiConnect
reachable at `http://localhost:8765`.

The companion `/zehntage/*` HTTP endpoints are guarded by a shared secret.
Put it in a `zehntage.key` file next to `server.py` (or set the `ZEHNTAGE_KEY`
environment variable); requests must send it in the `X-Zehntage-Key` header.
The `zehntage.key` file is git-ignored and must never be committed.

The `/upload` endpoint has no header auth — it's meant to sit behind a secret
URL prefix on a reverse proxy. Put the full public upload URL (including that
prefix) in an `upload_url.txt` file next to `server.py` (or set `ANKI_UPLOAD_URL`);
it's git-ignored. When set, the URL is advertised in the server instructions so
the client knows where to upload; when absent, the upload hint is simply omitted.

To run it as a service, adapt and install `anki-mcp.service`:

```sh
sudo cp anki-mcp.service /etc/systemd/system/
sudo systemctl enable --now anki-mcp
```
