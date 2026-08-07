from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.exporters.ut103_nomenclature_classifications import (
    NomenclatureClassificationIntentRow,
    NomenclatureClassificationUpdateMessage,
    OneCClassificationReference,
    build_nomenclature_classification_message_id,
    build_nomenclature_classification_updates_xml,
    parse_nomenclature_classification_exchange_result,
    prepare_nomenclature_classification_command,
    result_fingerprint,
    validate_nomenclature_classification_result,
    write_nomenclature_classification_updates_message,
)

KIND_OLD = "11111111-1111-1111-1111-111111111111"
KIND_NEW = "22222222-2222-2222-2222-222222222222"
GROUP_OLD = "33333333-3333-3333-3333-333333333333"
GROUP_NEW = "44444444-4444-4444-4444-444444444444"
CATEGORY_OLD = "55555555-5555-5555-5555-555555555555"
CATEGORY_NEW = "66666666-6666-6666-6666-666666666666"
NOMENCLATURE = "77777777-7777-7777-7777-777777777777"
UNRELATED_CATEGORY = "88888888-8888-8888-8888-888888888888"
OPERATION_ID = "99999999-9999-4999-8999-999999999999"


def _intent(
    *, key: str = "nom-class:РБ000001:decision-42:r1"
) -> NomenclatureClassificationIntentRow:
    return NomenclatureClassificationIntentRow(
        idempotency_key=key,
        nomenclature_code="РБ000001",
        nomenclature_guid=NOMENCLATURE,
        expected_kind=OneCClassificationReference(KIND_OLD, "OLD-KIND", "Старый вид"),
        target_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND", "Новый вид"),
        expected_group=OneCClassificationReference(GROUP_OLD, "OLD-GROUP", "Старая группа"),
        target_group=OneCClassificationReference(GROUP_NEW, "NEW-GROUP", "Новая группа"),
        expected_category=OneCClassificationReference(
            CATEGORY_OLD, "OLD-CATEGORY", "Старая категория"
        ),
        target_category=OneCClassificationReference(
            CATEGORY_NEW, "NEW-CATEGORY", "Новая категория"
        ),
        group_mode="set",
        category_mode="replace_expected",
        reason="Утверждённое наведение порядка",
    )


def _message(mode: str = "dry_run") -> NomenclatureClassificationUpdateMessage:
    rows, command_hash, _ = prepare_nomenclature_classification_command(
        (_intent(),), approved_by="115204"
    )
    return NomenclatureClassificationUpdateMessage(
        operation_id=OPERATION_ID,
        command_hash=command_hash,
        message_id=build_nomenclature_classification_message_id(OPERATION_ID, command_hash, mode),
        rows=rows,
        approved_by="115204",
        mode=mode,
    )


def _result_xml(
    message: NomenclatureClassificationUpdateMessage, *, mode: str | None = None
) -> str:
    row = message.rows[0]
    result_mode = mode or message.mode
    result_status = "validated" if result_mode == "dry_run" else "applied"
    old_categories = [UNRELATED_CATEGORY]
    if row.expected_category.guid:
        old_categories.insert(0, row.expected_category.guid)
    projected_categories = list(old_categories)
    if row.category_mode in {"replace_expected", "remove_expected"}:
        projected_categories = [
            guid for guid in projected_categories if guid != row.expected_category.guid
        ]
    if (
        row.category_mode != "remove_expected"
        and row.target_category.guid not in projected_categories
    ):
        projected_categories.insert(0, row.target_category.guid)
    readback_categories = old_categories if result_mode == "dry_run" else projected_categories
    readback_kind = row.expected_kind.guid if result_mode == "dry_run" else row.target_kind.guid
    readback_group = row.expected_group.guid if result_mode == "dry_run" else row.target_group.guid
    return f"""<?xml version="1.0" encoding="windows-1251"?>
<ExchangeResult>
  <OperationId>{message.operation_id}</OperationId>
  <MessageId>{message.message_id}</MessageId>
  <Schema>nomenclature_classification_updates.v3</Schema>
  <Mode>{result_mode}</Mode><CommandHash>{message.command_hash}</CommandHash>
  <Status>success</Status><ProcessedAt>2026-08-04T12:00:00</ProcessedAt>
  <Loaded>1</Loaded><Failed>0</Failed><Errors></Errors>
  <ItemResults><ItemResult>
    <IdempotencyKey>{row.idempotency_key}</IdempotencyKey>
    <DecisionHash>{row.decision_hash}</DecisionHash>
    <NomenclatureCode>{row.nomenclature_code}</NomenclatureCode>
    <NomenclatureGuid>{row.nomenclature_guid}</NomenclatureGuid>
    <ExpectedKindGuid>{row.expected_kind.guid}</ExpectedKindGuid><ExpectedKindCode>{row.expected_kind.code}</ExpectedKindCode>
    <TargetKindGuid>{row.target_kind.guid}</TargetKindGuid><TargetKindCode>{row.target_kind.code}</TargetKindCode>
    <ExpectedGroupGuid>{row.expected_group.guid}</ExpectedGroupGuid><ExpectedGroupCode>{row.expected_group.code}</ExpectedGroupCode>
    <TargetGroupGuid>{row.target_group.guid}</TargetGroupGuid><TargetGroupCode>{row.target_group.code}</TargetGroupCode>
    <GroupMode>{row.group_mode}</GroupMode><CategoryMode>{row.category_mode}</CategoryMode>
    <ExpectedCategoryGuid>{row.expected_category.guid}</ExpectedCategoryGuid><ExpectedCategoryCode>{row.expected_category.code}</ExpectedCategoryCode>
    <TargetCategoryGuid>{row.target_category.guid}</TargetCategoryGuid><TargetCategoryCode>{row.target_category.code}</TargetCategoryCode>
    <Result>{result_status}</Result><Message>OK</Message>
    <OldKindGuid>{row.expected_kind.guid}</OldKindGuid>
    <ReadbackKindGuid>{readback_kind}</ReadbackKindGuid>
    <OldGroupGuid>{row.expected_group.guid}</OldGroupGuid>
    <ReadbackGroupGuid>{readback_group}</ReadbackGroupGuid>
    <OldCategoryGuids>{';'.join(old_categories)}</OldCategoryGuids>
    <ProjectedCategoryGuids>{';'.join(projected_categories)}</ProjectedCategoryGuids>
    <ReadbackCategoryGuids>{';'.join(readback_categories)}</ReadbackCategoryGuids>
  </ItemResult></ItemResults>
</ExchangeResult>"""


def test_service_computes_stable_hash_and_v3_message_identity() -> None:
    message = _message()
    root = ET.fromstring(build_nomenclature_classification_updates_xml(message))

    assert root.findtext("Header/Schema") == "nomenclature_classification_updates.v3"
    assert root.findtext("Header/OperationId") == OPERATION_ID
    assert root.findtext("Header/Mode") == "dry_run"
    assert root.findtext("Header/CommandHash") == message.command_hash
    assert root.findtext("Header/ApprovedBy") == "115204"
    assert root.find("Items/Item/ApprovedBy") is None
    assert root.findtext("Items/Item/DecisionHash") == message.rows[0].decision_hash
    assert root.findtext("Items/Item/GroupMode") == "set"
    assert message.message_id.endswith("-dry-run")


def test_hash_changes_for_every_semantic_change_and_not_input_order() -> None:
    base = _intent()
    second = replace(
        _intent(key="nom-class:РБ000002:decision-42:r1"),
        nomenclature_code="РБ000002",
        nomenclature_guid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    rows_a, hash_a, _ = prepare_nomenclature_classification_command(
        (base, second), approved_by="115204"
    )
    rows_b, hash_b, _ = prepare_nomenclature_classification_command(
        (second, base), approved_by="115204"
    )
    semantic_changes = (
        replace(base, nomenclature_code="РБ000009"),
        replace(base, nomenclature_guid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        replace(
            base,
            expected_kind=replace(
                base.expected_kind,
                guid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            ),
        ),
        replace(
            base,
            target_kind=replace(
                base.target_kind,
                guid="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            ),
        ),
        replace(
            base,
            expected_group=replace(
                base.expected_group,
                guid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            ),
        ),
        replace(
            base,
            target_group=replace(
                base.target_group,
                guid="ffffffff-ffff-4fff-8fff-ffffffffffff",
            ),
        ),
        replace(base, category_mode="ensure_present"),
        replace(
            base,
            expected_category=replace(
                base.expected_category,
                guid="00000000-0000-4000-8000-000000000001",
            ),
        ),
        replace(
            base,
            target_category=replace(
                base.target_category,
                guid="00000000-0000-4000-8000-000000000002",
            ),
        ),
        replace(base, reason="Другая причина"),
    )

    assert rows_a == rows_b
    assert hash_a == hash_b
    for changed in semantic_changes:
        changed_rows, changed_hash, _ = prepare_nomenclature_classification_command(
            (changed, second), approved_by="115204"
        )
        assert changed_hash != hash_a
        assert changed_rows[0].decision_hash != rows_a[0].decision_hash

    changed_approver_rows, changed_approver_hash, _ = prepare_nomenclature_classification_command(
        (base, second), approved_by="other"
    )
    assert changed_approver_hash != hash_a
    assert changed_approver_rows[0].decision_hash != rows_a[0].decision_hash

    recovery = replace(
        base,
        group_mode="clear_expected",
        target_group=OneCClassificationReference(),
    )
    recovery_rows, recovery_hash, recovery_payload = prepare_nomenclature_classification_command(
        (recovery, second), approved_by="115204"
    )
    assert recovery_hash != hash_a
    assert recovery_rows[0].decision_hash != rows_a[0].decision_hash
    assert recovery_payload["items"][0]["group_mode"] == "clear_expected"


def test_hash_canonicalizes_whitespace_guid_case_and_row_order() -> None:
    base = _intent()
    normalized_rows, normalized_hash, normalized_payload = (
        prepare_nomenclature_classification_command((base,), approved_by="115204")
    )
    noisy = replace(
        base,
        idempotency_key=f"  {base.idempotency_key}\t",
        nomenclature_code="\nРБ000001  ",
        nomenclature_guid=NOMENCLATURE.upper(),
        expected_kind=replace(
            base.expected_kind,
            guid=KIND_OLD.upper(),
            code=" OLD-KIND ",
            name="  Старый   вид ",
        ),
        target_group=replace(
            base.target_group,
            name=" Новая\tгруппа ",
        ),
        reason=" Утверждённое   наведение\nпорядка ",
    )
    noisy_rows, noisy_hash, noisy_payload = prepare_nomenclature_classification_command(
        (noisy,),
        approved_by="  115204\t",
        source=" pricing-service ",
        target=" 1c_ut_10_3\n",
    )

    assert noisy_rows == normalized_rows
    assert noisy_hash == normalized_hash
    assert noisy_payload == normalized_payload


def test_result_requires_exact_mode_identity_and_full_category_preservation(
    tmp_path: Path,
) -> None:
    message = _message()
    result_path = tmp_path / "result.xml"
    result_path.write_text(_result_xml(message), encoding="windows-1251")
    result = parse_nomenclature_classification_exchange_result(result_path)
    validate_nomenclature_classification_result(message, result)

    wrong_mode_path = tmp_path / "wrong-mode.xml"
    wrong_mode_path.write_text(_result_xml(message, mode="apply"), encoding="windows-1251")
    wrong_mode = parse_nomenclature_classification_exchange_result(wrong_mode_path)
    with pytest.raises(ValueError, match="header"):
        validate_nomenclature_classification_result(message, wrong_mode)

    missing_category_path = tmp_path / "missing-category.xml"
    missing_category_path.write_text(
        _result_xml(message).replace(
            f"<ProjectedCategoryGuids>{CATEGORY_NEW};{UNRELATED_CATEGORY}</ProjectedCategoryGuids>",
            f"<ProjectedCategoryGuids>{CATEGORY_NEW}</ProjectedCategoryGuids>",
        ),
        encoding="windows-1251",
    )
    missing_category = parse_nomenclature_classification_exchange_result(missing_category_path)
    with pytest.raises(ValueError, match="projected categories"):
        validate_nomenclature_classification_result(message, missing_category)

    other_product_path = tmp_path / "other-product.xml"
    other_product_path.write_text(
        _result_xml(message).replace(NOMENCLATURE, OPERATION_ID),
        encoding="windows-1251",
    )
    other_product = parse_nomenclature_classification_exchange_result(other_product_path)
    with pytest.raises(ValueError, match="result identity"):
        validate_nomenclature_classification_result(message, other_product)

    other_expected_path = tmp_path / "other-expected.xml"
    other_expected_path.write_text(
        _result_xml(message).replace(
            f"<ExpectedKindGuid>{KIND_OLD}</ExpectedKindGuid>",
            f"<ExpectedKindGuid>{GROUP_OLD}</ExpectedKindGuid>",
        ),
        encoding="windows-1251",
    )
    other_expected = parse_nomenclature_classification_exchange_result(other_expected_path)
    with pytest.raises(ValueError, match="result identity"):
        validate_nomenclature_classification_result(message, other_expected)


def test_clear_group_and_remove_expected_category_have_exact_empty_targets(
    tmp_path: Path,
) -> None:
    recovery_intent = replace(
        _intent(),
        idempotency_key="nom-class:РБ000001:restore:r2",
        expected_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND"),
        target_kind=OneCClassificationReference(KIND_NEW, "NEW-KIND"),
        expected_group=OneCClassificationReference(GROUP_NEW, "NEW-GROUP"),
        target_group=OneCClassificationReference(),
        group_mode="clear_expected",
        expected_category=OneCClassificationReference(CATEGORY_NEW, "NEW-CATEGORY"),
        target_category=OneCClassificationReference(),
        category_mode="remove_expected",
    )
    rows, command_hash, _ = prepare_nomenclature_classification_command(
        (recovery_intent,), approved_by="115204"
    )
    message = NomenclatureClassificationUpdateMessage(
        operation_id=OPERATION_ID,
        command_hash=command_hash,
        message_id=build_nomenclature_classification_message_id(
            OPERATION_ID, command_hash, "apply"
        ),
        rows=rows,
        approved_by="115204",
        mode="apply",
    )
    request_root = ET.fromstring(build_nomenclature_classification_updates_xml(message))
    assert request_root.findtext("Items/Item/GroupMode") == "clear_expected"
    assert request_root.find("Items/Item/TargetGroupGuid") is not None
    assert request_root.findtext("Items/Item/TargetGroupGuid") in {None, ""}
    assert request_root.findtext("Items/Item/CategoryMode") == "remove_expected"
    assert request_root.find("Items/Item/TargetCategoryGuid") is not None
    assert request_root.findtext("Items/Item/TargetCategoryGuid") in {None, ""}

    result_path = tmp_path / "restore-result.xml"
    result_path.write_text(_result_xml(message), encoding="windows-1251")
    result = parse_nomenclature_classification_exchange_result(result_path)
    validate_nomenclature_classification_result(message, result)
    assert result.item_results[0].readback_group_guid == ""
    assert result.item_results[0].projected_category_guids == UNRELATED_CATEGORY


@pytest.mark.parametrize(
    ("changed", "error"),
    (
        (replace(_intent(), group_mode="invalid"), "group_mode"),
        (
            replace(
                _intent(),
                group_mode="clear_expected",
                target_group=OneCClassificationReference(GROUP_NEW, "NEW-GROUP"),
            ),
            "target_group must be empty",
        ),
        (
            replace(
                _intent(),
                group_mode="clear_expected",
                expected_group=OneCClassificationReference(),
                target_group=OneCClassificationReference(),
            ),
            "expected_group.guid is required",
        ),
        (replace(_intent(), category_mode="invalid"), "category_mode"),
        (
            replace(
                _intent(),
                category_mode="remove_expected",
                target_category=OneCClassificationReference(CATEGORY_NEW, "NEW-CATEGORY"),
            ),
            "target_category must be empty",
        ),
        (
            replace(
                _intent(),
                category_mode="remove_expected",
                expected_category=OneCClassificationReference(),
                target_category=OneCClassificationReference(),
            ),
            "expected_category.guid is required",
        ),
    ),
)
def test_invalid_v3_mode_combinations_fail_closed(
    changed: NomenclatureClassificationIntentRow,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        prepare_nomenclature_classification_command((changed,), approved_by="115204")


def test_result_fingerprint_ignores_only_retry_processed_at(tmp_path: Path) -> None:
    message = _message()
    original_path = tmp_path / "original.xml"
    retry_path = tmp_path / "retry.xml"
    changed_path = tmp_path / "changed.xml"
    original_xml = _result_xml(message)
    original_path.write_text(original_xml, encoding="windows-1251")
    retry_path.write_text(
        original_xml.replace(
            "<ProcessedAt>2026-08-04T12:00:00</ProcessedAt>",
            "<ProcessedAt>2026-08-04T12:05:00</ProcessedAt>",
        ),
        encoding="windows-1251",
    )
    changed_path.write_text(
        original_xml.replace("<Message>OK</Message>", "<Message>changed</Message>"),
        encoding="windows-1251",
    )

    original = parse_nomenclature_classification_exchange_result(original_path)
    retry = parse_nomenclature_classification_exchange_result(retry_path)
    changed = parse_nomenclature_classification_exchange_result(changed_path)

    assert result_fingerprint(retry) == result_fingerprint(original)
    assert result_fingerprint(changed) != result_fingerprint(original)


@pytest.mark.parametrize(
    "legacy_schema",
    ("nomenclature_classification_updates.v1", "nomenclature_classification_updates.v2"),
)
def test_legacy_results_and_noncanonical_message_ids_are_rejected(
    tmp_path: Path,
    legacy_schema: str,
) -> None:
    message = _message()
    legacy = tmp_path / "legacy.xml"
    legacy.write_text(
        _result_xml(message).replace(
            "nomenclature_classification_updates.v3",
            legacy_schema,
        ),
        encoding="windows-1251",
    )
    with pytest.raises(ValueError, match="unexpected result schema"):
        parse_nomenclature_classification_exchange_result(legacy)

    with pytest.raises(ValueError, match="message_id does not match"):
        build_nomenclature_classification_updates_xml(
            replace(message, message_id="arbitrary-dry-run")
        )

    empty_message_id = tmp_path / "empty-message-id.xml"
    empty_message_id.write_text(
        _result_xml(message).replace(
            f"<MessageId>{message.message_id}</MessageId>",
            "<MessageId></MessageId>",
        ),
        encoding="windows-1251",
    )
    with pytest.raises(ValueError, match="MessageId"):
        parse_nomenclature_classification_exchange_result(empty_message_id)

    invalid_mode = tmp_path / "invalid-mode.xml"
    invalid_mode.write_text(
        _result_xml(message).replace("<Mode>dry_run</Mode>", "<Mode>overwrite</Mode>"),
        encoding="windows-1251",
    )
    with pytest.raises(ValueError, match="unexpected result mode"):
        parse_nomenclature_classification_exchange_result(invalid_mode)


def test_write_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    message = _message(mode="apply")
    path = write_nomenclature_classification_updates_message(tmp_path, message)
    assert path.name.endswith("-apply.ready.xml")
    assert not list(path.parent.glob("*.tmp"))
    with pytest.raises(FileExistsError):
        write_nomenclature_classification_updates_message(tmp_path, message)
