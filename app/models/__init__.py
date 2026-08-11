import os

from app.models.base import Base

if not os.getenv("ALEMBIC_SKIP_MODEL_IMPORT"):
    from app.models.card_balance_reconciliation import (
        CardBalanceCashbox,
        CardBalanceReconciliation,
        CardBalanceReconciliationEvent,
    )
    from app.models.competitor import Competitor
    from app.models.competitor_ftp import (
        CompetitorFtpFile,
        CompetitorFtpRawRow,
        CompetitorFtpRecord,
    )
    from app.models.competitor_item import (
        CompetitorItem,
        CompetitorItemParseStatus,
        CompetitorItemSnapshot,
    )
    from app.models.competitor_item_compatibility import CompetitorItemCompatibility
    from app.models.competitor_item_match import CompetitorItemMatch
    from app.models.competitor_item_url_alias import CompetitorItemUrlAlias
    from app.models.competitor_manufacturer_map import CompetitorManufacturerMap
    from app.models.competitor_price import CompetitorPrice
    from app.models.counterparty_duplicate_case import CounterpartyDuplicateCase
    from app.models.counterparty_folder_snapshot import CounterpartyFolderSnapshot
    from app.models.counterparty_manager_assignment import CounterpartyManagerAssignment
    from app.models.customer_price_type import (
        CustomerPriceTypeCase,
        CustomerPriceTypeCaseEvent,
        CustomerPriceTypeProfile,
        CustomerPriceTypeQualitySample,
        CustomerPriceTypeReviewBatch,
        CustomerPriceTypeReviewBatchItem,
        CustomerPriceTypeRun,
        CustomerPriceTypeSnapshot,
    )
    from app.models.customer_settlement import (
        CustomerAccount,
        CustomerAccountSiteBinding,
        CustomerAccountSourceBinding,
        CustomerSettlementAssertionJti,
        CustomerSettlementBalance,
        CustomerSettlementMappingEntry,
        CustomerSettlementMappingRevision,
        CustomerSettlementPilotAccess,
        CustomerSettlementRevision,
    )
    from app.models.device_brand import (
        CompatibilityMappingDecision,
        DeviceBrand,
        DeviceBrandAlias,
    )
    from app.models.device_model import Keyword, KeywordDemand, PhoneModel, PhoneModelAlias
    from app.models.executive_dashboard import (
        ExecutiveActionItem,
        ExecutiveDashboardSnapshot,
        ExecutiveManagementBalanceAudit,
        ExecutiveManagementBalanceLine,
        ExecutiveManagementBalanceSnapshot,
        ExecutiveServiceAccrualAudit,
        ExecutiveServiceAccrualEntry,
        ExecutiveServiceAccrualRule,
        ExecutiveSourceFreshness,
    )
    from app.models.expertise import ExpertiseCase, ExpertiseCaseAttachment, ExpertiseCaseEvent
    from app.models.logistics import (
        LogisticsBotSession,
        LogisticsBotSessionPhoto,
        LogisticsDraft,
        LogisticsDraftItem,
        LogisticsDriver,
        LogisticsEventPhoto,
        LogisticsManualReview,
        LogisticsRouteRun,
        LogisticsRouteRunItem,
        LogisticsTransfer,
        LogisticsTransferEvent,
        LogisticsTransferState,
        LogisticsUser,
        LogisticsWarehouse,
    )
    from app.models.matching_property_mapping import (
        MatchingPropertyProfile,
        MatchingPropertyRule,
        MatchingPropertyRuleAudit,
        MatchingPropertyValueMap,
    )
    from app.models.onec_sales_daily_kpi import OneCSalesDailyKpi
    from app.models.orchestration import (
        OrchestrationApiRequest,
        OrchestrationDeliveryAttempt,
        OrchestrationDeliveryIntent,
        OrchestrationJobRun,
    )
    from app.models.price_recommendation import PriceRecommendation
    from app.models.pricing_strategy_version import PricingStrategyVersion
    from app.models.procurement_order_formation import (
        ProcurementClassificationProposal,
        ProcurementLifecycleTransitionProposal,
        ProcurementOrderFormation,
        ProcurementOrderFormationEvent,
        ProcurementOrderFormationLine,
        ProcurementSupplierProfile,
    )
    from app.models.product import Product
    from app.models.product_compatibility import ProductCompatibility
    from app.models.product_competitor_item_decision import ProductCompetitorItemDecision
    from app.models.product_live_candidate_cache import ProductLiveCandidateCache
    from app.models.product_match import ProductMatch
    from app.models.product_match_override import ProductMatchOverride
    from app.models.product_match_rejection import ProductMatchRejection
    from app.models.product_phone_model import ProductPhoneModel
    from app.models.product_sku_plan import ProductSkuPlan
    from app.models.product_stock import ProductStock
    from app.models.receivable_balance_snapshot import ReceivableBalanceSnapshot
    from app.models.receivable_case import ReceivableCase
    from app.models.receivable_credit_decision import ReceivableCreditDecisionOperation
    from app.models.receivable_ledger_event import ReceivableLedgerEvent
    from app.models.receivable_reconciliation_snapshot import ReceivableReconciliationSnapshot
    from app.models.receivable_work import (
        ReceivableSmsLog,
        ReceivableSupervisorNote,
        ReceivableWorkEvent,
        ReceivableWorkItem,
    )
    from app.models.receivable_workplace_cache import (
        ReceivableBitrixUserAccess,
        ReceivableFolderRecommendationCache,
        ReceivableOpenDebtCache,
    )
    from app.models.return_scheme_alert_batch import ReturnSchemeAlertBatch
    from app.models.return_scheme_incident import ReturnSchemeIncident
    from app.models.site_defect_archive import (
        SiteDefectArchiveCase,
        SiteDefectArchiveFile,
        SiteDefectArchiveMessage,
    )
    from app.models.site_order_fulfillment import (
        BitrixChatMention,
        BitrixChatMessage,
        SiteOrderExecutionCase,
        SiteOrderExecutionEvent,
    )
    from app.models.smartphone_release import ReleaseStatus, SmartphoneRelease, SourceType
    from app.models.staff_member import StaffMember
    from app.models.staffing_snapshot import StaffingSnapshot
    from app.models.store_shift_fact import StoreShiftFact
    from app.models.store_shift_plan import StoreShiftPlan
    from app.models.telephony import TelephonyUserLineSnapshot
    from app.models.weekly_kpi_report import (
        WeeklyKpiIngestRequest,
        WeeklyKpiReportMetricSnapshot,
        WeeklyKpiReportSnapshot,
    )
    from app.models.weekly_smartphone_digest import WeeklySmartphoneDigest

    __all__ = [
        "Base",
        "Product",
        "ProductStock",
        "ProcurementOrderFormation",
        "ProcurementOrderFormationLine",
        "ProcurementOrderFormationEvent",
        "ProcurementClassificationProposal",
        "ProcurementSupplierProfile",
        "ProcurementLifecycleTransitionProposal",
        "ProductCompatibility",
        "Competitor",
        "CompetitorPrice",
        "CardBalanceCashbox",
        "CardBalanceReconciliation",
        "CardBalanceReconciliationEvent",
        "CounterpartyDuplicateCase",
        "CounterpartyFolderSnapshot",
        "CustomerSettlementRevision",
        "CustomerAccount",
        "CustomerAccountSiteBinding",
        "CustomerAccountSourceBinding",
        "CustomerSettlementBalance",
        "CustomerSettlementMappingRevision",
        "CustomerSettlementMappingEntry",
        "CustomerSettlementPilotAccess",
        "CustomerSettlementAssertionJti",
        "CustomerPriceTypeProfile",
        "CustomerPriceTypeRun",
        "CustomerPriceTypeSnapshot",
        "CustomerPriceTypeCase",
        "CustomerPriceTypeCaseEvent",
        "CustomerPriceTypeQualitySample",
        "CustomerPriceTypeReviewBatch",
        "CustomerPriceTypeReviewBatchItem",
        "CompetitorFtpFile",
        "CompetitorFtpRawRow",
        "CompetitorFtpRecord",
        "ProductMatch",
        "ProductMatchOverride",
        "ProductMatchRejection",
        "ProductCompetitorItemDecision",
        "ProductLiveCandidateCache",
        "PriceRecommendation",
        "PricingStrategyVersion",
        "PhoneModel",
        "PhoneModelAlias",
        "DeviceBrand",
        "DeviceBrandAlias",
        "CompatibilityMappingDecision",
        "Keyword",
        "KeywordDemand",
        "ExpertiseCase",
        "ExpertiseCaseEvent",
        "ExpertiseCaseAttachment",
        "ExecutiveDashboardSnapshot",
        "ExecutiveActionItem",
        "ExecutiveSourceFreshness",
        "ExecutiveManagementBalanceSnapshot",
        "ExecutiveManagementBalanceLine",
        "ExecutiveManagementBalanceAudit",
        "ExecutiveServiceAccrualRule",
        "ExecutiveServiceAccrualEntry",
        "ExecutiveServiceAccrualAudit",
        "LogisticsWarehouse",
        "LogisticsDriver",
        "LogisticsUser",
        "LogisticsBotSession",
        "LogisticsBotSessionPhoto",
        "LogisticsTransfer",
        "LogisticsTransferState",
        "LogisticsTransferEvent",
        "LogisticsEventPhoto",
        "LogisticsDraft",
        "LogisticsDraftItem",
        "LogisticsRouteRun",
        "LogisticsRouteRunItem",
        "LogisticsManualReview",
        "OneCSalesDailyKpi",
        "OrchestrationApiRequest",
        "OrchestrationJobRun",
        "OrchestrationDeliveryIntent",
        "OrchestrationDeliveryAttempt",
        "MatchingPropertyProfile",
        "MatchingPropertyRule",
        "MatchingPropertyRuleAudit",
        "MatchingPropertyValueMap",
        "ProductPhoneModel",
        "ProductSkuPlan",
        "ReturnSchemeIncident",
        "ReturnSchemeAlertBatch",
        "SmartphoneRelease",
        "ReleaseStatus",
        "SourceType",
        "CompetitorItem",
        "CompetitorItemParseStatus",
        "CompetitorItemSnapshot",
        "CompetitorItemCompatibility",
        "CompetitorItemMatch",
        "CompetitorItemUrlAlias",
        "CompetitorManufacturerMap",
        "CounterpartyManagerAssignment",
        "StaffMember",
        "StoreShiftPlan",
        "StoreShiftFact",
        "StaffingSnapshot",
        "TelephonyUserLineSnapshot",
        "SiteDefectArchiveCase",
        "SiteDefectArchiveMessage",
        "SiteDefectArchiveFile",
        "SiteOrderExecutionCase",
        "BitrixChatMessage",
        "BitrixChatMention",
        "SiteOrderExecutionEvent",
        "WeeklySmartphoneDigest",
        "WeeklyKpiReportSnapshot",
        "WeeklyKpiReportMetricSnapshot",
        "WeeklyKpiIngestRequest",
        "ReceivableLedgerEvent",
        "ReceivableBalanceSnapshot",
        "ReceivableReconciliationSnapshot",
        "ReceivableCase",
        "ReceivableCreditDecisionOperation",
        "ReceivableWorkItem",
        "ReceivableWorkEvent",
        "ReceivableSmsLog",
        "ReceivableSupervisorNote",
        "ReceivableOpenDebtCache",
        "ReceivableFolderRecommendationCache",
        "ReceivableBitrixUserAccess",
    ]
else:
    __all__ = ["Base"]
