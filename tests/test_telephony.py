from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base
from app.services.telephony import (
    TELEPHONY_MAPPING_MODE_NO_ACTIVE_OWNER,
    TELEPHONY_MAPPING_MODE_SERVICE_OVERLAY,
    TELEPHONY_MAPPING_MODE_SHARED,
    TELEPHONY_MAPPING_MODE_SINGLE_BITRIX,
    TELEPHONY_MAPPING_MODE_SINGLE_NO_BITRIX,
    TelephonyUserLineRow,
    build_retail_line_map_projection,
    build_telephony_health,
    sync_telephony_user_line_snapshot,
)


def _row(
    *,
    user_ref_hex: str,
    extension: str | None,
    employment_status: str | None,
    bitrix_user_id: str | None = None,
    bitrix_full_name: str | None = None,
    physical_person_name: str | None = None,
    store_name: str | None = None,
    snapshot_date: date = date(2026, 4, 18),
) -> TelephonyUserLineRow:
    return TelephonyUserLineRow(
        snapshot_date=snapshot_date,
        mapping_source="onec_user_workstation",
        user_ref_hex=user_ref_hex,
        user_name=physical_person_name,
        physical_person_ref_hex=f"pp-{user_ref_hex}",
        physical_person_name=physical_person_name,
        computer_name=f"pc-{extension or user_ref_hex}",
        extension=extension,
        store_name=store_name,
        staff_store_name=store_name,
        employment_status=employment_status,
        bitrix_user_id=bitrix_user_id,
        bitrix_full_name=bitrix_full_name,
        is_marked=False,
        has_extension=bool(extension),
        has_bitrix=bool(bitrix_user_id),
    )


def test_build_retail_line_map_projection_picks_mode_by_extension_ownership() -> None:
    rows = [
        _row(
            user_ref_hex="u-1",
            extension="531",
            employment_status="active",
            bitrix_user_id="10837",
            bitrix_full_name="Асадбек Олимжонов",
            physical_person_name="Асадбек Олимжонов",
            store_name="Савелово",
        ),
        _row(
            user_ref_hex="u-2",
            extension="620",
            employment_status="active",
            bitrix_user_id="10893",
            bitrix_full_name="Байрамгулыев Кувватгелди",
            physical_person_name="Байрамгулыев Кувватгелди",
            store_name="СПБ Садовая",
        ),
        _row(
            user_ref_hex="u-3",
            extension="620",
            employment_status="active",
            physical_person_name="Бигаев Сергей",
            store_name="СПБ Садовая",
        ),
        _row(
            user_ref_hex="u-4",
            extension="801",
            employment_status="active",
            physical_person_name="Закупка 801",
            store_name="Склад",
        ),
        _row(
            user_ref_hex="u-5",
            extension="702",
            employment_status="fired",
            physical_person_name="Эльдар",
            store_name="Админ",
        ),
    ]

    projection = build_retail_line_map_projection(rows)
    by_line = {item.line_id: item for item in projection}

    assert by_line["531"].mapping_mode == TELEPHONY_MAPPING_MODE_SINGLE_BITRIX
    assert by_line["531"].store_id == "telephony_user_10837"
    assert by_line["531"].store_name == "Асадбек Олимжонов"

    assert by_line["620"].mapping_mode == TELEPHONY_MAPPING_MODE_SHARED
    assert by_line["620"].store_id == "telephony_line_620"
    assert by_line["620"].active_user_count == 2
    assert "СПБ Садовая" in by_line["620"].store_name

    assert by_line["801"].mapping_mode == TELEPHONY_MAPPING_MODE_SINGLE_NO_BITRIX
    assert by_line["801"].store_id == "telephony_line_801"

    assert by_line["702"].mapping_mode == TELEPHONY_MAPPING_MODE_NO_ACTIVE_OWNER
    assert by_line["702"].active_user_count == 0


def test_build_retail_line_map_projection_applies_service_overlay_and_review_exclusions() -> None:
    rows = [
        _row(
            user_ref_hex="u-1",
            extension="731",
            employment_status="active",
            bitrix_user_id="10851",
            bitrix_full_name="Наимбаева Светлана",
            physical_person_name="Наимбаева Светлана",
            store_name="Наимбаева Светлана Наримановна",
        ),
        _row(
            user_ref_hex="u-2",
            extension="801",
            employment_status="active",
            bitrix_user_id="10895",
            bitrix_full_name="Лисовенко Вячеслав",
            physical_person_name="Лисовенко Вячеслав",
            store_name="Лисовенко Вячеслав Игоревич",
        ),
        _row(
            user_ref_hex="u-3",
            extension="802",
            employment_status="active",
            bitrix_user_id="10845",
            bitrix_full_name="Егоров Роман",
            physical_person_name="Егоров Роман",
            store_name="Егоров Роман",
        ),
        _row(
            user_ref_hex="u-4",
            extension="802",
            employment_status="active",
            bitrix_user_id="10899",
            bitrix_full_name="Саркисян Вараздат",
            physical_person_name="Саркисян Вараздат",
            store_name="Саркисян Вараздат Сосевич",
        ),
    ]

    projection = build_retail_line_map_projection(
        rows,
        service_line_labels={
            "701": "Admin",
            "733": "733 HR",
            "801": "Закупка 801",
        },
        exclude_line_ids={"733"},
    )
    by_line = {item.line_id: item for item in projection}

    assert "701" in by_line
    assert by_line["701"].mapping_mode == TELEPHONY_MAPPING_MODE_SERVICE_OVERLAY
    assert by_line["701"].store_id == "telephony_line_701"
    assert by_line["701"].store_name == "Admin"

    assert "733" not in by_line
    assert by_line["731"].store_id == "telephony_user_10851"
    assert by_line["801"].store_id == "telephony_user_10895"
    assert by_line["802"].mapping_mode == TELEPHONY_MAPPING_MODE_SHARED


def test_sync_and_health_report_fresh_snapshot_metrics() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        sync_telephony_user_line_snapshot(
            session,
            rows=[
                _row(
                    user_ref_hex="u-1",
                    extension="531",
                    employment_status="active",
                    bitrix_user_id="10837",
                    bitrix_full_name="Асадбек Олимжонов",
                    physical_person_name="Асадбек Олимжонов",
                    store_name="Савелово",
                ),
                _row(
                    user_ref_hex="u-2",
                    extension="620",
                    employment_status="active",
                    physical_person_name="Байрамгулыев Кувватгелди",
                    store_name="СПБ Садовая",
                ),
                _row(
                    user_ref_hex="u-3",
                    extension=None,
                    employment_status="fired",
                    physical_person_name="Бывший сотрудник",
                    store_name="Архив",
                ),
            ],
            snapshot_date=date(2026, 4, 18),
        )
        session.commit()

        health = build_telephony_health(
            session,
            requested_date=date(2026, 4, 18),
            max_lag_days=1,
        )

    assert health["status"] == "ok"
    assert health["freshness_status"] == "fresh"
    assert health["metrics"]["rows_total"] == 3
    assert health["metrics"]["active_rows_with_extension"] == 2
    assert health["metrics"]["unique_projection_rows"] == 2
