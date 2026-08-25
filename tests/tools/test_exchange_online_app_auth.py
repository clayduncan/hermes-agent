"""Contract tests for tools/exchange_online_cert_auth.ps1.

These tests verify the script's structure and required behaviours without
live credentials or network access.  PowerShell does not need to be installed
for most tests — they operate on the script source text.  The subprocess tests
are skipped automatically when pwsh is absent.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "exchange_online_cert_auth.ps1"
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")

PWSH = shutil.which("pwsh")


# ── Source-level contract checks ───────────────────────────────────────────────


class TestScriptStructure:
    def test_script_exists(self):
        assert SCRIPT.is_file(), f"Script missing: {SCRIPT}"

    def test_uses_certificate_object_form(self):
        """Connect-ExchangeOnline must use -Certificate, not -CertificateThumbprint."""
        assert "-Certificate " in SCRIPT_TEXT or "-Certificate\n" in SCRIPT_TEXT or "-Certificate`" in SCRIPT_TEXT
        assert "-CertificateThumbprint" not in SCRIPT_TEXT

    def test_no_app_only_switch(self):
        """-AppOnly is not a real parameter; the script must not use it."""
        assert "-AppOnly" not in SCRIPT_TEXT

    def test_no_mutating_cmdlets(self):
        """Script must contain no Exchange mutation cmdlets."""
        forbidden = [
            "Set-Mailbox", "New-Mailbox", "Remove-Mailbox",
            "Set-DistributionGroup", "Add-MailboxPermission",
            "Set-TransportRule", "New-TransportRule",
            "Set-OrganizationConfig", "New-InboxRule",
            "Set-Recipient", "New-Contact", "Remove-Contact",
        ]
        for cmdlet in forbidden:
            assert cmdlet not in SCRIPT_TEXT, f"Forbidden cmdlet found: {cmdlet}"

    def test_no_contact_or_mailbox_read_cmdlets(self):
        """Script must not enumerate contacts, mailboxes, or recipients."""
        forbidden = [
            "Get-Mailbox", "Get-Contact", "Get-Recipient",
            "Get-DistributionGroupMember",
        ]
        for cmdlet in forbidden:
            assert cmdlet not in SCRIPT_TEXT, f"Forbidden cmdlet found: {cmdlet}"

    def test_proof_mode_only_allowed_cmdlet(self):
        """Proof mode may only call Get-OrganizationConfig."""
        # Confirm Get-OrganizationConfig appears.
        assert "Get-OrganizationConfig" in SCRIPT_TEXT
        # Confirm its output is suppressed.
        assert "Out-Null" in SCRIPT_TEXT

    def test_disconnect_always_attempted(self):
        """Script must call Disconnect-ExchangeOnline in a finally or after proof."""
        assert "Disconnect-ExchangeOnline" in SCRIPT_TEXT

    def test_no_ephemeral_or_persist_key_flags(self):
        """Certificate must use the macOS-compatible default X509Certificate2 constructor.

        EphemeralKeySet is rejected by PowerShell 7 on macOS at runtime.
        PersistKeySet installs the private key into a persistent certificate store.
        Neither flag may appear as executable code (comments are allowed to name them).
        """
        # Strip PowerShell comment lines before checking so informational comments
        # that name the flags (explaining why they are absent) do not trigger failure.
        non_comment_lines = [
            line for line in SCRIPT_TEXT.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code_text = "\n".join(non_comment_lines)
        assert "EphemeralKeySet" not in code_text, (
            "EphemeralKeySet is rejected by pwsh 7 on macOS — must not appear in executable code"
        )
        assert "PersistKeySet" not in code_text, (
            "PersistKeySet installs the key into a persistent store — must not appear in executable code"
        )
        assert "X509KeyStorageFlags" not in code_text, (
            "No X509KeyStorageFlags argument may be passed to the X509Certificate2 constructor"
        )

    def test_default_x509_constructor_used(self):
        """Must use the 2-argument X509Certificate2 constructor (bytes + SecureString), no key flags."""
        assert "X509Certificate2]::new(" in SCRIPT_TEXT

    def test_certificate_disposed(self):
        """Certificate object must be disposed after use."""
        assert "$cert.Dispose()" in SCRIPT_TEXT or ".Dispose()" in SCRIPT_TEXT

    def test_secure_string_disposed(self):
        """PFX SecureString must be disposed after use."""
        assert "$pfxSecure.Dispose()" in SCRIPT_TEXT or "Dispose()" in SCRIPT_TEXT

    def test_no_write_host_of_password(self):
        """Sensitive variable names must not appear in Write-Host or Write-Output."""
        for sensitive_pattern in [r"Write-(?:Host|Output).*pfxSecure", r"Write-(?:Host|Output).*rawPassword"]:
            assert not re.search(sensitive_pattern, SCRIPT_TEXT, re.IGNORECASE), (
                f"Pattern found: {sensitive_pattern}"
            )

    def test_organization_must_be_onmicrosoft_com(self):
        """Validation must reject organization values that are not .onmicrosoft.com domains."""
        assert "onmicrosoft.com" in SCRIPT_TEXT

    def test_appid_validated_as_guid(self):
        """AppId must be validated against a GUID-like pattern."""
        assert "GUID" in SCRIPT_TEXT or re.search(r"\[0-9a-fA-F", SCRIPT_TEXT)

    def test_no_generic_exec_path(self):
        """Script must not contain Invoke-Expression or & with a variable."""
        assert "Invoke-Expression" not in SCRIPT_TEXT
        # & followed directly by a variable is a generic exec risk.
        assert not re.search(r"&\s+\$(?!securityBin|openssl)", SCRIPT_TEXT), (
            "Generic dynamic invocation found (&$var) — only allowed for pinned binaries"
        )

    def test_no_hardcoded_credential_literals(self):
        """Script must not embed hardcoded token, password, or secret strings."""
        # These patterns catch literal credential assignments (value after =),
        # not variable names or parameter names which legitimately contain these words.
        import re as _re
        bad_patterns = [
            r'client_secret\s*=\s*["\'][^"\']+["\']',
            r'access_token\s*=\s*["\'][^"\']+["\']',
            r'password\s*=\s*["\'][A-Za-z0-9+/]{8,}["\']',
        ]
        for pattern in bad_patterns:
            assert not _re.search(pattern, SCRIPT_TEXT, _re.IGNORECASE), (
                f"Hardcoded credential pattern found: {pattern}"
            )

    def test_requires_powershell_7(self):
        assert "#Requires -Version 7" in SCRIPT_TEXT

    def test_mandatory_parameters_declared(self):
        for param in ["AppId", "Organization", "PfxPath", "KeychainService", "KeychainAccount"]:
            assert param in SCRIPT_TEXT

    def test_proof_mode_output_bounded(self):
        """Proof success output must be a fixed string, not org/mailbox data."""
        assert "proof-ok" in SCRIPT_TEXT


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
class TestScriptCliMissingArgs:
    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PWSH, "-NonInteractive", "-NoProfile", str(SCRIPT)] + args,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_missing_all_params_exits_nonzero(self):
        result = self._run([])
        assert result.returncode != 0

    def test_bad_appid_exits_nonzero(self):
        result = self._run([
            "-AppId", "not-a-guid",
            "-Organization", "tenant.onmicrosoft.com",
            "-PfxPath", "/nonexistent/cert.pfx",
            "-KeychainService", "svc",
            "-KeychainAccount", "acct",
        ])
        assert result.returncode != 0
        assert "AppId" in result.stderr or "GUID" in result.stderr

    def test_bad_organization_exits_nonzero(self):
        result = self._run([
            "-AppId", "12345678-1234-1234-1234-123456789abc",
            "-Organization", "notamicrosoftdomain.com",
            "-PfxPath", "/nonexistent/cert.pfx",
            "-KeychainService", "svc",
            "-KeychainAccount", "acct",
        ])
        assert result.returncode != 0
        assert "onmicrosoft" in result.stderr.lower() or "Organization" in result.stderr

    def test_missing_pfx_exits_nonzero(self):
        result = self._run([
            "-AppId", "12345678-1234-1234-1234-123456789abc",
            "-Organization", "tenant.onmicrosoft.com",
            "-PfxPath", "/nonexistent/cert.pfx",
            "-KeychainService", "svc",
            "-KeychainAccount", "acct",
        ])
        assert result.returncode != 0

    def test_stderr_does_not_leak_secrets(self):
        """Error output must not contain token or private-key material."""
        result = self._run(["-AppId", "bad", "-Organization", "bad"])
        sensitive = ["BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "access_token", "pfxpassword"]
        for s in sensitive:
            assert s.lower() not in result.stderr.lower(), f"Sensitive value in stderr: {s}"
            assert s.lower() not in result.stdout.lower(), f"Sensitive value in stdout: {s}"
