WITH preferred_phone AS (
    SELECT
        ci._Fld6403_RRRef AS counterparty_ref,
        CAST(ci._Fld6406 AS nvarchar(255)) AS phone_value,
        ROW_NUMBER() OVER (
            PARTITION BY ci._Fld6403_RRRef
            ORDER BY
                CASE COALESCE(kind._Description, N'')
                    WHEN N'Телефон контрагента' THEN 1
                    WHEN N'Рабочий' THEN 2
                    WHEN N'Доп. телефон для переноса' THEN 3
                    ELSE 99
                END,
                CAST(ci._Fld6406 AS nvarchar(255)) DESC
        ) AS rn
    FROM _InfoRg6402 AS ci
    LEFT JOIN _Reference25 AS kind
        ON kind._IDRRef = ci._Fld6405_RRRef
    WHERE ci._Fld6403_RTRef = 0x00000036
      AND ci._Fld6406 IS NOT NULL
      AND LTRIM(RTRIM(CAST(ci._Fld6406 AS nvarchar(255)))) <> N''
      AND COALESCE(kind._Description, N'') IN (
          N'Телефон контрагента',
          N'Рабочий',
          N'Доп. телефон для переноса'
      )
)
SELECT
    master.dbo.fn_varbintohexstr(exp._IDRRef) AS external_id,
    master.dbo.fn_varbintohexstr(exp._IDRRef) AS onec_expertise_ref,
    exp._Number AS onec_expertise_number,
    exp._Date_Time AS created_at_source,
    CAST(CASE WHEN exp._Posted = 0x01 THEN 1 ELSE 0 END AS bit) AS posted,
    master.dbo.fn_varbintohexstr(exp._Fld9870RRef) AS organization_ref,
    org._Description AS organization_name,
    master.dbo.fn_varbintohexstr(exp._Fld9871RRef) AS store_ref,
    store._Code AS store_external_id,
    store._Description AS store_name,
    master.dbo.fn_varbintohexstr(exp._Fld9872RRef) AS counterparty_ref,
    counterparty._Description AS customer_name,
    phone.phone_value AS customer_phone,
    master.dbo.fn_varbintohexstr(exp._Fld9875RRef) AS responsible_ref,
    master.dbo.fn_varbintohexstr(exp._Fld9875RRef) AS owner_user_external_id,
    responsible._Description AS responsible_name,
    master.dbo.fn_varbintohexstr(exp._Fld9876RRef) AS contract_ref,
    contract._Description AS contract_name,
    master.dbo.fn_varbintohexstr(exp._Fld9877RRef) AS warehouse_ref,
    warehouse._Description AS warehouse_name,
    master.dbo.fn_varbintohexstr(exp._Fld9878RRef) AS base_document_ref,
    master.dbo.fn_varbintohexstr(exp._Fld9878RRef) AS linked_sale_ref,
    sale._Number AS base_document_number,
    sale._Number AS linked_sale_number,
    exp._Fld9884 AS manager_comment,
    exp._Fld9885 AS quality_comment,
    item._LineNo9887 AS item_line_no,
    master.dbo.fn_varbintohexstr(item._Fld9894RRef) AS item_nomenclature_ref,
    nomenclature._Description AS item_nomenclature_name,
    item._Fld9891 AS item_quantity,
    item._Fld9902 AS item_price,
    item._Fld9899 AS item_amount,
    master.dbo.fn_varbintohexstr(item._Fld9890RRef) AS item_quality_ref,
    quality._Description AS item_quality_name,
    master.dbo.fn_varbintohexstr(item._Fld9912RRef) AS item_return_reason_ref,
    return_reason._Description AS item_return_reason_name,
    master.dbo.fn_varbintohexstr(item._Fld9909RRef) AS item_linked_customer_order_ref,
    customer_order._Number AS item_linked_customer_order_number,
    CASE decision._EnumOrder
        WHEN 0 THEN N'Принято'
        WHEN 1 THEN N'Отказано'
        ELSE NULL
    END AS decision_label,
    master.dbo.fn_varbintohexstr(item._Fld9911RRef) AS item_decision_ref,
    CASE decision._EnumOrder
        WHEN 0 THEN N'Принято'
        WHEN 1 THEN N'Отказано'
        ELSE NULL
    END AS item_decision_label
FROM _Document9868 AS exp
JOIN _Document9868_VT9886 AS item
    ON item._Document9868_IDRRef = exp._IDRRef
LEFT JOIN _Reference66 AS org
    ON org._IDRRef = exp._Fld9870RRef
LEFT JOIN _Reference68 AS store
    ON store._IDRRef = exp._Fld9871RRef
LEFT JOIN _Reference54 AS counterparty
    ON counterparty._IDRRef = exp._Fld9872RRef
LEFT JOIN preferred_phone AS phone
    ON phone.counterparty_ref = exp._Fld9872RRef
   AND phone.rn = 1
LEFT JOIN _Reference69 AS responsible
    ON responsible._IDRRef = exp._Fld9875RRef
LEFT JOIN _Reference37 AS contract
    ON contract._IDRRef = exp._Fld9876RRef
LEFT JOIN _Reference80 AS warehouse
    ON warehouse._IDRRef = exp._Fld9877RRef
LEFT JOIN _Document203 AS sale
    ON sale._IDRRef = exp._Fld9878RRef
LEFT JOIN _Reference62 AS nomenclature
    ON nomenclature._IDRRef = item._Fld9894RRef
LEFT JOIN _Reference48 AS quality
    ON quality._IDRRef = item._Fld9890RRef
LEFT JOIN _Reference8913 AS return_reason
    ON return_reason._IDRRef = item._Fld9912RRef
LEFT JOIN _Document132 AS customer_order
    ON customer_order._IDRRef = item._Fld9909RRef
LEFT JOIN _Enum9869 AS decision
    ON decision._IDRRef = item._Fld9911RRef
WHERE exp._Marked = 0x00
ORDER BY
    exp._Date_Time,
    exp._Number,
    item._LineNo9887;
