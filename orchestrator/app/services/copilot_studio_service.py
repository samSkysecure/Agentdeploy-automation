"""
Copilot Studio / Power Platform solution import.

Ported from onboard_customer.ps1 steps 5-7. Kept hardcoded to the docgen
agent's connector/flow name patterns, per the "prove it on docgen first"
scope - same as everything else in this pipeline right now.

HARD PLATFORM CONSTRAINT (cannot be engineered around): Microsoft requires
a human user token - not a service principal - to create Power Platform
connections. This means this step cannot be fully unattended like the
Azure phases before it. The pipeline pauses here, surfaces a device code +
verification URL to the frontend (via DeploymentRecord.device_code_info),
and blocks polling for the human to complete login in a browser - exactly
the same blocking-poll pattern the original PowerShell script used, just
surfaced through the DeploymentRecord instead of a terminal.

Still shells out to the `pac` CLI for the two operations that don't have a
clean REST equivalent: `pac solution create-settings` and
`pac solution import` / `pac solution publish`. This is a real external
dependency (the `pac` CLI must be installed and on PATH wherever
deployment_service.py runs) - same as it was for onboard_customer.ps1,
just now required by the orchestrator process itself rather than a
separately-invoked script.
"""
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger("orchestrator.copilot_studio_service")

PAC_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"  # Microsoft's well-known public client ID for PAC/device-code flows
BAP_SCOPE = "https://management.core.windows.net/.default"


class CopilotStudioImportError(Exception):
    """Raised when any step of the Copilot Studio import sequence fails."""


@dataclass
class DeviceCodeInfo:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass
class CopilotStudioImportResult:
    flow_webhook_url: Optional[str]
    environment_guid: str
    custom_connection_id: str
    copilot_connection_id: str


import shutil

def _get_pac_cmd() -> list[str]:
    """Resolves the executable or command array for PAC CLI on Windows / Linux."""
    pac_path = shutil.which("pac")
    if pac_path:
        return [pac_path]
    return ["pac"]


def pac_auth_create_device_code(
    environment_id: str,
    tenant_id: str,
    work_dir: str,
    on_device_code: Callable[[DeviceCodeInfo], None],
) -> None:
    """
    Authenticates PAC CLI interactively via Device Code flow.
    Launches 'pac auth create --deviceCode', scrapes the stdout for the URL/code,
    reports it via the callback, and waits for the user to complete login.
    """
    import subprocess
    import re
    import time
    
    pac_exe = _get_pac_cmd()[0]
    args = [
        "auth", "create",
        "--deviceCode",
        "--tenant", tenant_id,
        "--environment", environment_id,
    ]
    quoted_args = " ".join(f'"{a}"' if " " in a else a for a in args)
    cmd_str = f'"{pac_exe}" {quoted_args}'
    
    logger.info("Running: pac %s (cwd=%s)", " ".join(args), work_dir)
    
    process = subprocess.Popen(
        cmd_str,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        shell=True,
        bufsize=1
    )
    
    code_found = False
    for line in iter(process.stdout.readline, ''):
        logger.debug("PAC OUT: %s", line.strip())
        if not code_found:
            match = re.search(r'open the page (https://.*?) and enter the code (.*?) to authenticate', line)
            if match:
                uri = match.group(1).strip()
                code = match.group(2).strip()
                info = DeviceCodeInfo(
                    device_code=code,
                    user_code=code,
                    verification_uri=uri,
                    interval=5,
                    expires_in=900
                )
                on_device_code(info)
                code_found = True
    
    process.wait()
    if process.returncode != 0:
        raise CopilotStudioImportError(f"pac auth create --deviceCode failed with exit code {process.returncode}")
    
    logger.info("pac auth create --deviceCode succeeded")


def inject_connector_host_and_repack(connector_zip_path: str, container_app_fqdn: str, work_dir: str) -> str:
    """Unpacks the connector solution, injects the Container App's FQDN as
    the swagger `host`, bumps the solution version, repacks. Falls back to
    the original zip path (unmodified) if injection fails for any reason -
    same fallback behavior as the original script."""
    import glob
    import shutil

    # Always import connector as Unmanaged — managed solution imports don't reliably
    # create fresh connector registrations in the Power Apps /apis endpoint when the
    # connector has never existed in the environment before. Unmanaged imports create
    # a proper custom API registration immediately.
    ptype = "Unmanaged"
    unpack_dir = Path(work_dir) / "unpacked_connector_temp"
    if unpack_dir.exists():
        shutil.rmtree(str(unpack_dir), ignore_errors=True)
    _run_pac(["solution", "unpack", "--zipfile", connector_zip_path, "--folder", str(unpack_dir), "--packagetype", ptype], cwd=work_dir)

    swagger_files = glob.glob(str(unpack_dir / "**" / "*_openapidefinition.json"), recursive=True)
    if swagger_files:
        swagger_path = Path(swagger_files[0])
        swagger = json.loads(swagger_path.read_text())
        swagger["host"] = container_app_fqdn
        swagger_path.write_text(json.dumps(swagger, indent=2))
        logger.info("Injected Container App FQDN into swagger: %s", swagger_path.name)
    else:
        logger.warning("Could not find swagger file to inject host - proceeding without injection.")

    _bump_solution_version(unpack_dir)

    injected_zip_path = str(Path(work_dir) / "documentConnector_injected.zip")
    _run_pac(["solution", "pack", "--zipfile", injected_zip_path, "--folder", str(unpack_dir), "--packagetype", ptype], cwd=work_dir)
    shutil.rmtree(str(unpack_dir), ignore_errors=True)

    if not Path(injected_zip_path).exists():
        logger.warning("Failed to create injected connector zip - falling back to original.")
        return connector_zip_path
    return injected_zip_path


def inject_kb_sources_and_repack(solution_zip_path: str, kb_site_urls: list[str], work_dir: str) -> str:
    """Unpacks the agent solution, replaces its KnowledgeSourceConfiguration
    botcomponents wholesale with the given SharePoint site URLs, bumps the
    solution version, repacks. Falls back to the original zip if anything
    about the injection can't be resolved (e.g. no botcomponents found)."""
    import hashlib
    import re
    import shutil

    agent_unpack_dir = Path(work_dir) / "unpacked_agent_temp"
    if agent_unpack_dir.exists():
        shutil.rmtree(str(agent_unpack_dir), ignore_errors=True)
    _run_pac(["solution", "unpack", "--zipfile", solution_zip_path, "--folder", str(agent_unpack_dir)], cwd=work_dir)

    botcomponents_dir = agent_unpack_dir / "botcomponents"
    if not botcomponents_dir.exists():
        logger.warning("No botcomponents folder found - skipping KB injection, using solution as-is.")
        return solution_zip_path

    sample = next((d for d in botcomponents_dir.iterdir() if d.is_dir()), None)
    if not sample:
        logger.warning("Could not resolve bot schema name from botcomponents - skipping KB injection.")
        return solution_zip_path
    bot_schema_name = sample.name.split(".")[0]

    # Strip any existing KB source folders (full replace, not additive)
    for folder in list(botcomponents_dir.iterdir()):
        data_file = folder / "data"
        if data_file.exists() and "kind: KnowledgeSourceConfiguration" in data_file.read_text():
            shutil.rmtree(str(folder), ignore_errors=True)

    for kb_url in kb_site_urls:
        kb_url = kb_url.strip()
        if not kb_url:
            continue
        stripped = re.sub(r"^https?://", "", kb_url)
        stripped = re.sub(r"[^a-zA-Z0-9]", "", stripped)[:50]
        short_hash = hashlib.md5(kb_url.encode("utf-8")).hexdigest()[:8]
        suffix = f"{stripped}{short_hash}"
        component_schema_name = f"{bot_schema_name}.topic.{suffix}"
        component_dir = botcomponents_dir / component_schema_name
        component_dir.mkdir(parents=True, exist_ok=True)

        (component_dir / "botcomponent.xml").write_text(
            f'<botcomponent schemaname="{component_schema_name}">\n'
            f"  <componenttype>16</componenttype>\n"
            f"  <description>This knowledge source provides information found in {kb_url}.</description>\n"
            f"  <iscustomizable>0</iscustomizable>\n"
            f"  <n>{kb_url}</n>\n"
            f"  <parentbotid>\n    <schemaname>{bot_schema_name}</schemaname>\n  </parentbotid>\n"
            f"  <statecode>0</statecode>\n  <statuscode>1</statuscode>\n</botcomponent>\n"
        )
        (component_dir / "data").write_text(
            f"kind: KnowledgeSourceConfiguration\nsource:\n  kind: SharePointSearchSource\n  site: {kb_url}\n"
        )
        logger.info("Registered KB source: %s (schema: %s)", kb_url, component_schema_name)

    _bump_solution_version(agent_unpack_dir)

    injected_agent_zip_path = str(Path(work_dir) / "docgen_injected.zip")
    _run_pac(["solution", "pack", "--zipfile", injected_agent_zip_path, "--folder", str(agent_unpack_dir)], cwd=work_dir)
    shutil.rmtree(str(agent_unpack_dir), ignore_errors=True)

    if not Path(injected_agent_zip_path).exists():
        logger.warning("Failed to create injected agent zip - falling back to original solution (no KB sources).")
        return solution_zip_path
    return injected_agent_zip_path


def import_connector_solution(injected_connector_zip_path: str, work_dir: str) -> None:
    _run_pac(["solution", "import", "--path", injected_connector_zip_path, "--force-overwrite", "--activate-plugins"], cwd=work_dir)


def _bump_solution_version(folder_or_file: Path) -> None:
    """Bumps solution.xml's version to force Power Platform to pick up
    component changes on re-import - same 1.0.0.<timestamp> scheme as the
    original script."""
    import xml.etree.ElementTree as ET
    from datetime import datetime
    import glob

    if folder_or_file.is_dir():
        files = glob.glob(str(folder_or_file / "**" / "[Ss]olution.xml"), recursive=True)
        if not files:
            logger.warning("Could not find Solution.xml in %s to bump version", folder_or_file)
            return
        solution_xml_path = Path(files[0])
    else:
        solution_xml_path = folder_or_file

    if not solution_xml_path.exists():
        return
    tree = ET.parse(solution_xml_path)
    version_el = tree.getroot().find(".//Version")
    if version_el is not None:
        version_el.text = f"1.0.0.{datetime.now().strftime('%m%d%H%M')}"
        tree.write(solution_xml_path)
        logger.info("Bumped solution version to %s in %s", version_el.text, solution_xml_path.name)


def start_device_code_flow(power_platform_tenant_id: str) -> DeviceCodeInfo:
    resp = httpx.post(
        f"https://login.microsoftonline.com/{power_platform_tenant_id}/oauth2/v2.0/devicecode",
        data={"client_id": PAC_CLIENT_ID, "scope": f"{BAP_SCOPE} offline_access"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise CopilotStudioImportError(f"Failed to start device code flow: {resp.status_code} {resp.text}")
    data = resp.json()
    return DeviceCodeInfo(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        interval=data.get("interval", 5),
        expires_in=data.get("expires_in", 900),
    )


def poll_for_user_token(
    device_code_info: DeviceCodeInfo,
    power_platform_tenant_id: str,
    on_still_waiting: Optional[Callable[[], None]] = None,
) -> str:
    """
    Blocks until the human completes the device-code login, or the code
    expires. This is a long-running, deliberately blocking call - the
    pipeline step calling this sets DeploymentRecord.status to
    AWAITING_USER_DEVICE_AUTH before invoking it, so the frontend can
    render the code/URL while this loop waits.
    """
    deadline = time.time() + device_code_info.expires_in
    while time.time() < deadline:
        time.sleep(device_code_info.interval)
        resp = httpx.post(
            f"https://login.microsoftonline.com/{power_platform_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": PAC_CLIENT_ID,
                "device_code": device_code_info.device_code,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]
        # authorization_pending is expected while waiting - anything else, keep trying until deadline
        if on_still_waiting:
            on_still_waiting()
    raise CopilotStudioImportError("Device code login was not completed before it expired. Restart the deployment to try again.")


def _run_pac(args: list[str], cwd: str) -> None:
    import shlex
    logger.info("Running: pac %s (cwd=%s)", " ".join(args), cwd)
    pac_exe = _get_pac_cmd()[0]
    # Build a properly quoted command string for Windows shell=True execution.
    # subprocess with shell=True + list doesn't quote args with spaces correctly on Windows.
    quoted_args = " ".join(f'"{a}"' if " " in a else a for a in args)
    cmd_str = f'"{pac_exe}" {quoted_args}'
    result = subprocess.run(cmd_str, cwd=cwd, capture_output=True, text=True, timeout=600, shell=True)
    if result.stdout.strip():
        logger.info("pac %s output:\n%s", args[0], result.stdout.strip())
    if result.returncode != 0:
        raise CopilotStudioImportError(f"pac {' '.join(args)} failed:\nstdout: {result.stdout}\nstderr: {result.stderr}")
    logger.info("pac %s succeeded", " ".join(args[:2]))


def generate_settings_json(solution_zip_path: str, work_dir: str) -> dict:
    settings_path = Path(work_dir) / "settings.json"
    _run_pac(["solution", "create-settings", "--solution-zip", solution_zip_path, "--settings-file", str(settings_path)], cwd=work_dir)
    if not settings_path.exists():
        raise CopilotStudioImportError("pac solution create-settings did not produce settings.json")
    return json.loads(settings_path.read_text())


def resolve_environment_guid(environment_id: str, user_token: str) -> str:
    import re
    if re.match(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", environment_id) or environment_id.startswith("Default-"):
        return environment_id

    resp = httpx.get(
        "https://api.powerapps.com/providers/Microsoft.PowerApps/environments?api-version=2020-06-01",
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise CopilotStudioImportError(f"Failed to list environments: {resp.status_code} {resp.text}")

    def normalize(url: Optional[str]) -> str:
        if not url:
            return ""
        return url.replace("https://", "").replace("http://", "").strip().rstrip("/")

    target = normalize(environment_id)
    for env in resp.json().get("value", []):
        inst_url = env.get("properties", {}).get("linkedEnvironmentMetadata", {}).get("instanceUrl")
        if normalize(inst_url) == target:
            return env["name"]

    logger.warning("Could not resolve environment '%s' to a GUID - using it as-is.", environment_id)
    return environment_id


def resolve_custom_connector_api(env_guid: str, user_token: str, connector_name_pattern: str, max_wait_seconds: int = 120) -> str:
    """
    Queries the Power Apps API list for a custom connector matching the given
    pattern. Retries for up to max_wait_seconds (default 2 minutes) because
    Dataverse takes 10-60+ seconds to index a newly imported connector after
    'pac solution import' returns.
    """
    filter_query = quote(f"environment eq '{env_guid}'")
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/apis?api-version=2020-06-01&$filter={filter_query}"
    
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        resp = httpx.get(url, headers={"Authorization": f"Bearer {user_token}"}, timeout=30)
        if resp.status_code != 200:
            raise CopilotStudioImportError(f"Failed to list connectors: {resp.status_code} {resp.text}")

        apis = resp.json().get("value", [])
        for api in apis:
            name = api.get("name", "")
            if connector_name_pattern in name and "shared_" in name:
                logger.info("Found custom connector '%s' after %d attempt(s)", name, attempt)
                return name

        if time.time() >= deadline:
            available_names = [api.get("name", "") for api in apis]
            logger.error(
                "Failed to find connector matching '%s' after %ds. Available custom APIs: %s",
                connector_name_pattern, max_wait_seconds, available_names
            )
            raise CopilotStudioImportError(
                f"Could not find the imported custom connector matching '{connector_name_pattern}' in Dataverse. "
                f"Available custom APIs: {available_names}. Ensure the connector solution zip was imported successfully."
            )

        wait = min(15, deadline - time.time())
        logger.info(
            "Connector '%s' not yet indexed in Dataverse (attempt %d), retrying in %.0fs...",
            connector_name_pattern, attempt, wait
        )
        time.sleep(wait)


def create_connection(env_guid: str, user_token: str, api_name: str, display_name: str) -> str:
    import uuid
    conn_guid = uuid.uuid4().hex
    resp = httpx.put(
        f"https://api.powerapps.com/providers/Microsoft.PowerApps/apis/{api_name}/connections/{conn_guid}"
        f"?api-version=2020-06-01&$filter=environment%20eq%20%27{env_guid}%27",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={"properties": {"displayName": display_name, "environment": {"name": env_guid}}},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise CopilotStudioImportError(f"Failed to create connection for {api_name}: {resp.status_code} {resp.text}")
    return conn_guid


def bind_connections_and_import_solution(
    settings: dict,
    custom_connection_id: str,
    copilot_connection_id: str,
    solution_zip_path: str,
    work_dir: str,
) -> None:
    for conn_ref in settings.get("ConnectionReferences", []):
        if "shared_microsoftcopilotstudio" not in conn_ref.get("ConnectorId", ""):
            conn_ref["ConnectionId"] = custom_connection_id
        else:
            conn_ref["ConnectionId"] = copilot_connection_id

    settings_path = Path(work_dir) / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))

    _run_pac(["solution", "import", "--path", solution_zip_path, "--settings-file", str(settings_path),
              "--force-overwrite", "--activate-plugins"], cwd=work_dir)
    _run_pac(["solution", "publish"], cwd=work_dir)


def fetch_flow_webhook_url(env_guid: str, user_token: str, flow_name_pattern: str) -> Optional[str]:
    resp = httpx.get(
        f"https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/{env_guid}/flows?api-version=2016-11-01",
        headers={"Authorization": f"Bearer {user_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise CopilotStudioImportError(f"Failed to list flows: {resp.status_code} {resp.text}")

    flow = next(
        (f for f in resp.json().get("value", [])
         if flow_name_pattern in f.get("properties", {}).get("displayName", "") or "request_trigger" in f.get("properties", {}).get("displayName", "")),
        None,
    )
    if not flow:
        raise CopilotStudioImportError(f"Could not find a flow matching '{flow_name_pattern}'. Did the solution import fail?")

    flow_id = flow["name"]
    callback_resp = httpx.post(
        f"https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/{env_guid}/flows/{flow_id}/triggers/manual/listCallbackUrl?api-version=2016-11-01",
        headers={"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"},
        json={},
        timeout=30,
    )
    if callback_resp.status_code != 200:
        raise CopilotStudioImportError(f"Failed to fetch flow webhook URL: {callback_resp.status_code} {callback_resp.text}")

    body = callback_resp.json()
    webhook_url = (body.get("response") or {}).get("value") or body.get("value")
    if not webhook_url:
        logger.warning("Flow webhook URL came back empty: %s", body)
    return webhook_url


def create_pp_environment(
    user_token: str,
    display_name: str,
    location: str,
    sku: str,
    currency: str = "USD",
    language_code: int = 1033,
    status_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Creates a new Power Platform environment via the Microsoft BAP API:
    POST https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments?api-version=2020-06-01

    Two-stage polling:
      1. Poll BAP API until provisioningState == 'Succeeded'.
      2. Poll PowerApps API until Dataverse linkedEnvironmentMetadata.instanceUrl is ready and instanceState == 'Ready'.

    Returns the ready Dataverse instanceUrl.
    """
    allowed_skus = {"Production", "Sandbox"}
    if sku not in allowed_skus:
        raise CopilotStudioImportError(
            f"Invalid environment SKU '{sku}'. Allowed values: {', '.join(sorted(allowed_skus))}"
        )

    if not display_name or not display_name.strip():
        raise CopilotStudioImportError("Environment display name is required.")

    if not location or not location.strip():
        raise CopilotStudioImportError("Environment location/region is required.")

    logger.info(
        "Initiating creation of Power Platform environment '%s' (SKU: %s, Location: %s)...",
        display_name, sku, location
    )

    url = "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments?api-version=2020-06-01"
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "location": location.strip(),
        "properties": {
            "displayName": display_name.strip(),
            "environmentSku": sku,
            "databaseType": "CommonDataService",
            "linkedEnvironmentMetadata": {
                "baseLanguage": language_code,
                "currency": {"code": currency},
            },
        },
    }

    resp = httpx.post(url, headers=headers, json=payload, timeout=45)
    if resp.status_code not in (200, 201, 202):
        err_msg = resp.text
        try:
            err_json = resp.json()
            err_msg = err_json.get("error", {}).get("message") or err_json.get("message") or resp.text
        except Exception:
            pass
        raise CopilotStudioImportError(
            f"Failed to create Power Platform environment (HTTP {resp.status_code}): {err_msg}"
        )

    env_body = resp.json()
    env_name = env_body.get("name")
    if not env_name:
        raise CopilotStudioImportError(f"Environment creation response missing 'name': {env_body}")

    logger.info("Environment shell created with ID: %s. Stage 1: Polling shell provisioningState...", env_name)
    if status_callback:
        status_callback("Creating Power Platform environment shell...")

    _poll_environment_shell_ready(user_token, env_name, timeout=600.0)

    logger.info("Stage 1 completed (provisioningState == Succeeded). Stage 2: Polling Dataverse instance readiness...", env_name)
    if status_callback:
        status_callback("Provisioning Dataverse database instance...")

    instance_url = _poll_dataverse_instance_ready(user_token, env_name, timeout=600.0)
    logger.info("Stage 2 completed. Environment '%s' (%s) is fully ready: %s", display_name, env_name, instance_url)
    return instance_url


def _poll_environment_shell_ready(user_token: str, env_name: str, timeout: float = 600.0) -> None:
    """Stage 1 poll: Waits until BAP API reports provisioningState == 'Succeeded'."""
    url = f"https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/environments/{env_name}?api-version=2020-06-01"
    headers = {"Authorization": f"Bearer {user_token}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                body = resp.json()
                props = body.get("properties", {})
                prov_state = props.get("provisioningState")
                logger.info("Poll Stage 1 (shell) for %s: provisioningState = %s", env_name, prov_state)
                if prov_state == "Succeeded":
                    return
                elif prov_state in ("Failed", "Canceled"):
                    err_details = props.get("provisioningDetails") or body
                    raise CopilotStudioImportError(f"Environment provisioning failed: {err_details}")
        except CopilotStudioImportError:
            raise
        except Exception as e:
            logger.warning("Transient error polling shell state for %s: %s", env_name, e)

        time.sleep(10)

    raise CopilotStudioImportError(f"Timed out after {int(timeout)}s waiting for environment shell provisioning to succeed.")


def _poll_dataverse_instance_ready(user_token: str, env_name: str, timeout: float = 600.0) -> str:
    """Stage 2 poll: Waits until PowerApps API reports Dataverse linkedEnvironmentMetadata instanceState == 'Ready' and provides instanceUrl."""
    url = f"https://api.powerapps.com/providers/Microsoft.PowerApps/environments/{env_name}?api-version=2020-06-01"
    headers = {"Authorization": f"Bearer {user_token}"}

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                body = resp.json()
                props = body.get("properties", {})
                linked_meta = props.get("linkedEnvironmentMetadata") or {}
                inst_state = linked_meta.get("instanceState")
                inst_url = linked_meta.get("instanceUrl")
                logger.info("Poll Stage 2 (Dataverse) for %s: instanceState = %s, instanceUrl = %s", env_name, inst_state, inst_url)
                if inst_state == "Ready" and inst_url and str(inst_url).strip():
                    return str(inst_url).rstrip("/")
        except CopilotStudioImportError:
            raise
        except Exception as e:
            logger.warning("Transient error polling Dataverse readiness for %s: %s", env_name, e)

        time.sleep(10)

    raise CopilotStudioImportError(f"Timed out after {int(timeout)}s waiting for Dataverse instance to become Ready.")

