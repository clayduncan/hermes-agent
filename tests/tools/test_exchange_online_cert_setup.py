"""Tests for scripts/setup_exchange_cert.sh.

All tests run without live credentials, network access, or Keychain mutation.
Subprocess calls to openssl and security are intercepted by PATH injection of
stub executables that record invocations and return controlled outputs.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup_exchange_cert.sh"


def _make_stubs(tmp_path: Path, *, keychain_exists: bool = False, openssl_path: str = "/usr/bin/openssl", security_path: str = "/usr/bin/security") -> Path:
    """Write stub binaries for openssl and security into tmp_path/bin/."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()

    # openssl stub: handles the subcommands used by the script.
    openssl_stub = stub_dir / "openssl"
    openssl_stub.write_text(
        textwrap.dedent("""\
        #!/usr/bin/env bash
        # Record invocation for inspection.
        echo "openssl $*" >> "$STUB_LOG"
        # Helper: find the argument following -out.
        _out_arg() {
            local prev="" cur=""
            for i in "$@"; do
                cur="$i"
                if [[ "$prev" == "-out" ]]; then echo "$cur"; return; fi
                prev="$cur"
            done
        }
        case "$1" in
            rand)   if [[ "$*" == *"-base64"* ]]; then echo "FAKEPW48AAAA"; fi ;;
            genrsa)
                    outfile="$(_out_arg "$@")"
                    if [[ -n "$outfile" ]]; then touch "$outfile"; fi
                    ;;
            req)    outfile="$(_out_arg "$@")"
                    if [[ -n "$outfile" ]]; then
                        printf '-----BEGIN CERTIFICATE-----\\nFAKECERTDATA\\n-----END CERTIFICATE-----\\n' > "$outfile"
                    fi
                    ;;
            pkcs12)
                    if [[ "${STUB_PKCS12_FAIL:-0}" == "1" ]]; then exit 1; fi
                    outfile="$(_out_arg "$@")"
                    if [[ -n "$outfile" ]]; then echo "FAKEPFXDATA" > "$outfile"; fi
                    ;;
            x509)
                if [[ "$*" == *"-fingerprint"* ]]; then
                    echo "SHA1 Fingerprint=AA:BB:CC:DD:EE"
                elif [[ "$*" == *"-enddate"* ]]; then
                    echo "notAfter=Aug 25 00:00:00 2027 GMT"
                else
                    outfile="$(_out_arg "$@")"
                    if [[ -n "$outfile" ]]; then echo "FAKEPUBCERT" > "$outfile"; fi
                fi
                ;;
        esac
        exit 0
        """)
    )
    openssl_stub.chmod(openssl_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # security stub.
    exists_exit = "0" if keychain_exists else "1"
    security_stub = stub_dir / "security"
    security_stub.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        echo "security $*" >> "$STUB_LOG"
        case "$1" in
            find-generic-password) exit {exists_exit} ;;
            add-generic-password)  exit 0 ;;
            delete-generic-password) exit 0 ;;
            *) exit 0 ;;
        esac
        """)
    )
    security_stub.chmod(security_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return stub_dir


def _run_script(
    args: list[str],
    *,
    stub_dir: Path,
    stub_log: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:/usr/bin:/bin"
    env["STUB_LOG"] = str(stub_log)
    # Override binary paths so the script uses stubs even though it resolves
    # them from env vars rather than PATH (the env vars take precedence).
    env["OPENSSL"] = str(stub_dir / "openssl")
    env["SECURITY"] = str(stub_dir / "security")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ── Script exists ──────────────────────────────────────────────────────────────


def test_script_exists():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)


# ── Dry-run behaviour ──────────────────────────────────────────────────────────


class TestDryRun:
    def test_no_apply_creates_no_files(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target)],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0, result.stderr
        assert not target.exists(), "Target dir should not be created in dry-run"
        # No openssl or security calls in dry-run.
        log_text = stub_log.read_text() if stub_log.exists() else ""
        assert "openssl genrsa" not in log_text
        assert "security add-generic-password" not in log_text

    def test_dry_run_prints_plan_not_secrets(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--keychain-service", "svc"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0
        assert "Plan:" in result.stdout
        assert "svc" in result.stdout
        # No password or private key material.
        assert "BEGIN PRIVATE KEY" not in result.stdout
        assert "BEGIN RSA PRIVATE KEY" not in result.stdout

    def test_missing_target_dir_fails(self, tmp_path):
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"
        result = _run_script([], stub_dir=stub_dir, stub_log=stub_log)
        assert result.returncode != 0
        assert "--target-dir" in result.stderr or "required" in result.stderr


# ── Apply mode ─────────────────────────────────────────────────────────────────


class TestApplyMode:
    def test_apply_creates_artifacts(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0, result.stderr
        assert (target / "exchange_app.pfx").exists()
        assert (target / "exchange_app_public.crt").exists()

    def test_apply_calls_openssl_genrsa(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        _run_script(["--target-dir", str(target), "--apply"], stub_dir=stub_dir, stub_log=stub_log)

        log = stub_log.read_text()
        assert "openssl genrsa" in log

    def test_apply_calls_security_add(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        _run_script(["--target-dir", str(target), "--apply"], stub_dir=stub_dir, stub_log=stub_log)

        log = stub_log.read_text()
        assert "security add-generic-password" in log

    def test_apply_output_has_no_secret_material(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        for stream in (result.stdout, result.stderr):
            assert "BEGIN PRIVATE KEY" not in stream
            assert "BEGIN RSA PRIVATE KEY" not in stream
            assert "FAKEPW48" not in stream, "PFX password must not appear in output"

    def test_apply_pfx_passout_uses_env_not_cmdline(self, tmp_path):
        """openssl pkcs12 -passout must use env: form, not pass: with literal password."""
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        _run_script(["--target-dir", str(target), "--apply"], stub_dir=stub_dir, stub_log=stub_log)

        log = stub_log.read_text()
        # env: prefix is required; plain pass: with a literal value is forbidden.
        pkcs12_lines = [l for l in log.splitlines() if "pkcs12" in l]
        assert pkcs12_lines, "pkcs12 command not logged"
        for line in pkcs12_lines:
            assert "env:PFXPASSWORD" in line or "env:" in line, (
                f"pkcs12 passout does not use env: form: {line}"
            )
            assert "-passout pass:" not in line, "Literal pass: found in pkcs12 cmdline"


# ── Overwrite protection ───────────────────────────────────────────────────────


class TestOverwriteProtection:
    def test_existing_pfx_blocks_apply_without_replace(self, tmp_path):
        target = tmp_path / "certs"
        target.mkdir()
        (target / "exchange_app.pfx").write_text("existing")

        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode != 0
        assert "already exists" in result.stderr or "replace" in result.stderr.lower()

    def test_existing_cert_blocks_apply_without_replace(self, tmp_path):
        target = tmp_path / "certs"
        target.mkdir()
        (target / "exchange_app.crt").write_text("existing")

        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode != 0

    def test_existing_keychain_item_blocks_apply_without_replace(self, tmp_path):
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path, keychain_exists=True)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode != 0
        assert "Keychain" in result.stderr or "already exists" in result.stderr

    def test_replace_flag_allows_overwrite(self, tmp_path):
        target = tmp_path / "certs"
        target.mkdir()
        (target / "exchange_app.pfx").write_text("existing")
        (target / "exchange_app.crt").write_text("existing")
        (target / "exchange_app_public.crt").write_text("existing")

        stub_dir = _make_stubs(tmp_path, keychain_exists=True)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply", "--replace"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0, result.stderr


# ── Repo boundary guard ────────────────────────────────────────────────────────


class TestRepoBoundary:
    def test_target_inside_repo_is_rejected(self, tmp_path):
        repo_root = Path(SCRIPT).resolve().parents[1]
        target = repo_root / "tmp_certs_should_not_exist"

        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode != 0
        assert "outside" in result.stderr or "repository" in result.stderr


# ── Missing tool prerequisites ─────────────────────────────────────────────────


class TestMissingPrerequisites:
    def test_missing_openssl_fails(self, tmp_path):
        target = tmp_path / "certs"
        # Point OPENSSL at a nonexistent binary so the prerequisite check fails.
        stub_log = tmp_path / "stubs.log"

        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        env["STUB_LOG"] = str(stub_log)
        env["OPENSSL"] = "/nonexistent/openssl"
        env["SECURITY"] = "/usr/bin/security"

        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--target-dir", str(target), "--apply"],
            capture_output=True, text=True, env=env, timeout=15,
        )

        assert result.returncode != 0
        assert "openssl" in result.stderr.lower()


# ── Temp key cleanup ───────────────────────────────────────────────────────────


class TestTmpKeyCleanup:
    def test_tmp_key_removed_on_success(self, tmp_path):
        """Temp unencrypted private key (.exchange_app_key_tmp.pem) is absent after successful apply."""
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0, result.stderr
        assert not (target / ".exchange_app_key_tmp.pem").exists(), (
            "Temp unencrypted private key must be removed after successful apply"
        )

    def test_tmp_key_removed_on_pfx_failure(self, tmp_path):
        """Temp unencrypted private key is cleaned up via EXIT trap when PFX export fails."""
        target = tmp_path / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target), "--apply"],
            stub_dir=stub_dir,
            stub_log=stub_log,
            env_extra={"STUB_PKCS12_FAIL": "1"},
        )

        assert result.returncode != 0, "Script should fail when PFX export fails"
        assert not (target / ".exchange_app_key_tmp.pem").exists(), (
            "Temp unencrypted private key must be cleaned up via EXIT trap even when PFX export fails"
        )


# ── Target directory path resolution ──────────────────────────────────────────


class TestTargetDirResolution:
    def test_nonexistent_nested_parent_resolves_to_correct_path(self, tmp_path):
        """Path with non-existent parent resolves correctly rather than collapsing to /<basename>."""
        # nested_dir does not exist — previously this caused TARGET_DIR to collapse to /certs.
        target = tmp_path / "nested_dir" / "certs"
        stub_dir = _make_stubs(tmp_path)
        stub_log = tmp_path / "stubs.log"

        result = _run_script(
            ["--target-dir", str(target)],
            stub_dir=stub_dir,
            stub_log=stub_log,
        )

        assert result.returncode == 0, result.stderr
        assert str(target) in result.stdout, (
            f"Expected full path {target!s} in Plan output, got: {result.stdout!r}"
        )
