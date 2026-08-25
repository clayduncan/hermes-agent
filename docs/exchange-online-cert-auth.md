# Exchange Online Certificate-Based App-Only Auth

## 1. Local setup

Run the certificate setup helper from outside the repository, using an explicit
target directory that will not be committed.  The default mode is a dry run;
pass `--apply` to create artifacts.

```sh
# Preview what will be created (no files written):
./scripts/setup_exchange_cert.sh \
  --target-dir ~/exchange-certs \
  --common-name "ExchangeAppOnly" \
  --keychain-service "hermes-exchange-pfx" \
  --keychain-account "exchange-app-only"

# Create the certificate, PFX, and Keychain entry:
./scripts/setup_exchange_cert.sh \
  --target-dir ~/exchange-certs \
  --common-name "ExchangeAppOnly" \
  --keychain-service "hermes-exchange-pfx" \
  --keychain-account "exchange-app-only" \
  --apply
```

The helper prints the thumbprint, expiry, and output paths.  The PFX password
is stored only in Keychain and is never printed.  The temporary private-key
file is overwritten and deleted before the script exits.

To overwrite an existing certificate, add `--replace`.

## 2. Tenant prerequisites

These steps are performed by the operator after the local certificate is ready.
They require access to the Entra admin centre and Exchange admin centre and must
not be automated.

1. **Upload the public certificate** (`~/exchange-certs/exchange_app_public.crt`)
   to the existing Entra app registration under *Certificates & secrets →
   Certificates*.  Do not create a second app registration.

2. **Confirm the application permission** `Office 365 Exchange Online →
   Exchange.ManageAsApp` is present and admin-consented on the existing app.

3. **Assign an Exchange role** to the app's service principal using the minimum
   role compatible with the required administration path (typically *Exchange
   Administrator* or a custom role scoped to the required operations).  Verify
   with the security team before assigning broader roles.

If any of these steps cannot be completed safely, stop and report the blocker
before proceeding.

## 3. Read-only proof command

After the tenant prerequisites are complete, verify connectivity without
mutating any Exchange data:

```powershell
pwsh -NonInteractive -NoProfile -File tools/exchange_online_cert_auth.ps1 `
  -AppId       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" `
  -Organization "contoso.onmicrosoft.com" `
  -PfxPath     "$HOME/exchange-certs/exchange_app.pfx" `
  -KeychainService "hermes-exchange-pfx" `
  -KeychainAccount "exchange-app-only" `
  -ProofMode
```

Expected output on success: `proof-ok`

Proof mode connects, runs only `Get-OrganizationConfig -ErrorAction Stop |
Out-Null` (suppressing all returned data), disconnects, and exits.  No
organisation or mailbox payload is emitted.  Any other output indicates a
failure; review the error and do not proceed until it is resolved.

## 4. Local credential teardown

To remove all local credential material:

```sh
# Remove the PFX and certificate files:
rm -f ~/exchange-certs/exchange_app.pfx \
       ~/exchange-certs/exchange_app.crt \
       ~/exchange-certs/exchange_app_public.crt

# Remove the Keychain entry:
/usr/bin/security delete-generic-password \
  -s "hermes-exchange-pfx" \
  -a "exchange-app-only"
```

After teardown, remove the public certificate from the Entra app registration
manually via the admin centre.  The connection helper will fail closed if the
Keychain entry or PFX is absent.
