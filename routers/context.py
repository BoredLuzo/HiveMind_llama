"""HTTP-Endpoint fuer Editor-Kontext (IDE -> HiveMind).

Die Gegenrichtung (HiveMind -> IDE, MCP auf Port 8090) gibt es schon. Was fehlte,
war der Kontext *aus* dem Editor: offene Datei, Selektion, Diagnostics. Ein
schlanker Client (JetBrains/VS Code/Neovim) schickt ihn hierher, und der naechste
Run in diesem Workspace kann ihn nutzen.

    POST   /context/editor   {"workspace": "...", "file": "...", "selection": "...", ...}
    GET    /context/editor?workspace=...     (Debug: was liegt gerade an)
    DELETE /context/editor?workspace=...
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra import editor_context as ec

logger = logging.getLogger("hivemind.context")

context_router = APIRouter()
# Paket-Konvention: routers/__init__.py importiert `router` aus jedem Modul
router = context_router

# 256 KB reichen fuer Selektion + Diagnostics; schuetzt den Prozess vor Riesen-POSTs
_MAX_BODY_BYTES = 256 * 1024


@context_router.post("/context/editor")
async def post_editor_context(req: Request):
    raw = await req.body()
    if len(raw) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "payload too large", "limit": _MAX_BODY_BYTES},
                            status_code=413)
    try:
        payload = await req.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)

    ctx = ec.parse_context(payload)
    if not ctx.normalized_workspace():
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    ec.store().put(ctx)
    logger.info("[EDITOR-CONTEXT] stored for %s file=%s selection=%d chars diagnostics=%d source=%s",
                ec.compact_workspace(ctx.workspace), ctx.file or "-",
                len(ctx.selection or ""), len(ctx.diagnostics or []), ctx.source)
    return {"ok": True, **ctx.summary()}


@context_router.get("/context/editor")
async def get_editor_context(workspace: str = ""):
    ctx = ec.store().get(workspace)
    if ctx is None:
        return {"ok": True, "context": None, "stored": ec.store().count()}
    return {"ok": True, "context": ctx.to_dict(), "stored": ec.store().count()}


@context_router.delete("/context/editor")
async def delete_editor_context(workspace: str = ""):
    removed = ec.store().clear(workspace)
    return {"ok": True, "removed": removed}
