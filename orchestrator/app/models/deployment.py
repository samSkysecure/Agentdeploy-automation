"""
Request/response models for the deployment orchestrator.
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class DeploymentStatus(str, Enum):
    QUEUED = "queued"
    CREATING_APP_REGISTRATION = "creating_app_registration"
    IMPORTING_IMAGE = "importing_image"
    DEPLOYING_CONTAINER_APP = "deploying_container_app"
    DEPLOYING_BOT_SERVICE = "deploying_bot_service"
    DEPLOYING_BOT_SERVICE_ONLY = "deploying_bot_service_only"
    GENERATING_MANIFEST = "generating_manifest"
    PROVISIONING_SHAREPOINT = "provisioning_sharepoint"
    AWAITING_USER_DEVICE_AUTH = "awaiting_user_device_auth"
    AWAITING_ENVIRONMENT_SELECTION = "awaiting_environment_selection"
    IMPORTING_COPILOT_SOLUTION = "importing_copilot_solution"
    PUBLISHING_MANIFEST = "publishing_manifest"
    TEARING_DOWN_DEPLOYMENT_SPN = "tearing_down_deployment_spn"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeploymentRequest(BaseModel):
    deployment_type: Optional[str] = Field(
        "sop5",
        description="Deprecated/General deployment type",
    )
    agent_slug: str = Field(..., description="Short identifier for the agent e.g. 'teamsagent' or 'copilotagent'")
    customer_slug: str = Field(..., description="Short identifier for the customer e.g. 'sstlab'")
    customer_tenant_id: str = Field(..., description="Customer's Entra tenant ID")
    customer_subscription_id: str = Field(..., description="Customer's Azure subscription ID")
    resource_group_name: str = Field(..., description="Customer's pre-existing resource group e.g. 'soc-agents'")
    agent_image_tag: str = Field(..., description="Image tag to deploy e.g. 'v1'")
    bot_display_name: str = Field(..., description="Human-readable bot name shown in Teams")
    location: Optional[str] = Field(None, description="Azure region override; defaults to Settings.default_location")
    bot_sku: str = Field("F0", description="F0 for testing, S1 for production")
    sharepoint_site_url: Optional[str] = Field(
    None,
    description="Customer's SharePoint site URL e.g. https://contoso.sharepoint.com/sites/hr-docgen"
    )

    # Copilot Studio agent schema name
    # Found in agent URL: /bots/cre6d_Toolrequest/ → schema name is cre6d_Toolrequest
    copilot_schema_name: Optional[str] = Field(
        None,
        description="Copilot Studio agent schema name.",
    )

    # Persist across redeployments so Teams sees it as an update not a new app
    teams_app_id: Optional[str] = Field(
        None,
        description="Existing Teams app UUID from a prior deployment. "
                    "Omit on first deployment - generated automatically. "
                    "Pass it back on every subsequent redeploy to update the same app.",
    )

    # Deployment SPN's Service Principal object ID *in the customer tenant*,
    # created by admin consent. Required at teardown to know exactly which
    # SP to delete. Populated by the "Assign Role" verification step before
    # onboarding starts (the SP must exist for the role assignment to have
    # been possible in the first place).
    deployment_spn_object_id_in_customer_tenant: str = Field(
        ..., description="Deployment SPN's Service Principal object ID in the customer's tenant."
    )

    # --- Copilot Studio / Power Platform import (ported from onboard_customer.ps1 steps 5-7) ---
    power_platform_tenant_id: Optional[str] = Field(
        "547b64a7-e66e-48df-a146-3e898cbcb60f", description="Tenant ID where Copilot Studio / Power Platform environment lives. Defaults to Skysecure tenant 547b64a7-e66e-48df-a146-3e898cbcb60f."
    )
    environment_id: Optional[str] = Field(
        None,
        description=(
            "Power Platform environment instance URL or GUID. "
            "When omitted (the default), the orchestrator auto-discovers available "
            "environments after the device-code login step and either selects the "
            "Production environment automatically or pauses to let the user pick "
            "from a dropdown in the wizard UI."
        )
    )
    solution_zip_path: str = Field(
        "../docgen_1_0_0_2.zip",
        description="Path to the agent's Copilot Studio solution zip, on the orchestrator's filesystem."
    )
    connector_solution_zip_path: str = Field(
        "../docgenConnector_1_0_0_2.zip",
        description="Path to the custom connector solution zip - must already be imported before this runs, same as today's manual Step 4 in onboard_customer.ps1."
    )
    knowledge_base_site_urls: list[str] = Field(default_factory=list, description="SharePoint KB site URLs to inject into the agent solution's botcomponents.")


class StepResult(BaseModel):
    step: str
    status: str
    detail: Optional[str] = None
    outputs: Optional[dict] = None


class DeploymentRecord(BaseModel):
    deployment_id: str
    status: DeploymentStatus
    request: DeploymentRequest
    steps: list[StepResult] = []
    resource_group_name: Optional[str] = None
    container_app_fqdn: Optional[str] = None
    bot_service_resource_id: Optional[str] = None
    teams_app_id: Optional[str] = None
    manifest_zip_path: Optional[str] = None
    catalog_teams_app_id: Optional[str] = None
    error: Optional[str] = None
    sharepoint_site_url: Optional[str] = None
    sharepoint_site_id: Optional[str] = None
    sharepoint_templates_drive_id: Optional[str] = None
    sharepoint_generated_drive_id: Optional[str] = None
    sharepoint_deployed_lists_id: Optional[str] = None

    # --- Per-agent App Registration (PERMANENT - never touched by teardown) ---
    agent_app_client_id: Optional[str] = None
    agent_app_client_secret: Optional[str] = None  # cleared from the record after being written to Key Vault
    agent_app_object_id: Optional[str] = None
    agent_app_service_principal_id: Optional[str] = None

    # --- Customer-owned ACR (PERMANENT) ---
    customer_acr_login_server: Optional[str] = None
    customer_acr_resource_id: Optional[str] = None
    customer_agent_image_reference: Optional[str] = None

    # --- Customer-owned Key Vault (PERMANENT) ---
    keyvault_name: Optional[str] = None
    keyvault_uri: Optional[str] = None
    keyvault_resource_id: Optional[str] = None
    keyvault_secret_uri: Optional[str] = None

    # --- Container App identity (needed to grant Key Vault access) ---
    container_app_principal_id: Optional[str] = None

    # --- Copilot Studio import phase (human-in-the-loop) ---
    device_code_info: Optional[dict] = None  # {"user_code": ..., "verification_uri": ..., "purpose": "pac_auth"|"user_token"}
    copilot_flow_webhook_url: Optional[str] = None
    power_platform_environment_guid: Optional[str] = None
    # Populated when multiple usable PP environments are found and auto-selection
    # is not possible; cleared once the user makes a selection via the wizard.
    available_pp_environments: Optional[list] = None  # [{"displayName", "instanceUrl", "environmentSku"}]

    # --- Teardown tracking (deployment SPN only - separate from the above) ---
    deployment_spn_teardown_completed: bool = False