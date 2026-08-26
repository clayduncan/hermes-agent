"""team_duncan_contacts plugin: top-level test entry point.

The full test suite lives in tests/plugins/team_duncan_contacts/test_registry.py.
This file re-exports all test classes so the standard validator invocation:

    pytest tests/plugins/test_team_duncan_contacts.py

collects and runs the complete suite without duplication.
"""

import sys
sys.dont_write_bytecode = True  # prevent .pyc files in validator-scanned dirs

from tests.plugins.team_duncan_contacts.test_registry import (  # noqa: F401
    TestSchemaInputRejection,
    TestPrepareReturnsMaskedOnly,
    TestPrepareMatchSemantics,
    TestContactCreationRequired,
    TestConfirmationTimestamp,
    TestConfirmationIdempotency,
    TestActivationTimestampImmutability,
    TestCutoffBoundary,
    TestLifecycle,
    TestHmacMappingEdgeCases,
    TestCanaryPhoneAbsence,
    TestFilePermissions,
    TestRestartPersistence,
    TestNoGhlWriteAccess,
    TestSanitizer,
    TestConfigRouting,
    TestExceptionCanary,
    TestNoEmDash,
)
