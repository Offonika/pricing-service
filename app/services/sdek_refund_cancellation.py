from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Collection, Sequence

FULL_REFUND_SITE_STATUS_IDS = frozenset({"YR"})
SAFE_PRE_HANDOVER_SDEK_STATUSES = frozenset({"ACCEPTED", "CREATED"})
SDEK_REMOVED_STATUS = "REMOVED"
TRACKING_NUMBER_RE = re.compile(r"(?<!\d)\d{8,20}(?!\d)")
REMOTE_JSON_MARKER = "MM_SDEK_REFUND_JSON:"


@dataclass(frozen=True, slots=True)
class SdekRefundCandidate:
    site_order_number: str
    bitrix_deal_id: int
    tracking_number: str | None

    @property
    def operation_key(self) -> str:
        return "|".join(
            (
                self.site_order_number,
                str(self.bitrix_deal_id),
                self.tracking_number or "no-track",
            )
        )


@dataclass(frozen=True, slots=True)
class SdekRefundInspection:
    candidate: SdekRefundCandidate
    lookup_result: str
    site_status_id: str | None
    refund_verified: bool
    account_id: str | None = None
    shipment_order_number: str | None = None
    statuses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SdekRefundResult:
    candidate: SdekRefundCandidate
    result: str
    refund_verified: bool
    statuses: tuple[str, ...] = ()
    applied: bool = False
    reason: str | None = None


Runner = Callable[..., subprocess.CompletedProcess[str]]


def extract_tracking_numbers(value: Any) -> list[str]:
    """Extract distinct CDEK numbers without treating the six-digit order as a track."""

    if value is None:
        return []
    if isinstance(value, dict):
        chunks: list[Any] = list(value.values())
    elif isinstance(value, (list, tuple, set)):
        chunks = list(value)
    else:
        chunks = [value]

    result: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, (dict, list, tuple, set)):
            nested = extract_tracking_numbers(chunk)
        else:
            nested = TRACKING_NUMBER_RE.findall(str(chunk))
        for tracking_number in nested:
            if tracking_number not in seen:
                seen.add(tracking_number)
                result.append(tracking_number)
    return result


def process_sdek_refund_candidates(
    candidates: Sequence[SdekRefundCandidate],
    *,
    apply: bool,
    authorized_order_numbers: Collection[str] | None = (),
    runner: Runner = subprocess.run,
    timeout: int = 60,
) -> list[SdekRefundResult]:
    """Inspect every candidate and cancel only explicitly authorized orders.

    ``None`` explicitly authorizes every safe candidate; the default empty
    collection is fail-closed.
    """
    unique_candidates = _unique_candidates(candidates)
    if not unique_candidates:
        return []
    authorized = (
        None
        if authorized_order_numbers is None
        else {_validated_order_number(value) for value in authorized_order_numbers}
    )

    try:
        inspections = inspect_sdek_refund_candidates(
            unique_candidates,
            runner=runner,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - the cron must retain an audit result.
        return [
            SdekRefundResult(
                candidate=candidate,
                result="inspection_error",
                refund_verified=False,
                reason=type(exc).__name__,
            )
            for candidate in unique_candidates
        ]

    results = [_evaluate_inspection(inspection) for inspection in inspections]
    if not apply:
        return results

    guarded_results: list[SdekRefundResult] = []
    ready_inspections: list[SdekRefundInspection] = []
    for inspection, result in zip(inspections, results, strict=True):
        if result.result != "cancel_ready":
            guarded_results.append(result)
            continue
        if authorized is not None and result.candidate.site_order_number not in authorized:
            guarded_results.append(
                SdekRefundResult(
                    candidate=result.candidate,
                    result="cancel_not_authorized",
                    refund_verified=result.refund_verified,
                    statuses=result.statuses,
                    reason="rollout_guard",
                )
            )
            continue
        guarded_results.append(result)
        ready_inspections.append(inspection)

    results = guarded_results
    if not ready_inspections:
        return results

    try:
        cancellation_rows = cancel_sdek_refund_shipments(
            ready_inspections,
            runner=runner,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the safe no-delete result.
        cancellation_rows = {
            inspection.candidate.operation_key: {
                "result": "cancel_error",
                "reason": type(exc).__name__,
            }
            for inspection in ready_inspections
        }

    merged: list[SdekRefundResult] = []
    for result in results:
        if result.result != "cancel_ready":
            merged.append(result)
            continue
        row = cancellation_rows.get(result.candidate.operation_key) or {}
        cancellation_result = _clean_string(row.get("result")) or "cancel_error"
        statuses = _normalize_statuses(row.get("statuses")) or result.statuses
        merged.append(
            SdekRefundResult(
                candidate=result.candidate,
                result=cancellation_result,
                refund_verified=bool(row.get("refund_verified", result.refund_verified)),
                statuses=statuses,
                applied=cancellation_result == "cancelled",
                reason=_clean_string(row.get("reason")) or None,
            )
        )
    return merged


def inspect_sdek_refund_candidates(
    candidates: Sequence[SdekRefundCandidate],
    *,
    runner: Runner = subprocess.run,
    timeout: int = 60,
) -> list[SdekRefundInspection]:
    payload = [
        {
            "operation_key": candidate.operation_key,
            "site_order_number": _validated_order_number(candidate.site_order_number),
            "bitrix_deal_id": candidate.bitrix_deal_id,
            "tracking_number": _validated_tracking_number(candidate.tracking_number),
        }
        for candidate in candidates
    ]
    rows = _run_remote_php(
        _build_inspection_php(payload),
        runner=runner,
        timeout=timeout,
    )
    rows_by_key = {
        _clean_string(row.get("operation_key")): row
        for row in rows
        if isinstance(row, dict) and _clean_string(row.get("operation_key"))
    }
    inspections: list[SdekRefundInspection] = []
    for candidate in candidates:
        row = rows_by_key.get(candidate.operation_key)
        if row is None:
            inspections.append(
                SdekRefundInspection(
                    candidate=candidate,
                    lookup_result="inspection_missing_result",
                    site_status_id=None,
                    refund_verified=False,
                )
            )
            continue
        inspections.append(
            SdekRefundInspection(
                candidate=candidate,
                lookup_result=_clean_string(row.get("lookup_result")) or "inspection_error",
                site_status_id=_clean_string(row.get("site_status_id")) or None,
                refund_verified=bool(row.get("refund_verified")),
                account_id=_clean_string(row.get("account_id")) or None,
                shipment_order_number=_clean_string(row.get("shipment_order_number")) or None,
                statuses=_normalize_statuses(row.get("statuses")),
            )
        )
    return inspections


def cancel_sdek_refund_shipments(
    inspections: Sequence[SdekRefundInspection],
    *,
    runner: Runner = subprocess.run,
    timeout: int = 60,
) -> dict[str, dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for inspection in inspections:
        if _evaluate_inspection(inspection).result != "cancel_ready":
            continue
        payload.append(
            {
                "operation_key": inspection.candidate.operation_key,
                "site_order_number": _validated_order_number(
                    inspection.candidate.site_order_number
                ),
                "tracking_number": _validated_tracking_number(inspection.candidate.tracking_number),
                "account_id": _validated_account_id(inspection.account_id),
            }
        )
    if not payload:
        return {}
    rows = _run_remote_php(
        _build_cancellation_php(payload),
        runner=runner,
        timeout=timeout,
    )
    return {
        _clean_string(row.get("operation_key")): row
        for row in rows
        if isinstance(row, dict) and _clean_string(row.get("operation_key"))
    }


def _evaluate_inspection(inspection: SdekRefundInspection) -> SdekRefundResult:
    common = {
        "candidate": inspection.candidate,
        "refund_verified": inspection.refund_verified,
        "statuses": inspection.statuses,
    }
    if not inspection.refund_verified:
        return SdekRefundResult(result=inspection.lookup_result, **common)
    if inspection.lookup_result == "missing_tracking":
        return SdekRefundResult(result="missing_tracking", **common)
    if inspection.lookup_result != "found":
        return SdekRefundResult(result=inspection.lookup_result, **common)
    if inspection.shipment_order_number != inspection.candidate.site_order_number:
        return SdekRefundResult(result="shipment_order_mismatch", **common)

    statuses = set(inspection.statuses)
    if SDEK_REMOVED_STATUS in statuses:
        return SdekRefundResult(result="already_removed", **common)
    if "CREATED" not in statuses:
        return SdekRefundResult(result="blocked_unknown_status", **common)
    if not statuses.issubset(SAFE_PRE_HANDOVER_SDEK_STATUSES):
        return SdekRefundResult(result="blocked_after_handover", **common)
    if not inspection.account_id:
        return SdekRefundResult(result="shipment_account_missing", **common)
    return SdekRefundResult(result="cancel_ready", **common)


def _run_remote_php(php_code: str, *, runner: Runner, timeout: int) -> list[dict[str, Any]]:
    completed = runner(
        ["ssh", "-o", "BatchMode=yes", "bitrix-box", "sudo -u mm php"],
        input=php_code,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("bitrix_box_php_failed")
    marker_position = completed.stdout.rfind(REMOTE_JSON_MARKER)
    if marker_position < 0:
        raise RuntimeError("bitrix_box_php_result_missing")
    raw_payload = completed.stdout[marker_position + len(REMOTE_JSON_MARKER) :].strip()
    payload = json.loads(raw_payload)
    if not isinstance(payload, list):
        raise RuntimeError("bitrix_box_php_result_invalid")
    return [row for row in payload if isinstance(row, dict)]


def _build_inspection_php(payload: list[dict[str, Any]]) -> str:
    encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    php_json = json.dumps(encoded_payload, ensure_ascii=False)
    return f"""<?php
$_SERVER['DOCUMENT_ROOT'] = '/var/www/mm/data/www/crm.master-mobile.ru';
define('NO_KEEP_STATISTIC', true);
define('NOT_CHECK_PERMISSIONS', true);
define('BX_CRONTAB', true);
chdir($_SERVER['DOCUMENT_ROOT']);
require $_SERVER['DOCUMENT_ROOT'].'/bitrix/modules/main/include/prolog_before.php';
$candidates = json_decode({php_json}, true);
$result = [];
if (!CModule::IncludeModule('ipol.sdek')) {{
    echo '{REMOTE_JSON_MARKER}'.json_encode([], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}}
$connection = Bitrix\\Main\\Application::getConnection();
$accounts = [];
$accountResult = sqlSdekLogs::select(['ACTIVE' => 'Y']);
while ($account = $accountResult->Fetch()) {{
    $accounts[] = $account;
}}
foreach ($candidates as $candidate) {{
    $orderNumber = (string)$candidate['site_order_number'];
    $trackingNumber = (string)($candidate['tracking_number'] ?? '');
    $row = [
        'operation_key' => (string)$candidate['operation_key'],
        'lookup_result' => 'order_not_found',
        'site_status_id' => null,
        'refund_verified' => false,
        'account_id' => null,
        'shipment_order_number' => null,
        'statuses' => [],
    ];
    $order = $connection->query(
        "SELECT ID, ACCOUNT_NUMBER, STATUS_ID FROM b_sale_order " .
        "WHERE ID = " . (int)$orderNumber . " OR ACCOUNT_NUMBER = '" . (int)$orderNumber . "' " .
        "ORDER BY ID DESC LIMIT 1"
    )->fetch();
    if (!$order) {{
        $result[] = $row;
        continue;
    }}
    $row['site_status_id'] = (string)$order['STATUS_ID'];
    if ((string)$order['STATUS_ID'] !== 'YR') {{
        $row['lookup_result'] = 'order_not_refunded';
        $result[] = $row;
        continue;
    }}
    $row['refund_verified'] = true;
    if ($trackingNumber === '') {{
        $row['lookup_result'] = 'missing_tracking';
        $result[] = $row;
        continue;
    }}
    foreach ($accounts as $account) {{
        try {{
            $application = new Ipolh\\SDEK\\SDEK\\SdekApplication(
                $account['ACCOUNT'], $account['SECURE'], false, 10,
                new Ipolh\\SDEK\\Bitrix\\Entity\\encoder(),
                new Ipolh\\SDEK\\Bitrix\\Entity\\cache()
            );
            $controller = new Ipolh\\SDEK\\Bitrix\\Controller\\Order($application);
            $info = $controller->getOrderInfoByNumber($trackingNumber);
            if (!$info->isSuccess()) {{
                continue;
            }}
            $data = $info->getData();
            if (empty($data['UUID'])) {{
                continue;
            }}
            $row['lookup_result'] = 'found';
            $row['account_id'] = (string)$account['ID'];
            $row['shipment_order_number'] = (string)($data['NUMBER'] ?? '');
            foreach (($data['STATUSES'] ?? []) as $status) {{
                if (!empty($status['STATUS'])) {{
                    $row['statuses'][] = (string)$status['STATUS'];
                }}
            }}
            $row['statuses'] = array_values(array_unique($row['statuses']));
            break;
        }} catch (Throwable $e) {{
            continue;
        }}
    }}
    if ($row['lookup_result'] !== 'found') {{
        $row['lookup_result'] = 'shipment_not_found';
    }}
    $result[] = $row;
}}
echo '{REMOTE_JSON_MARKER}'.json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
"""


def _build_cancellation_php(payload: list[dict[str, Any]]) -> str:
    encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    php_json = json.dumps(encoded_payload, ensure_ascii=False)
    return f"""<?php
$_SERVER['DOCUMENT_ROOT'] = '/var/www/mm/data/www/crm.master-mobile.ru';
define('NO_KEEP_STATISTIC', true);
define('NOT_CHECK_PERMISSIONS', true);
define('BX_CRONTAB', true);
chdir($_SERVER['DOCUMENT_ROOT']);
require $_SERVER['DOCUMENT_ROOT'].'/bitrix/modules/main/include/prolog_before.php';
$candidates = json_decode({php_json}, true);
$result = [];
if (!CModule::IncludeModule('ipol.sdek')) {{
    echo '{REMOTE_JSON_MARKER}'.json_encode([], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}}
$connection = Bitrix\\Main\\Application::getConnection();
$safeStatuses = ['ACCEPTED', 'CREATED'];
foreach ($candidates as $candidate) {{
    $orderNumber = (string)$candidate['site_order_number'];
    $trackingNumber = (string)$candidate['tracking_number'];
    $row = [
        'operation_key' => (string)$candidate['operation_key'],
        'result' => 'cancel_error',
        'refund_verified' => false,
        'statuses' => [],
        'reason' => null,
    ];
    $order = $connection->query(
        "SELECT ID, ACCOUNT_NUMBER, STATUS_ID FROM b_sale_order " .
        "WHERE ID = " . (int)$orderNumber . " OR ACCOUNT_NUMBER = '" . (int)$orderNumber . "' " .
        "ORDER BY ID DESC LIMIT 1"
    )->fetch();
    if (!$order) {{
        $row['result'] = 'order_not_found';
        $result[] = $row;
        continue;
    }}
    if ((string)$order['STATUS_ID'] !== 'YR') {{
        $row['result'] = 'order_not_refunded';
        $result[] = $row;
        continue;
    }}
    $row['refund_verified'] = true;
    $account = sqlSdekLogs::getById((int)$candidate['account_id']);
    if (!$account || (string)$account['ACTIVE'] !== 'Y') {{
        $row['result'] = 'shipment_account_missing';
        $result[] = $row;
        continue;
    }}
    try {{
        $application = new Ipolh\\SDEK\\SDEK\\SdekApplication(
            $account['ACCOUNT'], $account['SECURE'], false, 10,
            new Ipolh\\SDEK\\Bitrix\\Entity\\encoder(),
            new Ipolh\\SDEK\\Bitrix\\Entity\\cache()
        );
        $controller = new Ipolh\\SDEK\\Bitrix\\Controller\\Order($application);
        $info = $controller->getOrderInfoByNumber($trackingNumber);
        if (!$info->isSuccess()) {{
            $row['result'] = 'shipment_not_found';
            $result[] = $row;
            continue;
        }}
        $data = $info->getData();
        if ((string)($data['NUMBER'] ?? '') !== $orderNumber) {{
            $row['result'] = 'shipment_order_mismatch';
            $result[] = $row;
            continue;
        }}
        foreach (($data['STATUSES'] ?? []) as $status) {{
            if (!empty($status['STATUS'])) {{
                $row['statuses'][] = (string)$status['STATUS'];
            }}
        }}
        $row['statuses'] = array_values(array_unique($row['statuses']));
        if (in_array('REMOVED', $row['statuses'], true)) {{
            $row['result'] = 'already_removed';
            $result[] = $row;
            continue;
        }}
        $unsafeStatuses = array_diff($row['statuses'], $safeStatuses);
        if (!in_array('CREATED', $row['statuses'], true) || !empty($unsafeStatuses)) {{
            $row['result'] = empty($unsafeStatuses)
                ? 'blocked_unknown_status'
                : 'blocked_after_handover';
            $result[] = $row;
            continue;
        }}
        if (empty($data['UUID'])) {{
            $row['result'] = 'shipment_uuid_missing';
            $result[] = $row;
            continue;
        }}
        $delete = $controller->deleteOrder($data['UUID']);
        $row['result'] = $delete->isSuccess() ? 'cancelled' : 'cancel_error';
    }} catch (Throwable $e) {{
        $row['result'] = 'cancel_error';
        $row['reason'] = get_class($e);
    }}
    $result[] = $row;
}}
echo '{REMOTE_JSON_MARKER}'.json_encode($result, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
"""


def _unique_candidates(
    candidates: Sequence[SdekRefundCandidate],
) -> list[SdekRefundCandidate]:
    result: list[SdekRefundCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.operation_key in seen:
            continue
        _validated_order_number(candidate.site_order_number)
        _validated_tracking_number(candidate.tracking_number)
        seen.add(candidate.operation_key)
        result.append(candidate)
    return result


def _validated_order_number(value: str) -> str:
    cleaned = _clean_string(value)
    if not cleaned.isdigit() or not 1 <= len(cleaned) <= 20:
        raise ValueError("invalid_site_order_number")
    return cleaned


def _validated_tracking_number(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _clean_string(value)
    if not TRACKING_NUMBER_RE.fullmatch(cleaned):
        raise ValueError("invalid_sdek_tracking_number")
    return cleaned


def _validated_account_id(value: str | None) -> str:
    cleaned = _clean_string(value)
    if not cleaned.isdigit() or not cleaned:
        raise ValueError("invalid_sdek_account_id")
    return cleaned


def _normalize_statuses(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        status = _clean_string(item).upper()
        if status and status not in seen:
            seen.add(status)
            result.append(status)
    return tuple(result)


def _clean_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""
