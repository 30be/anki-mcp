# anki-mcp

A small [MCP](https://modelcontextprotocol.io) server that adds flashcards to
[Anki](https://apps.ankiweb.net/) through the
[AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on.

It exposes a single MCP tool, `add_cards`, which inserts notes into the `Mixed`
deck and syncs. Built on [FastMCP](https://github.com/modelcontextprotocol/python-sdk)
with the streamable-HTTP transport.

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

To run it as a service, adapt and install `anki-mcp.service`:

```sh
sudo cp anki-mcp.service /etc/systemd/system/
sudo systemctl enable --now anki-mcp
```
