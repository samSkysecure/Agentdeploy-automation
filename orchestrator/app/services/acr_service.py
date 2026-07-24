"""
Customer-owned ACR provisioning + server-side image import.

Replaces the old model where the Container App pulled directly from
Skysecure's shared ACR using long-lived admin credentials baked into the
ARM template (acrServer/acrUsername/acrPassword). That created a
permanent runtime pull dependency on Skysecure's infrastructure.

New flow, per deployment:
  1. Create (or reuse) an ACR in the customer's own resource group.
  2. Create a short-lived, repository-scoped token on Skysecure's SOURCE ACR.
  3. Server-to-server import (az acr import equivalent) into the customer ACR.
  4. Delete the temp token immediately - regardless of success or failure.

After this, the Container App pulls from the customer's own ACR using its
managed identity (AcrPull role), not credentials. No standing Skysecure
ACR dependency remains post-deploy.
"""
import hashlib
import logging
import secrets
import string
from dataclasses import dataclass

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import ClientSecretCredential
from azure.mgmt.containerregistry import ContainerRegistryManagementClient  # type: ignore
import azure.mgmt.containerregistry.models as acr_models  # type: ignore

logger = logging.getLogger("orchestrator.acr_service")


class AcrImportError(Exception):
    """Raised when customer ACR creation or image import fails."""


@dataclass
class AcrImportResult:
    customer_acr_login_server: str
    customer_acr_resource_id: str
    image_reference: str  # e.g. customeracr.azurecr.io/docgen:v1


def _sanitize_acr_name(customer_slug: str, agent_slug: str, customer_subscription_id: str) -> str:
    # ACR names: alphanumeric only, 5-50 chars, globally unique within Azure.
    sub_hash = hashlib.sha256(customer_subscription_id.encode("utf-8")).hexdigest()[:6]
    raw = f"acr{customer_slug}{agent_slug}{sub_hash}"
    cleaned = "".join(ch for ch in raw if ch.isalnum()).lower()
    if len(cleaned) < 5:
        cleaned = (cleaned + "acrspec")[:50]
    return cleaned[:50]


def _sanitize_token_name(customer_slug: str, agent_slug: str) -> str:
    # Token/Scope map names: alphanumeric and hyphens only, <= 50 chars.
    suffix = _random_suffix()
    raw = f"imp-{customer_slug}-{agent_slug}-{suffix}"
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch == "-").lower()
    return cleaned[:50].strip("-")


def import_image_to_customer_acr(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,
    customer_tenant_id: str = "",
    customer_subscription_id: str,
    customer_resource_group: str,
    location: str,
    customer_slug: str,
    agent_slug: str,
    source_acr_name: str,       # e.g. "skysecureacr" or "docgenagent"
    source_repository: str,     # e.g. "docgen"
    source_tag: str,            # e.g. "v1"
    source_acr_resource_group: str = "",
    source_acr_subscription_id: str = "",
    source_acr_username: str = "",
    source_acr_password: str = "",
) -> AcrImportResult:
    """
    Uses the deployment SPN's credential to provision a customer ACR and
    perform a server-to-server image import.
    """
    source_acr_subscription_id = source_acr_subscription_id or customer_subscription_id
    source_acr_resource_group = source_acr_resource_group or customer_resource_group

    # For Azure Lighthouse cross-tenant ARM operations (creating/importing customer ACR),
    # we MUST authenticate against Skysecure's home tenant (deployment_spn_tenant_id) where
    # the Lighthouse cross-tenant delegation was granted.
    customer_credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
        additionally_allowed_tenants=["*"],
    )

    source_credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
        additionally_allowed_tenants=["*"],
    )

    customer_acr_name = _sanitize_acr_name(customer_slug, agent_slug, customer_subscription_id)
    token_name = _sanitize_token_name(customer_slug, agent_slug)
    scope_map_name = f"map-{token_name}"[:50].strip("-")

    try:
        # --- 1. Reuse existing customer ACR or create if absent ---
        customer_acr_client = ContainerRegistryManagementClient(customer_credential, customer_subscription_id)
        try:
            customer_registry = customer_acr_client.registries.get(customer_resource_group, customer_acr_name)
            logger.info("Customer ACR '%s' already exists in %s", customer_acr_name, customer_resource_group)
        except ResourceNotFoundError:
            logger.info("Creating customer ACR '%s' in %s/%s", customer_acr_name, customer_subscription_id, customer_resource_group)
            poller = customer_acr_client.registries.begin_create(
                resource_group_name=customer_resource_group,
                registry_name=customer_acr_name,
                registry=acr_models.Registry(
                    location=location,
                    sku=acr_models.Sku(name="Basic"),
                    admin_user_enabled=False,
                ),
            )
            customer_registry = poller.result()

        customer_login_server = customer_registry.login_server
        customer_registry_id = customer_registry.id

        # --- 2. Perform Image Import ---
        source_registry_uri = f"{source_acr_name}.azurecr.io" if "." not in source_acr_name else source_acr_name
        source_image_name = f"{source_repository}:{source_tag}"

        if source_acr_username and source_acr_password:
            logger.info("Importing %s from %s into customer ACR %s using source credentials", source_image_name, source_registry_uri, customer_login_server)
            import_poller = customer_acr_client.registries.begin_import_image(
                resource_group_name=customer_resource_group,
                registry_name=customer_acr_name,
                parameters=acr_models.ImportImageParameters(
                    source=acr_models.ImportSource(
                        registry_uri=source_registry_uri,
                        source_image=source_image_name,
                        credentials=acr_models.ImportSourceCredentials(
                            username=source_acr_username,
                            password=source_acr_password,
                        ),
                    ),
                    target_tags=[f"{agent_slug}:{source_tag}"],
                    mode=acr_models.ImportMode.FORCE,
                ),
            )
            import_poller.result()
            logger.info("Image import into %s succeeded", customer_login_server)
        else:
            logger.info("Creating short-lived import token '%s' on source ACR '%s'", token_name, source_acr_name)
            source_acr_client = ContainerRegistryManagementClient(source_credential, source_acr_subscription_id)

            _ensure_scope_map(
                source_acr_client, source_acr_resource_group, source_acr_name, source_repository, scope_map_name,
            )

            scope_map_id = (
                f"/subscriptions/{source_acr_subscription_id}/resourceGroups/{source_acr_resource_group}"
                f"/providers/Microsoft.ContainerRegistry/registries/{source_acr_name}/scopeMaps/{scope_map_name}"
            )

            token_poller = source_acr_client.tokens.begin_create(
                resource_group_name=source_acr_resource_group,
                registry_name=source_acr_name,
                token_name=token_name,
                token_parameters=acr_models.Token(
                    scope_map_id=scope_map_id,
                    status="enabled",
                ),
            )
            token_poller.result()

            token_resource_id = (
                f"/subscriptions/{source_acr_subscription_id}/resourceGroups/{source_acr_resource_group}"
                f"/providers/Microsoft.ContainerRegistry/registries/{source_acr_name}/tokens/{token_name}"
            )

            creds = source_acr_client.registries.generate_credentials(
                resource_group_name=source_acr_resource_group,
                registry_name=source_acr_name,
                generate_credentials_parameters=acr_models.GenerateCredentialsParameters(
                    token_id=token_resource_id,
                ),
            )

            if not creds.passwords or not creds.passwords[0].value:
                raise AcrImportError(f"No password returned for temporary ACR import token '{token_name}'")
            token_password = creds.passwords[0].value

            try:
                logger.info("Importing %s into customer ACR %s using temporary token", source_image_name, customer_login_server)
                import_poller = customer_acr_client.registries.begin_import_image(
                    resource_group_name=customer_resource_group,
                    registry_name=customer_acr_name,
                    parameters=acr_models.ImportImageParameters(
                        source=acr_models.ImportSource(
                            resource_id=f"/subscriptions/{source_acr_subscription_id}/resourceGroups/{source_acr_resource_group}"
                                         f"/providers/Microsoft.ContainerRegistry/registries/{source_acr_name}",
                            source_image=source_image_name,
                            credentials=acr_models.ImportSourceCredentials(
                                username=token_name,
                                password=token_password,
                            ),
                        ),
                        target_tags=[f"{agent_slug}:{source_tag}"],
                        mode=acr_models.ImportMode.FORCE,
                    ),
                )
                import_poller.result()
                logger.info("Image import into %s succeeded", customer_login_server)
            finally:
                logger.info("Deleting temporary import token '%s' from source ACR", token_name)
                try:
                    source_acr_client.tokens.begin_delete(
                        resource_group_name=source_acr_resource_group,
                        registry_name=source_acr_name,
                        token_name=token_name,
                    ).result()
                    source_acr_client.scope_maps.begin_delete(
                        resource_group_name=source_acr_resource_group,
                        registry_name=source_acr_name,
                        scope_map_name=scope_map_name,
                    ).result()
                except Exception as exc:
                    logger.warning("Failed to clean up temporary token/scope map: %s", exc)

        _wait_for_acr_dns(customer_login_server)

        return AcrImportResult(
            customer_acr_login_server=customer_login_server,
            customer_acr_resource_id=customer_registry_id,
            image_reference=f"{customer_login_server}/{agent_slug}:{source_tag}",
        )

    except HttpResponseError as exc:
        logger.error("Azure ACR operation failed: %s", exc)
        raise AcrImportError(f"Customer ACR creation/import failed: {exc.message or str(exc)}") from exc
    except Exception as exc:
        if isinstance(exc, AcrImportError):
            raise
        logger.error("Unexpected error during ACR import: %s", exc)
        raise AcrImportError(f"ACR import failed: {str(exc)}") from exc


def _ensure_scope_map(client, resource_group, registry_name, repository, scope_map_name) -> None:
    client.scope_maps.begin_create(
        resource_group_name=resource_group,
        registry_name=registry_name,
        scope_map_name=scope_map_name,
        scope_map_create_parameters=acr_models.ScopeMap(
            actions=[f"repositories/{repository}/content/read"],
        ),
    ).result()


def _random_suffix() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def _wait_for_acr_dns(login_server: str, max_retries: int = 12, delay: int = 5) -> None:
    """Waits for Azure public/internal DNS to propagate the new ACR hostname."""
    import socket
    import time
    logger.info("Waiting for DNS resolution of ACR %s...", login_server)
    for attempt in range(1, max_retries + 1):
        try:
            socket.gethostbyname(login_server)
            logger.info("ACR DNS resolution verified for %s", login_server)
            return
        except Exception:
            logger.info("ACR DNS propagation pending for %s (attempt %d/%d)...", login_server, attempt, max_retries)
            time.sleep(delay)
    logger.warning("DNS resolution check timed out for %s - proceeding with deployment.", login_server)
