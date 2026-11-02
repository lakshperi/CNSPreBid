# CNS Pre-Bidding (Streamlit)

Streamlit deployment of the Pre-Bidding Application described in
`docs/Logical-Solution-Architecture.md` and the v3.0 Business Functional Requirements.

## Stack

- **UI:** Streamlit
- **Domain:** Python services (`prebidding/services`)
- **Store:** SQLite (local-friendly stand-in for PostgreSQL)
- **Auth:** Google OIDC via Streamlit `st.login()` (demo identities only as fallback)

## Quick start

```bash
cd CNSPreBid
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. python -m prebidding.seed
PYTHONPATH=. streamlit run app.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

## Google SSO setup

1. In [Google Cloud Console → Auth Platform → Clients](https://console.cloud.google.com/auth/clients), create a **Web application** client.
2. Add authorized redirect URI: `http://localhost:8501/oauth2callback`
3. While status is **Testing**, add your Google accounts under **Audience → Test users**.
4. Copy the example secrets file and fill in real values:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "<long-random-string>"
client_id = "<google-client-id>"
client_secret = "<google-client-secret>"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[prebidding]
admin_emails = ["you@yourdomain.com"]
allow_demo_login = false
```

5. Restart Streamlit. The login page will show **Log in with Google**.
6. Users in `admin_emails` become Administrators; all other Google users are Bidders.

## Google Drive photo sync

1. Enable **Google Drive API** for the same GCP project as your OAuth client.
2. Create a Drive folder and upload item photos named by item id (e.g. `ITEM-101.jpg`).
3. Put the folder ID/URL in `.streamlit/secrets.toml` as `drive_folder_id`, or paste it in **Administrator → Sync**.
4. Sign in as an admin with Google SSO, approve Drive access when prompted, then click **Sync photos from Google Drive**.
5. New photos import as unpublished (`Upcoming`) until an initial bid amount is set and the item is published.

### Demo users (fallback only)

Used when Google SSO secrets are missing, or when `allow_demo_login = true`.

| Name | Role |
| --- | --- |
| Alex Admin | Administrator |
| Jamie / Riley / Casey Bidder | Bidder |

## Features implemented

- Browse published items with photos and amounts
- Submit one bid per user per item with transactional revalidation
- Anonymous public bid history; admin identity visibility
- My Bids status (Highest / Outbid / Winning)
- Admin publish, schedule/close, correct/cancel with audit, winners export
- Seed sync stand-in for Google Drive / Sheets

## Streamlit Community Cloud

1. Push this repo to GitHub (private is fine).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app** → select the repo, branch `main`, file `app.py`.
3. In the app **Settings → Secrets**, paste a production `secrets.toml` (from `.streamlit/secrets.toml.example`).
4. Set cloud redirect URI to your app URL, for example:

```toml
[auth]
redirect_uri = "https://YOUR-APP-NAME.streamlit.app/oauth2callback"
```

5. In Google Cloud OAuth client, add the same `redirect_uri` under **Authorized redirect URIs**.
6. Put your Google account in `[prebidding].admin_emails`.

Notes:
- Free Cloud storage is ephemeral — SQLite data can reset on restart. Prefer a hosted DB later for production.
- **Connect Google Drive & Sync** uses a local loopback OAuth listener (`127.0.0.1:8090`) and works on your laptop. On Streamlit Cloud, sync photos locally (or add a service-account-based sync later).