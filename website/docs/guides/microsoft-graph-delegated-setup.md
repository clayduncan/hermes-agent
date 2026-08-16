---
title: "Microsoft Graph Delegated Auth Setup"
description: "Agent runbook for connecting a Microsoft account with delegated (user-consent) permissions to Hermes"
---

# Microsoft Graph Delegated Auth Setup

**Audience:** This is an agent-facing runbook.  Follow it step by step when walking Clay through connecting a new Microsoft account.

Delegated auth lets Hermes act **as a specific human user** — reading and writing that user's calendar events, mail, and other personal data.  It requires interactive user consent through a browser.  This is separate from the app-only (daemon) auth used by the Teams meeting pipeline.

---

## Background: how it works

1. Clay (or IT) registers an Azure app with **Delegated** permissions (`Calendars.ReadWrite`, `Mail.ReadWrite`, `offline_access`).
2. Hermes generates an authorization URL containing a PKCE challenge.
3. Clay opens the URL in a browser, approves the consent screen, and is redirected to a Microsoft URL with a `?code=...` query parameter.
4. Clay copies that code (or the full URL) and pastes it back to the agent.
5. Hermes exchanges the code for an access token + refresh token and stores them in `~/.hermes/msgraph_token_<account_key>.json`.
6. Future API calls auto-refresh silently using the stored refresh token.

---

## Prerequisites

Before running the setup commands, ensure:

- The Azure app registration exists with **Delegated** (not Application) permissions:
  - `Calendars.ReadWrite`
  - `Mail.ReadWrite`
  - `offline_access` (lets the server issue a refresh token)
- The redirect URI `https://login.microsoftonline.com/common/oauth2/nativeclient` is registered in the app's **Authentication** section.
- You have the **Application (client) ID** and **Directory (tenant) ID** from the app's Overview page.
- The client secret has been placed in `~/.hermes/.env` under the correct key.

---

## Env var naming scheme

```
MSGRAPH_<KEY>_TENANT_ID      = Azure Directory (tenant) ID
MSGRAPH_<KEY>_CLIENT_ID      = Azure Application (client) ID
MSGRAPH_<KEY>_CLIENT_SECRET  = Client secret value (never stored on disk)
```

`<KEY>` is the **account key** in uppercase.

| Account | Key | Env var prefix |
|---------|-----|----------------|
| clay@clayduncan.com | `clayduncan` | `MSGRAPH_CLAYDUNCAN_*` |
| clay@princetonmortgage.com (future) | `princeton` | `MSGRAPH_PRINCETON_*` |

---

## Account registry

### clayduncan (clay@clayduncan.com)

| Field | Value |
|-------|-------|
| Account key | `clayduncan` |
| Tenant ID | `d14d6257-4cf6-45a2-83fd-5d218bda8aee` |
| Client ID | `1bb196a6-47c2-40d0-be2a-f38ddb007820` |
| Client secret env var | `MSGRAPH_CLAYDUNCAN_CLIENT_SECRET` |
| Token file | `~/.hermes/msgraph_token_clayduncan.json` |

**Note:** The secret was originally placed in `.env` as `MS_CLAYDUNCAN_CLIENT_SECRET`.  Rename it to `MSGRAPH_CLAYDUNCAN_CLIENT_SECRET` (see Step 0 below).

### princeton (clay@princetonmortgage.com) — NOT YET REGISTERED

IT has not completed the Azure app registration for this account.  Once they do, follow the same steps below with `--account princeton` and the values they provide.

---

## Setup steps

### Step 0 (one-time, clayduncan only): rename the env var

In `~/.hermes/.env`, rename the existing secret key:

```
# Before:
MS_CLAYDUNCAN_CLIENT_SECRET=<secret>

# After:
MSGRAPH_CLAYDUNCAN_CLIENT_SECRET=<secret>
```

Then restart the gateway so it picks up the change:

```bash
hermes gateway restart
```

---

### Step 1: check whether auth is already valid

```bash
python scripts/microsoft_graph_delegated_setup.py --check --account clayduncan
```

- Exit 0 + `AUTHENTICATED`: auth is good, skip the rest of setup.
- Exit 1 + `NOT_AUTHENTICATED`: continue with Step 2.

---

### Step 2: configure the account (tenant ID + client ID)

```bash
python scripts/microsoft_graph_delegated_setup.py \
  --configure \
  --account clayduncan \
  --tenant-id d14d6257-4cf6-45a2-83fd-5d218bda8aee \
  --client-id 1bb196a6-47c2-40d0-be2a-f38ddb007820
```

This saves `~/.hermes/msgraph_config_clayduncan.json` and validates that the client secret env var is set.  It will error immediately if `MSGRAPH_CLAYDUNCAN_CLIENT_SECRET` is missing.

Optional: override the default scopes (`Calendars.ReadWrite Mail.ReadWrite offline_access`):

```bash
  --scopes "Calendars.ReadWrite Mail.ReadWrite offline_access"
```

---

### Step 3: generate the authorization URL

```bash
python scripts/microsoft_graph_delegated_setup.py --auth-url --account clayduncan
```

This prints a long authorization URL and saves a pending-session file.  Copy and send the URL to Clay.

Tell Clay:
> "Please open this URL in your browser. You'll see a Microsoft login and consent page.  After you approve, your browser will be redirected to a page that shows an error — that's normal.  Please copy the entire URL from your browser's address bar and paste it here."

---

### Step 4: exchange the authorization code

When Clay pastes the URL (or just the `code=...` value), run:

```bash
python scripts/microsoft_graph_delegated_setup.py \
  --auth-code "PASTE_CODE_OR_FULL_URL_HERE" \
  --account clayduncan
```

On success you'll see:
```
OK: Authenticated. Token saved to ~/.hermes/msgraph_token_clayduncan.json
```

---

### Step 5: verify

```bash
python scripts/microsoft_graph_delegated_setup.py --check --account clayduncan
```

Should exit 0 with `AUTHENTICATED`.

---

## Using the token in code

```python
from tools.microsoft_graph_delegated_auth import DelegatedTokenProvider
from tools.microsoft_graph_client import MicrosoftGraphClient

provider = DelegatedTokenProvider(account_key="clayduncan")
client = MicrosoftGraphClient(provider)

# Example: list the next 10 calendar events
events = await client.get_json(
    "/me/calendarView",
    params={
        "startDateTime": "2026-08-16T00:00:00Z",
        "endDateTime": "2026-08-23T00:00:00Z",
        "$top": 10,
        "$orderby": "start/dateTime",
    },
)
```

`DelegatedTokenProvider` is duck-type compatible with `MicrosoftGraphTokenProvider` — you can pass it directly to `MicrosoftGraphClient` as-is.

---

## Revoking access

To delete the local token file:

```bash
python scripts/microsoft_graph_delegated_setup.py --revoke --account clayduncan
```

**Important:** this only removes the local file.  The OAuth consent grant on Microsoft's side remains active until Clay removes it manually:

1. Visit <https://myaccount.microsoft.com/permissions>
2. Find the app in "Apps & services that have access to your data"
3. Click "Remove access"

For organization-managed accounts (Princeton), the tenant admin can revoke via Entra ID → Enterprise applications → User consent.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `DelegatedGraphSetupNeeded` raised | No token file for this account | Run Steps 2–5 |
| `DelegatedGraphReconsentNeeded` raised | Refresh token revoked or expired | Run `--revoke` then Steps 3–5 |
| `--configure` fails with missing env var | Secret not in `.env` or gateway not restarted | Add `MSGRAPH_<KEY>_CLIENT_SECRET` to `~/.hermes/.env` and restart gateway |
| `--auth-url` fails with "No configuration" | `--configure` was not run first | Run Step 2 |
| `--auth-code` fails with "state mismatch" | Stale pending session | Run `--auth-url` again to get a fresh URL |
| Token exchange returns "invalid_grant" | Code expired or already used | Run `--auth-url` again (codes are single-use, expire in ~10 min) |
| `AADSTS700016: Application not found` | Wrong client_id or tenant_id | Verify values in Azure portal against Step 2 |
| `AADSTS700082: Consent was not granted` | User denied the consent screen | Ask Clay to re-open the URL and approve all permissions |

---

## Adding a new account (Princeton, future)

1. Wait for IT to complete the Azure app registration and provide:
   - Tenant ID
   - Application (client) ID
   - Client secret value
2. Add to `~/.hermes/.env`:
   ```
   MSGRAPH_PRINCETON_TENANT_ID=<tenant-id>
   MSGRAPH_PRINCETON_CLIENT_ID=<client-id>
   MSGRAPH_PRINCETON_CLIENT_SECRET=<secret>
   ```
3. Restart the gateway: `hermes gateway restart`
4. Follow Steps 1–5 above with `--account princeton`.
