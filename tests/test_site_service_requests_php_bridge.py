from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.site_service_requests_auth import content_sha256, sign_site_request

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_DIR = ROOT / "integrations" / "master_mobile_site"
BRIDGE = BRIDGE_DIR / "service_ticket_bridge.php"
COMPONENT_PARAMS = BRIDGE_DIR / "service_ticket_component_params.php"
FIXTURES = BRIDGE_DIR / "fixtures"
PHP = shutil.which("php")


def test_bridge_is_inert_until_explicit_rollout_calls() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS" in source
    assert "INSERT IGNORE INTO" in source
    assert "OnAfterTicketAdd" in source
    assert "OnAfterTicketUpdate" in source
    assert "X-MM-Site-Signature" in source
    assert "EXTERNAL_FIELD_1" in source
    assert "MESSAGE_AUTHOR_USER_ID" in source
    assert "MESSAGE_CREATED_USER_ID" in source
    assert "OPTION_EMIT_ENABLED" in source
    assert "OPTION_OUTBOUND_ENABLED" in source
    assert "ServiceTicketBridge::registerHandlers();" not in source
    assert "ServiceTicketBridge::installSchema();" not in source
    assert "Bitrix\\Main\\Config\\Option::set" not in source
    assert "'support-team'" in source
    assert "'isVisibleToCustomer'" in source
    assert "`IS_HIDDEN`" in source
    assert " AND `ID` <= " in source
    assert "'leaseToken' => $leaseToken" in source
    assert "payloadFileIsUnavailable" in source
    assert "X-MM-Site-File-Error: file_unavailable" in source
    assert "str_repeat('0', 64)" in source
    assert "LEFT JOIN `b_file`" in source
    assert "`ATTACHED_FILE_ID`" in source
    assert "rawurlencode((string) $eventKey)" not in source
    assert "filename*=UTF-8" in source
    assert "file_response_mismatch" in source
    assert "file_error_report_response_mismatch" in source
    assert source.count("throw new BridgeFailure('file_api_unavailable');") == 1
    assert source.count("throw new BridgeFailure('file_hash_failed');") == 1
    assert "array(\n                                'file_not_found'" not in source
    assert "$isPermanentHttpError" in source
    assert "array(408, 413, 425, 429)" in source


def test_bridge_hardening_keeps_ambiguous_writes_and_field_repair_idempotent() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert source.count("findExistingCommandMessageId($ticketId, $marker)") >= 3
    assert "support_user_field_update_failed" in source
    assert "ensureSupportFieldEnum" in source
    assert "support_user_field_enum_readback_failed" in source


def test_event_handlers_preserve_bitrix_event_chain_contract() -> None:
    source = BRIDGE.read_text(encoding="utf-8")

    assert source.count("? $arguments[0]") == 2
    assert source.count(": array();") >= 2


def test_support_fields_are_declared_and_exposed_to_component() -> None:
    bridge_source = BRIDGE.read_text(encoding="utf-8")
    component_source = COMPONENT_PARAMS.read_text(encoding="utf-8")
    expected_fields = {
        "UF_MM_SERVICE_PHONE",
        "UF_MM_SERVICE_ORDER_NUMBER",
        "UF_MM_SERVICE_REQUEST_TYPE",
    }

    assert "SET_SHOW_USER_FIELD" in component_source
    for field_name in expected_fields:
        assert field_name in bridge_source
        assert field_name in component_source
    assert bridge_source.count("'MANDATORY' => 'Y'") == 2


def test_php_signature_fixture_matches_python_contract_without_php_runtime() -> None:
    fixture_source = (FIXTURES / "signature_fixture.php").read_text(encoding="utf-8")
    expected_hash = re.search(r"\$expectedHash = '([0-9a-f]{64})'", fixture_source)
    expected_signature = re.search(r"\$expectedSignature = '(v1=[0-9a-f]{64})'", fixture_source)
    assert expected_hash is not None
    assert expected_signature is not None

    body = b'{"schemaVersion":1,"eventId":"site-support:741:1201"}'
    digest = content_sha256(body)
    signature = sign_site_request(
        secret="test-only-site-service-secret",
        timestamp=1787389200,
        nonce="11111111-1111-4111-8111-111111111111",
        method="POST",
        path="/api/internal/site-service-requests/events",
        body_sha256=digest,
    )

    assert digest == expected_hash.group(1)
    assert signature == expected_signature.group(1)


def test_static_fixtures_cover_ddl_event_and_command_dedupe() -> None:
    ddl_source = (FIXTURES / "ddl_dry_run_fixture.php").read_text(encoding="utf-8")
    event_source = (FIXTURES / "event_extraction_fixture.php").read_text(encoding="utf-8")
    command_source = (FIXTURES / "command_duplicate_fixture.php").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS" in ddl_source
    assert "ux_mm_service_ticket_event_key" in ddl_source
    assert "OnAfterTicketUpdate" in event_source
    assert "'ticketId' => 741" in event_source
    assert "'messageId' => 1201" in event_source
    assert "mm-site-service-command:42" in command_source
    assert "findCommandMarkerInRows(42" in command_source


@pytest.mark.skipif(PHP is None, reason="php binary is not installed on this host")
@pytest.mark.parametrize(
    "path",
    [BRIDGE, COMPONENT_PARAMS, *sorted(FIXTURES.glob("*.php"))],
)
def test_php_source_lints(path: Path) -> None:
    result = subprocess.run(
        [str(PHP), "-l", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(PHP is None, reason="php binary is not installed on this host")
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "signature_fixture.php",
            {
                "contentSha256": (
                    "70de45086e16dfc8050b399626b7afc7f8f84084d33ff84ae33b182b21f552c9"
                ),
                "signature": (
                    "v1=a38241ff26b8956d165cd6f931299942a97b47dcea27ad00f61cbac62cf68b09"
                ),
            },
        ),
        (
            "event_extraction_fixture.php",
            {"ticketId": 741, "messageId": 1201, "eventType": "message.created"},
        ),
        ("command_duplicate_fixture.php", {"messageId": 8002}),
        (
            "ticket_set_contract_fixture.php",
            {
                "ticketId": 741,
                "checkRights": "N",
                "sendEmailToAuthor": "N",
                "sendEmailToTechsupport": "N",
                "fieldTicketId": 741,
            },
        ),
    ],
)
def test_php_contract_fixture_executes(fixture: str, expected: dict[str, object]) -> None:
    result = subprocess.run(
        [str(PHP), str(FIXTURES / fixture)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == expected
