"""
Manifest download only.

The onboarding pipeline previously triggered here (spawning
onboard_customer.ps1 as a subprocess and streaming its stdout over a
WebSocket) has been removed. Deployments now go through the /deployments
REST API in app/api/deployments.py, backed by app/services/deployment_service.py.

IMPORTANT GAP: deployment_service.py does not yet perform Copilot Studio
solution import / PAC CLI / connector wiring - onboard_customer.ps1 did
this. That logic still needs to be ported into the Python pipeline (or
deployment_service.py needs to shell out to a trimmed-down script for
just that part) before this fully replaces the old script's functionality.
"""
import os
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/api/manifest/{agent_slug}/{customer_slug}")
def download_manifest(agent_slug: str, customer_slug: str):
    manifest_dir = os.path.abspath(os.path.join(os.getcwd(), "generated_manifests"))

    file_path = os.path.join(manifest_dir, f"{agent_slug}-{customer_slug}-manifest.zip")

    if os.path.exists(file_path):
        return FileResponse(file_path, filename=f"{agent_slug}-{customer_slug}-manifest.zip", media_type="application/zip")

    return {"error": f"Manifest not found at {file_path}"}
