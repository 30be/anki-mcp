import hmac
import os

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

ANKI_CONNECT = "http://localhost:8765"
DECK = "Mixed"
MODEL = "Back+Front+Usage"


def _load_zehntage_key() -> str:
    """Shared secret for the /zehntage/* endpoints.

    Read from a `zehntage.key` file next to this script, or the ZEHNTAGE_KEY
    environment variable. The file is never committed to version control.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zehntage.key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("ZEHNTAGE_KEY", "")


ZEHNTAGE_KEY = _load_zehntage_key()

mcp = FastMCP(
    "Anki",
    instructions="MCP server for adding flashcards to Anki. Cards go to the 'Mixed' deck. After adding cards, ALWAYS display a table of the added cards (front, back, notes).",
    host="0.0.0.0",
    port=3457,
)


async def anki_request(action: str, **params):
    async with httpx.AsyncClient() as client:
        payload = {"action": action, "version": 6}
        if params:
            payload["params"] = params
        resp = await client.post(ANKI_CONNECT, json=payload, timeout=10)
        result = resp.json()
        if result.get("error"):
            raise Exception(f"AnkiConnect error: {result['error']}")
        return result.get("result")


@mcp.tool()
async def add_cards(cards: list[dict]) -> str:
    """Add flashcards to the Mixed deck. Each card is a dict with keys: front, back, notes, context.

    front: atomic question. For cloze deletions use '___'.
    back: atomic short answer, answerable in seconds.
    notes: REQUIRED. Extra explanation that aids memorization — a-ha moment, mnemonic, background reasoning, connection to other knowledge.
    context: link to source material (wikipedia, article, etc).
    """
    notes = []
    for card in cards:
        notes.append({
            "deckName": DECK,
            "modelName": MODEL,
            "fields": {
                "Front": card["front"],
                "Back": card["back"],
                "notes": card["notes"],
                "context": card.get("context", ""),
            },
            "options": {"allowDuplicate": False},
        })
    result = await anki_request("addNotes", notes=notes)
    added = sum(1 for r in result if r is not None)
    await anki_request("sync")
    return f"{added}/{len(cards)} cards added and synced"


# --- ZehnTage vocabulary integration -----------------------------------------
# Plain-HTTP endpoints used by the ZehnTage nvim plugin and browser extension.
# Cards added here are tagged "zehntage" so they can be listed and deleted
# independently of cards added through the add_cards MCP tool.
# Every request must carry the shared secret in the `X-Zehntage-Key` header.

def _auth_error(request: Request):
    """Return a 401 JSONResponse when the request lacks the right key, else None."""
    provided = request.headers.get("x-zehntage-key", "")
    if not ZEHNTAGE_KEY or not hmac.compare_digest(provided, ZEHNTAGE_KEY):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return None


def _note_payload(card: dict) -> dict:
    return {
        "deckName": DECK,
        "modelName": MODEL,
        "fields": {
            "Front": card.get("front", ""),
            "Back": card.get("back", ""),
            "notes": card.get("notes", ""),
            "context": card.get("context", ""),
        },
        "tags": ["zehntage"],
        "options": {"allowDuplicate": False},
    }


@mcp.custom_route("/zehntage/add", methods=["POST"])
async def zehntage_add(request: Request) -> JSONResponse:
    """Add one card (JSON object) or several (JSON array) tagged "zehntage"."""
    if (err := _auth_error(request)) is not None:
        return err
    try:
        body = await request.json()
        cards = body if isinstance(body, list) else [body]
        ids = await anki_request("addNotes", notes=[_note_payload(c) for c in cards])
        await anki_request("sync")
        return JSONResponse({"ok": True, "ids": ids})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/zehntage/list", methods=["GET"])
async def zehntage_list(request: Request) -> JSONResponse:
    """Return every "zehntage"-tagged note as [{front, back, notes, context}]."""
    if (err := _auth_error(request)) is not None:
        return err
    try:
        ids = await anki_request("findNotes", query="tag:zehntage")
        if not ids:
            return JSONResponse([])
        infos = await anki_request("notesInfo", notes=ids)
        words = []
        for n in infos:
            f = n.get("fields", {})
            words.append({
                "front": f.get("Front", {}).get("value", ""),
                "back": f.get("Back", {}).get("value", ""),
                "notes": f.get("notes", {}).get("value", ""),
                "context": f.get("context", {}).get("value", ""),
            })
        return JSONResponse(words)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/zehntage/delete", methods=["POST"])
async def zehntage_delete(request: Request) -> JSONResponse:
    """Delete the "zehntage"-tagged note(s) whose Front matches {"front": ...}."""
    if (err := _auth_error(request)) is not None:
        return err
    try:
        body = await request.json()
        front = (body.get("front") or "").replace('"', "")
        if not front:
            return JSONResponse({"ok": False, "error": "front required"}, status_code=400)
        ids = await anki_request("findNotes", query=f'tag:zehntage "Front:{front}"')
        if ids:
            await anki_request("deleteNotes", notes=ids)
            await anki_request("sync")
        return JSONResponse({"ok": True, "deleted": len(ids or [])})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
