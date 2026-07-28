"""
Publishes the generated Teams app manifest package to the customer tenant's
app catalog via Microsoft Graph.

POST /appCatalogs/teamsApps ONLY supports Delegated auth - Application
(client credentials) permissions always get 403 Forbidden. This module
therefore creates a temporary PUBLIC-client App Registration in the customer
tenant, pre-grants admin consent for AppCatalog.ReadWrite.All delegated via
the deployment SPN, then initiates a device-code flow with that public client
(no client_secret ever needed in token polling). After upload, the temp app
is deleted.

This matches the reference implementation (graph_auth.py + catalog_publish_service.py)
which used a public client + device code flow registered in the lab tenant.
"""
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger("orchestrator.catalog_publish_service")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Microsoft Graph service application ID (constant across all tenants)
_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# AppCatalog.ReadWrite.All delegated permission scope GUID in Microsoft Graph
_APP_CATALOG_RW_ALL_SCOPE_ID = "dc149144-f292-421e-b185-5b7a5df7571e"


class CatalogPublishError(Exception):
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _spn_graph_token(deployment_spn_client_id: str, deployment_spn_secret: str, tenant_id: str) -> str:
    """
    Acquires a Graph access token for the deployment SPN via client credentials.
    Used ONLY for setup/teardown tasks (creating temp app, granting consent, deleting app).
    NOT used for the actual manifest upload (that requires delegated token).
    """
    resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": deployment_spn_client_id,
            "client_secret": deployment_spn_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise CatalogPublishError(
            f"Failed to acquire SPN Graph token: {resp.status_code} {resp.text}"
        )
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Temporary public-client App Registration lifecycle
# ---------------------------------------------------------------------------

def create_temp_manifest_app(
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
) -> tuple[str, str]:
    """
    Creates a temporary public-client App Registration in the customer tenant
    specifically for the Teams manifest upload device-code flow.

    Returns (app_object_id, app_client_id).

    The app is created as a public client (isFallbackPublicClient=True) so that
    device-code token polling requires NO client_secret - eliminating AADSTS7000218.
    Admin consent for AppCatalog.ReadWrite.All delegated is pre-granted by the
    deployment SPN so no interactive consent prompt appears during device login.
    """
    token = _spn_graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1. Create the public-client application
    logger.info("Creating temp public-client manifest uploader app in tenant %s", customer_tenant_id)
    app_resp = httpx.post(
        f"{GRAPH_BASE}/applications",
        headers=headers,
        json={
            "displayName": "skysecure-manifest-uploader",
            "signInAudience": "AzureADMyOrg",
            "isFallbackPublicClient": True,          # <-- public client: no secret in device code poll
            "publicClient": {
                "redirectUris": [
                    "https://login.microsoftonline.com/common/oauth2/nativeclient"
                ]
            },
            "requiredResourceAccess": [
                {
                    "resourceAppId": _GRAPH_APP_ID,
                    "resourceAccess": [
                        {"id": _APP_CATALOG_RW_ALL_SCOPE_ID, "type": "Scope"},  # Delegated
                    ],
                }
            ],
        },
        timeout=30,
    )
    if app_resp.status_code not in (200, 201):
        raise CatalogPublishError(
            f"Failed to create temp manifest app: {app_resp.status_code} {app_resp.text}"
        )
    app = app_resp.json()
    app_object_id = app["id"]
    app_client_id = app["appId"]
    logger.info("Created temp manifest app: client_id=%s object_id=%s", app_client_id, app_object_id)

    # 2. Create a Service Principal for the app (needed to grant consent).
    # Retry with backoff: Entra ID replication can take several seconds after app creation,
    # so the first attempt often returns NoBackingApplicationObject.
    sp_object_id = None
    for attempt in range(10):
        time.sleep(5)  # always wait at least 5s before each attempt
        sp_resp = httpx.post(
            f"{GRAPH_BASE}/servicePrincipals",
            headers=headers,
            json={"appId": app_client_id},
            timeout=30,
        )
        if sp_resp.status_code in (200, 201):
            sp_object_id = sp_resp.json()["id"]
            logger.info("Created SP for temp manifest app: sp_id=%s (attempt %d)", sp_object_id, attempt + 1)
            break
        error_obj = sp_resp.json().get("error", {})
        err_code = error_obj.get("code", "")
        details_codes = [d.get("code", "") for d in error_obj.get("details", [])]
        if err_code == "NoBackingApplicationObject" or "NoBackingApplicationObject" in details_codes:
            logger.info(
                "App object %s not yet replicated in Entra ID, retrying in 5s... (attempt %d/10)",
                app_client_id, attempt + 1,
            )
            continue
        raise CatalogPublishError(
            f"Failed to create SP for temp manifest app: {sp_resp.status_code} {sp_resp.text}"
        )
    if sp_object_id is None:
        raise CatalogPublishError(
            f"Timed out waiting for Entra ID to replicate temp manifest app {app_client_id}"
        )

    # 3. Find the Microsoft Graph Service Principal in the customer tenant
    graph_sp_resp = httpx.get(
        f"{GRAPH_BASE}/servicePrincipals?$filter=appId eq '{_GRAPH_APP_ID}'",
        headers=headers,
        timeout=30,
    )
    graph_sp_list = graph_sp_resp.json().get("value", [])
    if not graph_sp_list:
        raise CatalogPublishError("Could not find Microsoft Graph SP in customer tenant.")
    graph_sp_id = graph_sp_list[0]["id"]

    # 4. Pre-grant admin consent for AppCatalog.ReadWrite.All delegated
    #    (AllPrincipals = org-wide admin consent, no per-user prompt)
    time.sleep(2)
    grant_resp = httpx.post(
        f"{GRAPH_BASE}/oauth2PermissionGrants",
        headers=headers,
        json={
            "clientId": sp_object_id,       # SP of our temp app
            "consentType": "AllPrincipals", # org-wide admin consent
            "resourceId": graph_sp_id,      # Microsoft Graph SP
            "scope": "AppCatalog.ReadWrite.All",
        },
        timeout=30,
    )
    if grant_resp.status_code in (200, 201):
        logger.info("Pre-granted admin consent for AppCatalog.ReadWrite.All on temp manifest app")
    else:
        # Non-fatal: user may need to consent during login, but the device code flow will still work
        logger.warning(
            "Could not pre-grant admin consent (HTTP %s): %s - user may see a consent prompt during login",
            grant_resp.status_code,
            grant_resp.text,
        )

    return app_object_id, app_client_id


def delete_temp_manifest_app(
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
    app_object_id: str,
) -> None:
    """Deletes the temporary manifest uploader app from the customer tenant."""
    try:
        token = _spn_graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
        resp = httpx.delete(
            f"{GRAPH_BASE}/applications/{app_object_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if resp.status_code in (200, 204):
            logger.info("Deleted temp manifest app object_id=%s", app_object_id)
        else:
            logger.warning("Could not delete temp manifest app: HTTP %s %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Error cleaning up temp manifest app: %s", exc)


# ---------------------------------------------------------------------------
# Device-code flow (public client - NO client_secret)
# ---------------------------------------------------------------------------

def start_device_code_flow_for_graph(client_id: str, tenant_id: str) -> dict:
    """
    Initiates device code flow for Microsoft Graph to acquire a Delegated token.
    POST /appCatalogs/teamsApps does NOT support Application permissions.
    """
    resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/devicecode",
        data={
            "client_id": client_id,
            "scope": "https://graph.microsoft.com/AppCatalog.ReadWrite.All offline_access",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise CatalogPublishError(
            f"Failed to start device code flow for Graph: {resp.status_code} {resp.text}"
        )
    return resp.json()


def poll_for_graph_token(
    device_code_info: dict,
    client_id: str,
    tenant_id: str,
    client_secret: str = "",
) -> str:
    """
    Polls for a delegated Graph token using the device code flow.
    For public clients, client_secret should be omitted (defaults to "").
    """
    expires_at = time.time() + device_code_info.get("expires_in", 900)
    interval = device_code_info.get("interval", 5)

    while time.time() < expires_at:
        poll_data = {
            "client_id": client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code_info["device_code"],
        }
        if client_secret:
            poll_data["client_secret"] = client_secret

        resp = httpx.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data=poll_data,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]

        data = resp.json()
        error = data.get("error")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        elif error == "authorization_declined":
            raise CatalogPublishError("User declined authorization.")
        elif error == "expired_token":
            raise CatalogPublishError("Device code expired.")
        else:
            raise CatalogPublishError(
                f"Token polling failed: {error} - {data.get('error_description')}"
            )

    raise CatalogPublishError("Timed out waiting for device code authorization.")


# ---------------------------------------------------------------------------
# Upload & verify
# ---------------------------------------------------------------------------

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
    Returns the app dict, or a minimal dict with just the id if the app is
    pending admin approval (404) — which is normal in tenants that require
    Teams admin approval before custom apps become visible.
    """
    url = f"{GRAPH_BASE}/appCatalogs/teamsApps/{teams_app_id}"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"$expand": "appDefinitions"}

    resp = httpx.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 200:
        app_data = resp.json()
        logger.info("Verified in catalog: %s (id=%s)", app_data.get("displayName"), app_data.get("id"))
        return app_data

    if resp.status_code == 404:
        # App was uploaded (201 received) but is pending Teams admin approval.
        # This is normal in tenants with custom app submission policies.
        # The tenant admin must approve the app in Teams Admin Center before it's visible.
        logger.warning(
            "App %s uploaded successfully but not yet visible in catalog (404 - likely pending "
            "admin approval in Teams Admin Center). This is not a failure.",
            teams_app_id,
        )
        return {"id": teams_app_id, "pendingApproval": True}

    raise CatalogPublishError(f"Verification failed (HTTP {resp.status_code}): {resp.text}")


def publish_manifest(*, zip_bytes: bytes, token: str) -> dict:
    """
    Full publish flow: upload -> verify.
    Returns the app's Graph resource dict (includes 'id' == teamsAppId).
    If the tenant requires admin approval, verification returns a minimal dict
    with pendingApproval=True — the upload itself is still considered successful.
    """
    teams_app_id = upload_new_app(token, zip_bytes)
    return verify_app(token, teams_app_id)
