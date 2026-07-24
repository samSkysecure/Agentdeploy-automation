"""
API surface: four endpoints.

POST /deployments                           -> queue a new deployment, returns immediately
GET  /deployments/{id}                      -> poll status/progress/outputs
GET  /deployments                           -> list all deployments
POST /deployments/{id}/select-environment   -> submit Power Platform environment selection

The actual ARM work happens off the event loop in a thread pool. This
matters: run_deployment() makes BLOCKING Azure SDK calls (poller.result()
blocks for minutes). FastAPI's BackgroundTasks run on the same event loop -
if we called run_deployment directly there, it would freeze the entire
server (including the GET status endpoint) for the full 3-5 minute
deployment. Running it via run_in_executor keeps the event loop free to
serve other requests, including status polling, while the deployment runs.
"""
import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from app.models.deployment import DeploymentRecord, DeploymentRequest, DeploymentStatus
from app.services.deployment_service import run_deployment
from app.services.deployment_store import store

logger = logging.getLogger("orchestrator.api")
router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.post("", response_model=DeploymentRecord, status_code=202)
async def create_deployment(request: DeploymentRequest):
    deployment_id = str(uuid.uuid4())
    record = DeploymentRecord(
        deployment_id=deployment_id,
        status=DeploymentStatus.QUEUED,
        request=request,
    )
    store.save(record)

    settings = get_settings()
    loop = asyncio.get_running_loop()
    # Fire-and-forget onto the default thread pool executor. We deliberately
    # don't await this - the HTTP response must return immediately while
    # the deployment continues in the background.
    loop.run_in_executor(None, run_deployment, record, settings)

    logger.info(
        "Queued deployment %s for agent=%s customer=%s",
        deployment_id,
        request.agent_slug,
        request.customer_slug,
    )
    return record


@router.get("/{deployment_id}", response_model=DeploymentRecord)
def get_deployment(deployment_id: str):
    record = store.get(deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return record


@router.get("", response_model=list[DeploymentRecord])
def list_deployments():
    return store.list_all()


class EnvironmentSelectionRequest(BaseModel):
    instance_url: str


@router.post("/{deployment_id}/select-environment", status_code=200)
def select_environment(deployment_id: str, body: EnvironmentSelectionRequest):
    """
    Called by the wizard when the user picks a Power Platform environment from the
    dropdown shown during the AWAITING_ENVIRONMENT_SELECTION phase.
    Unblocks the deployment background thread so it can proceed with PAC CLI auth.
    """
    record = store.get(deployment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if record.status != DeploymentStatus.AWAITING_ENVIRONMENT_SELECTION:
        raise HTTPException(
            status_code=409,
            detail=f"Deployment is not awaiting environment selection (status: {record.status})"
        )
    if not body.instance_url:
        raise HTTPException(status_code=400, detail="instance_url is required")

    woken = store.set_env_selection(deployment_id, body.instance_url.rstrip("/"))
    if not woken:
        raise HTTPException(
            status_code=409,
            detail="No deployment thread is waiting for environment selection. It may have timed out."
        )

    logger.info(
        "Environment selected for deployment %s: %s", deployment_id, body.instance_url
    )
    return {"status": "ok", "instance_url": body.instance_url}

