"""FastAPI surface: webhooks, cancel-intent, audit, report."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from grace.integrity import IntegrityGuard
from grace.intent.converter import convert
from grace.orchestrator import arm_db_path
from grace.rzp.webhooks import router as webhook_router
from grace.store import Store

RUN = os.getenv("GRACE_RUN", "demo")
ARM = os.getenv("GRACE_ARM", "agent")
RUN_DIR = Path("runs") / RUN
DECISION_DATE = date(2026, 9, 4)

@asynccontextmanager
async def lifespan(application: FastAPI):
    db = arm_db_path(RUN_DIR, ARM)
    application.state.store = Store(db) if db.exists() else None
    application.state.guard = (
        IntegrityGuard(application.state.store) if application.state.store else None
    )
    try:
        yield
    finally:
        if getattr(application.state, "store", None):
            application.state.store.close()


app = FastAPI(title="Grace", version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router)


def _store() -> Store:
    if not getattr(app.state, "store", None):
        raise HTTPException(503, f"no run at {arm_db_path(RUN_DIR, ARM)}; run `grace seed` first")
    return app.state.store


class IntentIn(BaseModel):
    subscription_id: str
    text: str


@app.get("/health")
def health() -> dict:
    return {"ok": True, "run": RUN, "arm": ARM,
            "cohort_loaded": getattr(app.state, "store", None) is not None}


@app.post("/cancel-intent")
def cancel_intent(body: IntentIn) -> dict:
    try:
        return convert(RUN_DIR, ARM, body.subscription_id, body.text,
                       offline=True, today=DECISION_DATE, execute_now=False)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.post("/cancel-intent/{subscription_id}/accept")
def accept_offer(subscription_id: str, body: IntentIn) -> dict:
    try:
        return convert(RUN_DIR, ARM, subscription_id, body.text,
                       offline=True, today=DECISION_DATE, execute_now=True)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/audit/{mandate_id}")
def audit(mandate_id: str) -> JSONResponse:
    s = _store()
    m = s.get_mandate(mandate_id)
    if m is None:
        raise HTTPException(404, f"unknown mandate {mandate_id}")
    return JSONResponse({
        "mandate": m.model_dump(mode="json"),
        "events": [e.model_dump(mode="json") for e in s.events_for(mandate_id)],
        "audit": s.audit_for(mandate_id),
    })


@app.get("/report", response_class=HTMLResponse)
def report() -> HTMLResponse:
    p = RUN_DIR / "report.html"
    if not p.exists():
        raise HTTPException(404, "no report yet; run `grace report`")
    return HTMLResponse(p.read_text())
