import hashlib
import hmac
import json
import os
import secrets

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

ANKI_CONNECT = "http://localhost:8765"
DECK = "Mixed"
MODEL = "Back+Front+Usage"

# Uploaded images are staged in /tmp (auto-cleaned by the OS) just long enough
# for add_cards to import them into Anki's own media — afterwards the image
# lives in the collection and the temp copy is disposable. add_cards accepts a
# path under this dir as a card `image`; nothing else on disk is reachable.
UPLOAD_DIR = "/tmp/anki-mcp-uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _image_ext(content_type: str, filename: str = "") -> str:
    """Pick a file extension from a content-type, falling back to the filename."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    base = os.path.basename(filename or "")
    if "." in base:
        return "." + base.rsplit(".", 1)[1].lower()
    return ".bin"


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


def _load_upload_url() -> str:
    """Public URL of the /upload endpoint, including its secret Caddy path prefix.

    That URL is the only gate on the keyless upload route, so it's kept out of
    source (this repo is public): read from `upload_url.txt` next to this script,
    or the ANKI_UPLOAD_URL environment variable. Absent in a fresh checkout —
    the upload hint is then simply omitted from the server instructions.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_url.txt")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("ANKI_UPLOAD_URL", "")


UPLOAD_URL = _load_upload_url()

_INSTRUCTIONS = (
    "MCP server for adding flashcards to Anki. After adding cards, ALWAYS "
    "display a table of the added cards (front, back, notes)."
)
if UPLOAD_URL:
    _INSTRUCTIONS += (
        " To attach an image without inlining a data: URI, POST the raw image "
        "bytes (with an image/* Content-Type) or a multipart `file` to "
        f"{UPLOAD_URL} — it returns {{\"path\": ...}}; pass that path as a card's `image`."
    )

mcp = FastMCP(
    "Anki",
    instructions=_INSTRUCTIONS,
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


FIELD_NAMES = ("Front", "Back", "notes", "context")


def _media_filename(src: str, mime: str = "") -> str:
    """Stable, collision-resistant media filename derived from the source.

    Identical sources reuse the same filename so Anki dedupes the media.
    """
    digest = hashlib.md5(src.encode("utf-8")).hexdigest()[:12]
    ext = ""
    if "svg" in mime:
        ext = ".svg"
    elif mime.startswith("image/"):
        ext = "." + mime.split("/", 1)[1].split("+")[0].split(";")[0]
    else:
        base = os.path.basename(src.split("?")[0])
        if "." in base:
            ext = "." + base.rsplit(".", 1)[1]
    return f"anki_{digest}{ext or '.png'}"


def _is_upload_path(s: str) -> bool:
    """True only for an existing file staged by /upload (inside UPLOAD_DIR).

    Restricting to UPLOAD_DIR means the model can use paths handed back by the
    upload endpoint but cannot pull arbitrary files off the server into a card.
    """
    if not os.path.isabs(s):
        return False
    real = os.path.realpath(s)
    return real.startswith(os.path.realpath(UPLOAD_DIR) + os.sep) and os.path.isfile(real)


def _resolve_image(src: str):
    """Classify an image source.

    Returns ("inline", html) for raw markup to drop straight into a field,
    or ("picture", payload) for an AnkiConnect picture object (path/url/data).
    Auto-detects: a path from /upload, data: URIs, and http(s) URLs become
    media files; anything else (e.g. an <svg>...</svg> or <img> string) is
    treated as inline markup. SVG works inline or as a file.
    """
    s = src.strip()
    low = s.lower()
    if low.startswith("data:"):
        header, _, b64 = s.partition(",")
        mime = header[5:].split(";")[0]
        return ("picture", {"filename": _media_filename(s, mime), "data": b64})
    if low.startswith(("http://", "https://")):
        return ("picture", {"filename": _media_filename(s), "url": s})
    if _is_upload_path(s):
        real = os.path.realpath(s)
        mime = "image/svg+xml" if real.lower().endswith(".svg") else ""
        return ("picture", {"filename": _media_filename(real, mime), "path": real})
    return ("inline", s)


def _build_note(card: dict, tags=None) -> dict:
    """Build an AnkiConnect note dict, applying optional image(s).

    `image` (alias `images`) may be a single source or a list. Each source is
    a local path, URL, data: URI, or inline <svg>/<img> markup. `image_field`
    picks the target field (default "notes").
    """
    fields = {
        "Front": card.get("front", ""),
        "Back": card.get("back", ""),
        "notes": card.get("notes", ""),
        "context": card.get("context", ""),
    }
    note = {
        "deckName": DECK,
        "modelName": MODEL,
        "fields": fields,
        "options": {"allowDuplicate": False},
    }
    if tags:
        note["tags"] = tags

    images = card.get("image") or card.get("images")
    if images:
        if isinstance(images, str):
            images = [images]
        field = card.get("image_field", "notes")
        if field not in fields:
            field = "notes"
        pictures = []
        for src in images:
            if not src:
                continue
            kind, payload = _resolve_image(str(src))
            if kind == "inline":
                sep = "<br>" if fields[field] else ""
                fields[field] = f"{fields[field]}{sep}{payload}"
            else:
                payload["fields"] = [field]
                pictures.append(payload)
        if pictures:
            note["picture"] = pictures
    return note


@mcp.tool()
async def add_cards(cards: list[dict]) -> str:
    """Add flashcards to the Mixed deck. Each card is a dict with keys: front, back, notes, context, image, image_field.

    front: atomic question. For cloze deletions use '___'.
    back: atomic short answer, answerable in seconds.
    notes: REQUIRED. Extra explanation that aids memorization — a-ha moment, mnemonic, background reasoning, connection to other knowledge.
    context: link to source material (wikipedia, article, etc).
    image: OPTIONAL. An image to attach, or a list of them. Each may be a path returned by the /upload endpoint, an http(s) URL, a data: URI, or raw inline markup such as a full '<svg>...</svg>' string or an '<img>' tag. SVG works both as a file and inlined directly.
    image_field: OPTIONAL. Which field the image goes in — one of Front/Back/notes/context (default notes).

    Returns JSON: {"ids": [...], "added": N, "skipped": [...]}. `ids` has one entry
    per input card in order — the note id, or null if that card was not added; `added`
    counts the non-null ids. `skipped` lists the cards that were not added, each as
    {"index", "front", "reason"} (e.g. a duplicate of an existing note). A duplicate is
    NOT an error: the addable cards still go in and the duplicates are reported here.
    Keep the ids: they are the only way to delete the cards later (via delete_cards),
    so deletion stays scoped to the cards you created.
    """
    notes = [_build_note(card) for card in cards]

    # Screen first so one bad/duplicate note can't abort the whole batch:
    # addNotes is all-or-nothing, but canAddNotesWithErrorDetail reports per note.
    try:
        checks = await anki_request("canAddNotesWithErrorDetail", notes=notes)
    except Exception:
        # Older AnkiConnect: fall back to the boolean-only check.
        flags = await anki_request("canAddNotes", notes=notes)
        checks = [{"canAdd": bool(f), "error": None if f else "cannot add"} for f in flags]

    addable = [i for i, c in enumerate(checks) if c.get("canAdd")]
    ids = [None] * len(cards)
    if addable:
        new_ids = await anki_request("addNotes", notes=[notes[i] for i in addable])
        await anki_request("sync")
        for slot, nid in zip(addable, new_ids):
            ids[slot] = nid

    skipped = [
        {
            "index": i,
            "front": cards[i].get("front", ""),
            "reason": (checks[i].get("error") or "not added"),
        }
        for i in range(len(cards))
        if ids[i] is None
    ]
    added = [i for i in ids if i is not None]
    return json.dumps({"ids": ids, "added": len(added), "skipped": skipped})


@mcp.tool()
async def delete_cards(ids: list[int]) -> str:
    """Delete cards by the note ids returned from add_cards.

    Pass the exact ids you received when the cards were created. This removes
    only those specific notes — deletion is always scoped to creation, never a
    search or a whole-deck operation. Returns JSON {"deleted": N, "requested": M}
    where `deleted` counts how many of the requested ids actually existed.
    """
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return json.dumps({"deleted": 0, "requested": 0})
    found = await anki_request("findNotes", query="nid:" + ",".join(map(str, ids)))
    await anki_request("deleteNotes", notes=ids)
    await anki_request("sync")
    return json.dumps({"deleted": len(found), "requested": len(ids)})


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
    return _build_note(card, tags=["zehntage"])


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


# --- Image upload ------------------------------------------------------------
# Upload a picture once and get back a /tmp path, so the model can pass a short
# path to add_cards' `image` field instead of an ugly inline data: URI. No auth
# header — reaching this route already requires the secret URL prefix that
# fronts the whole server. There is no serve endpoint: add_cards imports the
# file into Anki's media (it then lives in the collection), and the /tmp copy
# is left for the OS to clean up.

@mcp.custom_route("/upload", methods=["POST"])
async def image_upload(request: Request) -> JSONResponse:
    """Stage an uploaded image under /tmp and return {"path": ...} for add_cards.

    Accepts multipart/form-data (a `file` part) or a raw body whose Content-Type
    is the image's MIME type. The returned path is local to this server — hand it
    straight back to add_cards as a card `image`.
    """
    try:
        ct = request.headers.get("content-type", "")
        if ct.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                return JSONResponse({"ok": False, "error": "no file part"}, status_code=400)
            data = await upload.read()
            ext = _image_ext(getattr(upload, "content_type", ""), getattr(upload, "filename", ""))
        else:
            data = await request.body()
            ext = _image_ext(ct)

        if not data:
            return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"ok": False, "error": "too large (max 20MB)"}, status_code=413)

        path = os.path.join(UPLOAD_DIR, secrets.token_urlsafe(24) + ext)
        with open(path, "wb") as f:
            f.write(data)
        return JSONResponse({"ok": True, "path": path, "bytes": len(data)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
