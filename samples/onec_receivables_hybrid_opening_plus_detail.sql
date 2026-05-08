-- Hybrid receivables projector.
-- Strategy:
-- - non-employee counterparties:
--   - opening layer from monthly totals in _AccumRgTn7571 before :opening_balance_date;
--   - detailed events from _AccumRg7550 + payment docs after :window_start.
-- - employee counterparties:
--   - opening layer from summary totals in _AccumRgT7622 on/before :opening_balance_date;
--   - detailed events from summary register _AccumRg7614 after :window_start.
--
-- Recommended first run:
-- - opening_balance_date = 2025-01-01
-- - window_start = 2025-01-01T00:00:00
-- - window_end = NULL
--
-- Caveat:
-- - _AccumRgTn7571 is treated as a monthly turnover layer for the regular
--   receivables contour.
-- - employee summary debt is routed through _AccumRgT7622/_AccumRg7614 because
--   `ВедомостьРасчетыСКонтрагентами` matches that layer, while `_AccumRgTn7571`
--   only covers one side of mutual settlements for such counterparties.

WITH target_organization AS (
    SELECT _IDRRef
    FROM _Reference66 WITH (NOLOCK)
    WHERE _Description = N'MASTER MOBILE'
),
counterparty_tree AS (
    SELECT
        c._IDRRef,
        c._ParentIDRRef,
        c._Description,
        c._Folder,
        CAST(
            CASE
                WHEN LOWER(COALESCE(c._Description, N'')) LIKE N'%сотрудн%' THEN 1
                ELSE 0
            END AS int
        ) AS is_employee_branch
    FROM _Reference54 AS c WITH (NOLOCK)
    WHERE c._ParentIDRRef = 0x00000000000000000000000000000000

    UNION ALL

    SELECT
        child._IDRRef,
        child._ParentIDRRef,
        child._Description,
        child._Folder,
        CAST(
            CASE
                WHEN parent.is_employee_branch = 1 THEN 1
                WHEN LOWER(COALESCE(child._Description, N'')) LIKE N'%сотрудн%' THEN 1
                ELSE 0
            END AS int
        ) AS is_employee_branch
    FROM _Reference54 AS child WITH (NOLOCK)
    JOIN counterparty_tree AS parent
        ON child._ParentIDRRef = parent._IDRRef
),
employee_counterparties AS (
    SELECT DISTINCT _IDRRef
    FROM counterparty_tree
    WHERE _Folder = 0x01
      AND is_employee_branch = 1
),
contract_catalog AS (
    SELECT
        contract._IDRRef,
        master.dbo.fn_varbintohexstr(contract._IDRRef) AS contract_ref,
        contract._Description AS contract_name,
        master.dbo.fn_varbintohexstr(contract._Fld515RRef) AS contract_kind_ref,
        CASE master.dbo.fn_varbintohexstr(contract._Fld515RRef)
            WHEN '0x9363c6f0a10557bf4822a55db4862286' THEN N'С покупателем'
            WHEN '0x95db9a602e142ed645d7ccf13094909f' THEN N'С поставщиком'
            WHEN '0xa49b7e34b5f2cbb643d8f36270f8009f' THEN N'Прочее'
            ELSE N'Неизвестно'
        END AS contract_kind_name
    FROM _Reference37 AS contract WITH (NOLOCK)
),
latest_non_employee_opening_period AS (
    SELECT MAX(t._Period) AS period
    FROM _AccumRgTn7571 AS t WITH (NOLOCK)
    WHERE :opening_balance_date IS NOT NULL
      AND t._Period < CAST(:opening_balance_date AS datetime)
),
latest_employee_opening_period AS (
    SELECT MAX(t._Period) AS period
    FROM _AccumRgT7622 AS t WITH (NOLOCK)
    WHERE :opening_balance_date IS NOT NULL
      AND t._Period <= CAST(:opening_balance_date AS datetime)
),
non_employee_opening_rows AS (
    SELECT
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        contract.contract_ref AS contract_ref,
        contract.contract_name AS contract_name,
        contract.contract_kind_ref AS contract_kind_ref,
        contract.contract_kind_name AS contract_kind_name,
        master.dbo.fn_varbintohexstr(t._Fld7558RRef) AS organization_ref,
        N'regular_receivables' AS source_layer,
        SUM(CAST(t._Fld7562 AS decimal(18, 2))) AS amount_delta,
        CAST(0 AS bit) AS skip_ingest,
        CAST(NULL AS nvarchar(64)) AS return_ref,
        CAST(NULL AS nvarchar(64)) AS deal_tref,
        CAST(NULL AS nvarchar(64)) AS deal_ref
    FROM _AccumRgTn7571 AS t WITH (NOLOCK)
    JOIN latest_non_employee_opening_period AS p
        ON t._Period = p.period
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = t._Fld7559RRef
    LEFT JOIN contract_catalog AS contract
        ON contract._IDRRef = t._Fld7554RRef
    LEFT JOIN employee_counterparties AS employee
        ON employee._IDRRef = t._Fld7559RRef
    WHERE :opening_balance_date IS NOT NULL
      AND t._Fld7559RRef <> 0x00000000000000000000000000000000
      AND t._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
      AND employee._IDRRef IS NULL
    GROUP BY
        counterparty._IDRRef,
        counterparty._Description,
        counterparty._Fld9516,
        counterparty._Fld9865,
        counterparty._Fld9866,
        contract.contract_ref,
        contract.contract_name,
        contract.contract_kind_ref,
        contract.contract_kind_name,
        t._Fld7558RRef
    HAVING SUM(CAST(t._Fld7562 AS decimal(18, 2))) <> 0
),
employee_opening_rows AS (
    SELECT
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        contract.contract_ref AS contract_ref,
        contract.contract_name AS contract_name,
        contract.contract_kind_ref AS contract_kind_ref,
        contract.contract_kind_name AS contract_kind_name,
        master.dbo.fn_varbintohexstr(t._Fld7618RRef) AS organization_ref,
        N'employee_summary' AS source_layer,
        CAST(t._Fld7620 AS decimal(18, 2)) AS amount_delta,
        CASE
            WHEN contract.contract_kind_name = N'С покупателем'
             AND t._Fld7616_RTRef = 0x00000084
             AND deal_doc._Marked = 0x01
             AND deal_doc._Posted = 0x00
                THEN 1
            ELSE 0
        END AS skip_ingest,
        master.dbo.fn_varbintohexstr(t._Fld7617RRef) AS return_ref,
        master.dbo.fn_varbintohexstr(t._Fld7616_RTRef) AS deal_tref,
        master.dbo.fn_varbintohexstr(t._Fld7616_RRRef) AS deal_ref
    FROM _AccumRgT7622 AS t WITH (NOLOCK)
    JOIN latest_employee_opening_period AS p
        ON t._Period = p.period
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = t._Fld7619RRef
    LEFT JOIN contract_catalog AS contract
        ON contract._IDRRef = t._Fld7615RRef
    LEFT JOIN _Document132 AS deal_doc WITH (NOLOCK)
        ON t._Fld7616_RTRef = 0x00000084
       AND deal_doc._IDRRef = t._Fld7616_RRRef
    JOIN employee_counterparties AS employee
        ON employee._IDRRef = t._Fld7619RRef
    WHERE :opening_balance_date IS NOT NULL
      AND t._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND CAST(t._Fld7620 AS decimal(18, 2)) <> 0
),
opening_balance_rows AS (
    SELECT * FROM non_employee_opening_rows
    UNION ALL
    SELECT * FROM employee_opening_rows
),
opening_balance_events AS (
    SELECT
        'onec_hybrid_opening' AS source,
        'opening_balance' AS event_type,
        CAST(
            'opening|'
            + CONVERT(nvarchar(10), CAST(:opening_balance_date AS date), 23)
            + '|'
            + counterparty_ref
            AS nvarchar(128)
        ) AS external_document_ref,
        CAST(
            N'Остаток на '
            + CONVERT(nvarchar(10), CAST(:opening_balance_date AS date), 23)
            AS nvarchar(64)
        ) AS external_document_number,
        CAST(:opening_balance_date AS datetime) AS external_document_date,
        counterparty_ref,
        counterparty_name,
        contract_ref,
        contract_name,
        contract_kind_ref,
        contract_kind_name,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        organization_ref AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        source_layer,
        planned_payment_date,
        credit_depth_days,
        shipment_ban,
        ROW_NUMBER() OVER (
            PARTITION BY counterparty_ref
            ORDER BY contract_ref, organization_ref, return_ref, deal_tref, deal_ref, amount_delta
        ) AS line_no,
        amount_delta,
        skip_ingest
    FROM opening_balance_rows
),
base_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._Fld7554RRef AS contract_rref,
        r._Fld7559RRef AS counterparty_rref,
        r._Period AS movement_period,
        CAST(r._Fld7562 AS decimal(18, 2)) AS amount_delta
    FROM _AccumRg7550 AS r WITH (NOLOCK)
    LEFT JOIN employee_counterparties AS employee
        ON employee._IDRRef = r._Fld7559RRef
    WHERE r._RecorderTRef IN (0x000000CB, 0x0000006D)
      AND r._Active = 0x01
      AND r._Fld7559RRef <> 0x00000000000000000000000000000000
      AND r._Fld7558RRef IN (SELECT _IDRRef FROM target_organization)
      AND employee._IDRRef IS NULL
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
),
register_events AS (
    SELECT
        'onec_hybrid_opening' AS source,
        CASE
            WHEN b.recorder_tref = 0x000000CB THEN 'sale'
            WHEN b.recorder_tref = 0x0000006D THEN 'return'
        END AS event_type,
        master.dbo.fn_varbintohexstr(b.recorder_rref) AS external_document_ref,
        COALESCE(sale._Number, ret._Number) AS external_document_number,
        COALESCE(sale._Date_Time, ret._Date_Time, MAX(b.movement_period)) AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        contract.contract_ref AS contract_ref,
        contract.contract_name AS contract_name,
        contract.contract_kind_ref AS contract_kind_ref,
        contract.contract_kind_name AS contract_kind_name,
        COALESCE(
            master.dbo.fn_varbintohexstr(sale_actor._IDRRef),
            master.dbo.fn_varbintohexstr(ret_actor._IDRRef)
        ) AS manager_ref,
        COALESCE(sale_actor._Description, ret_actor._Description) AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        N'regular_receivables' AS source_layer,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS int) AS line_no,
        SUM(b.amount_delta) AS amount_delta,
        CAST(0 AS bit) AS skip_ingest
    FROM base_register AS b
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = b.counterparty_rref
    LEFT JOIN contract_catalog AS contract
        ON contract._IDRRef = b.contract_rref
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
        contract.contract_ref,
        contract.contract_name,
        contract.contract_kind_ref,
        contract.contract_kind_name,
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
        'onec_hybrid_opening' AS source,
        'payment' AS event_type,
        master.dbo.fn_varbintohexstr(pko._IDRRef) AS external_document_ref,
        pko._Number AS external_document_number,
        pko._Date_Time AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        CAST(NULL AS nvarchar(64)) AS contract_ref,
        CAST(NULL AS nvarchar(255)) AS contract_name,
        CAST(NULL AS nvarchar(64)) AS contract_kind_ref,
        CAST(NULL AS nvarchar(64)) AS contract_kind_name,
        master.dbo.fn_varbintohexstr(sale_actor._IDRRef) AS manager_ref,
        sale_actor._Description AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        N'regular_receivables' AS source_layer,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS int) AS line_no,
        CAST(-pko._Fld4688 AS decimal(18, 2)) AS amount_delta,
        CAST(0 AS bit) AS skip_ingest
    FROM _Document196 AS pko WITH (NOLOCK)
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = pko._Fld4684_RRRef
    LEFT JOIN employee_counterparties AS employee
        ON employee._IDRRef = pko._Fld4684_RRRef
    LEFT JOIN _Document203 AS base_sale WITH (NOLOCK)
        ON pko._Fld4697_RTRef = 0x000000CB
       AND base_sale._IDRRef = pko._Fld4697_RRRef
    LEFT JOIN _Reference54 AS sale_actor WITH (NOLOCK)
        ON sale_actor._IDRRef = base_sale._Fld4942RRef
    WHERE pko._Marked = 0x00
      AND pko._Posted = 0x01
      AND pko._Fld4680RRef IN (SELECT _IDRRef FROM target_organization)
      AND pko._Fld4684_RTRef = 0x00000036
      AND pko._Fld4684_RRRef <> 0x00000000000000000000000000000000
      AND employee._IDRRef IS NULL
      AND (:window_start IS NULL OR pko._Date_Time >= :window_start)
      AND (:window_end IS NULL OR pko._Date_Time < :window_end)
),
settlement_events AS (
    SELECT
        'onec_hybrid_opening' AS source,
        'settlement' AS event_type,
        master.dbo.fn_varbintohexstr(doc._IDRRef) AS external_document_ref,
        doc._Number AS external_document_number,
        doc._Date_Time AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        CAST(NULL AS nvarchar(64)) AS contract_ref,
        CAST(NULL AS nvarchar(255)) AS contract_name,
        CAST(NULL AS nvarchar(64)) AS contract_kind_ref,
        CAST(NULL AS nvarchar(64)) AS contract_kind_name,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        CAST(NULL AS nvarchar(64)) AS store_ref,
        CAST(NULL AS nvarchar(255)) AS store_name,
        N'regular_receivables' AS source_layer,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS int) AS line_no,
        CAST(-doc._Fld4852 AS decimal(18, 2)) AS amount_delta,
        CAST(0 AS bit) AS skip_ingest
    FROM _Document201 AS doc WITH (NOLOCK)
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = doc._Fld4848_RRRef
    LEFT JOIN employee_counterparties AS employee
        ON employee._IDRRef = doc._Fld4848_RRRef
    WHERE doc._Marked = 0x00
      AND doc._Posted = 0x01
      AND doc._Fld4843RRef IN (SELECT _IDRRef FROM target_organization)
      AND doc._Fld4848_RTRef = 0x00000036
      AND doc._Fld4848_RRRef <> 0x00000000000000000000000000000000
      AND employee._IDRRef IS NULL
      AND (:window_start IS NULL OR doc._Date_Time >= :window_start)
      AND (:window_end IS NULL OR doc._Date_Time < :window_end)
),
regular_summary_extra_register AS (
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
    JOIN _Reference37 AS contract_kind WITH (NOLOCK)
        ON contract_kind._IDRRef = r._Fld7615RRef
    LEFT JOIN employee_counterparties AS employee
        ON employee._IDRRef = r._Fld7619RRef
    WHERE r._Active = 0x01
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND employee._IDRRef IS NULL
      AND master.dbo.fn_varbintohexstr(contract_kind._Fld515RRef) = '0x9363c6f0a10557bf4822a55db4862286'
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
regular_summary_extra_events AS (
    SELECT
        'onec_hybrid_summary_regular' AS source,
        'payment' AS event_type,
        master.dbo.fn_varbintohexstr(r.recorder_rref) AS external_document_ref,
        doc186._Number AS external_document_number,
        COALESCE(
            doc186._Date_Time,
            r.movement_period
        ) AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        contract.contract_ref AS contract_ref,
        contract.contract_name AS contract_name,
        contract.contract_kind_ref AS contract_kind_ref,
        contract.contract_kind_name AS contract_kind_name,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        master.dbo.fn_varbintohexstr(r.organization_rref) AS store_ref,
        organization._Description AS store_name,
        N'regular_receivables' AS source_layer,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(NULL AS int) AS line_no,
        r.amount_delta AS amount_delta,
        CAST(0 AS bit) AS skip_ingest
    FROM regular_summary_extra_register AS r
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = r.counterparty_rref
    LEFT JOIN contract_catalog AS contract
        ON contract._IDRRef = r.contract_rref
    LEFT JOIN _Reference66 AS organization WITH (NOLOCK)
        ON organization._IDRRef = r.organization_rref
    LEFT JOIN _Document186 AS doc186 WITH (NOLOCK)
        ON r.recorder_tref = 0x000000BA
       AND doc186._IDRRef = r.recorder_rref
),
employee_summary_order_refs AS (
    SELECT DISTINCT
        r._RecorderRRef AS order_rref
    FROM _AccumRg7614 AS r WITH (NOLOCK)
    JOIN employee_counterparties AS employee
        ON employee._IDRRef = r._Fld7619RRef
    WHERE r._Active = 0x01
      AND r._RecorderTRef = 0x00000084
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
),
linked_sale_amounts AS (
    SELECT
        doc._Fld4939_RRRef AS order_rref,
        CAST(SUM(vt._Fld4979 + vt._Fld4980) AS decimal(18, 2)) AS sale_amount
    FROM _Document203 AS doc WITH (NOLOCK)
    JOIN _Document203_VT4966 AS vt WITH (NOLOCK)
        ON vt._Document203_IDRRef = doc._IDRRef
    JOIN employee_summary_order_refs AS employee_orders
        ON employee_orders.order_rref = doc._Fld4939_RRRef
    WHERE doc._Fld4939_RTRef = 0x00000084
    GROUP BY
        doc._Fld4939_RRRef
),
employee_summary_order_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        CAST(0 AS int) AS line_no,
        MAX(r._Period) AS movement_period,
        r._Fld7615RRef AS contract_rref,
        r._Fld7618RRef AS organization_rref,
        r._Fld7619RRef AS counterparty_rref,
        CAST(
            CASE
                WHEN linked_sale.sale_amount IS NOT NULL
                 AND SUM(CASE WHEN r._RecordKind = 0 THEN r._Fld7620 ELSE -r._Fld7620 END) >= 0
                    THEN linked_sale.sale_amount
                WHEN linked_sale.sale_amount IS NOT NULL
                    THEN -linked_sale.sale_amount
                ELSE SUM(CASE WHEN r._RecordKind = 0 THEN r._Fld7620 ELSE -r._Fld7620 END)
            END AS decimal(18, 2)
        ) AS amount_delta
    FROM _AccumRg7614 AS r WITH (NOLOCK)
    JOIN employee_counterparties AS employee
        ON employee._IDRRef = r._Fld7619RRef
    LEFT JOIN linked_sale_amounts AS linked_sale
        ON r._RecorderTRef = 0x00000084
       AND linked_sale.order_rref = r._RecorderRRef
    WHERE r._Active = 0x01
      AND r._RecorderTRef = 0x00000084
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
    GROUP BY
        r._RecorderTRef,
        r._RecorderRRef,
        r._Fld7615RRef,
        r._Fld7618RRef,
        r._Fld7619RRef,
        linked_sale.sale_amount
),
employee_summary_non_order_register AS (
    SELECT
        r._RecorderTRef AS recorder_tref,
        r._RecorderRRef AS recorder_rref,
        r._LineNo AS line_no,
        r._Period AS movement_period,
        r._Fld7615RRef AS contract_rref,
        r._Fld7618RRef AS organization_rref,
        r._Fld7619RRef AS counterparty_rref,
        CAST(
            CASE
                WHEN r._RecordKind = 0 THEN r._Fld7620
                ELSE -r._Fld7620
            END AS decimal(18, 2)
        ) AS amount_delta
    FROM _AccumRg7614 AS r WITH (NOLOCK)
    JOIN employee_counterparties AS employee
        ON employee._IDRRef = r._Fld7619RRef
    WHERE r._Active = 0x01
      AND r._RecorderTRef <> 0x00000084
      AND r._Fld7619RRef <> 0x00000000000000000000000000000000
      AND r._Fld7618RRef IN (SELECT _IDRRef FROM target_organization)
      AND (:window_start IS NULL OR r._Period >= :window_start)
      AND (:window_end IS NULL OR r._Period < :window_end)
),
employee_summary_register AS (
    SELECT * FROM employee_summary_order_register
    UNION ALL
    SELECT * FROM employee_summary_non_order_register
),
employee_summary_events AS (
    SELECT
        'onec_hybrid_employee_summary' AS source,
        'debt_adjustment' AS event_type,
        CAST(
            'summary7614|'
            + master.dbo.fn_varbintohexstr(r.recorder_rref)
            AS nvarchar(128)
        ) AS external_document_ref,
        CAST(NULL AS nvarchar(64)) AS external_document_number,
        r.movement_period AS external_document_date,
        master.dbo.fn_varbintohexstr(counterparty._IDRRef) AS counterparty_ref,
        counterparty._Description AS counterparty_name,
        contract.contract_ref AS contract_ref,
        contract.contract_name AS contract_name,
        contract.contract_kind_ref AS contract_kind_ref,
        contract.contract_kind_name AS contract_kind_name,
        CAST(NULL AS nvarchar(64)) AS manager_ref,
        CAST(NULL AS nvarchar(255)) AS manager_name,
        master.dbo.fn_varbintohexstr(r.organization_rref) AS store_ref,
        organization._Description AS store_name,
        N'employee_summary' AS source_layer,
        NULLIF(counterparty._Fld9516, CAST('1753-01-01' AS datetime)) AS planned_payment_date,
        CAST(counterparty._Fld9865 AS int) AS credit_depth_days,
        CASE WHEN counterparty._Fld9866 = 0x01 THEN 1 ELSE 0 END AS shipment_ban,
        CAST(r.line_no AS int) AS line_no,
        r.amount_delta AS amount_delta,
        CAST(0 AS bit) AS skip_ingest
    FROM employee_summary_register AS r
    JOIN _Reference54 AS counterparty WITH (NOLOCK)
        ON counterparty._IDRRef = r.counterparty_rref
    LEFT JOIN contract_catalog AS contract
        ON contract._IDRRef = r.contract_rref
    LEFT JOIN _Reference66 AS organization WITH (NOLOCK)
        ON organization._IDRRef = r.organization_rref
)
SELECT *
FROM opening_balance_events
UNION ALL
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
FROM regular_summary_extra_events
UNION ALL
SELECT *
FROM employee_summary_events;
