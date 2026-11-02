"""Web portal: JSON API plus one static page (static/index.html).

Signal ids contain "/" and "#", so they travel as the `id` query parameter.
The portal changes only the observer's own registry and proposals, and starts
agent runs (read-only tools, sandboxed code); it has no path to the plant.
"""
from __future__ import annotations

import os

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .service import Service

STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="HeatpumpsN observer", docs_url="/api/docs", redoc_url=None)
    user = os.environ.get("OBSERVER_PORTAL_USER", "admin")
    password = os.environ.get("OBSERVER_PORTAL_PASSWORD", "")

    if password:
        import base64
        import hmac

        from fastapi.responses import PlainTextResponse

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            """HTTP Basic authentication for everything but /healthz, when a portal password is set."""
            if request.url.path != "/healthz":
                ok = False
                header = request.headers.get("authorization", "")
                if header.lower().startswith("basic "):
                    try:
                        u, _, p = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                        ok = hmac.compare_digest(u.encode(), user.encode()) & hmac.compare_digest(p.encode(),
                                                                                              password.encode())
                    except (ValueError, UnicodeDecodeError):
                        ok = False
                if not ok:
                    return PlainTextResponse("login required", status_code=401,
                                             headers={"WWW-Authenticate": 'Basic realm="observer", charset="UTF-8"'})
            return await call_next(request)

    @app.get("/healthz")
    def healthz():
        from . import __version__

        return {"ok": True, "version": __version__, "mqtt": bool(service.mqtt and service.mqtt.connected),
                "storage": service.writer_kind}

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

    # --- the house understanding: review, owner knowledge, understanding ------
    @app.get("/api/house")
    def house():
        return service.house()

    @app.put("/api/house/notes")
    def save_notes(text: str = Body(..., embed=True)):
        service.save_notes(text)
        return {"ok": True}

    @app.post("/api/house/generate")
    async def generate_house(body: dict | None = Body(None)):
        keep = bool((body or {}).get("from_draft_knowledge"))
        try:
            return await run_in_threadpool(lambda: service.generate_house(from_draft_knowledge=keep))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(404, "no draft knowledge to build from") from exc
        except Exception as exc:
            raise HTTPException(502, f"LLM request failed: {exc}") from exc

    @app.put("/api/house/draft")
    def edit_draft(body: dict = Body(...)):
        try:
            service.edit_draft(body.get("description"), body.get("knowledge"))
        except FileNotFoundError as exc:
            raise HTTPException(404, "no draft") from exc
        return {"ok": True}

    @app.post("/api/house/accept")
    def accept_house(body: dict | None = Body(None)):
        try:
            return {"archived": service.accept_house(bool((body or {}).get("clear_review")))}
        except FileNotFoundError as exc:
            raise HTTPException(404, "no draft to accept") from exc

    @app.get("/api/house/archive")
    def archived(stamp: str = Query(...)):
        try:
            return service.archived(stamp)
        except KeyError as exc:
            raise HTTPException(404, "no such archived version") from exc

    # --- supervision: alerts and checks -----------------------------------------------
    @app.get("/api/alerts")
    def alerts():
        return service.supervisor.view()

    @app.post("/api/alerts/ack")
    def ack_alert(key: str = Query(...)):
        if not service.supervisor.store.acknowledge(key):
            raise HTTPException(404, "no open alert with this key")
        service.supervisor.publish()
        return {"ok": True}

    @app.get("/api/checks")
    def checks_text():
        return {"text": service.supervisor.text(), "path": service.supervisor.checks_path,
                "error": service.supervisor.error}

    @app.put("/api/checks")
    def save_checks(text: str = Body(..., embed=True)):
        try:
            service.supervisor.save_text(text)
        except Exception as exc:                        # YAML or check errors go back to the editor
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/checks/run")
    async def run_check(id: str = Query(...)):
        try:
            return await run_in_threadpool(service.supervisor.run_now, id)
        except KeyError as exc:
            raise HTTPException(404, f"no check {exc}") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- updates -------------------------------------------------------------------
    @app.get("/api/updates")
    def updates():
        return service.updates.summary()

    @app.post("/api/updates/check")
    async def check_updates():
        await run_in_threadpool(service.updates.check)
        return service.updates.summary()

    # --- the observer's state ------------------------------------------------------
    @app.get("/api/state/export")
    def state_export():
        import time as _t

        data = service.state_export()
        name = _t.strftime("observer-state-%Y%m%d-%H%M%S.tgz")
        return Response(data, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/state/import")
    async def state_import(request: Request):
        data = await request.body()
        try:
            return await run_in_threadpool(service.state_import, data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- database export / import ------------------------------------------------
    @app.get("/api/db")
    def db_status():
        return service.db_status()

    @app.post("/api/db/export")
    def db_export(body: dict | None = Body(None)):
        try:
            return service.db_export((body or {}).get("start"), (body or {}).get("stop"))
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/db/import")
    async def db_import(request: Request, dry_run: bool = Query(False), name: str = Query("upload.parquet")):
        import tempfile

        folder = os.path.join(service.cfg["paths"]["exports"], "uploads")
        os.makedirs(folder, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="import-", suffix=".parquet", dir=folder)
        size = 0
        with os.fdopen(fd, "wb") as fp:
            async for chunk in request.stream():
                size += len(chunk)
                fp.write(chunk)
        try:
            job = await run_in_threadpool(service.db_import, path, dry_run)
        except (ValueError, RuntimeError) as exc:
            os.remove(path)
            raise HTTPException(400, str(exc)) from exc
        job["upload"] = {"name": os.path.basename(name), "bytes": size}
        return job

    @app.get("/api/db/file")
    def db_file(name: str = Query(...)):
        try:
            return FileResponse(service.db_file(name), media_type="application/vnd.apache.parquet", filename=name)
        except KeyError as exc:
            raise HTTPException(404, "no such export") from exc

    @app.delete("/api/db/file")
    def db_file_delete(name: str = Query(...)):
        try:
            os.remove(service.db_file(name))
        except KeyError as exc:
            raise HTTPException(404, "no such export") from exc
        return {"ok": True}

    # --- settings ---------------------------------------------------------------
    @app.get("/api/settings")
    def settings():
        return service.settings.view()

    @app.put("/api/settings")
    def update_settings(body: dict = Body(...)):
        return service.settings.update(body)

    @app.get("/api/settings/models")
    async def provider_models(provider: str = Query(...)):
        try:
            return await run_in_threadpool(service.settings.models, provider)
        except KeyError as exc:
            raise HTTPException(404, "no such provider slot in .env") from exc
        except Exception as exc:
            raise HTTPException(502, f"listing models failed: {exc}") from exc

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

    # --- agents: skills, tools, sandbox, runs ---------------------------------
    @app.get("/api/agent")
    def agent_status():
        a = service.agents
        return {**a.status(), "core_tools": a.core_tools(), **a.library_view()}

    @app.get("/api/agent/runs")
    def agent_runs():
        return service.agents.runs()

    @app.post("/api/agent/runs")
    def start_run(body: dict = Body(...)):
        try:
            return {"id": service.agents.start(body.get("skill") or None, str(body.get("question") or ""))}
        except KeyError as exc:
            raise HTTPException(404, f"no skill {exc}") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/agent/run")
    def agent_run(id: str = Query(...)):
        try:
            return service.agents.run(id)
        except KeyError as exc:
            raise HTTPException(404, "no such run") from exc

    # --- harvest: proposals of tools and skills from a run, and their editors ------
    def _proposal_call(fn, *args):
        try:
            return fn(*args)
        except KeyError as exc:
            raise HTTPException(404, f"no such run or proposal {exc}") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/agent/run/harvest")
    def harvest(id: str = Query(...)):
        """Start the harvest in the background; poll GET /api/agent/run/harvest."""
        return _proposal_call(service.agents.start_harvest, id)

    @app.get("/api/agent/run/harvest")
    def harvest_status(id: str = Query(...)):
        return _proposal_call(service.agents.harvest_result, id)

    @app.get("/api/library/proposals")
    def proposals():
        return service.agents.proposals()

    @app.get("/api/library/proposal")
    def proposal(run: str = Query(...), kind: str = Query(...), index: int = Query(...)):
        return _proposal_call(lambda: service.agents.proposal(run, kind, index)[1])

    @app.put("/api/library/proposal")
    def update_proposal(run: str = Query(...), kind: str = Query(...), index: int = Query(...),
                        body: dict = Body(...)):
        return _proposal_call(service.agents.update_proposal, run, kind, index, body)

    @app.post("/api/library/proposal/write")
    def write_tool(run: str = Query(...), index: int = Query(...)):
        return _proposal_call(service.agents.start_write_tool, run, index)

    @app.post("/api/library/proposal/test")
    async def test_proposal(run: str = Query(...), index: int = Query(...)):
        return await run_in_threadpool(_proposal_call, service.agents.test_proposal, run, index)

    @app.post("/api/library/proposal/dismiss")
    def dismiss_proposal(run: str = Query(...), kind: str = Query(...), index: int = Query(...)):
        _proposal_call(service.agents.dismiss_proposal, run, kind, index)
        return {"ok": True}

    @app.post("/api/agent/run/harvest/accept")
    async def accept_harvest(id: str = Query(...), kind: str = Query(...), index: int = Query(...)):
        return await run_in_threadpool(_proposal_call, service.agents.accept_harvest, id, kind, index)

    @app.post("/api/agent/run/resume")
    def resume_run(id: str = Query(...)):
        try:
            return {"id": service.agents.resume(id)}
        except KeyError as exc:
            raise HTTPException(404, "no such run") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/agent/run/cancel")
    def cancel_run(id: str = Query(...)):
        return {"cancelled": service.agents.cancel(id)}

    # --- the library: analysis tools and skills, tests, audit flags ----------------
    @app.get("/api/library")
    def library():
        return service.agents.library_view()

    @app.get("/api/library/item")
    def library_item(kind: str = Query(...), name: str = Query(...)):
        try:
            return service.agents.library_item(kind, name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/library/export")
    def export_library(selection: str = Query("house"), name: str = Query(""), version: str = Query(""),
                       items: str = Query("")):
        """items: kind/name,kind/name for selection=items."""
        chosen = [i.split("/", 1) for i in items.split(",") if "/" in i]
        try:
            fname, data = service.agents.export_library(selection, chosen, name, version)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return Response(data, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/api/library/import")
    async def import_library(request: Request):
        data = await request.body()
        try:
            return service.agents.import_library(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/library/import-url")
    async def import_library_url(body: dict = Body(...)):
        try:
            result = await run_in_threadpool(service.agents.import_library_url, str(body.get("url") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"download failed: {exc}") from exc
        await run_in_threadpool(service.updates.check)       # the banner reflects the new version at once
        return result

    @app.delete("/api/library/pack")
    def remove_pack(name: str = Query(...)):
        try:
            service.agents.remove_pack(name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/library/test")
    async def test_tool(name: str = Query(...)):
        try:
            return await run_in_threadpool(service.agents.test_tool, name)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/library/audit")
    def audit(kind: str = Query(...), name: str = Query(...), body: dict = Body(...)):
        try:
            return service.agents.audit(kind, name, bool(body.get("audited")), str(body.get("note") or ""))
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.delete("/api/library/item")
    def delete_item(kind: str = Query(...), name: str = Query(...)):
        try:
            service.agents.delete_item(kind, name)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/agent/file")
    def run_file(id: str = Query(...), path: str = Query(...)):
        try:
            return FileResponse(service.agents.file(id, path))
        except KeyError as exc:
            raise HTTPException(404, "no such file") from exc

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
