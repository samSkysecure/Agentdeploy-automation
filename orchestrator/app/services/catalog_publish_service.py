"""
Publishes the generated Teams app manifest package to the customer tenant's
app catalog via Microsoft Graph.

Ported from the reference implementation in Manifest_upload.zip
(graph_auth.py + catalog_publish_service.py), which used delegated
device-code auth against a fixed lab tenant. That's swapped here for the
same client-credentials pattern already used everywhere else in this
pipeline (see app_registration_service._graph_token) so it runs
unattended against req.customer_tenant_id, using the deployment SPN's
AppCatalog.ReadWrite.All / AppCatalog.Submit application permission.

This is the final step of the deployment pipeline - it must run after the
Copilot Studio import step so the manifest's bot id / connection wiring
is already valid.
"""
import logging

import httpx

logger = logging.getLogger("orchestrator.catalog_publish_service")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class CatalogPublishError(Exception):
    pass


def _graph_token(deployment_spn_client_id: str, deployment_spn_secret: str, customer_tenant_id: str) -> str:
    """
    Client-credentials token for Graph, scoped to the CUSTOMER's tenant.
    Same pattern as app_registration_service._graph_token - relies on the
    deployment SPN's app-only AppCatalog.ReadWrite.All / AppCatalog.Submit
    role having been consented in that tenant.
    """
    resp = httpx.post(
        f"https://login.microsoftonline.com/{customer_tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": deployment_spn_client_id,
            "client_secret": deployment_spn_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise CatalogPublishError(f"Failed to acquire Graph token in customer tenant: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def upload_new_app(token: str, zip_bytes: bytes) -> str:
    """
    Uploads a new Teams app package (zip containing manifest.json + icons)
    to the customer tenant's app catalog. Returns the teamsAppId.
    """
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/zip",
    }

    resp = httpx.post(url, headers=headers, content=zip_bytes, timeout=60)

    if resp.status_code == 201:
        teams_app_id = resp.json().get("id")
        logger.info("Manifest published to catalog. teamsAppId=%s", teams_app_id)
        return teams_app_id

    if resp.status_code == 409:
        raise CatalogPublishError(
            "Upload failed (409 Conflict): an app with this Teams app ID already exists in the "
            "catalog. Use the appDefinitions update endpoint for a new version, or regenerate the "
            "manifest with a fresh teams_app_id."
        )
    if resp.status_code == 403:
        raise CatalogPublishError(
            "Upload failed (403 Forbidden). Either the AppCatalog.ReadWrite.All / AppCatalog.Submit "
            "app role hasn't propagated to this customer tenant yet, or the tenant blocks "
            f"custom/sideloaded app uploads at the Teams admin level. Graph response: {resp.text}"
        )
    if resp.status_code == 400:
        raise CatalogPublishError(
            f"Upload failed (400 Bad Request): manifest is likely malformed or has unresolved "
            f"placeholders. Response: {resp.text}"
        )

    raise CatalogPublishError(f"Upload failed (HTTP {resp.status_code}): {resp.text}")


def verify_app(token: str, teams_app_id: str) -> dict:
    """
    Fetches the app from the catalog to confirm it landed correctly.
    """
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps/{teams_app_id}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$expand": "appDefinitions"}

    resp = httpx.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        raise CatalogPublishError(f"Verification failed (HTTP {resp.status_code}): {resp.text}")

    app_data = resp.json()
    logger.info("Verified in catalog: %s (id=%s)", app_data.get("displayName"), app_data.get("id"))
    return app_data


def publish_manifest(
    *,
    zip_bytes: bytes,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
) -> dict:
    """
    Full publish flow: acquire token -> upload -> verify.
    Returns the verified app's Graph resource dict (includes 'id' == teamsAppId).
    """
    token = _graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
    teams_app_id = upload_new_app(token, zip_bytes)
    return verify_app(token, teams_app_id)
