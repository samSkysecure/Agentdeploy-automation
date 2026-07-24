"""
Per-customer App Registration provisioning.

Replaces the old model where every customer's Bot Service + Container App
shared Skysecure's own multi-tenant App Registration and secret. Instead,
for each deployment we create a NEW single-tenant App Registration that
lives inside the customer's own Entra ID tenant, with its own client
secret. This is the agent's permanent runtime identity:

  - Used as msaAppId / msaAppTenantId on the Bot Service (SingleTenant auth)
  - Used as CLIENT_ID/CLIENT_SECRET for bot channel auth in the container
  - Used as MSCLIENT_ID/MSCLIENT_SECRET for Graph/SharePoint calls

IMPORTANT: unlike the deployment SPN (which gets torn down after a
successful deployment - see deployment_service._step_teardown_deployment_spn),
this App Registration and its Service Principal are meant to persist
permanently. Teardown logic must never target these objects.

Auth note: every call here is made with the DEPLOYMENT SPN's Graph token
(client-credentials, app-only), which must have been granted
Application.ReadWrite.OwnedBy during admin consent. That scope is what
lets the deployment SPN create app registrations + service principals
in the customer's tenant - it does NOT grant it any ownership/control
over agent App Registration afterward beyond what's needed to create it.
"""
import logging
import secrets
import string
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger("orchestrator.app_registration_service")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Microsoft Graph application (app-only) permission role IDs - fixed,
# well-known GUIDs, identical across every tenant (only the Graph service
# principal's own object ID varies per tenant - resolved dynamically in
# grant_agent_graph_permissions below).
#   Mail.ReadWrite (Application): e2a3a72e-5f79-4c64-b1b1-878b674786c9
#   Mail.Send (Application):      b633e1c5-b582-4048-a93e-9f11b44c7e96
#   User.Read.All (Application):  df021288-bdef-4463-88db-98f22de89214
# NOTE: plain "User.Read" and "People.Read" have no Application-permission
# form in Graph (delegated-only) - not requestable here, deliberately
# dropped rather than requesting something Graph will always reject.
GRAPH_RESOURCE_APP_ID = "00000003-0000-0000-c000-000000000000"
AGENT_GRAPH_APP_ROLE_IDS = {
    "Mail.ReadWrite": "e2a3a72e-5f79-4c64-b1b1-878b674786c9",
    "Mail.Send": "b633e1c5-b582-4048-a93e-9f11b44c7e96",
    "User.Read.All": "df021288-bdef-4463-88db-98f22de89214",
}


class AppRegistrationError(Exception):
    """Raised when creating/configuring the per-customer App Registration fails."""


@dataclass
class AgentAppRegistration:
    client_id: str
    client_secret: str
    app_object_id: str          # /applications/{id} - needed to reference the app later
    service_principal_id: str   # /servicePrincipals/{id} - the object actually holding permission grants
    tenant_id: str


def _graph_token(deployment_spn_client_id: str, deployment_spn_secret: str, customer_tenant_id: str) -> str:
    """
    Client-credentials token for Graph, scoped to the CUSTOMER's tenant.
    This only works because admin consent already registered the deployment
    SPN's service principal in that tenant with Application.ReadWrite.OwnedBy.
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
        raise AppRegistrationError(f"Failed to acquire Graph token in customer tenant: {resp.status_code} {resp.text}")
    return resp.json()["access_token"]


def _generate_secret_display_name() -> str:
    suffix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"deploy-generated-{suffix}"


def create_agent_app_registration(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
    agent_slug: str,
    customer_slug: str,
    secret_expiry_days: int = 730,
) -> AgentAppRegistration:
    """
    Creates a new single-tenant App Registration + client secret + Service
    Principal inside the customer's tenant. This is a synchronous, multi-step
    Graph sequence - each step depends on the object ID/appId from the
    previous one, so it cannot be parallelized.
    """
    token = _graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    display_name = f"skysecure-{agent_slug}-{customer_slug}"

    # 1. Create the application object (single-tenant: AzureADMyOrg = this tenant only)
    logger.info("Creating App Registration '%s' in tenant %s", display_name, customer_tenant_id)
    app_resp = httpx.post(
        f"{GRAPH_BASE}/applications",
        headers=headers,
        json={
            "displayName": display_name,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [
                {
                    "resourceAppId": GRAPH_RESOURCE_APP_ID,
                    "resourceAccess": [
                        {"id": role_id, "type": "Role"}
                        for role_id in AGENT_GRAPH_APP_ROLE_IDS.values()
                    ],
                }
            ],
        },
        timeout=30,
    )
    if app_resp.status_code not in (200, 201):
        raise AppRegistrationError(f"Failed to create App Registration: {app_resp.status_code} {app_resp.text}")
    app = app_resp.json()
    app_object_id = app["id"]
    client_id = app["appId"]

    # 2. Set identifierUris + expose the access_as_user scope (needed if/when SSO is
    #    required by the agent; harmless to set unconditionally for a "common" identity).
    scope_id = _new_guid()
    for attempt in range(5):
        patch_resp = httpx.patch(
            f"{GRAPH_BASE}/applications/{app_object_id}",
            headers=headers,
            json={
                "identifierUris": [f"api://{client_id}"],
                "api": {
                    "oauth2PermissionScopes": [
                        {
                            "id": scope_id,
                            "adminConsentDescription": f"Allow the app to access {display_name} on behalf of the signed-in user.",
                            "adminConsentDisplayName": "Access as user",
                            "userConsentDescription": f"Allow the app to access {display_name} on your behalf.",
                            "userConsentDisplayName": "Access as user",
                            "value": "access_as_user",
                            "type": "User",
                            "isEnabled": True,
                        }
                    ]
                },
            },
            timeout=30,
        )
        if patch_resp.status_code in (200, 204):
            break
        if patch_resp.status_code == 404 and attempt < 4:
            logger.info("Application object %s not yet replicated in Entra ID, retrying in 2s... (attempt %d/5)", app_object_id, attempt + 1)
            time.sleep(2)
        else:
            logger.warning("Failed to set identifierUris/oauth2PermissionScopes on %s: %s %s", client_id, patch_resp.status_code, patch_resp.text)
            break

    # 3. Add a client secret (with retry for Entra ID directory replication latency)
    logger.info("Adding client secret to App Registration %s", client_id)
    secret_resp = None
    for attempt in range(5):
        secret_resp = httpx.post(
            f"{GRAPH_BASE}/applications/{app_object_id}/addPassword",
            headers=headers,
            json={
                "passwordCredential": {
                    "displayName": _generate_secret_display_name(),
                    "endDateTime": _far_future_iso(secret_expiry_days),
                }
            },
            timeout=30,
        )
        if secret_resp.status_code in (200, 201):
            break
        if secret_resp.status_code == 404 and attempt < 4:
            logger.info("Application object %s not yet replicated in Entra ID for addPassword, retrying in 2s... (attempt %d/5)", app_object_id, attempt + 1)
            time.sleep(2)
        else:
            break

    if secret_resp is None or secret_resp.status_code not in (200, 201):
        err_text = secret_resp.text if secret_resp is not None else "No response"
        err_code = secret_resp.status_code if secret_resp is not None else 500
        raise AppRegistrationError(f"Failed to add client secret: {err_code} {err_text}")
    client_secret = secret_resp.json()["secretText"]

    # 4. Create the Service Principal - required before the app can be used
    #    as msaAppId on a Bot Service, or hold Graph permission grants.
    logger.info("Creating Service Principal for App Registration %s", client_id)
    sp_resp = None
    for attempt in range(5):
        sp_resp = httpx.post(
            f"{GRAPH_BASE}/servicePrincipals",
            headers=headers,
            json={"appId": client_id},
            timeout=30,
        )
        if sp_resp.status_code in (200, 201):
            break
        if sp_resp.status_code in (400, 404) and attempt < 4:
            logger.info("App %s not yet replicated in Entra ID for Service Principal creation, retrying in 2s... (attempt %d/5)", client_id, attempt + 1)
            time.sleep(2)
        else:
            break

    if sp_resp is None or sp_resp.status_code not in (200, 201):
        err_text = sp_resp.text if sp_resp is not None else "No response"
        err_code = sp_resp.status_code if sp_resp is not None else 500
        raise AppRegistrationError(f"Failed to create Service Principal: {err_code} {err_text}")
    sp_object_id = sp_resp.json()["id"]

    logger.info(
        "Agent App Registration ready: client_id=%s app_object_id=%s sp_object_id=%s",
        client_id, app_object_id, sp_object_id,
    )

    return AgentAppRegistration(
        client_id=client_id,
        client_secret=client_secret,
        app_object_id=app_object_id,
        service_principal_id=sp_object_id,
        tenant_id=customer_tenant_id,
    )


def grant_agent_graph_permissions(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
    agent_service_principal_id: str,
) -> None:
    """
    Admin-consents the agent's requiredResourceAccess (declared at app-creation
    time in create_agent_app_registration) by creating appRoleAssignments on
    the agent's own Service Principal for each Graph app-only role it needs:
    Mail.ReadWrite, Mail.Send, User.Read.All.

    Requires the deployment SPN to hold AppRoleAssignment.ReadWrite.All
    (Application permission, admin-consented) in the customer tenant - this
    is a privileged directory action, separate from AppCatalog.Submit /
    Sites.ReadWrite.All used elsewhere in this pipeline.

    Declaring requiredResourceAccess on the app manifest alone does NOT grant
    consent - Entra still requires this explicit appRoleAssignments call (or
    a manual admin-consent click) before the permissions are actually usable.
    """
    token = _graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Resolve the Microsoft Graph service principal's object ID *in this
    # tenant* - the well-known appId (00000003-...) is constant, but the SP
    # object ID for it is tenant-specific.
    sp_lookup = httpx.get(
        f"{GRAPH_BASE}/servicePrincipals",
        headers=headers,
        params={"$filter": f"appId eq '{GRAPH_RESOURCE_APP_ID}'"},
        timeout=30,
    )
    if sp_lookup.status_code != 200 or not sp_lookup.json().get("value"):
        raise AppRegistrationError(
            f"Failed to resolve Microsoft Graph service principal in tenant {customer_tenant_id}: "
            f"{sp_lookup.status_code} {sp_lookup.text}"
        )
    graph_sp_object_id = sp_lookup.json()["value"][0]["id"]

    for permission_name, app_role_id in AGENT_GRAPH_APP_ROLE_IDS.items():
        resp = httpx.post(
            f"{GRAPH_BASE}/servicePrincipals/{agent_service_principal_id}/appRoleAssignments",
            headers=headers,
            json={
                "principalId": agent_service_principal_id,
                "resourceId": graph_sp_object_id,
                "appRoleId": app_role_id,
            },
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info("Granted Graph app permission %s to agent SP %s", permission_name, agent_service_principal_id)
        elif resp.status_code == 400 and "already exists" in resp.text.lower():
            logger.info("Graph app permission %s already granted to agent SP %s, skipping", permission_name, agent_service_principal_id)
        else:
            raise AppRegistrationError(
                f"Failed to grant Graph app permission {permission_name} to agent SP "
                f"{agent_service_principal_id}: {resp.status_code} {resp.text}"
            )


def grant_sharepoint_site_permission(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
    agent_app_client_id: str,
    site_id: str,
) -> None:
    """
    Grants the agent's own Service Principal read/write access to a single
    SharePoint site via Sites.Selected-style per-site permission (works even
    if the tenant-wide consent scope was the broader Sites.FullControl.All -
    this keeps the *agent's* long-lived grant narrow regardless of what the
    deployment SPN itself was consented for).
    """
    token = _graph_token(deployment_spn_client_id, deployment_spn_secret, customer_tenant_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = httpx.post(
        f"{GRAPH_BASE}/sites/{site_id}/permissions",
        headers=headers,
        json={
            "roles": ["write"],
            "grantedToIdentities": [
                {
                    "application": {
                        "id": agent_app_client_id,
                        "displayName": f"skysecure-agent-{agent_app_client_id}",
                    }
                }
            ],
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise AppRegistrationError(
            f"Failed to grant site permission to agent app {agent_app_client_id} on site {site_id}: "
            f"{resp.status_code} {resp.text}"
        )
    logger.info("Granted site permission on %s to agent app %s", site_id, agent_app_client_id)


def _new_guid() -> str:
    import uuid
    return str(uuid.uuid4())


def _far_future_iso(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
