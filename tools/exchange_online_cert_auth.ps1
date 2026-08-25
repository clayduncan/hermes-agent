#Requires -Version 7
<#
.SYNOPSIS
    Certificate-based app-only Exchange Online connection helper for macOS.

.DESCRIPTION
    Loads a PFX from disk as an ephemeral in-memory certificate, retrieves
    the PFX password from macOS Keychain, and connects to Exchange Online
    using the documented certificate-object form of Connect-ExchangeOnline.
    The private key is never installed into any certificate store.

.PARAMETER AppId
    The Entra application (client) ID authorised for Exchange.ManageAsApp.

.PARAMETER Organization
    The tenant's primary .onmicrosoft.com organisation domain.

.PARAMETER PfxPath
    Absolute path to the password-protected PFX file.

.PARAMETER KeychainService
    Name of the macOS Keychain generic-password service item that holds the
    PFX password.

.PARAMETER KeychainAccount
    Account name of the Keychain item.

.PARAMETER ProofMode
    When present, connects, runs only Get-OrganizationConfig | Out-Null,
    disconnects, and emits a bounded success/failure result. No payload is
    returned.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$AppId,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$Organization,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$PfxPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$KeychainService,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string]$KeychainAccount,
    [switch]$ProofMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── Prerequisite validation ────────────────────────────────────────────────────

if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    throw 'PowerShell 7 (pwsh) is required but not found on PATH.'
}

$securityBin = '/usr/bin/security'
if (-not (Test-Path $securityBin)) {
    throw "macOS Keychain binary not found at $securityBin."
}

$openssl = '/usr/bin/openssl'
if (-not (Test-Path $openssl)) {
    throw "openssl not found at $openssl."
}

if ($AppId -notmatch '^[0-9a-fA-F\-]{36}$') {
    throw "AppId does not look like a valid GUID: $AppId"
}

if ($Organization -notmatch '\.onmicrosoft\.com$') {
    throw "Organization must be a primary .onmicrosoft.com domain."
}

if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
    throw "PFX file not found: $PfxPath"
}

try {
    $module = Get-Module -Name ExchangeOnlineManagement -ListAvailable -ErrorAction Stop |
              Sort-Object Version -Descending |
              Select-Object -First 1
    if (-not $module) {
        throw 'ExchangeOnlineManagement module not found.'
    }
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
} catch {
    throw "ExchangeOnlineManagement is not installed or cannot be imported: $_"
}

# ── Load PFX password from Keychain ───────────────────────────────────────────

[System.Security.SecureString]$pfxSecure = $null
[System.Security.Cryptography.X509Certificates.X509Certificate2]$cert = $null

try {
    $rawPassword = & $securityBin find-generic-password -s $KeychainService -a $KeychainAccount -w 2>$null
    if (-not $rawPassword -or $rawPassword.Trim() -eq '') {
        throw "Keychain item not found or empty (service='$KeychainService', account='$KeychainAccount')."
    }

    $pfxSecure = ConvertTo-SecureString -String $rawPassword -AsPlainText -Force
    [Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode(
        [Runtime.InteropServices.Marshal]::StringToHGlobalUni($rawPassword)
    ) 2>$null
    $rawPassword = $null

    # ── Load PFX as ephemeral in-memory certificate ────────────────────────────

    $pfxBytes = [System.IO.File]::ReadAllBytes($PfxPath)

    # macOS-compatible default constructor: no key-storage flags.
    # EphemeralKeySet is rejected by PowerShell 7 on macOS at runtime.
    # PersistKeySet would install the private key into a persistent certificate store.
    # Platform-managed temporary key material is used only for the lifetime of this process.
    $cert = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
        $pfxBytes,
        $pfxSecure
    )

    if (-not $cert.HasPrivateKey) {
        throw 'Certificate loaded from PFX has no private key.'
    }

    if ($cert.Thumbprint -eq '' -or $null -eq $cert.Thumbprint) {
        throw 'Certificate thumbprint is empty; PFX may be corrupt.'
    }

    # ── Connect ────────────────────────────────────────────────────────────────

    Connect-ExchangeOnline `
        -Certificate $cert `
        -AppId       $AppId `
        -Organization $Organization `
        -ShowBanner:$false `
        -ErrorAction Stop

    # ── Proof mode ─────────────────────────────────────────────────────────────

    if ($ProofMode) {
        try {
            Get-OrganizationConfig -ErrorAction Stop | Out-Null
            Write-Output 'proof-ok'
        } catch {
            Write-Error "Proof command failed: $_"
            exit 1
        } finally {
            try { Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue } catch {}
        }
    }

} finally {
    # Clear sensitive variables regardless of outcome.
    if ($pfxSecure) { $pfxSecure.Dispose(); $pfxSecure = $null }
    if ($cert)      { $cert.Dispose();      $cert      = $null }
    [System.GC]::Collect()
    [System.GC]::WaitForPendingFinalizers()
}
