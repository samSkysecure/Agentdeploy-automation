"""
Teams app manifest generator.

Generates a customer-specific Teams app manifest zip ready for manual upload
to Teams Admin Center or automated publish via Graph API.

Not yet wired into Graph API publish - zip is saved locally and path
returned in the deployment record for manual upload.
"""
import base64
import json
import logging
import os
import uuid
import zipfile
import io
from typing import Optional

from app.core.config import Settings

logger = logging.getLogger("orchestrator.teams_manifest")


def _base_manifest(settings: Settings) -> dict:
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.26/MicrosoftTeams.schema.json",
        "manifestVersion": "1.26",
        "version": "1.0.3",
        "supportsChannelFeatures": "tier1",
        "id": None,
        "developer": {
            "name": settings.teams_developer_name,
            "websiteUrl": settings.teams_developer_website_url,
            "privacyUrl": settings.teams_developer_privacy_url,
            "termsOfUseUrl": settings.teams_developer_terms_url,
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "name": {"short": None, "full": None},
        "description": {"short": None, "full": None},
        "accentColor": "#FFFFFF",
        "bots": [
            {
                "botId": None,
                "scopes": ["team", "groupChat", "personal", "copilot"],
                "supportsFiles": True,
                "isNotificationOnly": False,
                "commandLists": [
                    {
                        "scopes": ["personal", "copilot"],
                        "commands": [
                            {"title": "Select Template", "description": "Fetch a Word template from SharePoint by typing its name."},
                            {"title": "Reset Session", "description": "Clear the current session state and start fresh."},
                        ],
                    }
                ],
            }
        ],
        "copilotAgents": {
            "customEngineAgents": [
                {"type": "bot", "id": None}
            ]
        },
        "composeExtensions": [],
        "configurableTabs": [],
        "staticTabs": [],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [],
    }


def generate_manifest(
    *,
    bot_id: str,
    container_app_fqdn: str,
    agent_slug: str,
    customer_slug: str,
    settings: Settings,
    agent_display_name: Optional[str] = None,
    agent_description: Optional[str] = None,
    teams_app_id: Optional[str] = None,
) -> dict:
    fqdn = container_app_fqdn.replace("https://", "").replace("http://", "").rstrip("/")
    app_id = teams_app_id or str(uuid.uuid4())

    if agent_slug == "docgenhybrid":
        display_name = agent_display_name or "Skysecure Docgen Agent"
        short_desc = agent_description or "AI-powered Teams bot for generating and modifying Word documents."
        full_desc = "SkySecure DocGen is an AI-powered Microsoft Teams assistant that helps users upload Word templates, collect required information conversationally, and generate finalized documents automatically."
    else:
        display_name = agent_display_name or agent_slug.replace("-", " ")
        short_desc = agent_description or f"ai-powered agent for {customer_slug}"
        full_desc = short_desc

    manifest = _base_manifest(settings)
    manifest["id"] = app_id
    manifest["name"]["short"] = display_name
    manifest["name"]["full"] = display_name
    manifest["description"]["short"] = short_desc
    manifest["description"]["full"] = full_desc
    manifest["bots"][0]["botId"] = bot_id
    manifest["copilotAgents"]["customEngineAgents"][0]["id"] = bot_id
    manifest["validDomains"] = [fqdn]

    logger.info("Generated manifest agent=%s customer=%s teams_app_id=%s", agent_slug, customer_slug, app_id)
    return manifest


def zip_manifest(
    manifest: dict,
    *,
    color_icon_path: Optional[str] = None,
    outline_icon_path: Optional[str] = None,
) -> bytes:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        if color_icon_path and os.path.exists(color_icon_path):
            zf.write(color_icon_path, "color.png")
        else:
            zf.writestr("color.png", _placeholder_png())
        if outline_icon_path and os.path.exists(outline_icon_path):
            zf.write(outline_icon_path, "outline.png")
        else:
            zf.writestr("outline.png", _placeholder_png())
    return zip_buffer.getvalue()


def generate_and_zip_manifest(
    *,
    bot_id: str,
    container_app_fqdn: str,
    agent_slug: str,
    customer_slug: str,
    settings: Settings,
    agent_display_name: Optional[str] = None,
    agent_description: Optional[str] = None,
    teams_app_id: Optional[str] = None,
    color_icon_path: Optional[str] = None,
    outline_icon_path: Optional[str] = None,
) -> tuple[bytes, str]:
    manifest = generate_manifest(
        bot_id=bot_id,
        container_app_fqdn=container_app_fqdn,
        agent_slug=agent_slug,
        customer_slug=customer_slug,
        settings=settings,
        agent_display_name=agent_display_name,
        agent_description=agent_description,
        teams_app_id=teams_app_id,
    )
    zip_bytes = zip_manifest(manifest, color_icon_path=color_icon_path, outline_icon_path=outline_icon_path)
    return zip_bytes, manifest["id"]


def _placeholder_png() -> bytes:
    png_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
    )
    return base64.b64decode(png_base64)
