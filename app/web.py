"""
Web UI and REST API for monitoring and editing saved searches.

Runs in the same asyncio loop as the poller. Search mutations go through
the SearchManager, which persists to searches.yaml and restarts the
affected polling task, so changes take effect immediately.

Endpoints:
    GET    /                      Single-page UI
    GET    /api/searches          All searches with runtime status and hit stats
    POST   /api/searches          Create a search (body: Search)
    PUT    /api/searches/{id}     Update a search (body: Search; id must match)
    DELETE /api/searches/{id}     Delete a search (hits/seen history is kept)
    POST   /api/searches/{id}/poll  Trigger an immediate poll
    GET    /api/hits              Recent hits (?search_id=...&limit=...)
    POST   /api/hits/delete      Delete selected hit rows (body: {items: [{search_id, item_id}]})
    POST   /api/hits/rerun       Re-run LLM analysis for one hit (body: {search_id, item_id})
"""

from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .models import Search
from .scheduler import SearchManager
from .store import Store

log = structlog.get_logger()

_STATIC_DIR = Path(__file__).parent / "static"


class HitRef(BaseModel):
    """Identifies a single hit row (the hits table's composite primary key)."""

    search_id: str
    item_id: str


class DeleteHitsBody(BaseModel):
    items: list[HitRef]


def create_app(manager: SearchManager, store: Store) -> FastAPI:
    app = FastAPI(title="eBay Alerts", docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/api/searches")
    async def list_searches() -> dict:
        stats = await store.hit_stats()
        return {
            "searches": [
                {
                    **s.model_dump(mode="json"),
                    "status": manager.status(s.id).as_dict(),
                    "hits": stats.get(s.id, {"total": 0, "notified": 0, "last_hit_at": None}),
                }
                for s in manager.searches()
            ]
        }

    @app.post("/api/searches", status_code=201)
    async def create_search(search: Search) -> dict:
        try:
            await manager.add(search)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return search.model_dump(mode="json")

    @app.put("/api/searches/{search_id}")
    async def update_search(search_id: str, search: Search) -> dict:
        if search.id != search_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Search id cannot be changed — it is the dedupe namespace. "
                    "Delete the search and create a new one instead."
                ),
            )
        try:
            await manager.update(search)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No search '{search_id}'")
        return search.model_dump(mode="json")

    @app.delete("/api/searches/{search_id}", status_code=204)
    async def delete_search(search_id: str) -> None:
        try:
            await manager.remove(search_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No search '{search_id}'")

    @app.post("/api/searches/{search_id}/poll", status_code=202)
    async def poll_now(search_id: str) -> dict:
        try:
            manager.trigger(search_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"No search '{search_id}'")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"triggered": search_id}

    @app.get("/api/hits")
    async def list_hits(
        search_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        return {"hits": await store.recent_hits(search_id, limit)}

    @app.post("/api/hits/delete")
    async def delete_hits(body: DeleteHitsBody) -> dict:
        deleted = await store.delete_hits(
            [(h.search_id, h.item_id) for h in body.items]
        )
        return {"deleted": deleted}

    @app.post("/api/hits/rerun")
    async def rerun_hit(ref: HitRef) -> dict:
        try:
            return await manager.rerun_analysis(ref.search_id, ref.item_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="Unknown hit, or its search was deleted"
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return app
