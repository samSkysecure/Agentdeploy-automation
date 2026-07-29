from app.core.config import get_settings
from app.services.teams_manifest import generate_and_zip_manifest

settings = get_settings()

# NOTE: this is a standalone manual test script, unrelated to the pipeline.
# Its premise (a single static bot_id) is now stale under the new per-customer
# App Registration model - there's no longer one fixed bot_id to use here.
# Replace with a real agent_app_client_id from an actual deployment record
# if you need to regenerate a manifest for testing.
zip_bytes, teams_app_id = generate_and_zip_manifest(
    bot_id=settings.deployment_spn_client_id,  # placeholder only - see note above
    container_app_fqdn="ca-teamsagent-sst.niceisland-a356fcde.southindia.azurecontainerapps.io",
    agent_slug="teamsagent",
    customer_slug="sstlab",
    agent_display_name="SST Lab Tool Governance Agent",
    settings=settings,
)

with open("manifest_package.zip", "wb") as f:
    f.write(zip_bytes)

print(f"Teams App ID: {teams_app_id}")
print("Manifest package written to manifest_package.zip")