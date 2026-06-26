from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import SiteDefectArchiveCase
from app.services.site_defect_workflow import (
    PRIORITY_CONFLICT_RISK,
    RETURN_DECISION_RETURN_GOODS,
    RETURN_ECONOMICS_LEAVE,
    RETURN_ECONOMICS_NEEDS_MANAGER,
    RETURN_ECONOMICS_TAKE_BACK,
    RETURN_STATUS_CREATED,
    RETURN_STATUS_NEEDS_CREATE,
    SiteDefectWorkingBitrixConfig,
    analyze_working_bitrix_item,
    analyze_working_reclamation_text,
    build_working_reclamations_report_from_items,
    evaluate_return_economics,
)


def _archive_case(
    db_session,
    *,
    post_id: str = "1001",
    text: str = "Заказ 062493, клиент пишет: верните новым качеством",
    problem_type: str = "money_refund",
) -> SiteDefectArchiveCase:
    row = SiteDefectArchiveCase(
        idempotency_key=f"old_bitrix:chat69465:post:{post_id}",
        source_dialog_id="chat69465",
        source_post_message_id=post_id,
        title=f"Брак сайта 062493 / {post_id}",
        summary=text,
        problem_type=problem_type,
        status="archive",
        search_text=text,
        extracted_numbers=["062493"],
        extracted_numbers_text="062493",
        comment_count=1,
        file_count=0,
        bitrix_entity_id=post_id,
        bitrix_detail_url=f"https://crm.example/type/1134/details/{post_id}/",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_working_analysis_detects_money_risk_and_similar_archive(db_session) -> None:
    archive = _archive_case(db_session)

    result = analyze_working_reclamation_text(
        db_session,
        title="Новая рекламация 062493",
        problem_description="Клиент пишет: верните деньги, это обман",
        customer_request="деньги",
    )

    assert "062493" in result["numbers"]
    assert result["problem_type"] == "money_refund"
    assert result["priority"] == PRIORITY_CONFLICT_RISK
    assert {task["role"] for task in result["recommended_tasks"]} >= {
        "responsible",
        "okk",
        "finance",
    }
    assert result["similar_archive_cases"][0]["id"] == archive.id
    assert "Похожие архивные случаи" in result["comment"]


def test_working_analysis_recommends_expertise_task(db_session) -> None:
    result = analyze_working_reclamation_text(
        db_session,
        title="Телефон не включается",
        problem_description="Клиент принес брак, нужна экспертиза",
        customer_request="экспертиза",
    )

    assert result["recommended_stage"] == "need_expertise"
    assert result["problem_type"] == "expertise"
    assert any(task["role"] == "okk" for task in result["recommended_tasks"])


def test_return_economics_recommends_leave_when_return_cost_is_higher() -> None:
    result = evaluate_return_economics(item_value=1000, estimated_return_cost=1200)

    assert result["result"] == RETURN_ECONOMICS_LEAVE
    assert result["ratio"] == 1.2


def test_return_economics_uses_manager_review_gray_zone() -> None:
    result = evaluate_return_economics(item_value=1000, estimated_return_cost=800)

    assert result["result"] == RETURN_ECONOMICS_NEEDS_MANAGER


def test_working_analysis_recommends_return_track_task_when_decision_is_take_back(
    db_session,
) -> None:
    result = analyze_working_reclamation_text(
        db_session,
        title="Возврат товара",
        problem_description="Клиент согласовал возврат через СДЭК",
        item_value=1000,
        estimated_return_cost=300,
        return_goods_decision=RETURN_DECISION_RETURN_GOODS,
    )

    assert result["return_economics"]["result"] == RETURN_ECONOMICS_TAKE_BACK
    assert result["return_status_update"] == RETURN_STATUS_NEEDS_CREATE
    assert any(
        task["role"] == "logistics" and task["title"] == "Создать трек-номер возврата"
        for task in result["recommended_tasks"]
    )


def test_working_analysis_does_not_duplicate_track_task_when_track_exists(db_session) -> None:
    result = analyze_working_reclamation_text(
        db_session,
        title="Возврат товара",
        item_value=1000,
        estimated_return_cost=300,
        return_goods_decision=RETURN_DECISION_RETURN_GOODS,
        return_tracking_number="CDEK123",
        return_status=RETURN_STATUS_NEEDS_CREATE,
    )

    assert result["return_status_update"] == RETURN_STATUS_CREATED
    assert not any(
        task["role"] == "logistics" and task["title"] == "Создать трек-номер возврата"
        for task in result["recommended_tasks"]
    )


def test_working_report_flags_control_buckets(db_session) -> None:
    config = SiteDefectWorkingBitrixConfig(
        webhook_url=None,
        entity_type_id=1134,
        working_category_id=55,
        working_stage_map={
            "need_expertise": "DT1134_55:NEED_EXPERTISE",
            "refund_or_decision": "DT1134_55:REFUND_DECISION",
            "closed": "DT1134_55:CLOSED",
        },
        field_map={
            "reaction_deadline": "reactionDeadline",
            "linked_expertise": "linkedExpertise",
            "linked_expertise_crm": "linkedExpertiseCrm",
            "decision_result": "decisionResult",
            "problem_description": "problemDescription",
            "item_value": "itemValue",
            "estimated_return_cost": "estimatedReturnCost",
            "return_goods_decision": "returnGoodsDecision",
            "return_leave_reason": "returnLeaveReason",
            "return_decision_approved_by": "returnDecisionApprovedBy",
            "return_tracking_number": "returnTrackingNumber",
            "return_tracking_created_at": "returnTrackingCreatedAt",
            "return_status": "returnStatus",
        },
        enum_map={
            "return_goods_decision": {
                "return_goods": "return",
                "leave_with_client": "leave",
            },
            "return_status": {
                "needs_create": "needs_create",
                "created": "created",
                "in_transit": "in_transit",
            },
        },
        created_by_user_id=None,
        okk_user_ids=[],
        finance_user_ids=[],
        logistics_user_ids=[],
        leader_user_ids=[],
    )
    now = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
    items = [
        {
            "id": 1,
            "title": "Без ответственного",
            "categoryId": 55,
            "stageId": "DT1134_55:NEW",
            "assignedById": 0,
            "reactionDeadline": (now - timedelta(hours=1)).isoformat(),
        },
        {
            "id": 2,
            "title": "Нужна экспертиза",
            "categoryId": 55,
            "stageId": "DT1134_55:NEED_EXPERTISE",
            "assignedById": 10,
            "problemDescription": "не включается, брак",
        },
        {
            "id": 3,
            "title": "Возврат денег",
            "categoryId": 55,
            "stageId": "DT1134_55:REFUND_DECISION",
            "assignedById": 11,
            "problemDescription": "клиент просит вернуть деньги",
        },
        {
            "id": 4,
            "title": "Экспертиза связана",
            "categoryId": 55,
            "stageId": "DT1134_55:NEED_EXPERTISE",
            "assignedById": 12,
            "linkedExpertiseCrm": "DYNAMIC_1112_100",
            "problemDescription": "брак, экспертиза уже есть",
        },
        {
            "id": 5,
            "title": "Доставка дороже товара",
            "categoryId": 55,
            "stageId": "DT1134_55:CLARIFY",
            "assignedById": 12,
            "itemValue": "1000",
            "estimatedReturnCost": "1200",
        },
        {
            "id": 6,
            "title": "Нужен трек",
            "categoryId": 55,
            "stageId": "DT1134_55:CLARIFY",
            "assignedById": 12,
            "returnGoodsDecision": "return",
            "itemValue": "1000",
            "estimatedReturnCost": "300",
        },
        {
            "id": 7,
            "title": "Едет давно",
            "categoryId": 55,
            "stageId": "DT1134_55:CLARIFY",
            "assignedById": 12,
            "returnGoodsDecision": "return",
            "returnTrackingNumber": "CDEK123",
            "returnStatus": "in_transit",
            "returnTrackingCreatedAt": (now - timedelta(days=8)).isoformat(),
            "itemValue": "1000",
            "estimatedReturnCost": "300",
        },
    ]

    report = build_working_reclamations_report_from_items(
        db_session,
        items=items,
        config=config,
        now=now,
    )

    assert report["items_checked"] == 7
    assert [item["id"] for item in report["buckets"]["without_responsible"]] == [1]
    assert [item["id"] for item in report["buckets"]["overdue"]] == [1]
    assert [item["id"] for item in report["buckets"]["need_expertise_without_link"]] == [2]
    assert [item["id"] for item in report["buckets"]["refund_without_decision"]] == [3]
    assert [item["id"] for item in report["buckets"]["leave_with_client_needs_approval"]] == [5]
    assert [item["id"] for item in report["buckets"]["return_track_required"]] == [6]
    assert [item["id"] for item in report["buckets"]["return_in_transit_overdue"]] == [7]


def test_working_analysis_reads_selected_enum_values(db_session) -> None:
    config = SiteDefectWorkingBitrixConfig(
        webhook_url=None,
        entity_type_id=1134,
        working_category_id=55,
        working_stage_map={},
        field_map={
            "customer_request_choice": "customerRequestChoice",
            "problem_description": "problemDescription",
        },
        enum_map={"customer_request_choice": {"refund_money": "777"}},
        created_by_user_id=None,
        okk_user_ids=[],
        finance_user_ids=[],
        logistics_user_ids=[],
        leader_user_ids=[],
    )

    result = analyze_working_bitrix_item(
        db_session,
        item={
            "id": 10,
            "title": "Короткая рекламация",
            "customerRequestChoice": "777",
            "problemDescription": "Клиент недоволен",
        },
        config=config,
    )

    assert result["problem_type"] == "money_refund"
    assert result["priority"] == PRIORITY_CONFLICT_RISK
