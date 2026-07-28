"""
sharepoint.py
-------------
Standalone SharePoint client for Skysecure Container App agents.

Responsibilities:
  - Authenticate to Microsoft Graph using the SPN credentials already
    present as env vars (MSTENANT_ID, MSCLIENT_ID, MSCLIENT_SECRET) or passed in.
  - On first use, ensure the customer's SharePoint site has the expected
    document library structure (Templates, Generated) and lists (Deployed Lists).
    Creates them if missing.
  - Download template files from the Templates library.
  - Upload generated documents to the Generated library.
  - List files in either library.
  - Delete files from either library.
"""

import os
import time
import logging
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse, quote

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

TEMPLATES_LIBRARY = os.getenv("SHAREPOINT_TEMPLATES_LIBRARY", "deployed template")
GENERATED_LIBRARY = os.getenv("SHAREPOINT_GENERATED_LIBRARY", "Deployed document library")
TOKEN_BUFFER = int(os.getenv("GRAPH_TOKEN_BUFFER_SECONDS", "60"))

# Global token cache (indexed by (tenant_id, client_id))
_token_cache: dict = {}


def resolve_customer_sharepoint_site_url(
    input_site_url: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    """
    Validates the input SharePoint site URL against the customer's tenant.
    If input_site_url is empty, invalid, or cross-tenant, auto-discovers
    and returns the customer's tenant SharePoint site URL.
    """
    token_url = TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
    response = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": GRAPH_SCOPE,
        },
        timeout=30,
    )
    _raise_for_graph_error(response, context="discovering customer root site")
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Discover customer tenant root SharePoint site
    root_res = httpx.get(f"{GRAPH_BASE}/sites/root", headers=headers, timeout=30)
    _raise_for_graph_error(root_res, context="fetching root site")
    root_data = root_res.json()
    customer_root_url = root_data.get("webUrl", "").rstrip("/")
    customer_hostname = urlparse(customer_root_url).hostname

    if input_site_url and customer_hostname and customer_hostname in input_site_url:
        # User provided a URL matching their tenant hostname
        parsed = urlparse(input_site_url)
        encoded_path = quote(parsed.path, safe="/")
        check_url = f"{GRAPH_BASE}/sites/{customer_hostname}:{encoded_path}"
        check_res = httpx.get(check_url, headers=headers, timeout=30)
        if check_res.status_code == 200:
            return input_site_url.rstrip("/")

    logger.info(
        "Auto-discovered customer SharePoint root site: %s (input URL '%s' was empty or invalid for tenant)",
        customer_root_url,
        input_site_url,
    )
    return customer_root_url


# ---------------------------------------------------------------------------
# Public client class
# ---------------------------------------------------------------------------

class SharePointClient:
    """
    Stateful client for a single customer SharePoint site.
    Resolves the site, library, and list IDs on first use and caches them
    for the lifetime of the instance.

    Usage:
        client = SharePointClient()  # reads from env vars
        # Or pass credentials dynamically:
        client = SharePointClient(site_url, tenant_id, client_id, client_secret)
        
        structure = client.ensure_structure()
    """

    def __init__(
        self,
        site_url: Optional[str] = None,
        tenant_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.site_url = (site_url or os.getenv("SHAREPOINT_SITE_URL") or _require_env("SHAREPOINT_SITE_URL")).rstrip("/")
        self.tenant_id = tenant_id or os.getenv("MSTENANT_ID") or _require_env("MSTENANT_ID")
        self.client_id = client_id or os.getenv("MSCLIENT_ID") or _require_env("MSCLIENT_ID")
        self.client_secret = client_secret or os.getenv("MSCLIENT_SECRET") or _require_env("MSCLIENT_SECRET")

        self._site_id: Optional[str] = None
        self._template_drive_id: Optional[str] = None
        self._generated_drive_id: Optional[str] = None
        self._deployed_lists_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Authentication & headers
    # ------------------------------------------------------------------

    def _get_graph_token(self) -> str:
        """
        Returns a valid Graph access token, refreshing if expired or close to expiry.
        Uses client credentials flow with self.tenant_id, client_id, client_secret.
        """
        key = (self.tenant_id, self.client_id)
        now = time.time()
        
        if key in _token_cache:
            token, expires_at = _token_cache[key]
            if now < expires_at - TOKEN_BUFFER:
                return token

        url = TOKEN_URL_TEMPLATE.format(tenant_id=self.tenant_id)
        response = httpx.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": GRAPH_SCOPE,
            },
            timeout=30,
        )
        _raise_for_graph_error(response, context="token acquisition")

        data = response.json()
        token = data["access_token"]
        expires_at = now + int(data.get("expires_in", 3600))
        _token_cache[key] = (token, expires_at)

        logger.debug("Graph token refreshed for client %s, expires in %ss", self.client_id, data.get("expires_in"))
        return token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_graph_token()}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Lazy initialisation — resolves IDs on first use
    # ------------------------------------------------------------------

    def _ensure_initialised(self):
        if self._site_id is not None:
            return
        logger.info("Initialising SharePoint client for site: %s", self.site_url)
        self._site_id = self._get_site_id()
        self._template_drive_id = self._get_or_create_library(TEMPLATES_LIBRARY)
        self._generated_drive_id = self._get_or_create_library(GENERATED_LIBRARY)
        self._deployed_lists_id = self._get_or_create_deployed_lists("Deployed Lists")
        logger.info(
            "SharePoint client ready. Templates drive: %s | Generated drive: %s | Deployed Lists: %s",
            self._template_drive_id,
            self._generated_drive_id,
            self._deployed_lists_id,
        )

    @property
    def site_id(self) -> str:
        self._ensure_initialised()
        return self._site_id

    @property
    def template_drive_id(self) -> str:
        self._ensure_initialised()
        return self._template_drive_id

    @property
    def generated_drive_id(self) -> str:
        self._ensure_initialised()
        return self._generated_drive_id

    @property
    def deployed_lists_id(self) -> str:
        self._ensure_initialised()
        return self._deployed_lists_id

    # ------------------------------------------------------------------
    # Site resolution
    # ------------------------------------------------------------------

    def _get_site_id(self) -> str:
        """Resolves the site URL to its Graph site ID."""
        parsed = urlparse(self.site_url)
        hostname = parsed.hostname
        path = parsed.path
        
        encoded_path = quote(path, safe="/")
        url = f"{GRAPH_BASE}/sites/{hostname}:{encoded_path}"

        response = httpx.get(url, headers=self._headers(), timeout=30)
        _raise_for_graph_error(response, context=f"resolving site {self.site_url}")

        site_id = response.json()["id"]
        logger.debug("Resolved site ID: %s", site_id)
        return site_id

    # ------------------------------------------------------------------
    # Library setup helpers
    # ------------------------------------------------------------------

    def _list_drives(self) -> list[dict]:
        url = f"{GRAPH_BASE}/sites/{self._site_id}/drives"
        response = httpx.get(url, headers=self._headers(), timeout=30)
        _raise_for_graph_error(response, context="listing drives")
        return response.json().get("value", [])

    def _get_or_create_library(self, library_name: str) -> str:
        """
        Returns the drive ID for the named document library.
        Creates the library if it does not exist.
        """
        drives = self._list_drives()
        existing = next((d for d in drives if d["name"] == library_name), None)

        if existing:
            logger.debug("Library '%s' found, drive ID: %s", library_name, existing["id"])
            return existing["id"]

        # Create the document library
        logger.info("Library '%s' not found — creating it.", library_name)
        url = f"{GRAPH_BASE}/sites/{self._site_id}/lists"
        payload = {
            "displayName": library_name,
            "list": {"template": "documentLibrary"},
        }
        response = httpx.post(url, headers=self._headers(), json=payload, timeout=30)
        _raise_for_graph_error(response, context=f"creating library '{library_name}'")

        # The list creation returns a list object, not a drive.
        # Fetch drives again to get the drive ID for the new library.
        for attempt in range(6):
            time.sleep(3)
            drives = self._list_drives()
            created = next((d for d in drives if d["name"] == library_name), None)
            if created:
                logger.info("Library '%s' created, drive ID: %s", library_name, created["id"])
                return created["id"]
            logger.debug("Waiting for library '%s' to appear in drives... attempt %d/6", library_name, attempt + 1)

        raise SharePointError(
            f"Library '{library_name}' was created but could not be found in drives. "
            "This can happen if SharePoint provisioning is still in progress — retry in a few seconds."
        )

    # ------------------------------------------------------------------
    # Custom List setup helpers
    # ------------------------------------------------------------------

    def _get_or_create_deployed_lists(self, list_name: str) -> str:
        """
        Returns the list ID for the custom SharePoint list.
        Creates the list with specific columns if it does not exist.
        """
        url = f"{GRAPH_BASE}/sites/{self._site_id}/lists"
        response = httpx.get(url, headers=self._headers(), timeout=30)
        _raise_for_graph_error(response, context="listing lists")

        lists = response.json().get("value", [])
        existing = next((l for l in lists if l["displayName"] == list_name), None)

        if existing:
            logger.debug("List '%s' found, list ID: %s", list_name, existing["id"])
            return existing["id"]

        # Create the list with columns
        logger.info("List '%s' not found — creating it with custom columns.", list_name)
        
        columns_schema = [
            {"name": "TicketID", "text": {}},
            {"name": "Description", "text": {"allowMultipleLines": True}},
            {"name": "AssignedCategory", "text": {}},
            {
                "name": "Priority",
                "choice": {
                    "choices": ["Low", "Medium", "High"],
                    "allowTextEntry": False
                }
            },
            {
                "name": "Status",
                "choice": {
                    "choices": ["New", "Active", "Resolved", "Closed"],
                    "allowTextEntry": False
                }
            },
            {"name": "CreatedByAadId", "text": {}},
            {"name": "AssignedTo", "text": {}},
            {"name": "ScreenshotLink", "hyperlinkOrPicture": {}},
            {"name": "SubmitterEmail", "text": {}}
        ]

        create_response = httpx.post(
            url,
            headers=self._headers(),
            json={
                "displayName": list_name,
                "columns": columns_schema,
                "list": {
                    "template": "genericList"
                }
            },
            timeout=30,
        )
        _raise_for_graph_error(create_response, context=f"creating list '{list_name}'")

        created_list = create_response.json()
        logger.info("List '%s' created successfully. List ID: %s", list_name, created_list["id"])
        return created_list["id"]

    # ------------------------------------------------------------------
    # Templates library
    # ------------------------------------------------------------------

    def list_templates(self) -> list[dict]:
        """
        Lists all files in the Templates library.
        """
        url = f"{GRAPH_BASE}/drives/{self.template_drive_id}/root/children"
        response = httpx.get(url, headers=self._headers(), timeout=30)
        _raise_for_graph_error(response, context="listing templates")

        return [
            {
                "name": item["name"],
                "id": item["id"],
                "size": item.get("size", 0),
                "modified": item.get("lastModifiedDateTime"),
                "download_url": item.get("@microsoft.graph.downloadUrl"),
            }
            for item in response.json().get("value", [])
            if "file" in item  # skip folders
        ]

    def download_template(self, filename: str) -> bytes:
        """
        Downloads a template file by name from the Templates library.
        """
        self._ensure_initialised()
        encoded = quote(filename)
        url = f"{GRAPH_BASE}/drives/{self.template_drive_id}/root:/{encoded}:/content"

        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self._get_graph_token()}"},
            follow_redirects=True,
            timeout=60,
        )
        if response.status_code == 404:
            raise SharePointFileNotFoundError(
                f"Template '{filename}' not found in the Templates library."
            )
        _raise_for_graph_error(response, context=f"downloading template '{filename}'")

        logger.info("Downloaded template '%s' (%d bytes)", filename, len(response.content))
        return response.content

    def upload_template(self, filename: str, content: bytes) -> dict:
        """
        Uploads a file to the Templates library.
        """
        return self._upload(self.template_drive_id, filename, content, context="template")

    # ------------------------------------------------------------------
    # Generated library
    # ------------------------------------------------------------------

    def list_generated(self) -> list[dict]:
        """
        Lists all files in the Generated library.
        """
        url = f"{GRAPH_BASE}/drives/{self.generated_drive_id}/root/children"
        response = httpx.get(url, headers=self._headers(), timeout=30)
        _raise_for_graph_error(response, context="listing generated docs")

        return [
            {
                "name": item["name"],
                "id": item["id"],
                "size": item.get("size", 0),
                "modified": item.get("lastModifiedDateTime"),
                "download_url": item.get("@microsoft.graph.downloadUrl"),
            }
            for item in response.json().get("value", [])
            if "file" in item
        ]

    def upload_generated(self, filename: str, content: bytes) -> dict:
        """
        Uploads a generated document to the Generated library.
        """
        return self._upload(self.generated_drive_id, filename, content, context="generated doc")

    def download_generated(self, filename: str) -> bytes:
        """
        Downloads a previously generated document by name.
        """
        self._ensure_initialised()
        encoded = quote(filename)
        url = f"{GRAPH_BASE}/drives/{self.generated_drive_id}/root:/{encoded}:/content"

        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {self._get_graph_token()}"},
            follow_redirects=True,
            timeout=60,
        )
        if response.status_code == 404:
            raise SharePointFileNotFoundError(
                f"Generated file '{filename}' not found in the Generated library."
            )
        _raise_for_graph_error(response, context=f"downloading generated '{filename}'")
        return response.content

    # ------------------------------------------------------------------
    # Delete (works on either library)
    # ------------------------------------------------------------------

    def delete_template(self, filename: str) -> None:
        """Deletes a file from the Templates library by name."""
        self._delete(self.template_drive_id, filename)

    def delete_generated(self, filename: str) -> None:
        """Deletes a file from the Generated library by name."""
        self._delete(self.generated_drive_id, filename)

    # ------------------------------------------------------------------
    # Structure setup (called explicitly during agent bootstrap)
    # ------------------------------------------------------------------

    def ensure_structure(self) -> dict:
        """
        Ensures the Templates, Generated libraries, and Deployed Lists exist on the site.
        Safe to call on every agent startup — is a no-op if already present.

        Returns a dict with the resolved IDs for logging/debugging:
          { site_id, template_drive_id, generated_drive_id, deployed_lists_id, site_url }
        """
        self._ensure_initialised()
        return {
            "site_url": self.site_url,
            "site_id": self._site_id,
            "template_drive_id": self._template_drive_id,
            "generated_drive_id": self._generated_drive_id,
            "deployed_lists_id": self._deployed_lists_id,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upload(self, drive_id: str, filename: str, content: bytes, context: str) -> dict:
        self._ensure_initialised()
        encoded = quote(filename)

        if len(content) < 4 * 1024 * 1024:
            url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded}:/content"
            response = httpx.put(
                url,
                headers={
                    "Authorization": f"Bearer {self._get_graph_token()}",
                    "Content-Type": _mime_type(filename),
                },
                content=content,
                timeout=60,
            )
            _raise_for_graph_error(response, context=f"uploading {context} '{filename}'")
            logger.info("Uploaded %s '%s' (%d bytes)", context, filename, len(content))
            return response.json()

        else:
            session_url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded}:/createUploadSession"
            session_payload = {
                "item": {
                    "@microsoft.graph.conflictBehavior": "replace",
                    "name": filename,
                }
            }
            session_response = httpx.post(
                session_url,
                headers=self._headers(),
                json=session_payload,
                timeout=30,
            )
            _raise_for_graph_error(session_response, context="creating upload session")
            upload_url = session_response.json()["uploadUrl"]

            chunk_size = 4 * 1024 * 1024
            total = len(content)
            offset = 0
            last_response = None

            while offset < total:
                chunk = content[offset: offset + chunk_size]
                end = offset + len(chunk) - 1
                upload_response = httpx.put(
                    upload_url,
                    headers={
                        "Content-Range": f"bytes {offset}-{end}/{total}",
                        "Content-Length": str(len(chunk)),
                    },
                    content=chunk,
                    timeout=60,
                )
                if upload_response.status_code not in (200, 201, 202):
                    _raise_for_graph_error(upload_response, context=f"chunk upload at offset {offset}")
                last_response = upload_response
                offset += len(chunk)
                logger.debug("Uploaded chunk %d-%d of %d", offset - len(chunk), end, total)

            logger.info("Uploaded large %s '%s' (%d bytes)", context, filename, total)
            return last_response.json()

    def _delete(self, drive_id: str, filename: str) -> None:
        self._ensure_initialised()
        encoded = quote(filename)
        url = f"{GRAPH_BASE}/drives/{drive_id}/root:/{encoded}"
        response = httpx.delete(
            url,
            headers={"Authorization": f"Bearer {self._get_graph_token()}"},
            timeout=30,
        )
        if response.status_code == 404:
            raise SharePointFileNotFoundError(f"File '{filename}' not found.")
        _raise_for_graph_error(response, context=f"deleting '{filename}'")
        logger.info("Deleted '%s' from drive %s", filename, drive_id)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SharePointError(Exception):
    """Base exception for SharePoint client errors."""
    pass


class SharePointFileNotFoundError(SharePointError):
    """Raised when a requested file does not exist in the library."""
    pass


class SharePointAuthError(SharePointError):
    """Raised when Graph authentication fails."""
    pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Ensure the Container App is configured correctly."
        )
    return value


def _raise_for_graph_error(response: httpx.Response, context: str = "") -> None:
    if response.status_code in (200, 201, 202, 204):
        return
    if response.status_code in (401, 403):
        raise SharePointAuthError(
            f"Graph authentication/permission error during {context}. "
            f"Status: {response.status_code}. "
            f"Ensure the SPN has Sites.ReadWrite.All on the customer tenant. "
            f"Detail: {response.text}"
        )
    if response.status_code == 404:
        raise SharePointFileNotFoundError(
            f"Resource not found during {context}. Detail: {response.text}"
        )
    raise SharePointError(
        f"Graph API error during {context}. "
        f"Status: {response.status_code}. Detail: {response.text}"
    )


def _mime_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf":  "application/pdf",
        "png":  "image/png",
        "jpg":  "image/jpeg",
        "jpeg": "image/jpeg",
        "txt":  "text/plain",
        "json": "application/json",
        "csv":  "text/csv",
    }.get(ext, "application/octet-stream")