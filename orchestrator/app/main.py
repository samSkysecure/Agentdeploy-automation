import logging
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import os

from app.api.deployments import router as deployments_router
from app.api.frontend import router as frontend_router
from app.api.azure_discovery import router as azure_discovery_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(
    title="Agent Factory - Deployment Orchestrator",
    description="Deploys Teams Bot agents into customer Azure subscriptions (SOP 1)",
    version="0.1.0",
)

app.include_router(deployments_router)
app.include_router(frontend_router)
app.include_router(azure_discovery_router)

# Mount the static directory for the frontend
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")

@app.get("/health")
def health():
    return {"status": "ok"}
