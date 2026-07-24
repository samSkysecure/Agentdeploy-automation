"""
Post-deployment teardown of the DEPLOYMENT SPN's presence in the customer
tenant. Runs only after every prior step in the pipeline has succeeded.

Removes:
  - The Contributor role assignment the customer manually granted (via the
    "Assign Role" portal button) to the deployment SPN.
  - The deployment SPN's own Service Principal (Enterprise Application)
    entry in the customer tenant - this also revokes the Graph consent
    grants (Sites.Selected/FullControl.All, Application.ReadWrite.OwnedBy,
    etc.) since those live on the Service Principal object.
  - The Azure Lighthouse delegation assignment from the customer subscription,
    self-revoking Skysecure's deployment SPN access upon pipeline success.

Does NOT touch:
  - The per-agent App Registration / Service Principal created by
    app_registration_service.py. That identity is meant to persist
    permanently as the deployed agent's own runtime identity and must
    never be deleted here. Callers must pass only deployment-SPN-related
    IDs into this module - never the agent app's IDs.
"""
import logging

import httpx
from azure.identity import ClientSecretCredential
from azure.mgmt.authorization import AuthorizationManagementClient

logger = logging.getLogger("orchestrator.teardown_service")

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class TeardownError(Exception):
    """Raised when teardown fails. Deployment is still reported SUCCEEDED to
    the user (infra is live and working) but this must be surfaced loudly
    in logs/alerts, since a failed teardown leaves the deployment SPN's
    access lingering in the customer tenant."""


def teardown_deployment_spn(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,       # Skysecure's own home tenant (where the SPN's app object lives)
    deployment_spn_object_id: str,       # SPN's object ID *in the customer tenant* (from admin consent)
    customer_tenant_id: str,
    customer_subscription_id: str,
) -> None:
    _remove_role_assignment(
        deployment_spn_client_id=deployment_spn_client_id,
        deployment_spn_secret=deployment_spn_secret,
        deployment_spn_tenant_id=deployment_spn_tenant_id,
        deployment_spn_object_id=deployment_spn_object_id,
        customer_subscription_id=customer_subscription_id,
    )
    _delete_service_principal(
        deployment_spn_client_id=deployment_spn_client_id,
        deployment_spn_secret=deployment_spn_secret,
        customer_tenant_id=customer_tenant_id,
        deployment_spn_object_id=deployment_spn_object_id,
    )
    _remove_lighthouse_delegation(
        deployment_spn_client_id=deployment_spn_client_id,
        deployment_spn_secret=deployment_spn_secret,
        deployment_spn_tenant_id=deployment_spn_tenant_id,
        customer_subscription_id=customer_subscription_id,
    )


def _remove_role_assignment(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,
    deployment_spn_object_id: str,
    customer_subscription_id: str,
) -> None:
    credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
    )
    auth_client = AuthorizationManagementClient(credential, customer_subscription_id)
    scope = f"/subscriptions/{customer_subscription_id}"

    assignments = list(auth_client.role_assignments.list_for_scope(
        scope=scope,
        filter=f"principalId eq '{deployment_spn_object_id}'",
    ))
    if not assignments:
        logger.warning("No role assignment found for deployment SPN %s at teardown - already removed?", deployment_spn_object_id)
        return

    for assignment in assignments:
        logger.info("Removing role assignment %s for deployment SPN", assignment.name)
        try:
            auth_client.role_assignments.delete_by_id(assignment.id)
        except Exception as exc:
            raise TeardownError(f"Failed to remove role assignment {assignment.id}: {exc}") from exc


def _delete_service_principal(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    customer_tenant_id: str,
    deployment_spn_object_id: str,
) -> None:
    """
    Deletes the deployment SPN's OWN Service Principal object in the
    customer tenant - the "Enterprise Application" the customer sees.
    This uses a Graph token acquired in the customer tenant (the SPN still
    has an active session at this point, right before we delete it).
    """
    token_resp = httpx.post(
        f"https://login.microsoftonline.com/{customer_tenant_id}/oauth2/v2.0/token",
        data={
            "client_id": deployment_spn_client_id,
            "client_secret": deployment_spn_secret,
            "grant_type": "client_credentials",
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if token_resp.status_code != 200:
        raise TeardownError(f"Failed to acquire Graph token for SP deletion: {token_resp.status_code} {token_resp.text}")
    token = token_resp.json()["access_token"]

    del_resp = httpx.delete(
        f"{GRAPH_BASE}/servicePrincipals/{deployment_spn_object_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if del_resp.status_code not in (200, 204, 404):
        raise TeardownError(
            f"Failed to delete deployment SPN's Service Principal {deployment_spn_object_id}: "
            f"{del_resp.status_code} {del_resp.text}"
        )
    logger.info("Deployment SPN Service Principal %s deleted from customer tenant %s", deployment_spn_object_id, customer_tenant_id)


def _remove_lighthouse_delegation(
    *,
    deployment_spn_client_id: str,
    deployment_spn_secret: str,
    deployment_spn_tenant_id: str,
    customer_subscription_id: str,
) -> None:
    """
    Deletes all Azure Lighthouse registration assignments for the customer subscription.
    Uses an ARM token acquired for Skysecure's deployment SPN against Skysecure's home tenant.
    Requires Managed Services Registration Assignment Delete Role (91c1777a-f3dc-4fae-b103-61d183457e46),
    granted via lighthouse-delegation.json.
    Must run LAST in teardown, since deleting the delegation revokes ARM management access.
    """
    credential = ClientSecretCredential(
        tenant_id=deployment_spn_tenant_id,
        client_id=deployment_spn_client_id,
        client_secret=deployment_spn_secret,
    )
    try:
        token = credential.get_token("https://management.azure.com/.default").token
    except Exception as exc:
        raise TeardownError(f"Failed to acquire ARM token for Lighthouse delegation removal: {exc}") from exc

    headers = {"Authorization": f"Bearer {token}"}
    list_url = (
        f"https://management.azure.com/subscriptions/{customer_subscription_id}/"
        f"providers/Microsoft.ManagedServices/registrationAssignments?api-version=2022-10-01"
    )

    try:
        resp = httpx.get(list_url, headers=headers, timeout=30)
    except Exception as exc:
        raise TeardownError(f"Network error querying Lighthouse assignments on subscription {customer_subscription_id}: {exc}") from exc

    if resp.status_code == 404:
        logger.info("No Lighthouse delegation found on subscription %s", customer_subscription_id)
        return
    elif resp.status_code != 200:
        raise TeardownError(
            f"Failed to list Lighthouse assignments on subscription {customer_subscription_id}: "
            f"{resp.status_code} {resp.text}"
        )

    assignments = resp.json().get("value", [])
    if not assignments:
        logger.info("No active Lighthouse registration assignments found on subscription %s", customer_subscription_id)
        return

    for assignment in assignments:
        assignment_id = assignment["name"]
        logger.info("Deleting Lighthouse assignment %s on subscription %s", assignment_id, customer_subscription_id)
        delete_url = (
            f"https://management.azure.com/subscriptions/{customer_subscription_id}/"
            f"providers/Microsoft.ManagedServices/registrationAssignments/{assignment_id}?api-version=2022-10-01"
        )
        try:
            del_resp = httpx.delete(delete_url, headers=headers, timeout=30)
        except Exception as exc:
            raise TeardownError(f"Network error deleting Lighthouse assignment {assignment_id}: {exc}") from exc

        if del_resp.status_code not in (200, 202, 204, 404):
            raise TeardownError(
                f"Failed to delete Lighthouse assignment {assignment_id}: "
                f"{del_resp.status_code} {del_resp.text}"
            )

    logger.info("Successfully revoked Azure Lighthouse delegation from subscription %s", customer_subscription_id)

