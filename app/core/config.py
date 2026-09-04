from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pricing Service"
    environment: str = "development"
    debug: bool = False
    app_port: int = 8000
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/pricing"
    database_pool_size: int | None = None
    database_max_overflow: int | None = None
    database_pool_timeout_seconds: float | None = None
    database_pool_recycle_seconds: int | None = None
    redis_url: str = "redis://localhost:6379/0"
    onec_database_url: str | None = None
    telephony_mdm_database_url: str | None = None
    telephony_service_line_labels: dict[str, str] = Field(default_factory=dict)
    telephony_review_line_ids: list[str] = Field(default_factory=list)
    onec_query_timeout_seconds: int = 300
    onec_login_timeout_seconds: int = 30

    competitor_source_mode: str = "zenno"  # zenno | internal
    competitor_parse_limit: int = 10
    proxy_api_url: str | None = None
    proxy_api_token: str | None = None
    proxy_timeout_seconds: float = 10.0
    proxy_max_retries: int = 3
    proxy_rps_limit: float | None = None
    competitor_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
    )
    competitor_accept_language: str = "ru,en;q=0.9"
    competitor_cookies: str | None = None
    zenlogs_import_enabled: bool = True
    zenlogs_moba_url: str | None = None
    zenlogs_sources: str | None = None
    zenlogs_http_timeout_sec: float = 30.0
    zenlogs_verify_ssl: bool = True
    competitor_http_import_enabled: bool = False
    competitor_http_sources: str | None = None  # name:https-url-with-{date}, comma-separated
    competitor_http_timeout_sec: float = 30.0
    competitor_http_max_files_per_source: int = 2
    captcha_provider: str = "2captcha"
    captcha_api_key: str | None = None

    # LLM / OpenAI
    openai_api_key: str | None = None
    openai_api_base: str | None = None
    openai_http_proxy: str | None = None
    openai_model: str = "gpt-4o-mini"
    local_llm_base_url: str | None = None
    local_llm_chat_model: str | None = None

    weekly_buyer_digest_enabled: bool = False
    weekly_buyer_digest_model: str = "gpt-5.1"
    return_scheme_enabled: bool = False
    return_scheme_window_days: int = 7
    return_scheme_retail_price_types: str = "Розница"
    return_scheme_output_dir: str = "reports/return_scheme"
    return_scheme_internal_api_token: str | None = None
    weekly_kpi_ingest_internal_api_token: str | None = None
    return_scheme_alert_telegram_token: str | None = None
    return_scheme_alert_telegram_chat_id: str | None = None
    return_scheme_direct_telegram_enabled: bool = False
    counterparty_duplicate_enabled: bool = False
    counterparty_duplicate_internal_api_token: str | None = None
    counterparty_duplicate_sql: str | None = None
    counterparty_duplicate_sql_file: str | None = None
    counterparty_duplicate_detection_window_hours: int = 25
    counterparty_duplicate_antiduplicate_hours: int = 24
    counterparty_duplicate_sla_hours: int = 24
    counterparty_duplicate_owner_code: str = "finance"
    counterparty_duplicate_p2_enabled: bool = False
    counterparty_duplicate_fuzzy_threshold: float = 0.9
    management_internal_api_token: str | None = None
    orchestration_internal_api_token: str | None = None
    sms_journal_internal_api_token: str | None = None
    sms_journal_encryption_key: str | None = None
    sms_journal_phone_hash_key: str | None = None
    sms_journal_export_allowed_actors: str = ""
    logistics_internal_api_token: str | None = None
    customer_return_bitrix_writes_enabled: bool = False
    customer_return_service_links_enabled: bool = False
    customer_return_service_links_roles: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["admin"]
    )
    customer_return_bitrix_webhook_url: str | None = None
    customer_return_bitrix_group_id: int | None = Field(default=None, ge=1)
    customer_return_bitrix_created_by_user_id: int | None = Field(default=None, ge=1)
    customer_return_bitrix_responsible_user_id: int | None = Field(default=None, ge=1)
    customer_return_bitrix_accomplice_user_ids: list[int] = Field(default_factory=list)
    customer_return_bitrix_auditor_user_ids: list[int] = Field(default_factory=list)
    customer_return_worker_batch_size: int = Field(default=25, ge=1, le=200)
    customer_return_worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    customer_return_worker_max_attempts: int = Field(default=5, ge=1, le=20)
    expertise_internal_api_token: str | None = None
    expertise_onec_sql: str | None = None
    expertise_onec_sql_file: str | None = None
    expertise_bitrix_webhook_url: str | None = None
    expertise_bitrix_entity_type_id: int | None = None
    expertise_bitrix_category_id: int | None = None
    expertise_bitrix_stage_map: dict[str, str] = Field(default_factory=dict)
    expertise_bitrix_field_map: dict[str, str] = Field(default_factory=dict)
    expertise_bitrix_root_folder_id: int | None = None
    expertise_bitrix_notify_responsible_user_id: int | None = None
    expertise_bitrix_notify_auditor_user_ids: list[int] = Field(default_factory=list)
    expertise_bitrix_store_department_map: dict[str, int] = Field(default_factory=dict)
    expertise_bitrix_notify_owner_user_map: dict[str, int] = Field(default_factory=dict)
    expertise_bitrix_notify_excluded_position_keywords: list[str] = Field(
        default_factory=lambda: ["курьер"]
    )
    expertise_bitrix_notify_manager_position_keywords: list[str] = Field(
        default_factory=lambda: ["менедж", "управля"]
    )
    expertise_sla_store_group_map: dict[str, str] = Field(default_factory=dict)
    expertise_sla_delivery_days_map: dict[str, int] = Field(
        default_factory=lambda: {
            "moscow": 2,
            "spb": 8,
            "other": 8,
        }
    )
    expertise_sla_review_days_map: dict[str, int] = Field(
        default_factory=lambda: {
            "moscow": 3,
            "spb": 14,
            "other": 14,
        }
    )
    expertise_alarm_review_warning_hours: int = 24
    expertise_alarm_notify_warning_hours: int = 48
    expertise_alarm_notify_escalation_hours: int = 48
    expertise_alarm_review_primary_days_map: dict[str, int] = Field(
        default_factory=lambda: {
            "moscow": 2,
            "spb": 13,
            "other": 13,
        }
    )
    expertise_alarm_review_escalation_days_map: dict[str, int] = Field(
        default_factory=lambda: {
            "moscow": 4,
            "spb": 15,
            "other": 15,
        }
    )
    expertise_alarm_review_top_escalation_days_map: dict[str, int] = Field(
        default_factory=lambda: {
            "moscow": 12,
            "spb": 23,
            "other": 23,
        }
    )
    expertise_alarm_review_primary_user_ids: list[int] = Field(default_factory=list)
    expertise_alarm_review_escalation_user_ids: list[int] = Field(default_factory=list)
    expertise_alarm_review_top_escalation_user_ids: list[int] = Field(default_factory=list)
    site_defect_archive_internal_api_token: str | None = None
    site_defect_archive_bitrix_webhook_url: str | None = None
    site_defect_archive_bitrix_entity_type_id: int | None = None
    site_defect_archive_bitrix_working_category_id: int | None = None
    site_defect_archive_bitrix_archive_category_id: int | None = None
    site_defect_archive_bitrix_archive_stage_id: str | None = None
    site_defect_archive_bitrix_root_folder_id: int | None = None
    site_defect_archive_bitrix_working_stage_map: dict[str, str] = Field(default_factory=dict)
    site_defect_archive_bitrix_field_map: dict[str, str] = Field(default_factory=dict)
    site_defect_workflow_created_by_user_id: int | None = None
    site_defect_workflow_okk_user_ids: list[int] = Field(default_factory=list)
    site_defect_workflow_finance_user_ids: list[int] = Field(default_factory=list)
    site_defect_workflow_logistics_user_ids: list[int] = Field(default_factory=list)
    site_defect_workflow_leader_user_ids: list[int] = Field(default_factory=list)
    site_service_requests_ingest_enabled: bool = False
    site_service_requests_email_ingest_enabled: bool = False
    site_service_requests_bitrix_writes_enabled: bool = False
    site_service_requests_outbound_replies_enabled: bool = False
    site_service_requests_legacy_field_replies_enabled: bool = False
    site_service_requests_ui_enabled: bool = False
    site_service_requests_ui_replies_enabled: bool = False
    site_service_requests_command_attachments_enabled: bool = False
    site_service_requests_hmac_secret: str | None = None
    site_service_requests_event_encryption_key: str | None = None
    site_service_requests_ui_session_secret: str | None = None
    site_service_requests_ui_handler_url: str | None = None
    site_service_requests_ui_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    site_service_requests_ui_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    site_service_requests_ui_allowed_user_ids: list[int] = Field(default_factory=list)
    site_service_requests_ui_write_allowed_user_ids: list[int] = Field(default_factory=list)
    site_service_requests_ui_session_ttl_seconds: int = Field(default=900, ge=300, le=3600)
    site_service_requests_conversation_retention_days: int = Field(default=90, ge=1, le=3650)
    site_service_requests_ui_max_files_per_reply: int = Field(default=5, ge=1, le=20)
    site_service_requests_ui_max_file_bytes: int = Field(
        default=10 * 1024 * 1024, ge=1, le=10 * 1024 * 1024
    )
    site_service_requests_ui_max_total_file_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024
    )
    site_service_requests_bitrix_webhook_url: str | None = None
    site_service_requests_bitrix_entity_type_id: int = 1134
    site_service_requests_bitrix_working_category_id: int = 55
    site_service_requests_bitrix_archive_category_id: int = 56
    site_service_requests_bitrix_field_map: dict[str, str] = Field(default_factory=dict)
    site_service_requests_bitrix_stage_map: dict[str, str] = Field(default_factory=dict)
    site_service_requests_bitrix_enum_map: dict[str, str] = Field(default_factory=dict)
    site_service_requests_bitrix_root_folder_id: int | None = Field(default=None, gt=0)
    site_service_requests_crm_order_field: str | None = None
    site_service_requests_first_line_user_ids: list[int] = Field(default_factory=list)
    site_service_requests_escalation_user_id: int | None = None
    site_service_requests_finance_user_id: int | None = None
    site_service_requests_daily_report_enabled: bool = False
    site_service_requests_daily_report_dialog_id: str | None = None
    site_service_requests_daily_report_hour: int = Field(default=11, ge=0, le=23)
    site_service_requests_expected_user_names: dict[str, str] = Field(default_factory=dict)
    site_service_requests_timezone: str = "Europe/Moscow"
    site_service_requests_first_response_hours: int = Field(default=4, ge=1, le=24)
    site_service_requests_timestamp_tolerance_seconds: int = Field(
        default=300,
        ge=1,
        le=900,
    )
    site_service_requests_nonce_ttl_seconds: int = Field(default=600, ge=300, le=3600)
    site_service_requests_command_lease_seconds: int = Field(default=300, ge=30, le=3600)
    site_service_requests_file_spool_dir: str = ".local/site-service-requests/files"
    site_service_requests_worker_batch_size: int = Field(default=20, ge=1, le=100)
    site_service_requests_worker_stale_seconds: int = Field(default=180, ge=60, le=3600)
    site_service_requests_max_crm_files_per_item: int = Field(default=50, ge=1, le=1000)
    site_service_requests_site_base_url: str = "https://master-mobile.ru"
    site_service_requests_health_lag_alert_seconds: int = Field(
        default=300,
        ge=60,
        le=86400,
    )
    site_service_requests_max_event_body_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1024,
        le=16 * 1024 * 1024,
    )
    site_service_requests_max_ack_body_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        le=1024 * 1024,
    )
    site_service_requests_max_file_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        le=10 * 1024 * 1024,
    )
    card_balance_reconciliation_internal_api_token: str | None = None
    card_balance_bitrix_webhook_url: str | None = None
    card_balance_bitrix_entity_type_id: int | None = None
    card_balance_bitrix_category_id: int | None = None
    card_balance_bitrix_stage_map: dict[str, str] = Field(default_factory=dict)
    card_balance_bitrix_field_map: dict[str, str] = Field(default_factory=dict)
    card_balance_bitrix_employee_overrides: dict[str, str] = Field(default_factory=dict)
    card_balance_auto_create_daily: bool = False
    card_balance_pilot_cashbox_codes: Annotated[list[str], NoDecode] = Field(default_factory=list)
    card_balance_require_workday: bool = True
    card_balance_tolerance_rub: float = 0.0
    card_balance_max_stale_days: int = 1
    card_balance_ocr_enabled: bool = True
    card_balance_ocr_required: bool = False
    card_balance_ocr_model: str = "gpt-4o-mini"
    card_balance_ocr_min_confidence: float = 0.75
    card_balance_ocr_timeout_seconds: float = 60.0
    card_balance_ocr_max_image_bytes: int = 10 * 1024 * 1024
    order_fulfillment_internal_api_token: str | None = None
    order_payment_control_internal_api_token: str | None = None
    order_payment_control_require_posted: bool = False
    order_payment_control_closure_blocks_payment: bool = True
    order_payment_control_closure_allowed_reasons: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["Исполнение заказа", "Частичное исполнение заказа"]
    )
    order_fulfillment_bitrix_webhook_url: str | None = None
    order_fulfillment_artifact_dir: str = ".local/order-fulfillment-pilot"
    order_fulfillment_site_chat_dialog_id: str = "chat733"
    order_fulfillment_spb_courier_chat_dialog_id: str = "chat727"
    order_fulfillment_chat_auto_apply_enabled: bool = False
    order_fulfillment_site_chat_apply_author_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_courier_chat_apply_author_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_bot_enabled: bool = False
    order_fulfillment_bot_apply_enabled: bool = False
    order_fulfillment_pickup_stage_apply_enabled: bool = False
    order_fulfillment_pickup_auto_arrival_enabled: bool = False
    order_fulfillment_pickup_evidence_tracking_enabled: bool = False
    order_fulfillment_pickup_missing_receipt_enabled: bool = False
    order_fulfillment_bot_sms_enabled: bool = False
    order_fulfillment_pickup_sla_enabled: bool = False
    order_fulfillment_pickup_inventory_enabled: bool = False
    order_fulfillment_inventory_won_enabled: bool = False
    order_fulfillment_lost_orders_enabled: bool = False
    order_fulfillment_execution_master_enabled: bool = False
    order_fulfillment_execution_ingest_enabled: bool = False
    order_fulfillment_execution_reconciliation_enabled: bool = False
    order_fulfillment_execution_stage_apply_enabled: bool = False
    order_fulfillment_dismantling_auto_apply_enabled: bool = False
    order_fulfillment_execution_historical_apply_enabled: bool = False
    order_fulfillment_execution_cutover_at: datetime | None = None
    order_fulfillment_execution_recent_limit: int = Field(default=100, ge=1, le=1000)
    order_fulfillment_execution_history_limit: int = Field(default=250, ge=1, le=1000)
    order_fulfillment_execution_cursor_path: str = (
        "/opt/MM/pricing-service/.local/order-fulfillment-pilot/"
        "executing-reconciliation-cursor.json"
    )
    order_fulfillment_state_projection_stale_after_seconds: int = Field(
        default=15 * 60, ge=60, le=24 * 60 * 60
    )
    order_fulfillment_crm_projection_cursor_path: str = (
        "/opt/MM/pricing-service/.local/order-fulfillment-pilot/" "crm-projection-cursor.json"
    )
    order_fulfillment_crm_projection_overlap_minutes: int = Field(default=10, ge=1, le=120)
    order_fulfillment_crm_projection_batch_limit: int = Field(default=500, ge=1, le=5000)
    order_fulfillment_shipments_master_enabled: bool = False
    order_fulfillment_shipments_ingest_enabled: bool = False
    order_fulfillment_shipments_crm_fields_enabled: bool = False
    order_fulfillment_shipments_stage_apply_enabled: bool = False
    order_fulfillment_shipments_notifications_enabled: bool = False
    order_fulfillment_shipments_email_enabled: bool = False
    order_fulfillment_shipments_sms_enabled: bool = False
    order_fulfillment_shipments_gateway_apply_enabled: bool = False
    order_fulfillment_shipments_poller_enabled: bool = False
    order_fulfillment_shipments_poller_limit: int = Field(default=100, ge=1, le=1000)
    order_fulfillment_shipments_poller_overlap: int = Field(default=20, ge=1, le=250)
    order_fulfillment_shipments_notification_recovery_minutes: int = Field(
        default=30, ge=5, le=1440
    )
    order_fulfillment_shipments_poller_cursor_path: str = (
        "/opt/MM/pricing-service/.local/order-fulfillment-pilot/" "shipment-poller-cursor.json"
    )
    order_fulfillment_shipments_gateway_url: str | None = None
    order_fulfillment_shipments_gateway_token: str | None = None
    order_fulfillment_shipments_email_workflow_template_id: int | None = Field(default=None, ge=1)
    order_fulfillment_shipments_sms_workflow_template_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_source_chat_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["chat8729", "chat733", "chat8961", "chat729", "chat739"]
    )
    order_fulfillment_bot_card_ttl_hours: int = Field(default=24, ge=1, le=168)
    order_fulfillment_bot_dry_run_card_limit: int = Field(default=20, ge=0)
    order_fulfillment_bot_cutover_at: datetime | None = None
    order_fulfillment_pickup_evidence_cutover_at: datetime | None = None
    order_fulfillment_bot_application_token: str | None = None
    order_fulfillment_bot_client_id: str | None = None
    order_fulfillment_bot_callback_secret: str | None = None
    order_fulfillment_bot_callback_max_body_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        le=1024 * 1024,
    )
    order_fulfillment_bot_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_bot_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_bot_excluded_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_bot_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_command_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_command: str = "pickup_action"
    order_fulfillment_bot_search_command_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_search_command: str = "pickup"
    order_fulfillment_bot_arrival_command_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_arrival_command: str = "pickup_arrival"
    order_fulfillment_bot_menu_message_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_portal_base_url: str | None = None
    order_fulfillment_bot_callback_url: str | None = None
    order_fulfillment_bot_sms_workflow_template_id: int | None = Field(default=None, ge=1)
    order_fulfillment_bot_pickup_sms_field: str = "UF_CRM_MM_PICKUP_READY_SMS_AT"
    order_fulfillment_bot_task_responsible_id: int | None = Field(default=None, ge=1)
    order_fulfillment_internet_shop_task_responsible_id: int | None = Field(default=None, ge=1)
    order_fulfillment_site_return_task_responsible_id: int | None = Field(default=None, ge=1)
    order_fulfillment_point_task_routes: dict[str, dict[str, int]] = Field(default_factory=dict)
    order_fulfillment_pickup_warehouse_external_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_pickup_warehouse_aliases: dict[str, list[str]] = Field(default_factory=dict)
    order_fulfillment_inventory_won_warehouse_external_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_fulfillment_bot_call_after_hours: int = Field(default=72, ge=1, le=720)
    order_fulfillment_bot_dismantle_after_hours: int = Field(default=96, ge=1, le=720)
    order_fulfillment_pickup_receipt_question_after_hours: int = Field(default=24, ge=1, le=720)
    order_fulfillment_pickup_receipt_task_after_hours: int = Field(default=48, ge=1, le=720)
    order_fulfillment_pickup_ready_chat_dialog_id: str = "chat8729"
    order_fulfillment_pickup_inventory_chat_dialog_id: str = "chat8961"
    order_fulfillment_pickup_movement_chat_dialog_id: str = "chat729"
    order_fulfillment_pickup_exception_chat_dialog_id: str = "chat739"
    order_fulfillment_pickup_notification_confirmer_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [131016, 132252]
    )
    order_fulfillment_reaction_lookback_hours: int = Field(default=168, ge=1, le=2160)
    order_fulfillment_chat_poll_page_limit: int = Field(default=20, ge=1, le=200)
    order_fulfillment_ocr_enabled: bool = True
    order_fulfillment_ocr_model: str | None = None
    order_fulfillment_ocr_min_confidence: float = 0.75
    order_fulfillment_ocr_timeout_seconds: float = 60.0
    order_fulfillment_ocr_max_image_bytes: int = 10 * 1024 * 1024
    order_fulfillment_notify_enabled: bool = False
    order_fulfillment_notify_business_user_ids: list[int] = Field(default_factory=list)
    order_fulfillment_notify_tech_user_ids: list[int] = Field(default_factory=list)
    order_fulfillment_notify_method: str = "im.notify.system.add"
    order_fulfillment_notify_site_dialog_id: str | None = None
    order_fulfillment_notify_site_dialog_method: str = "im.message.add"
    order_fulfillment_notify_state_path: str = (
        ".local/order-fulfillment-pilot/order-fulfillment-notify-state.json"
    )
    order_fulfillment_known_raw_deliveries: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "Самовывоз",
            "СДЭК (Самовывоз)",
            "Доставка курьером",
            "Почта России (Доставка в отделение)",
        ]
    )
    bank_payments_internal_api_token: str | None = None
    bank_payments_artifact_dir: str = ".local/bank-payments"
    bank_payments_own_accounts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    bank_payments_acquiring_contract_code: str = "РБ0022772"
    bank_payments_salary_person_name: str = "Зарплата"
    bank_payments_b24_webhook_url: str | None = None
    bank_payments_b24_input_folder_id: int | None = None
    bank_payments_b24_ready_folder_id: int | None = None
    bank_payments_b24_error_folder_id: int | None = None
    bank_payments_b24_poll_limit: int = 50
    bank_payments_b24_max_file_bytes: int = 10 * 1024 * 1024
    bank_payments_b24_state_file: str = ".local/bank-payments/bitrix-state.json"
    bank_payments_source_database_url: str | None = None
    bank_payments_source_schema: str = "finance"
    bank_payments_own_name: str = ""
    bank_payments_own_inn: str = ""
    bank_payments_own_kpp: str = ""
    bank_payments_own_bank_name: str = ""
    bank_payments_own_bank_bic: str = ""
    bank_payments_own_bank_correspondent_account: str = ""
    receivable_ledger_window_chunk_days: int = 1
    receivable_canonical_opening_max_lag_days: int = Field(default=45, ge=0)
    receivable_workflow_enabled: bool = False
    receivable_bitrix_webhook_url: str | None = None
    receivable_bitrix_entity_type_id: int | None = None
    receivable_bitrix_category_id: int | None = None
    receivable_bitrix_stage_map: dict[str, str] = Field(default_factory=dict)
    receivable_bitrix_field_map: dict[str, str] = Field(default_factory=dict)
    receivable_bitrix_enum_map: dict[str, dict[str, str]] = Field(default_factory=dict)
    receivable_sms_mode: str = "dry_run"
    receivable_task_payloads_enabled: bool = True
    receivable_retail_network_head_user_id: int | None = None
    receivable_department_manager_map: dict[str, int] = Field(default_factory=dict)
    receivable_workflow_department_refs: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_workflow_department_names: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_workplace_bitrix_enabled: bool = False
    receivable_workplace_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_workplace_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_workplace_bitrix_full_access_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_credit_decision_enabled: bool = False
    receivable_credit_decision_bitrix_webhook_url: str | None = None
    receivable_credit_decision_entity_type_id: int | None = None
    receivable_credit_decision_category_id: int | None = None
    receivable_credit_decision_stage_map: dict[str, str] = Field(default_factory=dict)
    receivable_credit_decision_field_map: dict[str, str] = Field(default_factory=dict)
    receivable_credit_decision_approver_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    receivable_credit_decision_auto_apply_enabled: bool = False
    receivable_credit_decision_pilot_counterparty_codes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["РБ030337"]
    )
    receivable_credit_decision_poll_limit: int = 50
    receivable_credit_decision_result_timeout_seconds: int = 900
    receivable_credit_decision_max_dry_run_attempts: int = 3
    receivable_credit_decision_max_readback_attempts: int = Field(default=3, ge=1, le=3)
    receivable_credit_decision_mapping_path: str = (
        "build/bitrix/receivable_credit_decision_mapping.json"
    )
    receivable_workplace_bitrix_session_secret: str | None = None
    receivable_workplace_bitrix_session_ttl_seconds: int = 3600
    receivable_workplace_bitrix_rest_timeout_seconds: float = 6.0
    customer_settlements_enabled: bool = False
    customer_settlements_eligibility_enabled: bool = False
    customer_settlements_shadow_enabled: bool = False
    customer_settlements_expected_database_name: str | None = None
    customer_settlements_organization_ref: str | None = None
    customer_settlements_organization_guid: str | None = None
    customer_settlements_opening_organization_field: str | None = None
    customer_settlements_movement_organization_field: str | None = None
    customer_settlements_counterparty_inn_field: str = "_Fld611"
    customer_settlements_source_mode: str = "onec_canonical_mutual_statement_7002"
    customer_settlements_source_validated: bool = False
    customer_settlements_mapping_mode: str = "manual_confirmed"
    customer_settlements_access_mode: Literal["pilot_whitelist", "all_linked"] = "pilot_whitelist"
    customer_settlements_max_scope_users: int = Field(default=10, ge=1, le=100_000)
    customer_settlements_excluded_counterparty_hashes: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    customer_settlements_query_timeout_seconds: int = 30
    customer_settlements_stale_after_seconds: int = 2 * 60 * 60
    customer_settlements_hide_after_seconds: int = 6 * 60 * 60
    customer_settlements_mapping_stale_after_seconds: int = 2 * 60 * 60
    customer_settlements_success_retention_days: int = 30
    customer_settlements_failed_retention_days: int = 7
    customer_settlements_jti_retention_hours: int = 24
    customer_settlements_assertion_issuer: str = "master-mobile.ru"
    customer_settlements_assertion_audience: str = "pricing-service:customer-settlements"
    customer_settlements_assertion_active_kid: str | None = None
    customer_settlements_assertion_active_secret: str | None = None
    customer_settlements_assertion_previous_kid: str | None = None
    customer_settlements_assertion_previous_secret: str | None = None
    customer_settlements_assertion_ttl_seconds: int = 60
    customer_settlements_assertion_clock_skew_seconds: int = 30
    customer_settlements_allowed_source_ips: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    customer_settlements_correlation_salt: str | None = None
    customer_settlements_crm_webhook_url: str | None = None
    customer_settlements_crm_timeout_seconds: float = 6.0
    customer_settlements_alerts_enabled: bool = False
    customer_settlements_alert_task_id: str | None = None
    customer_settlements_alert_webhook_url: str | None = None
    customer_settlements_alert_repeat_seconds: int = 6 * 60 * 60
    executive_dashboard_finance_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/finance_snapshot.json"
    )
    executive_dashboard_cashflow_period_cache_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/cashflow_period_cache.json"
    )
    executive_dashboard_warehouse_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/warehouse_snapshot.json"
    )
    executive_dashboard_instruments_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/infrastructure_snapshot.json"
    )
    executive_dashboard_instruments_max_lag_minutes: int = 30
    executive_dashboard_owner_cash_control_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/owner_cash_transit_snapshot.json"
    )
    executive_dashboard_sales_plan_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/sales_plan_monthly_snapshot.json"
    )
    executive_management_balance_bp_tax_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/bp_tax_snapshot.json"
    )
    executive_management_balance_bp_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/bp_balance_snapshot.json"
    )
    executive_management_balance_opening_equity_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/"
        "management-opening-equity/2026-01-01/current.json"
    )
    executive_dashboard_bp_tax_accrual_root: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/bp-tax-accruals"
    )
    executive_management_balance_payroll_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/employee_payroll_balance_snapshot.json"
    )
    executive_dashboard_source_max_lag_days: int = 1
    yandex_metrika_token: str | None = None
    yandex_metrika_counter_id: str = "49993429"
    yandex_metrika_timeout_seconds: float = 20.0
    executive_management_balance_accounting_database_url: str | None = None
    executive_management_balance_tolerance_rub: float = 1.0
    executive_service_accrual_source_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/service_accrual_source_snapshot.json"
    )
    executive_dashboard_bitrix_enabled: bool = False
    executive_dashboard_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    executive_dashboard_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    executive_dashboard_bitrix_full_access_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    executive_dashboard_bitrix_domain_access_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    executive_dashboard_access_rules_json: str | None = None
    executive_dashboard_bitrix_session_secret: str | None = None
    executive_dashboard_bitrix_session_ttl_seconds: int = 3600
    executive_dashboard_bitrix_rest_timeout_seconds: float = 6.0
    customer_price_type_bitrix_enabled: bool = False
    customer_price_type_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    customer_price_type_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    customer_price_type_bitrix_full_access_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    customer_price_type_access_rules_json: str | None = None
    customer_price_type_bitrix_session_secret: str | None = None
    customer_price_type_bitrix_session_ttl_seconds: int = 3600
    customer_price_type_bitrix_rest_timeout_seconds: float = 6.0
    management_receivables_max_lag_days: int = 1
    management_staffing_max_lag_days: int = 1
    management_task_payloads_max_lag_days: int = 1
    management_telephony_max_lag_days: int = 1
    management_task_efficiency_database_url: str | None = None
    management_task_efficiency_schema: str = "reconciliation"
    management_task_efficiency_source_scope: str = "personal_tasks_on_time_share_v1"
    management_task_efficiency_low_threshold_pct: float = 80.0
    weekly_kpi_artifact_dir: str = "reports/weekly_kpi"
    logistics_bot_token: str | None = None
    logistics_bot_poll_timeout_seconds: int = 30
    logistics_bot_webhook_secret: str | None = None
    logistics_bot_webhook_url: str | None = None
    logistics_web_session_secret: str | None = None
    logistics_web_session_ttl_seconds: int = 8 * 60 * 60
    logistics_web_fallback_token_ttl_seconds: int = 5 * 60
    logistics_bitrix_app_enabled: bool = False
    logistics_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    logistics_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    logistics_bitrix_session_secret: str | None = None
    logistics_bitrix_session_ttl_seconds: int = 3600
    logistics_bitrix_rest_timeout_seconds: float = 6.0
    logistics_stage_automation_enabled: bool = False
    logistics_stage_pilot_warehouse_external_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    logistics_stage_worker_batch_size: int = 50
    pickup_ready_sms_enabled: bool = False
    pickup_ready_sms_pilot_warehouse_external_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    logistics_transfer_assistant_pickup_hold_days: int = 7

    # Embeddings / matching pipeline
    embeddings_model: str = "text-embedding-3-small"
    embeddings_batch_size: int = 64
    embeddings_dir: str = "embeddings"
    matching_top_k: int = 20
    matching_top_k_llm: int = 5
    matching_min_llm_confidence: float = 0.60
    matching_min_embed_score: float = 0.40
    matching_min_gap: float = 0.02

    # Smartphone releases / news ingestion
    smartphone_releases_enabled: bool = False
    smartphone_news_api_base_url: str | None = "https://newsapi.org/v2/everything"
    smartphone_news_api_key: str | None = None
    smartphone_news_language: str = "ru,en"
    smartphone_news_query: str = '"смартфон" OR "smartphone" OR "phone launch"'
    smartphone_news_days_back: int = 5
    smartphone_news_page_size: int = 10
    smartphone_news_max_pages: int = 1
    smartphone_news_max_items: int | None = 40
    smartphone_release_request_delay_seconds: float = 0.25
    smartphone_release_llm_model: str | None = None
    smartphone_gsmarena_enabled: bool = False
    smartphone_gsmarena_rss_url: str = "https://www.gsmarena.com/rss-news-reviews.php"
    smartphone_gsmarena_max_items: int | None = 40

    # Yandex Direct / demand
    yandex_direct_api_token: str | None = None
    yandex_direct_api_base_url: str = "https://api.direct.yandex.ru/json/v5/keywordsresearch"
    yandex_default_region: str = "225"  # Russia (пример кода региона)
    yandex_direct_timeout: float = 10.0
    yandex_direct_batch_size: int = 100
    yandex_direct_rps_limit: float | None = None
    yandex_direct_client_login: str | None = None
    yandex_demand_days_window: int = 30
    yandex_demand_update_limit: int = 200
    yandex_demand_staleness_days: int = 7
    feature_yandex_demand_enabled: bool = False
    yandex_wordstat_enabled: bool = False
    yandex_wordstat_base_url: str = "https://api.wordstat.yandex.net"
    yandex_wordstat_devices: str = "all"

    phone_model_autocreate_from_competitor_enabled: bool = True
    phone_model_autocreate_min_confidence: float = 0.85
    phone_model_autocreate_min_confidence_onec: float = 0.90
    phone_model_alias_review_enabled: bool = True

    # CORS / UI
    cors_allow_origins: str | None = None  # comma-separated
    cors_allow_credentials: bool = False
    cors_allow_methods: str = "*"
    cors_allow_headers: str = "*"

    # API auth (Basic)
    api_basic_user: str | None = None
    api_basic_password: str | None = None

    # Bitrix24 embedded matching app
    matching_bitrix_enabled: bool = False
    matching_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    matching_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    matching_bitrix_allowed_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    matching_bitrix_session_secret: str | None = None
    matching_bitrix_session_ttl_seconds: int = 3600
    matching_bitrix_rest_timeout_seconds: float = 6.0
    procurement_labels_bitrix_enabled: bool = False
    procurement_labels_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    procurement_labels_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    procurement_labels_bitrix_allowed_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    procurement_labels_bitrix_session_secret: str | None = None
    procurement_labels_bitrix_session_ttl_seconds: int = 3600
    procurement_labels_bitrix_rest_timeout_seconds: float = 6.0
    procurement_labels_bitrix_webhook_url: str | None = None
    procurement_bitrix_webhook_url: str | None = None
    bitrix_box_webhook_base: str | None = None
    procurement_labels_bitrix_root_folder_id: int | None = None
    procurement_labels_entity_type_id: int = 1056
    procurement_labels_mapping_path: str = "build/bitrix/procurement_order_mapping.json"
    procurement_labels_artifact_dir: str = ".local/procurement-labels"
    procurement_labels_barcode_catalog_path: str = ".local/procurement-labels/barcodes.json"
    procurement_labels_certificate_catalog_path: str = ".local/procurement-labels/certificates.json"
    procurement_assortment_manual_overrides_path: str = (
        "config/assortment/display-manual-overrides.json"
    )
    procurement_order_formation_entity_type_id: int | None = None
    procurement_order_formation_mapping_path: str = "build/bitrix/order_formation_mapping.json"
    procurement_product_row_exclusions_path: str = "config/procurement-product-row-exclusions.json"
    procurement_order_formation_bitrix_enabled: bool = False
    procurement_order_formation_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    procurement_order_formation_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    procurement_order_formation_bitrix_allowed_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["115204", "130757", "4241"]
    )
    procurement_order_formation_bitrix_session_secret: str | None = None
    procurement_order_formation_bitrix_session_ttl_seconds: int = 3600
    procurement_order_formation_bitrix_rest_timeout_seconds: float = 6.0
    procurement_order_formation_classification_approver_user_ids: Annotated[list[str], NoDecode] = (
        Field(default_factory=lambda: ["130757", "4241"])
    )
    procurement_order_formation_lifecycle_approver_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["130757", "4241"]
    )
    procurement_order_formation_display_responsible_user_id: str = "130757"
    procurement_order_formation_property_apply_enabled: bool = False
    procurement_order_formation_onec_apply_enabled: bool = False
    procurement_family_review_decisions_enabled: bool = False
    order_closure_bitrix_enabled: bool = False
    order_closure_bitrix_allowed_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_closure_bitrix_allowed_member_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_closure_bitrix_allowed_user_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    order_closure_operator_user_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    order_closure_bitrix_session_secret: str | None = None
    order_closure_bitrix_session_ttl_seconds: int = 3600
    order_closure_apply_enabled: bool = False
    order_closure_internal_api_token: str | None = None
    procurement_order_formation_label_max_pages: int = Field(default=1000, ge=1, le=10000)
    procurement_product_card_mapping_path: str = (
        "build/bitrix/procurement_product_card_mapping.json"
    )
    procurement_product_card_apply_enabled: bool = False
    procurement_product_card_catalog_id: int = 17
    procurement_product_card_placement: str = ""
    procurement_product_card_insights_base_url: str = (
        "https://crm.master-mobile.ru/marketplace/placement/190"
    )
    procurement_product_card_stale_hours: int = 36
    procurement_product_card_batch_size: int = 40
    master_mobile_catalog_base_url: str = "https://master-mobile.ru"
    master_mobile_catalog_timeout_seconds: float = 15.0
    master_mobile_catalog_max_attempts: int = 3
    master_mobile_catalog_max_workers: int = 4

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", env_nested_delimiter="__", extra="ignore"
    )

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, value):
        if isinstance(value, bool):
            return value
        if value is None:
            return True
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "debug", "dev", "development"}:
            return True
        if normalized in {"0", "false", "no", "off", "release", "prod", "production"}:
            return False
        return value

    @model_validator(mode="after")
    def _reject_production_debug(self):
        if self.environment.strip().casefold() in {"prod", "production"} and self.debug:
            raise ValueError("DEBUG must be disabled in production")
        return self

    @field_validator(
        "expertise_bitrix_stage_map",
        "expertise_bitrix_field_map",
        "site_defect_archive_bitrix_working_stage_map",
        "site_defect_archive_bitrix_field_map",
        "expertise_sla_store_group_map",
        "card_balance_bitrix_stage_map",
        "card_balance_bitrix_field_map",
        "card_balance_bitrix_employee_overrides",
        "receivable_bitrix_stage_map",
        "receivable_bitrix_field_map",
        "receivable_credit_decision_stage_map",
        "receivable_credit_decision_field_map",
        "telephony_service_line_labels",
        mode="before",
    )
    @classmethod
    def _parse_string_mapping(cls, value: Any) -> dict[str, str]:
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return {str(key): str(item) for key, item in value.items() if item is not None}
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("expected JSON object")
            return {str(key): str(item) for key, item in parsed.items() if item is not None}
        raise ValueError("unsupported mapping value")

    @field_validator("receivable_bitrix_enum_map", mode="before")
    @classmethod
    def _parse_nested_string_mapping(cls, value: Any) -> dict[str, dict[str, str]]:
        if value in (None, ""):
            return {}
        if isinstance(value, str):
            parsed = json.loads(value)
        else:
            parsed = value
        if not isinstance(parsed, dict):
            raise ValueError("expected JSON object")
        result: dict[str, dict[str, str]] = {}
        for key, nested in parsed.items():
            if not isinstance(nested, dict):
                continue
            result[str(key)] = {
                str(nested_key): str(nested_value)
                for nested_key, nested_value in nested.items()
                if nested_value is not None
            }
        return result

    @field_validator(
        "telephony_review_line_ids",
        "bank_payments_own_accounts",
        "card_balance_pilot_cashbox_codes",
        "order_payment_control_closure_allowed_reasons",
        "order_fulfillment_known_raw_deliveries",
        "order_fulfillment_bot_source_chat_ids",
        "order_fulfillment_pickup_warehouse_external_ids",
        "order_fulfillment_inventory_won_warehouse_external_ids",
        "order_fulfillment_bot_allowed_domains",
        "order_fulfillment_bot_allowed_member_ids",
        "receivable_workflow_department_refs",
        "receivable_workflow_department_names",
        "receivable_workplace_bitrix_allowed_domains",
        "receivable_workplace_bitrix_allowed_member_ids",
        "receivable_workplace_bitrix_full_access_user_ids",
        "receivable_credit_decision_approver_user_ids",
        "receivable_credit_decision_pilot_counterparty_codes",
        "customer_settlements_allowed_source_ips",
        "customer_settlements_excluded_counterparty_hashes",
        "executive_dashboard_bitrix_allowed_domains",
        "executive_dashboard_bitrix_allowed_member_ids",
        "executive_dashboard_bitrix_full_access_user_ids",
        "executive_dashboard_bitrix_domain_access_user_ids",
        "customer_price_type_bitrix_allowed_domains",
        "customer_price_type_bitrix_allowed_member_ids",
        "customer_price_type_bitrix_full_access_user_ids",
        "site_service_requests_ui_allowed_domains",
        "site_service_requests_ui_allowed_member_ids",
        "logistics_bitrix_allowed_domains",
        "logistics_bitrix_allowed_member_ids",
        "logistics_stage_pilot_warehouse_external_ids",
        "pickup_ready_sms_pilot_warehouse_external_ids",
        "customer_return_service_links_roles",
        mode="before",
    )
    @classmethod
    def _parse_string_list(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if not isinstance(parsed, list):
                    raise ValueError("expected JSON array")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [chunk.strip() for chunk in stripped.split(",") if chunk.strip()]
        raise ValueError("unsupported list value")

    @field_validator(
        "matching_bitrix_allowed_domains",
        "matching_bitrix_allowed_member_ids",
        "matching_bitrix_allowed_user_ids",
        "procurement_labels_bitrix_allowed_domains",
        "procurement_labels_bitrix_allowed_member_ids",
        "procurement_labels_bitrix_allowed_user_ids",
        "procurement_order_formation_bitrix_allowed_domains",
        "procurement_order_formation_bitrix_allowed_member_ids",
        "procurement_order_formation_bitrix_allowed_user_ids",
        "procurement_order_formation_classification_approver_user_ids",
        "procurement_order_formation_lifecycle_approver_user_ids",
        "order_closure_bitrix_allowed_domains",
        "order_closure_bitrix_allowed_member_ids",
        "order_closure_bitrix_allowed_user_ids",
        "order_closure_operator_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_matching_string_list(cls, value: Any) -> list[str]:
        return cls._parse_string_list(value)

    @field_validator(
        "expertise_bitrix_store_department_map",
        "expertise_sla_delivery_days_map",
        "expertise_sla_review_days_map",
        "expertise_alarm_review_primary_days_map",
        "expertise_alarm_review_escalation_days_map",
        "expertise_alarm_review_top_escalation_days_map",
        "receivable_department_manager_map",
        mode="before",
    )
    @classmethod
    def _parse_int_mapping(cls, value: Any) -> dict[str, int]:
        if value in (None, ""):
            return {}
        if isinstance(value, dict):
            return {str(key): int(item) for key, item in value.items() if item not in (None, "")}
        if isinstance(value, str):
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                raise ValueError("expected JSON object")
            return {str(key): int(item) for key, item in parsed.items() if item not in (None, "")}
        raise ValueError("unsupported mapping value")

    @field_validator("order_fulfillment_point_task_routes", mode="before")
    @classmethod
    def _parse_order_fulfillment_point_task_routes(cls, value: Any) -> dict[str, dict[str, int]]:
        if value in (None, ""):
            return {}
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise ValueError("expected JSON object")
        result: dict[str, dict[str, int]] = {}
        allowed_roles = {"operator", "senior"}
        for warehouse_ref, route in parsed.items():
            if not isinstance(route, dict):
                raise ValueError("point task route must be an object")
            unknown = set(route) - allowed_roles
            if unknown:
                raise ValueError(f"unsupported point task roles: {sorted(unknown)}")
            normalized = {
                role: int(user_id) for role, user_id in route.items() if user_id not in (None, "")
            }
            if normalized:
                result[str(warehouse_ref)] = normalized
        return result

    @field_validator("order_fulfillment_pickup_warehouse_aliases", mode="before")
    @classmethod
    def _parse_order_fulfillment_pickup_warehouse_aliases(
        cls,
        value: Any,
    ) -> dict[str, list[str]]:
        if value in (None, ""):
            return {}
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, dict):
            raise ValueError("expected JSON object")
        result: dict[str, list[str]] = {}
        for warehouse_ref, aliases in parsed.items():
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list):
                raise ValueError("pickup warehouse aliases must be a string or array")
            normalized = list(
                dict.fromkeys(str(alias).strip() for alias in aliases if str(alias).strip())
            )
            if normalized:
                result[str(warehouse_ref)] = normalized
        return result

    @field_validator(
        "expertise_bitrix_notify_auditor_user_ids",
        "expertise_alarm_review_primary_user_ids",
        "expertise_alarm_review_escalation_user_ids",
        "expertise_alarm_review_top_escalation_user_ids",
        "order_fulfillment_notify_business_user_ids",
        "order_fulfillment_notify_tech_user_ids",
        "order_fulfillment_site_chat_apply_author_ids",
        "order_fulfillment_courier_chat_apply_author_ids",
        "order_fulfillment_bot_excluded_user_ids",
        "site_service_requests_first_line_user_ids",
        "order_fulfillment_pickup_notification_confirmer_ids",
        "customer_return_bitrix_accomplice_user_ids",
        "customer_return_bitrix_auditor_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_int_list(cls, value: Any) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return [int(item) for item in value if item not in (None, "")]
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if not isinstance(parsed, list):
                    raise ValueError("expected JSON array")
                return [int(item) for item in parsed if item not in (None, "")]
            return [int(chunk.strip()) for chunk in stripped.split(",") if chunk.strip()]
        raise ValueError("unsupported list value")

    @field_validator(
        "site_service_requests_ui_allowed_user_ids",
        "site_service_requests_ui_write_allowed_user_ids",
        mode="before",
    )
    @classmethod
    def _parse_site_service_request_ui_user_ids(cls, value: Any) -> list[int]:
        if value in (None, ""):
            return []
        parsed = (
            json.loads(value) if isinstance(value, str) and value.strip().startswith("[") else value
        )
        if isinstance(parsed, str):
            parsed = [chunk.strip() for chunk in parsed.split(",") if chunk.strip()]
        if not isinstance(parsed, list):
            raise ValueError("expected user ID array")
        result: list[int] = []
        for item in parsed:
            if type(item) is int:
                user_id = item
            elif isinstance(item, str) and item.isascii() and item.isdigit():
                user_id = int(item)
            else:
                raise ValueError("user IDs must be positive integers")
            if user_id <= 0:
                raise ValueError("user IDs must be positive integers")
            result.append(user_id)
        return result


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
