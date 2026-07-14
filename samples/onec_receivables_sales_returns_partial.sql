-- Partial live projector for receivables foundation.
-- Scope:
-- - active receivable movements from _AccumRg7550 /
--   "ВзаиморасчетыСКонтрагентамиПоДокументамРасчетов" for sales and returns;
-- - customer cash receipts / payments (_Document196) by direct document mapping;
-- - additional customer settlements (_Document201) by direct document mapping.
--
-- This query is intentionally conservative:
-- - it reconstructs confirmed cash payments and one additional settlement flow,
--   but still does not include all bank payments, debt adjustments, settlements,
--   and all legacy payment flows;
-- - it is suitable for a controlled partial backfill and much closer to the
--   accounting statement than sales/returns-only mode, but still not the final
--   production ledger of full receivables history.

WITH base_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._Fld7559RRef AS counterparty_rref,
        r._Period AS movement_period,
        CAST(r._Fld7562 AS decimal(18, 2)) AS amount_delta
    FROM _AccumRg7550 AS r WITH (NOLOCK)
    WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND r._Active = 0x01
      AND r._Fld7559RRef <> 0x00000000000000000000000000000000
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
),
register_events AS (
    SELECT
        'onec_sales_returns_partial' AS source,
        CASE
            WHEN b.recorder_tref = 0x000000CB THEN 'sale'
            WHEN b.recorder_tref = 0x0000006D THEN 'return'
        END AS event_type,
        master.dbo.fn_varbintohexstr(b.recorder_rref) AS external_document_ref,
        COALESCE(sale._Number, ret._Number) AS external_document_number,
        COALESCE(sale._Date_Time, ret._Date_Time, MAX(b.movement_period)) AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        COALESCE(
            master.dbo.fn_varbintohexstr(sale_actor._IDRRef),
            master.dbo.fn_varbintohexstr(ret_actor._IDRRef)
        ) AS manager_ref,
        COALESCE(sale_actor._Description, ret_actor._Description) AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        CAST(NULL AS int) AS line_no,
        SUM(b.amount_delta) AS amount_delta
    FROM base_register AS b
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = b.counterparty_rref
    LEFT JOIN _Document203 AS sale WITH (NOLOCK)
        ON b.recorder_tref = 0x000000CB
       AND sale._IDRRef = b.recorder_rref
    LEFT JOIN _Document109 AS ret WITH (NOLOCK)
        ON b.recorder_tref = 0x0000006D
       AND ret._IDRRef = b.recorder_rref
    LEFT JOIN _Reference54 AS sale_actor WITH (NOLOCK)
        ON b.recorder_tref = 0x000000CB
       AND sale_actor._IDRRef = sale._Fld4942RRef
    LEFT JOIN _Reference54 AS ret_actor WITH (NOLOCK)
        ON b.recorder_tref = 0x0000006D
       AND ret_actor._IDRRef = ret._Fld1682RRef
    GROUP BY
        b.recorder_tref,
        b.recorder_rref,
        sale._Number,
        sale._Date_Time,
        ret._Number,
        ret._Date_Time,
        counterparty._IDRRef,
        counterparty._Description,
        counterparty._Fld9516,
        counterparty._Fld9865,
        counterparty._Fld9866,
        sale_actor._IDRRef,
        sale_actor._Description,
        ret_actor._IDRRef,
        ret_actor._Description
),
payment_events AS (
    SELECT
        'onec_sales_returns_partial' AS source,
        'payment' AS event_type,
        master.dbo.fn_varbintohexstr(pko._IDRRef) AS external_document_ref,
        pko._Number AS external_document_number,
        pko._Date_Time AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        master.dbo.fn_varbintohexstr(sale_actor._IDRRef) AS manager_ref,
        sale_actor._Description AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        CAST(NULL AS int) AS line_no,
        CAST(-pko._Fld4688 AS decimal(18, 2)) AS amount_delta
    FROM _Document196 AS pko WITH (NOLOCK)
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = pko._Fld4684_RRRef
    LEFT JOIN _Document203 AS base_sale WITH (NOLOCK)
        ON pko._Fld4697_RTRef = 0x000000CB
       AND base_sale._IDRRef = pko._Fld4697_RRRef
    LEFT JOIN _Reference54 AS sale_actor WITH (NOLOCK)
        ON sale_actor._IDRRef = base_sale._Fld4942RRef
    WHERE pko._Marked = 0x00
      AND pko._Posted = 0x01
      AND pko._Fld4684_RTRef = 0x00000036
      AND pko._Fld4684_RRRef <> 0x00000000000000000000000000000000
      AND (:window_start IS NULL OR pko._Date_Time >= :window_start)
      AND (:window_end IS NULL OR pko._Date_Time < :window_end)
),
settlement_events AS (
    SELECT
        'onec_sales_returns_partial' AS source,
        'settlement' AS event_type,
        master.dbo.fn_varbintohexstr(doc._IDRRef) AS external_document_ref,
        doc._Number AS external_document_number,
        doc._Date_Time AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        CAST(NULL AS int) AS line_no,
        CAST(-doc._Fld4852 AS decimal(18, 2)) AS amount_delta
    FROM _Document201 AS doc WITH (NOLOCK)
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = doc._Fld4848_RRRef
    WHERE doc._Marked = 0x00
      AND doc._Posted = 0x01
      AND doc._Fld4848_RTRef = 0x00000036
      AND doc._Fld4848_RRRef <> 0x00000000000000000000000000000000
      AND (:window_start IS NULL OR doc._Date_Time >= :window_start)
      AND (:window_end IS NULL OR doc._Date_Time < :window_end)
),
summary_extra_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        MAX(r._Period) AS movement_period,
        r._Fld7615RRef AS contract_rref,
        r._Fld7618RRef AS organization_rref,
        r._Fld7619RRef AS counterparty_rref,
        CAST(
            SUM(
                CASE
                    WHEN r._RecordKind = 0 THEN r._Fld7620
                    ELSE -r._Fld7620
                END
            ) AS decimal(18, 2)
        ) AS amount_delta
    FROM _AccumRg7614 AS r WITH (NOLOCK)
    JOIN _Reference37 AS contract WITH (NOLOCK)
        ON contract._IDRRef = r._Fld7615RRef
    WHERE r._Active = 0x01
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND master.dbo.fn_varbintohexstr(contract._Fld515RRef) = '0x9363c6f0a10557bf4822a55db4862286'
      AND r._RecorderTRef = 0x000000BA
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
    GROUP BY
        r._RecorderTRef,
        r._RecorderRRef,
        r._Fld7615RRef,
        r._Fld7618RRef,
        r._Fld7619RRef
    HAVING SUM(
        CASE
            WHEN r._RecordKind = 0 THEN r._Fld7620
            ELSE -r._Fld7620
        END
    ) <> 0
),
summary_extra_events AS (
    SELECT
        'onec_sales_returns_partial' AS source,
        'payment' AS event_type,
        master.dbo.fn_varbintohexstr(r.recorder_rref) AS external_document_ref,
        doc186._Number AS external_document_number,
        COALESCE(
            doc186._Date_Time,
            r.movement_period
        ) AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        master.dbo.fn_varbintohexstr(r.organization_rref) AS store_ref,
        organization._Description AS store_name,
        CAST(NULL AS int) AS line_no,
        r.amount_delta AS amount_delta
    FROM summary_extra_register AS r
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = r.counterparty_rref
    LEFT JOIN _Reference66 AS organization WITH (NOLOCK)
        ON organization._IDRRef = r.organization_rref
    LEFT JOIN _Document186 AS doc186 WITH (NOLOCK)
        ON r.recorder_tref = 0x000000BA
       AND doc186._IDRRef = r.recorder_rref
)
SELECT *
FROM register_events
UNION ALL
SELECT *
FROM payment_events
UNION ALL
SELECT *
FROM settlement_events
UNION ALL
SELECT *
FROM summary_extra_events;
