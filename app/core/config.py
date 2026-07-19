from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Pricing Service"
    environment: str = "development"
    debug: bool = True
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
    competitor_ftp_import_enabled: bool = False
    competitor_ftp_host: str | None = None
    competitor_ftp_port: int = 21
    competitor_ftp_user: str | None = None
    competitor_ftp_password: str | None = None
    competitor_ftp_tls: bool = False
    competitor_ftp_timeout_sec: float = 30.0
    competitor_ftp_sources: str | None = None  # name:directory:pattern with {date}, comma-separated
    competitor_ftp_max_files_per_source: int = 2
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
    logistics_internal_api_token: str | None = None
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
    order_fulfillment_bitrix_webhook_url: str | None = None
    order_fulfillment_artifact_dir: str = ".local/order-fulfillment-pilot"
    order_fulfillment_site_chat_dialog_id: str = "chat733"
    order_fulfillment_spb_courier_chat_dialog_id: str = "chat727"
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
    receivable_workplace_bitrix_session_secret: str | None = None
    receivable_workplace_bitrix_session_ttl_seconds: int = 3600
    receivable_workplace_bitrix_rest_timeout_seconds: float = 6.0
    executive_dashboard_finance_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/finance_snapshot.json"
    )
    executive_dashboard_cashflow_period_cache_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/cashflow_period_cache.json"
    )
    executive_dashboard_warehouse_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/warehouse_snapshot.json"
    )
    executive_dashboard_owner_cash_control_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/owner_cash_transit_snapshot.json"
    )
    executive_dashboard_sales_plan_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/sales_plan_monthly_snapshot.json"
    )
    executive_management_balance_bp_tax_snapshot_path: str = (
        "/var/lib/mm-data-contracts/executive-dashboard/bp_tax_snapshot.json"
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
        "order_fulfillment_known_raw_deliveries",
        "receivable_workflow_department_refs",
        "receivable_workflow_department_names",
        "receivable_workplace_bitrix_allowed_domains",
        "receivable_workplace_bitrix_allowed_member_ids",
        "receivable_workplace_bitrix_full_access_user_ids",
        "executive_dashboard_bitrix_allowed_domains",
        "executive_dashboard_bitrix_allowed_member_ids",
        "executive_dashboard_bitrix_full_access_user_ids",
        "executive_dashboard_bitrix_domain_access_user_ids",
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

    @field_validator(
        "expertise_bitrix_notify_auditor_user_ids",
        "expertise_alarm_review_primary_user_ids",
        "expertise_alarm_review_escalation_user_ids",
        "expertise_alarm_review_top_escalation_user_ids",
        "order_fulfillment_notify_business_user_ids",
        "order_fulfillment_notify_tech_user_ids",
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
