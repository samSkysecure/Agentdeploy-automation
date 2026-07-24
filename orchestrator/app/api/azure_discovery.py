import logging
import re
import base64
import json
from typing import Optional
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from azure.identity import ClientSecretCredential
from azure.mgmt.resource import SubscriptionClient, ResourceManagementClient
from azure.mgmt.authorization import AuthorizationManagementClient
from app.core.config import get_settings, Settings

logger = logging.getLogger("orchestrator.azure_discovery")

router = APIRouter(prefix="/api/azure", tags=["azure-discovery"])

settings = get_settings()

# Azure's built-in Owner role definition ID — fixed across all tenants
OWNER_ROLE_DEFINITION_ID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"


def _resolve_tenant_id_from_subscription(subscription_id: str) -> str:
    """
    Anonymously queries ARM to find the tenant associated with a subscription ID
    from the WWW-Authenticate header.
    """
    url = f"https://management.azure.com/subscriptions/{subscription_id}?api-version=2020-01-01"
    try:
        response = httpx.get(url)
        auth_header = response.headers.get("WWW-Authenticate", "")
        match = re.search(r"login\.(?:windows\.net|microsoftonline\.com)/([^/\"'\s]+)", auth_header)
        if match:
            return match.group(1)
    except Exception as e:
        logger.error("Failed to anonymously resolve tenant ID for subscription %s: %s", subscription_id, e)
    raise HTTPException(
        status_code=400,
        detail=f"Could not resolve Tenant ID for Subscription ID '{subscription_id}'. Please check if the Subscription ID is correct."
    )


def _deployment_spn_credential(settings: Settings, tenant_id: str = None) -> ClientSecretCredential:
    """
    Authenticates as the Deployment SPN against Skysecure's home (managing) tenant.
    Under Azure Lighthouse delegation, token acquisition MUST be scoped to the
    managing tenant (settings.deployment_spn_tenant_id), NOT the customer's tenant.
    """
    return ClientSecretCredential(
        tenant_id=settings.deployment_spn_tenant_id,
        client_id=settings.deployment_spn_client_id,
        client_secret=settings.deployment_spn_secret
    )


@router.get("/sp-details")
async def get_sp_details(settings: Settings = Depends(get_settings)):
    """Return the deployment SPN's client ID for display in the wizard."""
    return {
        "clientId": settings.deployment_spn_client_id
    }


@router.get("/resolve-tenant-id")
async def resolve_tenant_id(query: str):
    """
    Resolves an email address (e.g. user@contoso.com) or domain (e.g. contoso.com)
    or GUID string to its Azure Entra ID Tenant ID.
    """
    if not query:
        raise HTTPException(status_code=400, detail="email or domain query is required")

    clean_query = query.strip().lower()
    
    # If query is already a GUID, return directly
    guid_pattern = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    if re.match(guid_pattern, clean_query):
        return {"tenantId": clean_query, "domain": clean_query}

    domain = clean_query.split("@")[-1]
    url = f"https://login.microsoftonline.com/{domain}/v2.0/.well-known/openid-configuration"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                issuer = resp.json().get("issuer", "")
                parts = issuer.rstrip("/").split("/")
                if len(parts) >= 2:
                    tenant_id = parts[-2]
                    return {"tenantId": tenant_id, "domain": domain}
    except Exception as e:
        logger.error("Failed to resolve tenant ID for domain %s: %s", domain, e)

    raise HTTPException(
        status_code=404,
        detail=f"Could not resolve Tenant ID for '{query}'. Please check spelling or enter a valid work email/domain."
    )


@router.get("/validate-subscription")
async def validate_subscription(
    subscription_id: str,
    settings: Settings = Depends(get_settings)
):
    """
    Validates that the subscription exists and is accessible by Skysecure's SPN via Lighthouse delegation.
    """
    if not subscription_id:
        raise HTTPException(
            status_code=400,
            detail="subscription_id is required"
        )

    resolved_tenant_id = _resolve_tenant_id_from_subscription(subscription_id)
    credential = _deployment_spn_credential(settings)

    try:
        sub_client = SubscriptionClient(credential)
        sub = sub_client.subscriptions.get(subscription_id)
    except Exception as e:
        logger.error("Subscription lookup failed for %s: %s", subscription_id, e)
        raise HTTPException(
            status_code=404,
            detail=(
                f"Subscription '{subscription_id}' not delegated via Azure Lighthouse or not accessible. "
                "Please complete the Azure Lighthouse delegation step first."
            )
        )

    logger.info("Subscription %s validated successfully via Azure Lighthouse", subscription_id)
    return {
        "valid": True,
        "subscriptionId": sub.subscription_id,
        "displayName": sub.display_name,
        "state": sub.state,
        "tenantId": resolved_tenant_id,
        "plannedResourceGroup": "skysecure-agents-rg"
    }


@router.get("/assign-role-link")
async def get_assign_role_link(
    subscription_id: str,
    settings: Settings = Depends(get_settings)
):
    """
    Returns the Azure Lighthouse Delegation "Deploy to Azure" portal link.

    This directs the customer's admin to deploy the Lighthouse ARM template.
    The customer selects their own subscription IN the Azure portal when the
    template deploys — a real subscription_id is not required upfront.

    The frontend passes a placeholder (00000000-...) when no subscription has
    been selected yet; the portal URL works regardless because the portal itself
    asks the user to pick a subscription during deployment.
    """
    PLACEHOLDER_SUB = "00000000-0000-0000-0000-000000000000"
    real_sub_provided = subscription_id and subscription_id != PLACEHOLDER_SUB

    # Points to the unified Lighthouse delegation template in the current repo.
    # Both docgenhybrid and teamsagent use the same delegation template so we
    # use docgenhybrid as the canonical source (they are identical).
    TEMPLATE_RAW_URL = (
        "https://raw.githubusercontent.com/samSkysecure/Agentdeploy-automation/main"
        "/orchestrator/arm-templates/docgenhybrid/lighthouse-delegation.json"
    )
    TEMPLATE_RAW_URL_ENCODED = (
        "https%3A%2F%2Fraw.githubusercontent.com%2FsamSkysecure%2FAgentdeploy-automation%2Fmain"
        "%2Forchestrator%2Farm-templates%2Fdocgenhybrid%2Flighthouse-delegation.json"
    )

    lighthouse_url = (
        "https://portal.azure.com/#create/Microsoft.Template/uri/"
        + TEMPLATE_RAW_URL_ENCODED
    )

    if real_sub_provided:
        cli_command = (
            f"az deployment sub create --location eastus "
            f"--template-uri {TEMPLATE_RAW_URL} "
            f"--subscription {subscription_id}"
        )
    else:
        cli_command = (
            f"az deployment sub create --location eastus "
            f"--template-uri {TEMPLATE_RAW_URL} "
            "--subscription <YOUR_SUBSCRIPTION_ID>"
        )

    return {
        "portalUrl": lighthouse_url,
        "deploymentSpnClientId": settings.deployment_spn_client_id,
        "instructions": (
            "Click 'Deploy to Azure' to open the Azure portal. "
            "Select your subscription in the portal, review the template parameters, and click 'Create' to delegate "
            "Contributor and User Access Administrator access to Skysecure. "
            "Once the deployment completes, return to this wizard and click 'Refresh Subscriptions' in Step 4."
        ),
        "powershellFallback": cli_command,
    }



@router.get("/deployment-spn-object-id")
async def get_deployment_spn_object_id(
    tenant_id: str,
    settings: Settings = Depends(get_settings)
):
    """
    After admin consent, the deployment SPN gets a Service Principal object
    in the CUSTOMER's tenant - a different object ID than its home-tenant SP.
    Teardown needs this specific object ID (see teardown_service.py). This
    resolves it via Graph, authenticating with a token scoped to the
    customer tenant (works because consent already happened).
    """
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    token_resp = httpx.post(
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": settings.deployment_spn_client_id,
            "client_secret": settings.deployment_spn_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not acquire a Graph token in this tenant - admin consent "
                "may not have completed yet. " + token_resp.text
            ),
        )
    token = token_resp.json()["access_token"]

    sp_resp = httpx.get(
        "https://graph.microsoft.com/v1.0/servicePrincipals",
        headers={"Authorization": f"Bearer {token}"},
        params={"$filter": f"appId eq '{settings.deployment_spn_client_id}'"},
        timeout=30,
    )
    if sp_resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Graph lookup failed: {sp_resp.text}")

    values = sp_resp.json().get("value", [])
    if not values:
        raise HTTPException(
            status_code=404,
            detail="No Service Principal found for the deployment SPN in this tenant - has admin consent been granted?",
        )
    return {"deploymentSpnObjectId": values[0]["id"]}


@router.get("/subscriptions")
async def list_subscriptions(
    tenant_id: str,
    settings: Settings = Depends(get_settings)
):
    """
    Lists all subscriptions delegated to Skysecure via Azure Lighthouse.

    Authenticates as the deployment SPN against Skysecure's HOME (managing) tenant.
    Under Azure Lighthouse, cross-tenant ARM access is scoped to the managing tenant —
    tokens must be acquired from the managing tenant to enumerate delegated subscriptions.

    Admin consent (Step 2) only grants identity presence in the customer tenant and
    Graph API access. ARM role assignments come from Lighthouse delegation (Step 3),
    which delegates subscription access back to Skysecure's managing tenant SPN.
    """
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    try:
        # IMPORTANT: Authenticate against Skysecure's HOME tenant (managing tenant),
        # NOT the customer's tenant. Under Lighthouse cross-tenant delegation, the
        # managing tenant's SPN is what holds ARM role assignments on the customer's
        # delegated subscriptions. Using the customer tenant credential here would
        # return an empty list even after Lighthouse is set up, because the SPN only
        # has ARM access via the managing tenant token.
        credential = _deployment_spn_credential(settings)
        sub_client = SubscriptionClient(credential)

        subscriptions = [
            {
                "subscriptionId": sub.subscription_id,
                "displayName": sub.display_name,
                "tenantId": sub.tenant_id,
                "state": sub.state
            }
            for sub in sub_client.subscriptions.list()
            # Filter to only the customer's tenant subscriptions
            if sub.tenant_id == tenant_id
        ]

        # Fallback: if tenant_id filtering returns nothing (e.g., sub.tenant_id is None
        # or differs), return all accessible subscriptions from the managing tenant
        if not subscriptions:
            all_subs = [
                {
                    "subscriptionId": sub.subscription_id,
                    "displayName": sub.display_name,
                    "tenantId": sub.tenant_id,
                    "state": sub.state
                }
                for sub in sub_client.subscriptions.list()
            ]
            if all_subs:
                logger.warning(
                    "No subscriptions matched tenant_id=%s exactly; returning all %d accessible subscription(s). "
                    "Lighthouse delegation may be targeting a different tenant mapping.",
                    tenant_id, len(all_subs)
                )
                return all_subs

            raise HTTPException(
                status_code=404,
                detail=(
                    "No subscriptions found via Azure Lighthouse delegation. "
                    "Please complete Step 3 (Lighthouse Delegation) before selecting a subscription. "
                    "Admin consent alone (Step 2) does not grant ARM subscription access — "
                    "Lighthouse delegation is required to delegate subscription visibility to Skysecure's managing tenant."
                )
            )

        return subscriptions

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to list subscriptions for tenant %s: %s", tenant_id, e)
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve subscriptions. Error: {str(e)}"
        )


@router.get("/resource-groups")
async def list_resource_groups(
    tenant_id: str,
    subscription_id: str,
    settings: Settings = Depends(get_settings)
):
    """List resource groups in the specified subscription."""
    if not tenant_id or not subscription_id:
        raise HTTPException(status_code=400, detail="tenant_id and subscription_id are required")
    
    try:
        credential = _deployment_spn_credential(settings, tenant_id=tenant_id)  # NOTE: will return empty/fail until customer has completed "Assign Role" - see flag below
        rg_client = ResourceManagementClient(credential, subscription_id)
        resource_groups = []
        for rg in rg_client.resource_groups.list():
            resource_groups.append(rg.name)
        return resource_groups
    except Exception as e:
        logger.error("Failed to list Azure resource groups: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list resource groups. Error: {str(e)}"
        )


# ---------------------------------------------------------------------------
# Power Platform Environment Discovery (Delegated User Device-Code Flow)
# ---------------------------------------------------------------------------

class PowerPlatformDeviceCodeRequest(BaseModel):
    tenant_id: Optional[str] = None

class PowerPlatformDeviceTokenRequest(BaseModel):
    tenant_id: Optional[str] = None
    device_code: str

class PowerPlatformEnvironmentsRequest(BaseModel):
    access_token: str

PAC_WELL_KNOWN_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"


@router.post("/power-platform-device-code")
async def start_power_platform_device_code(req: PowerPlatformDeviceCodeRequest):
    """
    Initiates a delegated device-code flow against Microsoft Entra for Power Platform.
    Uses the well-known PAC CLI client ID (1950a258-227b-4e31-a9cf-717495945fc2).
    Defaults to Skysecure's Power Platform tenant (547b64a7-e66e-48df-a146-3e898cbcb60f).
    """
    target_tenant_id = settings.power_platform_tenant_id or "547b64a7-e66e-48df-a146-3e898cbcb60f"

    url = f"https://login.microsoftonline.com/{target_tenant_id}/oauth2/v2.0/devicecode"
    data = {
        "client_id": PAC_WELL_KNOWN_CLIENT_ID,
        "scope": "https://service.powerapps.com/.default offline_access",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data, timeout=15)
            if resp.status_code != 200:
                logger.error("Device code request failed for tenant %s: %s", target_tenant_id, resp.text)
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Failed to initiate device code login: {resp.text}"
                )
            res_data = resp.json()
            return {
                "user_code": res_data.get("user_code"),
                "verification_uri": res_data.get("verification_uri"),
                "device_code": res_data.get("device_code"),
                "expires_in": res_data.get("expires_in", 900),
                "interval": res_data.get("interval", 5),
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error initiating device code for tenant %s: %s", target_tenant_id, e)
        raise HTTPException(status_code=500, detail=f"Device code initiation failed: {str(e)}")


@router.post("/power-platform-device-token")
async def poll_power_platform_device_token(req: PowerPlatformDeviceTokenRequest):
    """
    Polls Microsoft Entra token endpoint for completion of device-code authorization.
    Returns the user's access token once granted.
    """
    if not req.device_code:
        raise HTTPException(status_code=400, detail="device_code is required")

    target_tenant_id = settings.power_platform_tenant_id or "547b64a7-e66e-48df-a146-3e898cbcb60f"

    url = f"https://login.microsoftonline.com/{target_tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": PAC_WELL_KNOWN_CLIENT_ID,
        "device_code": req.device_code,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=data, timeout=15)
            res_data = resp.json()

            if resp.status_code == 200:
                return {
                    "completed": True,
                    "access_token": res_data.get("access_token"),
                    "refresh_token": res_data.get("refresh_token"),
                }
            
            error_code = res_data.get("error")
            if error_code in ["authorization_pending", "slow_down"]:
                return {"completed": False, "pending": True}
            
            # Auth failed or expired
            logger.warning("Device token polling error: %s - %s", error_code, res_data.get("error_description"))
            return {
                "completed": False,
                "pending": False,
                "error": error_code,
                "error_description": res_data.get("error_description"),
            }

    except Exception as e:
        logger.error("Error polling device token: %s", e)
        raise HTTPException(status_code=500, detail=f"Token polling error: {str(e)}")


@router.post("/power-platform-environments")
async def discover_power_platform_environments(req: PowerPlatformEnvironmentsRequest):
    """
    Lists and filters Power Platform environments in the customer tenant using delegated user token.
    
    Filtering Rules:
      - linkedEnvironmentMetadata exists
      - linkedEnvironmentMetadata.instanceUrl is non-empty
      - properties.states.runtime.id == "Enabled"
      - linkedEnvironmentMetadata.instanceState == "Ready"
      
    Auto-Selection Preference Order:
      1. environmentSku == "Production"
      2. environmentSku == "Sandbox"
      3. null (admin picks manually)
    """
    if not req.access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    url = "https://api.powerapps.com/providers/Microsoft.PowerApps/environments?api-version=2020-06-01"
    headers = {"Authorization": f"Bearer {req.access_token}"}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=30)
            if resp.status_code != 200:
                logger.error("PowerApps environments lookup failed (HTTP %s): %s", resp.status_code, resp.text)
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"PowerApps environments lookup failed (HTTP {resp.status_code}): {resp.text}"
                )
            
            data = resp.json()
            raw_envs = data.get("value", [])
            usable_envs = []

            for env in raw_envs:
                props = env.get("properties", {})
                linked_meta = props.get("linkedEnvironmentMetadata")
                
                # Rule 1: linkedEnvironmentMetadata exists
                if not linked_meta or not isinstance(linked_meta, dict):
                    continue

                # Rule 2: instanceUrl is non-empty
                instance_url = linked_meta.get("instanceUrl")
                if not instance_url or not str(instance_url).strip():
                    continue

                # Rule 3: properties.states.runtime.id == "Enabled"
                runtime_state = props.get("states", {}).get("runtime", {}).get("id")
                if runtime_state != "Enabled":
                    continue

                # Rule 4: linkedEnvironmentMetadata.instanceState == "Ready"
                instance_state = linked_meta.get("instanceState")
                if instance_state != "Ready":
                    continue

                sku = props.get("environmentSku", "Unknown")
                is_default = bool(props.get("isDefault", False))
                display_name = props.get("displayName") or env.get("name")
                env_id = env.get("name")  # Environment GUID

                usable_envs.append({
                    "id": env_id,
                    "displayName": display_name,
                    "environmentSku": sku,
                    "instanceUrl": instance_url,
                    "isDefault": is_default,
                })

            # Auto-selection logic (Production -> Sandbox -> None)
            auto_selected_id = None
            prod_match = next((e for e in usable_envs if e["environmentSku"].lower() == "production"), None)
            if prod_match:
                auto_selected_id = prod_match["id"]
            else:
                sandbox_match = next((e for e in usable_envs if e["environmentSku"].lower() == "sandbox"), None)
                if sandbox_match:
                    auto_selected_id = sandbox_match["id"]

            return {
                "environments": usable_envs,
                "autoSelectedId": auto_selected_id,
                "count": len(usable_envs),
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to discover Power Platform environments: %s", e)
        raise HTTPException(status_code=500, detail=f"Environment discovery failed: {str(e)}")