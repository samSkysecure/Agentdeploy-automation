"""
Customer-owned Key Vault provisioning for the agent App Registration's
client secret.

Previously (and still, as a fallback path in deployment_service.py if this
step is skipped) the secret flowed straight from Graph into the ARM
deployment as a `securestring` parameter - functionally fine for the
Container App itself, but it means the secret also sits in:
  - Azure's ARM deployment history (securestring values ARE excluded from
    deployment history/logs by Azure itself, so this is less bad than it
    sounds - but still worth removing from the DeploymentRecord as a
    defense-in-depth measure).
  - `DeploymentRecord.agent_app_client_secret` in the orchestrator's own
    store/logs for longer than necessary.

This module creates a Key Vault in the customer's own resource group,
writes the secret there, grants the Container App's system-assigned
identity `get` access via RBAC (Key Vault Secrets User), and returns a
Key Vault secret URI. The ARM template then references the secret via
`keyVaultReference` instead of embedding the raw value, and
deployment_service.py clears `agent_app_client_secret` from the record
immediately after this step succeeds.
"""
import logging
from dataclasses import dataclass

from azure.identity import ClientSecretCredential  # type: ignore
from azure.mgmt.keyvault import KeyVaultManagementClient  # type: ignore
from azure.mgmt.keyvault.models import (  # type: ignore
    VaultCreateOrUpdateParameters,
    VaultProperties,
    Sku as KvSku,
    AccessPolicyEntry,
    Permissions,
    SecretPermissions,
)
from azure.keyvault.secrets import SecretClient  # type: ignore

logger = logging.getLogger("orchestrator.keyvault_service")


class KeyVaultError(Exception):
    """Raised when Key Vault creation or secret storage fails."""


@dataclass
class KeyVaultResult:
    vault_name: str
    vault_uri: str
    vault_resource_id: str
    secret_name: str
    secret_uri: str  # includes version - stable reference for the Container App


def _sanitize_vault_name(customer_slug: str, agent_slug: str) -> str:
    # Key Vault names: alphanumeric + hyphens, 3-24 chars, globally unique.
    raw = f"kv-{customer_slug}-{agent_slug}"
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch == "-").lower()
    return cleaned[:24].rstrip("-")


def store_agent_secret_in_customer_keyvault(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,
    customer_subscription_id: str,
    customer_resource_group: str,
    customer_tenant_id: str,
    location: str,
    customer_slug: str,
    agent_slug: str,
    agent_app_client_secret: str,
    deployment_spn_object_id: str = "",
) -> KeyVaultResult:
    credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
        additionally_allowed_tenants=["*"],
    )
    vault_name = _sanitize_vault_name(customer_slug, agent_slug)

    kv_mgmt_client = KeyVaultManagementClient(credential, customer_subscription_id)
    logger.info("Creating/ensuring Key Vault '%s' in %s/%s", vault_name, customer_subscription_id, customer_resource_group)

    sp_object_ids = []
    if deployment_spn_object_id:
        sp_object_ids.append(deployment_spn_object_id)

    access_policies = [
        AccessPolicyEntry(
            tenant_id=customer_tenant_id,
            object_id=oid,
            permissions=Permissions(
                secrets=[SecretPermissions.GET, SecretPermissions.LIST, SecretPermissions.SET, SecretPermissions.DELETE]
            ),
        )
        for oid in sp_object_ids
    ]

    try:
        poller = kv_mgmt_client.vaults.begin_create_or_update(
            resource_group_name=customer_resource_group,
            vault_name=vault_name,
            parameters=VaultCreateOrUpdateParameters(
                location=location,
                properties=VaultProperties(
                    tenant_id=customer_tenant_id,
                    sku=KvSku(name="standard", family="A"),
                    enable_rbac_authorization=False,  # Access Policy mode works seamlessly with Contributor role
                    enabled_for_deployment=False,
                    enabled_for_template_deployment=True,
                    access_policies=access_policies,
                ),
            ),
        )
        vault = poller.result()
    except Exception as exc:
        err_msg = str(exc)
        if any(term in err_msg for term in ["ConflictError", "deleted state", "VaultAlreadyExists", "already in use", "recoverable state"]):
            logger.warning("Key Vault '%s' exists in soft-deleted or conflict state. Purging...", vault_name)
            try:
                purge_poller = kv_mgmt_client.vaults.begin_purge_deleted(vault_name, location)
                purge_poller.result()
                logger.info("Purged soft-deleted Key Vault '%s'. Retrying creation...", vault_name)
                poller = kv_mgmt_client.vaults.begin_create_or_update(
                    resource_group_name=customer_resource_group,
                    vault_name=vault_name,
                    parameters=VaultCreateOrUpdateParameters(
                        location=location,
                        properties=VaultProperties(
                            tenant_id=customer_tenant_id,
                            sku=KvSku(name="standard", family="A"),
                            enable_rbac_authorization=False,
                            enabled_for_deployment=False,
                            enabled_for_template_deployment=True,
                            access_policies=access_policies,
                        ),
                    ),
                )
                vault = poller.result()
            except Exception as purge_exc:
                raise KeyVaultError(f"Key Vault '{vault_name}' in soft-deleted state could not be purged: {purge_exc}") from purge_exc
        else:
            raise KeyVaultError(f"Failed to create Key Vault '{vault_name}': {exc}") from exc

    vault_uri = vault.properties.vault_uri
    vault_resource_id = vault.id

    secret_name = f"{agent_slug}-app-secret"
    set_secret_result = _write_secret_with_retry(
        vault_uri=vault_uri,
        vault_name=vault_name,
        secret_name=secret_name,
        secret_value=agent_app_client_secret,
        credential=credential,
    )

    logger.info("Secret '%s' stored in Key Vault '%s'", secret_name, vault_name)

    return KeyVaultResult(
        vault_name=vault_name,
        vault_uri=vault_uri,
        vault_resource_id=vault_resource_id,
        secret_name=secret_name,
        secret_uri=set_secret_result.id,  # versioned URI
    )


def _write_secret_with_retry(
    *,
    vault_uri: str,
    vault_name: str,
    secret_name: str,
    secret_value: str,
    credential,
    max_attempts: int = 8,
):
    import time

    secret_client = SecretClient(vault_url=vault_uri, credential=credential)
    delays = [3, 5, 8, 10, 15, 20, 25, 30]

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "Writing secret '%s' into Key Vault '%s' (attempt %d/%d)...",
                secret_name,
                vault_name,
                attempt,
                max_attempts,
            )
            return secret_client.set_secret(secret_name, secret_value)
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            delay = delays[min(attempt - 1, len(delays) - 1)]

            if "getaddrinfo failed" in exc_str or "ServiceRequestError" in exc_str or "ConnectTimeout" in exc_str:
                logger.warning(
                    "DNS resolution pending for '%s' (attempt %d/%d). Retrying in %ds...",
                    vault_uri,
                    attempt,
                    max_attempts,
                    delay,
                )
            elif "Forbidden" in exc_str or "ForbiddenByRbac" in exc_str:
                logger.warning(
                    "Access/RBAC propagation pending for '%s' (attempt %d/%d). Retrying in %ds...",
                    vault_name,
                    attempt,
                    max_attempts,
                    delay,
                )
            else:
                logger.warning(
                    "Error writing secret to Key Vault '%s' (attempt %d/%d): %s. Retrying in %ds...",
                    vault_name,
                    attempt,
                    max_attempts,
                    exc,
                    delay,
                )

            time.sleep(delay)

    raise KeyVaultError(
        f"Failed to write secret into Key Vault '{vault_name}' after {max_attempts} attempts (DNS/RBAC propagation issue): {last_exc}"
    ) from last_exc


def _grant_role_assignment_safe(
    *,
    credential,
    customer_subscription_id: str,
    scope: str,
    principal_id: str,
    role_definition_id: str,
) -> None:
    from azure.mgmt.authorization import AuthorizationManagementClient
    import uuid

    try:
        auth_client = AuthorizationManagementClient(credential, customer_subscription_id)
        role_def = (
            f"/subscriptions/{customer_subscription_id}/providers/Microsoft.Authorization/"
            f"roleDefinitions/{role_definition_id}"
        )
        auth_client.role_assignments.create(
            scope=scope,
            role_assignment_name=str(uuid.uuid4()),
            parameters={
                "role_definition_id": role_def,
                "principal_id": principal_id,
                "principal_type": "ServicePrincipal",
            },
        )
        logger.info("Granted role '%s' to principal '%s' on %s", role_definition_id, principal_id, scope)
    except Exception as exc:
        if "RoleAssignmentExists" in str(exc):
            logger.info("Role assignment '%s' already exists for %s", role_definition_id, principal_id)
        else:
            logger.warning("Role assignment '%s' for %s on %s failed: %s", role_definition_id, principal_id, scope, exc)


def grant_secrets_user_role(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,
    customer_tenant_id: str = "",
    customer_subscription_id: str,
    vault_resource_id: str,
    principal_id: str,
) -> None:
    """
    Grants a principal (the Container App's system-assigned identity)
    'get' access on the Key Vault.
    """
    from azure.mgmt.keyvault.models import VaultAccessPolicyParameters, VaultAccessPolicyProperties  # type: ignore

    credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
        additionally_allowed_tenants=["*"],
    )
    
    parts = vault_resource_id.split("/")
    if len(parts) >= 9:
        rg_name = parts[4]
        vault_name = parts[8]
    else:
        logger.warning("Could not parse vault_resource_id '%s'", vault_resource_id)
        return

    kv_mgmt_client = KeyVaultManagementClient(credential, customer_subscription_id)

    try:
        vault = kv_mgmt_client.vaults.get(rg_name, vault_name)
        tenant_id = vault.properties.tenant_id

        kv_mgmt_client.vaults.update_access_policy(
            resource_group_name=rg_name,
            vault_name=vault_name,
            operation_kind="add",
            parameters=VaultAccessPolicyParameters(
                properties=VaultAccessPolicyProperties(
                    access_policies=[
                        AccessPolicyEntry(
                            tenant_id=tenant_id,
                            object_id=principal_id,
                            permissions=Permissions(secrets=[SecretPermissions.GET]),
                        )
                    ]
                )
            ),
        )
        logger.info("Granted Key Vault secret GET access on %s to principal %s", vault_resource_id, principal_id)
    except Exception as exc:
        logger.warning("Failed to grant Key Vault access policy on %s to %s: %s", vault_resource_id, principal_id, exc)
