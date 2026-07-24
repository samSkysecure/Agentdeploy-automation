import os
import logging
from dotenv import load_dotenv
from app.services.sharepoint import SharePointClient

# Setup logging to see details
logging.basicConfig(level=logging.INFO)

# Load env variables prioritizing .env.docgenhybrid over .env
load_dotenv(".env.docgenhybrid")
load_dotenv("../.env.docgenhybrid")
load_dotenv()
load_dotenv("../.env")

LAB_TENANT_ID = "547b64a7-e66e-48df-a146-3e898cbcb60f"
CLIENT_ID = os.getenv("SKYSECURE_APP_ID")
CLIENT_SECRET = os.getenv("SKYSECURE_APP_SECRET")
SHAREPOINT_SITE_URL = "https://skysecurelab.sharepoint.com/sites/Engineering_test"

print("=" * 60)
print("SharePoint Client Provisioning Diagnostic Test")
print("=" * 60)
print(f"Target Site URL: {SHAREPOINT_SITE_URL}")
print(f"Auth Tenant ID:  {LAB_TENANT_ID}")
print(f"App Client ID:   {CLIENT_ID}")
print("-" * 60)

if not CLIENT_ID or not CLIENT_SECRET:
    print("Error: SKYSECURE_APP_ID or SKYSECURE_APP_SECRET is not set in your .env file.")
    exit(1)

# Instantiate client with explicit credentials
client = SharePointClient(
    site_url=SHAREPOINT_SITE_URL,
    tenant_id=LAB_TENANT_ID,
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET
)

print("Provisioning structure using SharePointClient.ensure_structure()...")
structure = client.ensure_structure()

print("-" * 60)
print("[SUCCESS] Provisioning successful! Details:")
print(f"Site ID:            {structure['site_id']}")
print(f"Templates Drive ID: {structure['template_drive_id']}")
print(f"Generated Drive ID: {structure['generated_drive_id']}")
print(f"Deployed Lists ID:  {structure['deployed_lists_id']}")
print("=" * 60)
