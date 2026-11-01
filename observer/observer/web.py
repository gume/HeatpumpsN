"""Web portal: JSON API plus one static page (static/index.html).

Signal ids contain "/" and "#", so they travel as the `id` query parameter.
The portal changes only the observer's own registry and proposals; it has
no path to the plant.
"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .service import Service

STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="HeatpumpsN observer", docs_url="/api/docs", redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/signals")
    def signals():
        return service.signals()

    @app.put("/api/signal")
    def set_signal(id: str = Query(...), entry: dict = Body(...)):
        return service.set_signal(id, entry)

    @app.delete("/api/signal")
    def remove_signal(id: str = Query(...)):
        if not service.remove_signal(id):
            raise HTTPException(404, "no registry entry for this signal")
        return {"ok": True}

    @app.put("/api/signals")
    def update_signals(body: dict = Body(...)):
        ids = body.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "ids: a non-empty list of signal ids")
        return {"updated": service.update_signals(ids, body.get("set") or {})}

    @app.post("/api/signal/polish")
    async def polish(id: str = Query(...), draft: dict = Body(...)):
        try:
            return await run_in_threadpool(service.polish, id, draft)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"LLM request failed: {exc}") from exc

    @app.get("/api/model")
    def model():
        return service.model()

    @app.get("/api/model/inputs")
    def model_inputs():
        return service.model_inputs()

    @app.post("/api/model/generate")
    async def generate_model():
        try:
            return await run_in_threadpool(service.generate_model)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"LLM request failed: {exc}") from exc

    @app.post("/api/model/accept")
    def accept_model():
        try:
            service.accept_model()
        except FileNotFoundError as exc:
            raise HTTPException(404, "no proposal to accept") from exc
        return {"ok": True}

    @app.put("/api/model")
    def save_model(text: str = Body(..., embed=True)):
        try:
            service.save_model_text(text)
        except Exception as exc:
            raise HTTPException(400, f"not saved: {exc}") from exc
        return {"ok": True}

    @app.get("/api/rules")
    def rules():
        return service.rules()

    @app.get("/api/proposals")
    def proposals():
        return service.proposals()

    @app.post("/api/proposals/run")
    async def run_proposals(limit: int = Body(40, embed=True)):
        try:
            count = await run_in_threadpool(service.run_proposals, limit)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:  # network, model answer
            raise HTTPException(502, f"LLM request failed: {exc}") from exc
        return {"proposals": count}

    @app.post("/api/proposal/accept")
    def accept(id: str = Query(...), edits: dict | None = Body(None)):
        try:
            return service.accept_proposal(id, edits)
        except KeyError as exc:
            raise HTTPException(404, "no such proposal") from exc

    @app.delete("/api/proposal")
    def reject(id: str = Query(...)):
        if not service.reject_proposal(id):
            raise HTTPException(404, "no such proposal")
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
