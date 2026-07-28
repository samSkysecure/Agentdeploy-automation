"""
Deployment sequence orchestrator.

Deploys a Container App (Copilot Studio relay bot) + Bot Service + Manifest.
Optionally provisions SharePoint document library structure (Templates + Generated)
on the customer's SharePoint site if sharepoint_site_url is provided in the request.
"""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote

import httpx

from app.core.config import Settings
from app.models.deployment import DeploymentRecord, DeploymentStatus, StepResult
from app.services.azure_client import ArmDeploymentError, AzureDeploymentClient
from app.services.teams_manifest import generate_and_zip_manifest
from app.services.sharepoint import SharePointClient, resolve_customer_sharepoint_site_url
from app.services.app_registration_service import (
    AppRegistrationError,
    create_agent_app_registration,
    grant_sharepoint_site_permission,
    grant_agent_graph_permissions,
)
from app.services.acr_service import AcrImportError, import_image_to_customer_acr
from app.services.teardown_service import TeardownError, teardown_deployment_spn
from app.services.keyvault_service import (
    KeyVaultError,
    store_agent_secret_in_customer_keyvault,
    grant_secrets_user_role,
    _grant_role_assignment_safe,
)
from app.services import copilot_studio_service as cs
from app.services.copilot_studio_service import CopilotStudioImportError
from app.services.catalog_publish_service import publish_manifest, CatalogPublishError

logger = logging.getLogger("orchestrator.deployment_service")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_deployment(record: DeploymentRecord, settings: Settings) -> None:
    req = record.request
    location = req.location or settings.default_location
    record.resource_group_name = req.resource_group_name

    client = AzureDeploymentClient(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        customer_tenant_id=req.customer_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
        templates_dir=settings.arm_templates_dir,
    )

    try:
        # Step 0: Confirm Azure access is active for the subscription
        logger.info("Verifying Azure access on subscription %s", req.customer_subscription_id)
        client.verify_role_assignment(req.deployment_spn_object_id_in_customer_tenant)

        logger.info("Ensuring Resource Group %s exists in %s", record.resource_group_name, location)
        client.create_resource_group(record.resource_group_name, location)

        logger.info("Ensuring required Azure Resource Providers are registered on subscription %s", req.customer_subscription_id)
        client.register_resource_providers()

        _step_create_app_registration(record, req, settings)
        _step_import_image(record, req, settings, location)
        _step_store_secret_in_keyvault(record, req, settings, location)
        _step_deploy_container_app(record, client, req, location, settings)
        _step_grant_keyvault_access(record, req, settings)
        _step_deploy_bot_service(record, client, req, settings)
        _step_generate_manifest(record, req, settings)

        _step_provision_sharepoint(record, req, settings)

        _step_import_copilot_studio_solution(record, req, settings)

        _step_publish_manifest_to_catalog(record, req, settings)

        _step_teardown_deployment_spn(record, req, settings)

        record.status = DeploymentStatus.SUCCEEDED
        logger.info("Deployment %s succeeded", record.deployment_id)

    except (ArmDeploymentError, AppRegistrationError, AcrImportError, KeyVaultError, CopilotStudioImportError, CatalogPublishError) as exc:
        record.status = DeploymentStatus.FAILED
        record.error = str(exc)
        logger.error("Deployment %s failed: %s", record.deployment_id, exc)
    except TeardownError as exc:
        # Infra is live and working - only the deployment SPN's own cleanup failed.
        # Report SUCCEEDED but surface this loudly; a lingering deployment SPN grant
        # needs manual/alerted follow-up, not a failed deployment for the customer.
        record.status = DeploymentStatus.SUCCEEDED
        record.error = f"Deployment succeeded but teardown failed - manual cleanup required: {exc}"
        logger.error("Deployment %s teardown FAILED (deployment itself succeeded): %s", record.deployment_id, exc)
    except Exception as exc:
        record.status = DeploymentStatus.FAILED
        record.error = f"Unexpected error: {exc}"
        logger.exception("Deployment %s hit an unexpected error", record.deployment_id)


# ---------------------------------------------------------------------------
# SharePoint provisioning
# ---------------------------------------------------------------------------

def _step_provision_sharepoint(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.PROVISIONING_SHAREPOINT
    raw_site_url = (req.sharepoint_site_url or "").rstrip("/")
    if raw_site_url.count("https://") > 1:
        raw_site_url = "https://" + [p for p in raw_site_url.split("https://") if p][0]
    elif raw_site_url.count("http://") > 1:
        raw_site_url = "http://" + [p for p in raw_site_url.split("http://") if p][0]

    # Validate input URL or auto-discover the customer's actual tenant SharePoint site
    site_url = resolve_customer_sharepoint_site_url(
        input_site_url=raw_site_url,
        tenant_id=req.customer_tenant_id,
        client_id=settings.deployment_spn_client_id,
        client_secret=settings.deployment_spn_secret,
    )

    logger.info("Provisioning SharePoint structure on site: %s", site_url)

    # Provisioning itself still uses the DEPLOYMENT SPN
    client = SharePointClient(
        site_url=site_url,
        tenant_id=req.customer_tenant_id,
        client_id=settings.deployment_spn_client_id,
        client_secret=settings.deployment_spn_secret,
    )
    
    structure = client.ensure_structure()
    
    site_id = structure["site_id"]
    templates_drive_id = structure["template_drive_id"]
    generated_drive_id = structure["generated_drive_id"]
    deployed_lists_id = structure["deployed_lists_id"]

    # Grant the PERMANENT agent App Registration its own narrow, long-lived
    # read/write grant on this specific site - this is the credential the
    # deployed agent actually uses at runtime, independent of the deployment
    # SPN which gets torn down at the end of this pipeline.
    grant_sharepoint_site_permission(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        customer_tenant_id=req.customer_tenant_id,
        agent_app_client_id=record.agent_app_client_id,
        site_id=site_id,
    )

    # Store on record so they can be injected into Container App env vars
    record.sharepoint_site_url = site_url
    record.sharepoint_site_id = site_id
    record.sharepoint_templates_drive_id = templates_drive_id
    record.sharepoint_generated_drive_id = generated_drive_id
    record.sharepoint_deployed_lists_id = deployed_lists_id

    record.steps.append(StepResult(
        step="provision_sharepoint",
        status="succeeded",
        outputs={
            "site_url": site_url,
            "site_id": site_id,
            "templates_drive_id": templates_drive_id,
            "generated_drive_id": generated_drive_id,
            "deployed_lists_id": deployed_lists_id,
        },
        detail=_timestamp(),
    ))
    logger.info(
        "SharePoint provisioned. Site: %s | Templates: %s | Generated: %s | Deployed Lists: %s",
        site_id, templates_drive_id, generated_drive_id, deployed_lists_id,
    )


# ---------------------------------------------------------------------------
# App Registration (PERMANENT - replaces the old shared skysecure app id/secret)
# ---------------------------------------------------------------------------

def _step_create_app_registration(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.CREATING_APP_REGISTRATION
    agent_app = create_agent_app_registration(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        customer_tenant_id=req.customer_tenant_id,
        agent_slug=req.agent_slug,
        customer_slug=req.customer_slug,
    )
    record.agent_app_client_id = agent_app.client_id
    record.agent_app_client_secret = agent_app.client_secret
    record.agent_app_object_id = agent_app.app_object_id
    record.agent_app_service_principal_id = agent_app.service_principal_id

    grant_agent_graph_permissions(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        customer_tenant_id=req.customer_tenant_id,
        agent_service_principal_id=agent_app.service_principal_id,
    )

    record.steps.append(StepResult(
        step="create_app_registration",
        status="succeeded",
        outputs={
            "client_id": agent_app.client_id,
            "app_object_id": agent_app.app_object_id,
            "service_principal_id": agent_app.service_principal_id,
            # secret intentionally omitted from step output/logs
        },
        detail=_timestamp(),
    ))
    logger.info("Agent App Registration created: client_id=%s", agent_app.client_id)


# ---------------------------------------------------------------------------
# ACR import (PERMANENT - replaces the old shared ACR admin credentials)
# ---------------------------------------------------------------------------

def _step_import_image(
    record: DeploymentRecord,
    req,
    settings: Settings,
    location: str,
) -> None:
    record.status = DeploymentStatus.IMPORTING_IMAGE
    result = import_image_to_customer_acr(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        customer_tenant_id=req.customer_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
        customer_resource_group=req.resource_group_name,
        location=location,
        customer_slug=req.customer_slug,
        agent_slug=req.agent_slug,
        source_acr_name=settings.source_acr_name,
        source_repository=settings.source_acr_repository or req.agent_slug,
        source_tag=req.agent_image_tag,
        source_acr_resource_group=settings.source_acr_resource_group or req.resource_group_name,
        source_acr_subscription_id=settings.source_acr_subscription_id or req.customer_subscription_id,
        source_acr_username=settings.source_acr_username,
        source_acr_password=settings.source_acr_password,
    )
    record.customer_acr_login_server = result.customer_acr_login_server
    record.customer_acr_resource_id = result.customer_acr_resource_id
    record.customer_agent_image_reference = result.image_reference
    record.customer_acr_username = result.customer_acr_username
    record.customer_acr_password = result.customer_acr_password

    record.steps.append(StepResult(
        step="import_image_to_customer_acr",
        status="succeeded",
        outputs={
            "customer_acr_login_server": result.customer_acr_login_server,
            "image_reference": result.image_reference,
        },
        detail=_timestamp(),
    ))
    logger.info("Image imported into customer ACR: %s", result.image_reference)


# ---------------------------------------------------------------------------
# Key Vault (PERMANENT - durable secure home for the agent app's secret;
# see the ordering note in keyvault_service.py re: why the Container App
# deployment itself still receives the secret as a securestring parameter
# rather than a live Key Vault reference on this first revision)
# ---------------------------------------------------------------------------

def _step_store_secret_in_keyvault(
    record: DeploymentRecord,
    req,
    settings: Settings,
    location: str,
) -> None:
    if not record.agent_app_client_secret:
        raise KeyVaultError("Cannot store secret in Key Vault - agent App Registration step must run first.")

    kv_result = store_agent_secret_in_customer_keyvault(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
        customer_resource_group=req.resource_group_name,
        customer_tenant_id=req.customer_tenant_id,
        location=location,
        customer_slug=req.customer_slug,
        agent_slug=req.agent_slug,
        agent_app_client_secret=record.agent_app_client_secret,
        deployment_spn_object_id=req.deployment_spn_object_id_in_customer_tenant,
    )
    record.keyvault_name = kv_result.vault_name
    record.keyvault_uri = kv_result.vault_uri
    record.keyvault_resource_id = kv_result.vault_resource_id
    record.keyvault_secret_uri = kv_result.secret_uri

    record.steps.append(StepResult(
        step="store_secret_in_keyvault",
        status="succeeded",
        outputs={"vault_name": kv_result.vault_name, "vault_uri": kv_result.vault_uri},
        detail=_timestamp(),
    ))
    logger.info("Agent app secret stored in Key Vault: %s", kv_result.vault_name)


def _step_grant_keyvault_access(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    if not record.container_app_principal_id:
        logger.warning("Container App has no principalId recorded - skipping Key Vault access grant. Check ARM output wiring.")
        return
    if not record.keyvault_resource_id:
        logger.warning("No Key Vault provisioned - skipping access grant.")
        return

    grant_secrets_user_role(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        customer_tenant_id=req.customer_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
        vault_resource_id=record.keyvault_resource_id,
        principal_id=record.container_app_principal_id,
    )
    # Now that the secret has a durable, access-controlled home AND the
    # step that needed the plaintext value (ARM deployment) has already
    # run, clear it from the in-memory/store record as defense-in-depth.
    record.agent_app_client_secret = None

    record.steps.append(StepResult(
        step="grant_keyvault_access",
        status="succeeded",
        outputs={"principal_id": record.container_app_principal_id},
        detail=_timestamp(),
    ))
    logger.info("Granted Container App identity access to Key Vault %s", record.keyvault_name)


# ---------------------------------------------------------------------------
# Teardown (deployment SPN only - runs last, on overall success)
# ---------------------------------------------------------------------------

def _step_teardown_deployment_spn(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.TEARING_DOWN_DEPLOYMENT_SPN
    teardown_deployment_spn(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        deployment_spn_object_id=req.deployment_spn_object_id_in_customer_tenant,
        customer_tenant_id=req.customer_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
    )
    record.deployment_spn_teardown_completed = True
    record.steps.append(StepResult(
        step="teardown_deployment_spn",
        status="succeeded",
        outputs={"deployment_spn_object_id": req.deployment_spn_object_id_in_customer_tenant},
        detail=_timestamp(),
    ))
    logger.info("Deployment SPN torn down for customer tenant %s", req.customer_tenant_id)


# ---------------------------------------------------------------------------
# Copilot Studio / Power Platform import (ported from onboard_customer.ps1
# steps 3-7). Contains a hard human-in-the-loop pause - see
# copilot_studio_service.py's module docstring.
# ---------------------------------------------------------------------------

def _fetch_usable_pp_environments(user_token: str) -> list[dict]:
    """
    Fetches Power Platform environments accessible with the given user token
    and filters to only those that are usable (Enabled, Ready, have an instanceUrl).
    Returns a list of dicts with keys: displayName, instanceUrl, environmentSku, id.
    """
    resp = httpx.get(
        "https://api.powerapps.com/providers/Microsoft.PowerApps/environments?api-version=2020-06-01",
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise CopilotStudioImportError(
            f"Failed to list Power Platform environments: {resp.status_code} {resp.text}"
        )

    usable = []
    for env in resp.json().get("value", []):
        props = env.get("properties", {})
        linked_meta = props.get("linkedEnvironmentMetadata")
        if not linked_meta or not isinstance(linked_meta, dict):
            continue
        instance_url = linked_meta.get("instanceUrl")
        if not instance_url or not str(instance_url).strip():
            continue
        if props.get("states", {}).get("runtime", {}).get("id") != "Enabled":
            continue
        if linked_meta.get("instanceState") != "Ready":
            continue
        usable.append({
            "displayName": props.get("displayName") or env.get("name"),
            "instanceUrl": instance_url.rstrip("/"),
            "environmentSku": props.get("environmentSku", "Unknown"),
            "id": env.get("name"),
        })
    return usable


def _select_pp_environment(record: DeploymentRecord, user_token: str) -> str:
    """
    Discovers available Power Platform environments and returns the selected
    instance URL, either automatically (if one Production env or only one env
    exists) or by waiting for the user to pick from a dropdown in the wizard UI.

    When user interaction is needed:
      1. Stores the list in record.available_pp_environments
      2. Sets record.status = AWAITING_ENVIRONMENT_SELECTION
      3. Blocks on store.wait_for_env_selection() (threading.Event)
      4. The /deployments/{id}/select-environment endpoint unblocks this thread
         when the user submits their choice.
    """
    from app.services.deployment_store import store

    usable_envs = _fetch_usable_pp_environments(user_token)
    if not usable_envs:
        raise CopilotStudioImportError(
            "No usable Power Platform environments found for this account. "
            "Ensure the logged-in user has access to at least one active, "
            "Dataverse-enabled Power Platform environment."
        )

    # Auto-selection: Production first, then single-env fallback.
    prod_env = next((e for e in usable_envs if e["environmentSku"].lower() == "production"), None)
    if prod_env:
        logger.info(
            "Auto-selected Production Power Platform environment: %s (%s)",
            prod_env["displayName"], prod_env["instanceUrl"]
        )
        return prod_env["instanceUrl"]

    if len(usable_envs) == 1:
        env = usable_envs[0]
        logger.info(
            "Only one usable PP environment found, auto-selecting: %s (%s)",
            env["displayName"], env["instanceUrl"]
        )
        return env["instanceUrl"]

    # Multiple environments with no Production — pause and ask the user.
    logger.info(
        "Multiple PP environments found (%d), awaiting user selection.", len(usable_envs)
    )
    record.available_pp_environments = usable_envs
    record.status = DeploymentStatus.AWAITING_ENVIRONMENT_SELECTION
    store.create_env_selection_event(record.deployment_id)

    selected_url = store.wait_for_env_selection(record.deployment_id, timeout=600.0)

    record.available_pp_environments = None  # clear — selection is done
    if not selected_url:
        raise CopilotStudioImportError(
            "Timed out waiting for Power Platform environment selection (10 min). "
            "Please restart the deployment and select an environment when prompted."
        )

    logger.info("User selected PP environment: %s", selected_url)
    return selected_url


def _step_import_copilot_studio_solution(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    import tempfile
    from pathlib import Path as _Path

    if not record.container_app_fqdn:
        raise CopilotStudioImportError("Cannot import Copilot Studio solution - Container App must be deployed first.")

    # By default, Copilot Studio is deployed into the customer's tenant alongside the Azure infra.
    # If a caller explicitly sets power_platform_tenant_id on the request or settings, honour it;
    # otherwise fallback to the customer's tenant.
    power_platform_tenant_id = req.power_platform_tenant_id or settings.power_platform_tenant_id or req.customer_tenant_id
    work_dir = tempfile.mkdtemp(prefix=f"copilot-import-{req.customer_slug}-{req.agent_slug}-")

    # Resolve zip paths to absolute so PAC CLI finds them regardless of cwd (temp dir)
    abs_connector_zip = str(_Path(req.connector_solution_zip_path).resolve())
    abs_solution_zip = str(_Path(req.solution_zip_path).resolve())

    logger.info("Connector zip: %s (exists: %s)", abs_connector_zip, _Path(abs_connector_zip).exists())
    logger.info("Solution zip:  %s (exists: %s)", abs_solution_zip, _Path(abs_solution_zip).exists())

    if not _Path(abs_connector_zip).exists():
        raise CopilotStudioImportError(f"Connector solution zip not found: {abs_connector_zip}")
    if not _Path(abs_solution_zip).exists():
        raise CopilotStudioImportError(f"Agent solution zip not found: {abs_solution_zip}")

    # --- Step 3: inject Container App FQDN into the connector's swagger host ---
    injected_connector_zip = cs.inject_connector_host_and_repack(
        connector_zip_path=abs_connector_zip,
        container_app_fqdn=record.container_app_fqdn,
        work_dir=work_dir,
    )

    # --- Step 3b: inject SharePoint KB sources into the agent solution ---
    injected_solution_zip = cs.inject_kb_sources_and_repack(
        solution_zip_path=abs_solution_zip,
        kb_site_urls=req.knowledge_base_site_urls or [],
        work_dir=work_dir,
    )

    # --- Step 4: Human device-code login — MOVED FIRST so we have a user token
    # before PAC CLI auth, enabling auto-discovery of Power Platform environments.
    # The pipeline pauses here, surfaces a device code + verification URL to the
    # frontend (via DeploymentRecord.device_code_info), and blocks polling for the
    # human to complete login in a browser.
    record.status = DeploymentStatus.AWAITING_USER_DEVICE_AUTH
    device_code_info = cs.start_device_code_flow(power_platform_tenant_id)
    record.device_code_info = {
        "user_code": device_code_info.user_code,
        "verification_uri": device_code_info.verification_uri,
        "purpose": "user_token",
    }
    # Flush to store so frontend shows the popup while we block on polling below
    from app.services.deployment_store import store as _store
    _store.save(record)
    user_token = cs.poll_for_user_token(device_code_info, power_platform_tenant_id)
    record.device_code_info = None

    # --- Step 4b: Discover and select the Power Platform environment.
    # Uses the freshly-acquired user token to list environments.
    # Auto-selects Production if available, auto-selects if only one exists,
    # otherwise pauses again (AWAITING_ENVIRONMENT_SELECTION) for user to pick.
    record.status = DeploymentStatus.IMPORTING_COPILOT_SOLUTION
    selected_instance_url = _select_pp_environment(record, user_token)
    env_guid = cs.resolve_environment_guid(selected_instance_url, user_token)
    record.power_platform_environment_guid = env_guid
    record.status = DeploymentStatus.IMPORTING_COPILOT_SOLUTION

    # --- Step 5: Authenticate PAC CLI interactively via Device Code.
    # Microsoft Power Platform blocks Service Principals from importing Custom Connectors
    # unless they are explicitly assigned the System Administrator role in the environment.
    # By using the user's Global Admin account here, we bypass that limitation and fully
    # automate the import process!
    def _on_pac_device_code(info: cs.DeviceCodeInfo):
        from app.services.deployment_store import store
        record.status = DeploymentStatus.AWAITING_USER_DEVICE_AUTH
        record.device_code_info = {
            "user_code": info.user_code,
            "verification_uri": info.verification_uri,
            "purpose": "pac_auth",
        }
        # Force a state flush so the frontend picks it up immediately while the subprocess blocks
        store.save(record)

    cs.pac_auth_create_device_code(
        environment_id=selected_instance_url,
        tenant_id=power_platform_tenant_id,
        work_dir=work_dir,
        on_device_code=_on_pac_device_code,
    )
    record.device_code_info = None

    cs.import_connector_solution(injected_connector_zip, work_dir=work_dir)

    record.steps.append(StepResult(
        step="import_connector_solution",
        status="succeeded",
        outputs={"connector_zip": injected_connector_zip},
        detail=_timestamp(),
    ))

    settings_json = cs.generate_settings_json(injected_solution_zip, work_dir=work_dir)

    connector_pattern = (
        "docgen-20sharepoint-20connector"
        if req.agent_slug == "teamsagent"
        else "openapi-5fdocgen-5fagent"
    )
    connector_api_name = cs.resolve_custom_connector_api(env_guid, user_token, connector_name_pattern=connector_pattern)
    custom_connection_id = cs.create_connection(env_guid, user_token, connector_api_name, "DocGen Custom Connector")
    copilot_connection_id = cs.create_connection(env_guid, user_token, "shared_microsoftcopilotstudio", "Microsoft Copilot Studio Connection")

    cs.bind_connections_and_import_solution(
        settings=settings_json,
        custom_connection_id=custom_connection_id,
        copilot_connection_id=copilot_connection_id,
        solution_zip_path=injected_solution_zip,
        work_dir=work_dir,
    )

    # --- Step 6: fetch the flow's webhook URL, patch it into the Container App ---
    flow_webhook_url = None
    if req.agent_slug == "teamsagent":
        flow_webhook_url = cs.fetch_flow_webhook_url(env_guid, user_token, flow_name_pattern="docgen flow")
    record.copilot_flow_webhook_url = flow_webhook_url

    client = AzureDeploymentClient(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        deployment_spn_tenant_id=settings.deployment_spn_tenant_id,
        customer_subscription_id=req.customer_subscription_id,
        templates_dir=settings.arm_templates_dir,
    )
    env_updates = {}
    if flow_webhook_url:
        env_updates["COPILOT_FLOW_URL"] = flow_webhook_url
    if req.sharepoint_site_url:
        env_updates["SHAREPOINT_SITE_URL"] = req.sharepoint_site_url
    if env_updates:
        container_app_name = f"ca-{req.agent_slug}-{req.customer_slug}"
        client.patch_container_app_env_vars(req.resource_group_name, container_app_name, env_updates)

    record.steps.append(StepResult(
        step="import_copilot_studio_solution",
        status="succeeded",
        outputs={"environment_guid": env_guid, "flow_webhook_url": flow_webhook_url or ""},
        detail=_timestamp(),
    ))
    logger.info("Copilot Studio solution import complete for %s/%s", req.customer_slug, req.agent_slug)



# ---------------------------------------------------------------------------
# deployment steps 
# ---------------------------------------------------------------------------

def _step_deploy_container_app(
    record: DeploymentRecord,
    client: AzureDeploymentClient,
    req,
    location: str,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.DEPLOYING_CONTAINER_APP
    deployment_name = f"deploy-containerapp-{req.agent_slug}-{req.customer_slug}"

    # Image now comes from the customer's OWN ACR (imported in _step_import_image),
    # pulled via the Container App's managed identity - no ACR credentials at all.
    if not record.customer_agent_image_reference:
        raise ArmDeploymentError("Container App deployment requires customer_agent_image_reference - image import step must run first.")
    agent_image = record.customer_agent_image_reference

    # Bot auth + Graph auth now use the per-customer App Registration created
    # in _step_create_app_registration - NOT Skysecure's shared app id/secret.
    if not record.agent_app_client_id or not record.agent_app_client_secret:
        raise ArmDeploymentError("Container App deployment requires the agent App Registration - that step must run first.")

    parameters = {
        "agentSlug": req.agent_slug,
        "customerSlug": req.customer_slug,
        "location": location,
        "agentImage": agent_image,
        "customerAcrLoginServer": record.customer_acr_login_server,
        "customerAcrResourceId": record.customer_acr_resource_id,
        "microsoftAppId": record.agent_app_client_id,
        "microsoftAppPassword": record.agent_app_client_secret,
        "customerTenantId": req.customer_tenant_id,
        "sharepointTenantId": req.customer_tenant_id,
        "sharepointSiteUrl": record.sharepoint_site_url or req.sharepoint_site_url or "",
        "msClientId": record.agent_app_client_id,
        "msClientSecret": record.agent_app_client_secret,
        "redisHost": settings.REDIS_HOST,
        "redisPort": str(settings.REDIS_PORT),
        "redisPassword": settings.REDIS_PASSWORD,
        "langchainApiKey": settings.LANGCHAIN_API_KEY,
        "langchainProject": settings.LANGCHAIN_PROJECT,
        "azureDocumentIntelKey": settings.AZURE_DOCUMENT_INTEL_KEY,
        "azureDocumentIntelEndpoint": settings.AZURE_DOCUMENT_INTEL_ENDPOINT,
        "azureOpenAiApiKey": settings.azure_openai_api_key,
        "azureOpenAiEndpoint": settings.azure_openai_endpoint or settings.azure_openai_endpoints,
        "azureOpenAiDeploymentName": settings.azure_openai_deployment_name,
        "azureOpenAiApiVersion": settings.azure_openai_api_version,
        "fileDownloadBaseUrl": settings.FILE_DOWNLOAD_BASE_URL,
        "azureStorageContainerName": settings.AZURE_STORAGE_CONTAINER_NAME,
        "azureBlobSasUrl": settings.AZURE_STORAGE_SAS_URL,
        "customerAcrUsername": record.customer_acr_username or "",
        "customerAcrPassword": record.customer_acr_password or "",
    }

    from pathlib import Path
    arm_dir = Path(settings.arm_templates_dir)

    if (arm_dir / req.agent_slug / "containerapp.json").exists():
        containerapp_template = f"{req.agent_slug}/containerapp.json"
    elif (arm_dir / f"{req.agent_slug}-containerapp.json").exists():
        containerapp_template = f"{req.agent_slug}-containerapp.json"
    else:
        containerapp_template = "template1-containerapp.json"

    outputs = client.deploy_at_resource_group_scope(
        resource_group_name=req.resource_group_name,
        deployment_name=deployment_name,
        template_filename=containerapp_template,
        parameters=parameters,
    )

    record.container_app_fqdn = outputs["containerAppFQDN"]
    record.container_app_principal_id = outputs.get("containerAppPrincipalId")
    record.steps.append(StepResult(
        step="deploy_container_app",
        status="succeeded",
        outputs=outputs,
        detail=_timestamp(),
    ))
    logger.info("Container App deployed, FQDN: %s", record.container_app_fqdn)


def _step_deploy_bot_service(
    record: DeploymentRecord,
    client: AzureDeploymentClient,
    req,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.DEPLOYING_BOT_SERVICE
    deployment_name = f"deploy-botservice-{req.agent_slug}-{req.customer_slug}"

    arm_dir = Path(settings.arm_templates_dir)
    if (arm_dir / req.agent_slug / "botservice.json").exists():
        botservice_template = f"{req.agent_slug}/botservice.json"
    elif (arm_dir / f"{req.agent_slug}-botservice.json").exists():
        botservice_template = f"{req.agent_slug}-botservice.json"
    else:
        botservice_template = "template2-botservice.json"

    outputs = client.deploy_at_resource_group_scope(
        resource_group_name=req.resource_group_name,
        deployment_name=deployment_name,
        template_filename=botservice_template,
        parameters={
            "agentSlug": req.agent_slug,
            "customerSlug": req.customer_slug,
            "botDisplayName": req.bot_display_name,
            # Now genuinely SingleTenant: the agent App Registration was
            # created IN req.customer_tenant_id (see app_registration_service.py),
            # so msaAppId/msaAppTenantId are finally consistent with each other -
            # previously this used Skysecure's home-tenant app id here, which
            # was never actually valid for true SingleTenant bot auth.
            "msaAppId": record.agent_app_client_id,
            "msaAppTenantId": req.customer_tenant_id,
            "containerAppFQDN": record.container_app_fqdn,
            "sku": req.bot_sku,
        },
    )

    record.bot_service_resource_id = outputs["botServiceResourceId"]
    record.steps.append(StepResult(
        step="deploy_bot_service",
        status="succeeded",
        outputs=outputs,
        detail=_timestamp(),
    ))
    logger.info("Bot Service deployed: %s", record.bot_service_resource_id)


def _step_generate_manifest(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    record.status = DeploymentStatus.GENERATING_MANIFEST

    import os
    resources_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "resources")
    color_path = os.path.join(resources_dir, "color.png")
    outline_path = os.path.join(resources_dir, "outline.png")

    zip_bytes, teams_app_id = generate_and_zip_manifest(
        bot_id=record.agent_app_client_id,
        container_app_fqdn=record.container_app_fqdn,
        agent_slug=req.agent_slug,
        customer_slug=req.customer_slug,
        settings=settings,
        teams_app_id=req.teams_app_id,
        color_icon_path=color_path,
        outline_icon_path=outline_path,
    )

    output_dir = Path(settings.manifest_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    zip_path = output_dir / f"{req.agent_slug}-{req.customer_slug}-manifest.zip"
    zip_path.write_bytes(zip_bytes)

    record.teams_app_id = teams_app_id
    record.manifest_zip_path = str(zip_path)
    record.steps.append(StepResult(
        step="generate_manifest",
        status="succeeded",
        outputs={"teams_app_id": teams_app_id, "manifest_zip_path": str(zip_path)},
        detail=_timestamp(),
    ))
    logger.info(
        "Manifest generated agent=%s customer=%s teams_app_id=%s path=%s (not yet published)",
        req.agent_slug, req.customer_slug, teams_app_id, zip_path,
    )


def _step_publish_manifest_to_catalog(
    record: DeploymentRecord,
    req,
    settings: Settings,
) -> None:
    """
    Publishes the manifest zip generated in _step_generate_manifest to the
    customer tenant's Teams app catalog via Graph.

    POST /appCatalogs/teamsApps only accepts DELEGATED tokens (Application
    permissions always return 403). We therefore:
      1. Create a temporary PUBLIC-client App Registration in the customer
         tenant (isFallbackPublicClient=True → no client_secret needed in poll).
      2. Pre-grant admin consent for AppCatalog.ReadWrite.All delegated.
      3. Run device-code flow with that public client.
      4. Upload the manifest zip.
      5. Delete the temporary app (cleanup).
    """
    if not record.manifest_zip_path:
        raise CatalogPublishError("Cannot publish manifest - no manifest_zip_path on the deployment record.")

    import app.services.catalog_publish_service as cps
    from app.services.deployment_store import store as _store

    # --- Step 1: create temp public-client app in the customer tenant -------
    record.status = DeploymentStatus.AWAITING_USER_DEVICE_AUTH
    _store.save(record)
    app_object_id, public_client_id = cps.create_temp_manifest_app(
        deployment_spn_client_id=settings.deployment_spn_client_id,
        deployment_spn_secret=settings.deployment_spn_secret,
        customer_tenant_id=req.customer_tenant_id,
    )

    try:
        # --- Step 2: start device-code flow (public client, no secret) ------
        device_code_info = cps.start_device_code_flow_for_graph(
            client_id=public_client_id,
            tenant_id=req.customer_tenant_id,
        )
        record.device_code_info = {
            "user_code": device_code_info["user_code"],
            "verification_uri": device_code_info["verification_uri"],
            "purpose": "graph_auth",
        }
        _store.save(record)

        try:
            # Public client → no client_secret in poll → no AADSTS7000218
            token = cps.poll_for_graph_token(
                device_code_info=device_code_info,
                client_id=public_client_id,
                tenant_id=req.customer_tenant_id,
            )
        finally:
            record.device_code_info = None
            _store.save(record)

        # --- Step 3: upload manifest ----------------------------------------
        record.status = DeploymentStatus.PUBLISHING_MANIFEST
        _store.save(record)

        zip_bytes = Path(record.manifest_zip_path).read_bytes()
        app_data = cps.publish_manifest(zip_bytes=zip_bytes, token=token)

    finally:
        # --- Step 4: clean up temp app regardless of success/failure --------
        cps.delete_temp_manifest_app(
            deployment_spn_client_id=settings.deployment_spn_client_id,
            deployment_spn_secret=settings.deployment_spn_secret,
            customer_tenant_id=req.customer_tenant_id,
            app_object_id=app_object_id,
        )

    record.catalog_teams_app_id = app_data.get("id")
    record.steps.append(StepResult(
        step="publish_manifest_to_catalog",
        status="succeeded",
        outputs={"catalog_teams_app_id": record.catalog_teams_app_id, "displayName": app_data.get("displayName")},
        detail=_timestamp(),
    ))
    logger.info(
        "Manifest published to catalog customer=%s catalog_teams_app_id=%s",
        req.customer_slug, record.catalog_teams_app_id,
    )