"""Serves the built React app (frontend/dist). The SPA handles its own routes; everything under /api is Django Ninja."""
from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse

DIST = Path(settings.FRONTEND_DIST)


def spa(request, *args, **kwargs):
    index = DIST / "index.html"
    if not index.exists():
        return HttpResponse("<h1>LLM Wiki</h1><p>The frontend is not built. Run <code>npm run build</code> in frontend/ "
                            "(the built files are normally committed).</p><p>API docs: <a href='/api/docs'>/api/docs</a></p>")
    return HttpResponse(index.read_text(encoding="utf-8"))


def asset(request, name: str):
    p = (DIST / "assets" / name).resolve()
    if not str(p).startswith(str((DIST / "assets").resolve())) or not p.exists():
        raise Http404()
    resp = FileResponse(open(p, "rb"), content_type=mimetypes.guess_type(str(p))[0] or "application/octet-stream")
    resp["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp
