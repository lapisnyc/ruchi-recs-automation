"""One-time helper: exchange a Dev Dashboard app install for a permanent
offline Admin API token (shpat_...).

Since Jan 2026, custom apps created in the Shopify Dev Dashboard no longer
show a static token in the UI; you get one via a single OAuth code exchange.
Run this on your own machine:

    python scripts/get_token.py

Prerequisites (in the Dev Dashboard app):
  - Scopes requested: read_products, write_products
  - Allowed redirection URL includes: http://localhost:3000/callback
  - Custom distribution set to the store, app installed on the store
"""
import json
import urllib.parse
import urllib.request

store = input("Store domain (e.g. ruchi-new-york.myshopify.com): ").strip()
client_id = input("Client ID: ").strip()
client_secret = input("Client secret: ").strip()
redirect_uri = "http://localhost:3000/callback"

auth_url = (
    f"https://{store}/admin/oauth/authorize?"
    + urllib.parse.urlencode({
        "client_id": client_id,
        "scope": "read_products,write_products",
        "redirect_uri": redirect_uri,
    })
)
print("\n1. Open this URL in the browser where you're logged into the store admin:\n")
print(auth_url)
print("\n2. Approve. The browser will land on a localhost URL that won't load; that's fine.")
print("   Copy the FULL address from the address bar and paste it here.\n")
redirected = input("Paste redirect URL: ").strip()

code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query)["code"][0]
req = urllib.request.Request(
    f"https://{store}/admin/oauth/access_token",
    data=json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req) as resp:
    body = json.loads(resp.read())

print("\nSUCCESS. Your permanent Admin API token (save as SHOPIFY_ADMIN_TOKEN secret):\n")
print(body["access_token"])
print("\nGranted scopes:", body.get("scope"))
print("Note: the code in the redirect URL is single-use and expires in minutes.")
print("If the exchange fails, just run this script again from the top.")
