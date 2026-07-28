"""
Thin wrapper around azure-mgmt-resource for running ARM template deployments.

Why this file exists separately from the orchestration logic:
ARM has two distinct deployment scopes that use different SDK clients/calls:
  - Resource-group-scope (Templates 1, 2 - everything that lives inside it)

Authentication note: this uses ClientSecretCredential against the
DEPLOYMENT SPN's home tenant. This works because the customer's admin
manually granted the deployment SPN a Contributor role assignment on
their subscription via the portal's "Assign Role" button - NOT via
Azure Lighthouse (deliberately not used here, since the role assignment
gets deleted again at teardown; see teardown_service.py).
"""
import json
import logging
from pathlib import Path
from typing import Any

from azure.identity import ClientSecretCredential
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.resource.resources.models import (
    Deployment,
    DeploymentMode,
    DeploymentProperties,
)

logger = logging.getLogger("orchestrator.azure_client")


class ArmDeploymentError(Exception):
    """Raised when an ARM deployment fails, carrying the Azure error detail."""

    def __init__(self, message: str, correlation_id: str | None = None):
        super().__init__(message)
        self.correlation_id = correlation_id


def _load_template(templates_dir: str, filename: str) -> dict[str, Any]:
    path = Path(templates_dir) / filename
    if not path.exists():
        raise FileNotFoundError(f"ARM template not found: {path}")
    return json.loads(path.read_text())


class AzureDeploymentClient:
    """
    One instance per deployment job. Holds the credential scoped to the
    customer's tenant and exposes the ARM deployment scope we need.
    """

    def __init__(
        self,
        *,
        deployment_spn_client_id: str,
        deployment_spn_secret: str,
        deployment_spn_tenant_id: str,
        customer_tenant_id: str = "",
        customer_subscription_id: str,
        templates_dir: str,
    ):
        self.customer_subscription_id = customer_subscription_id
        self.templates_dir = templates_dir

        # For Azure Lighthouse cross-tenant operations, we MUST authenticate against
        # Skysecure's home tenant (deployment_spn_tenant_id) where the delegated SPN identity
        # lives.
        self.credential = ClientSecretCredential(
            tenant_id=deployment_spn_tenant_id,
            client_id=deployment_spn_client_id,
            client_secret=deployment_spn_secret,
            additionally_allowed_tenants=["*"],
        )

        self.resource_client = ResourceManagementClient(
            self.credential, customer_subscription_id
        )

    def create_resource_group(self, name: str, location: str) -> None:
        """
        Synchronously creates the resource group if it doesn't exist.
        """
        try:
            logger.info("Ensuring resource group %s exists in %s", name, location)
            self.resource_client.resource_groups.create_or_update(
                name,
                {"location": location}
            )
            logger.info("Resource group %s is ready", name)
        except Exception as exc:
            logger.error("Failed to create resource group %s: %s", name, exc)
            raise ArmDeploymentError(f"Failed to create resource group: {exc}")

    def register_resource_providers(self) -> None:
        """
        Ensures all required Azure Resource Providers are registered on the customer subscription.
        Fresh or newly created subscriptions often lack provider registrations like
        Microsoft.ContainerRegistry, Microsoft.KeyVault, Microsoft.App, etc.
        """
        import time

        required_providers = [
            "Microsoft.ContainerRegistry",
            "Microsoft.KeyVault",
            "Microsoft.App",
            "Microsoft.OperationalInsights",
            "Microsoft.BotService",
            "Microsoft.ManagedIdentity",
        ]
        for provider in required_providers:
            try:
                p_info = self.resource_client.providers.get(provider)
                if p_info.registration_state != "Registered":
                    logger.info("Registering resource provider '%s' on subscription %s...", provider, self.customer_subscription_id)
                    self.resource_client.providers.register(provider)
                    for _ in range(30):
                        time.sleep(2)
                        check = self.resource_client.providers.get(provider)
                        if check.registration_state == "Registered":
                            logger.info("Resource provider '%s' registered successfully on subscription %s.", provider, self.customer_subscription_id)
                            break
                else:
                    logger.debug("Resource provider '%s' is already registered", provider)
            except Exception as exc:
                logger.warning("Could not verify/register resource provider '%s': %s", provider, exc)

    def verify_role_assignment(self, spn_object_id: str | None = None) -> bool:
        """
        Verifies that the deployment SPN has access to the customer subscription.
        Retries with delay to accommodate Entra ID/Azure ARM role propagation latency across
        different resource providers (Microsoft.Resources, Microsoft.ContainerRegistry, Microsoft.KeyVault).
        """
        import time
        from azure.mgmt.containerregistry import ContainerRegistryManagementClient
        from azure.mgmt.keyvault import KeyVaultManagementClient

        max_attempts = 36  # 36 * 10 seconds = 6 minutes max retry window
        for attempt in range(max_attempts):
            try:
                # 1. Verify Microsoft.Resources (Resource Groups)
                list(self.resource_client.resource_groups.list())

                # 2. Verify Microsoft.ContainerRegistry
                try:
                    acr_client = ContainerRegistryManagementClient(self.credential, self.customer_subscription_id)
                    list(acr_client.registries.list())
                except Exception as e:
                    err_str = str(e)
                    # If the error is a tenant mismatch or authorization failure, it means the role has not propagated.
                    # Otherwise, other errors (like provider not registered) mean authorization itself succeeded.
                    if "InvalidAuthenticationTokenTenant" in err_str or "AuthorizationFailed" in err_str:
                        raise e
                    logger.debug("Container Registry auth check succeeded (but hit: %s)", err_str)

                # 3. Verify Microsoft.KeyVault
                try:
                    kv_client = KeyVaultManagementClient(self.credential, self.customer_subscription_id)
                    list(kv_client.vaults.list_by_subscription())
                except Exception as e:
                    err_str = str(e)
                    if "InvalidAuthenticationTokenTenant" in err_str or "AuthorizationFailed" in err_str:
                        raise e
                    logger.debug("Key Vault auth check succeeded (but hit: %s)", err_str)

                logger.info("Access verified successfully across Resource Groups, ACR, and Key Vault on subscription %s", self.customer_subscription_id)
                return True
            except Exception as exc:
                err_msg = str(exc)
                if attempt < max_attempts - 1:
                    logger.info(
                        "Azure role/delegation assignment not yet propagated across all providers on subscription %s (attempt %d/%d). "
                        "Retrying in 10s... Error: %s",
                        self.customer_subscription_id, attempt + 1, max_attempts, err_msg
                    )
                    time.sleep(10)
                else:
                    logger.error("Failed to verify access on subscription %s after %d attempts: %s", self.customer_subscription_id, max_attempts, exc)
                    raise ArmDeploymentError(f"Azure authentication or authorization failed: {exc}") from exc
        return False

    def deploy_at_resource_group_scope(
        self,
        *,
        resource_group_name: str,
        deployment_name: str,
        template_filename: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """For Templates 1 and 2 - everything that lives inside the resource group."""
        template = _load_template(self.templates_dir, template_filename)
        formatted_params = {k: {"value": v} for k, v in parameters.items()}

        poller = self.resource_client.deployments.begin_create_or_update(
            resource_group_name=resource_group_name,
            deployment_name=deployment_name,
            parameters=Deployment(
                properties=DeploymentProperties(
                    mode=DeploymentMode.INCREMENTAL,
                    template=template,
                    parameters=formatted_params,
                )
            ),
        )
        return self._wait_and_extract_outputs(poller, deployment_name)

    @staticmethod
    def _wait_and_extract_outputs(poller, deployment_name: str) -> dict[str, Any]:
        """
        Blocks until the deployment finishes (this runs inside a background
        task, so blocking here is fine - it does not block the API response).
        Raises ArmDeploymentError with Azure's own error detail on failure,
        which is what actually tells you WHY a deployment failed.
        """
        try:
            result = poller.result()
        except Exception as exc:
            logger.error("ARM deployment '%s' failed: %s", deployment_name, exc)
            raise ArmDeploymentError(str(exc)) from exc

        if result.properties.provisioning_state != "Succeeded":
            raise ArmDeploymentError(
                f"Deployment '{deployment_name}' ended in state "
                f"'{result.properties.provisioning_state}'"
            )

        outputs = result.properties.outputs or {}
        # ARM returns outputs as {"key": {"type": "...", "value": ...}} - flatten it
        return {k: v["value"] for k, v in outputs.items()}

    def patch_container_app_env_vars(
        self,
        resource_group_name: str,
        container_app_name: str,
        env_updates: dict[str, str],
    ) -> None:
        """
        Ported from onboard_customer.ps1's step 6 (webhook URL patch).
        Merges env_updates into the Container App's existing env var list
        (updating in place if a name already exists, appending otherwise)
        and PATCHes the resource - used post-Copilot-Studio-import to
        inject COPILOT_FLOW_URL / SHAREPOINT_SITE_URL, which aren't known
        until the solution has actually been imported and published.
        """
        import httpx as _httpx

        token = self.credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com/subscriptions/{self.customer_subscription_id}"
            f"/resourceGroups/{resource_group_name}/providers/Microsoft.App/containerApps/"
            f"{container_app_name}?api-version=2023-05-01"
        )
        headers = {"Authorization": f"Bearer {token}"}

        get_resp = _httpx.get(url, headers=headers, timeout=30)
        if get_resp.status_code != 200:
            raise ArmDeploymentError(f"Failed to read Container App state for env var patch: {get_resp.status_code} {get_resp.text}")
        state = get_resp.json()

        env_vars = state["properties"]["template"]["containers"][0].get("env", [])
        for name, value in env_updates.items():
            existing = next((v for v in env_vars if v.get("name") == name), None)
            if existing:
                existing["value"] = value
            else:
                env_vars.append({"name": name, "value": value})
        state["properties"]["template"]["containers"][0]["env"] = env_vars

        patch_resp = _httpx.patch(
            url,
            headers={**headers, "Content-Type": "application/json"},
            json={"properties": {"template": state["properties"]["template"]}},
            timeout=60,
        )
        if patch_resp.status_code not in (200, 201, 202):
            raise ArmDeploymentError(f"Failed to patch Container App env vars: {patch_resp.status_code} {patch_resp.text}")
        logger.info("Patched Container App '%s' env vars: %s", container_app_name, list(env_updates.keys()))
