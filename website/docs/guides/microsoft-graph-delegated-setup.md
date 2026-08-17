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
3. Clay opens the URL in a browser and approves the consent screen.
4. The browser is automatically redirected to `http://localhost:8765/callback` — a tiny HTTP listener running on the same machine as the terminal.  **No manual code-copy step.**
5. Hermes captures the code from the callback, exchanges it for an access token + refresh token, and stores them in `~/.hermes/msgraph_token_<account_key>.json`.
6. Future API calls auto-refresh silently using the stored refresh token.

---

## Prerequisites

Before running the setup commands, ensure:

- The Azure app registration exists with **Delegated** (not Application) permissions:
  - `Calendars.ReadWrite`
  - `Mail.ReadWrite`
  - `offline_access` (lets the server issue a refresh token)
- The following redirect URIs are registered in the app's **Authentication → Mobile and desktop applications** platform (both can coexist):
  - `https://login.microsoftonline.com/common/oauth2/nativeclient` *(existing, keep for fallback)*
  - `http://localhost:8765/callback` *(new — required for the loopback listener flow)*
- You have the **Application (client) ID** and **Directory (tenant) ID** from the app's Overview page.
- The client secret has been placed in `~/.hermes/.env` under the correct key.

### Adding the loopback redirect URI in Azure (one-time step)

1. Go to [portal.azure.com](https://portal.azure.com) → Azure Active Directory → App registrations → your app.
2. Click **Authentication** in the left sidebar.
3. Under **Mobile and desktop applications**, click **Add URI**.
4. Enter exactly: `http://localhost:8765/callback`
5. Click **Save**.

The nativeclient URI already there does **not** need to be removed.

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

### Step 3: complete the OAuth consent flow

```bash
python scripts/microsoft_graph_delegated_setup.py --auth-url --account clayduncan
```

**This is a single blocking command** — it does everything in one step:

1. A local HTTP listener starts on port 8765 (localhost only, invisible to the user).
2. The script prints a long authorization URL — relay it to Clay.
3. Clay opens the URL in their browser, logs in, and approves the consent screen.
4. The browser automatically redirects to `http://localhost:8765/callback`.
5. The script captures the code, exchanges it for tokens, saves them, and exits with `OK: Authenticated.`

Tell Clay:
> "Please open this URL in your browser. You'll see a Microsoft login and consent page.  After you approve, your browser will briefly redirect — you don't need to do anything else.  Just let me know when it's done."

The script will complete automatically once the browser callback arrives (up to 5 minutes).  **Clay does not need to copy or paste anything.**

On success you'll see:
```
OK: Authenticated. Token saved to ~/.hermes/msgraph_token_clayduncan.json
  Account   : clayduncan
  Tenant ID : d14d6257-4cf6-45a2-83fd-5d218bda8aee
  Scopes    : Calendars.ReadWrite Mail.ReadWrite offline_access
```

---

### Step 4: verify

```bash
python scripts/microsoft_graph_delegated_setup.py --check --account clayduncan
```

Should exit 0 with `AUTHENTICATED`.

---

## Manual fallback (--auth-code)

If port 8765 is unavailable or the loopback flow fails, you can still complete auth manually:

1. If `--auth-url` saved a pending session before failing, skip to step 3.
2. Otherwise start fresh: run `--auth-url` (it will error on port bind, but will print a message).
3. Ask Clay to copy the full URL from the browser address bar after consent and paste it.
4. Run:
   ```bash
   python scripts/microsoft_graph_delegated_setup.py \
     --auth-code "PASTE_CODE_OR_FULL_URL_HERE" \
     --account clayduncan
   ```

This works because `--auth-url` always saves a pending session file before starting the listener.  The `--auth-code` path reads that pending file for the state/PKCE values it needs.

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
| `DelegatedGraphSetupNeeded` raised | No token file for this account | Run Steps 2–4 |
| `DelegatedGraphReconsentNeeded` raised | Refresh token revoked or expired | Run `--revoke` then Step 3 |
| `--configure` fails with missing env var | Secret not in `.env` or gateway not restarted | Add `MSGRAPH_<KEY>_CLIENT_SECRET` to `~/.hermes/.env` and restart gateway |
| `--auth-url` fails with "No configuration" | `--configure` was not run first | Run Step 2 |
| `--auth-url` fails with "Could not bind loopback listener on port 8765" | Port 8765 is in use | Find and stop whatever is using port 8765, or use the `--auth-code` manual fallback |
| `--auth-url` times out after 5 minutes | Browser flow not completed | Pending session is kept — try `--auth-code` manually, or re-run `--auth-url` |
| `--auth-code` fails with "state mismatch" | Stale pending session | Run `--auth-url` again to start a fresh session |
| Token exchange returns "invalid_grant" | Code expired or already used | Run `--auth-url` again (codes are single-use, expire in ~10 min) |
| `AADSTS700016: Application not found` | Wrong client_id or tenant_id | Verify values in Azure portal against Step 2 |
| `AADSTS700082: Consent was not granted` | User denied the consent screen | Ask Clay to re-open the URL and approve all permissions |
| `AADSTS9002326: Cross-origin token redemption` | Old nativeclient redirect + JS race | Confirm `http://localhost:8765/callback` is registered in Azure and re-run `--auth-url` |
| Browser redirects to `/common/wrongplace` | nativeclient URI is being used (old flow) | Confirm `http://localhost:8765/callback` is in Azure and re-run `--auth-url` |

---

## Adding a new account (Princeton, future)

1. Wait for IT to complete the Azure app registration and provide:
   - Tenant ID
   - Application (client) ID
   - Client secret value
   - Confirm that `http://localhost:8765/callback` has been added as a redirect URI
2. Add to `~/.hermes/.env`:
   ```
   MSGRAPH_PRINCETON_TENANT_ID=<tenant-id>
   MSGRAPH_PRINCETON_CLIENT_ID=<client-id>
   MSGRAPH_PRINCETON_CLIENT_SECRET=<secret>
   ```
3. Restart the gateway: `hermes gateway restart`
4. Follow Steps 1–4 above with `--account princeton`.
