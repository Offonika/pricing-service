-- Partial live projector for receivables foundation.
-- Scope: only confirmed 1C mappings for sales (_Document203) and customer returns (_Document109)
-- via register _AccumRg7550 / "ВзаиморасчетыСКонтрагентамиПоДокументамРасчетов".
--
-- This query is intentionally conservative:
-- - it does not try to reconstruct payments/corrections yet;
-- - manager fields are left NULL until the physical mapping of
--   Counterparty.ОсновнойМенеджерПокупателя is confirmed in SQL;
-- - it is suitable for the first controlled backfill of recent "new debt" cases,
--   not for final production accounting of full receivables history.

WITH base AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._Fld7554RRef AS contract_rref,
        r._Fld7559RRef AS counterparty_rref,
        r._Period AS movement_period,
        CAST(r._Fld7562 AS decimal(18, 2)) AS amount_delta
    FROM _AccumRg7550 AS r WITH (NOLOCK)
    WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND r._Fld7559RRef <> 0x00000000000000000000000000000000
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
)
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
FROM base AS b
JOIN _Reference37 AS contract WITH (NOLOCK)
    ON contract._IDRRef = b.contract_rref
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
WHERE contract._OwnerIDRRef = counterparty._IDRRef
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
ORDER BY
    external_document_date,
    external_document_ref;
