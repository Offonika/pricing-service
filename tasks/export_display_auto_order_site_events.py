"""Export anonymized display SKU events from the Bitrix site into frozen CSV.

The command is deliberately read-only.  It executes two SELECT queries on the
Bitrix box, keeps only the identifiers needed by the backtest, anonymizes the
site session in memory and writes a deterministic CSV.  No customer fields or
raw FUSER_ID values are persisted.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import secrets
import shlex
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CSV_COLUMNS = (
    "event_date",
    "event_type",
    "product_xml_id",
    "quantity",
    "order_number",
    "cancelled_at",
    "session_key",
    "event_key",
    "delay_flag",
)


def anonymize_key(value: Any, *, salt: bytes, namespace: str) -> str:
    """Return a stable, non-reversible key within one frozen export."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    payload = f"{namespace}:{raw}".encode()
    return hmac.new(salt, payload, hashlib.sha256).hexdigest()


def _positive_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value or "0").strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return max(Decimal("0"), parsed)


def normalize_export_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    salt: bytes,
) -> list[dict[str, str]]:
    """Drop raw site identities and return rows suitable for the frozen CSV."""

    normalized: list[dict[str, str]] = []
    for row in rows:
        event_type = str(row.get("event_type") or "").strip()
        order_id = str(row.get("order_id") or "").strip()
        fuser_id = str(row.get("fuser_id") or "").strip()
        xml_id = str(row.get("product_xml_id") or "").strip()
        event_date = str(row.get("event_date") or "").strip()[:10]
        identity = order_id if event_type == "site_order" else fuser_id
        normalized.append(
            {
                "event_date": event_date,
                "event_type": event_type,
                "product_xml_id": xml_id,
                "quantity": str(_positive_decimal(row.get("quantity"))),
                "order_number": str(row.get("order_number") or "").strip(),
                "cancelled_at": str(row.get("cancelled_at") or "").strip()[:10],
                "session_key": anonymize_key(fuser_id, salt=salt, namespace="session"),
                "event_key": anonymize_key(
                    f"{identity}:{xml_id}:{event_date}",
                    salt=salt,
                    namespace=event_type or "site_event",
                ),
                "delay_flag": "Y" if str(row.get("delay_flag") or "").upper() == "Y" else "N",
            }
        )
    normalized.sort(
        key=lambda row: (
            row["event_date"],
            row["event_type"],
            row["product_xml_id"],
            row["event_key"],
        )
    )
    return normalized


def write_site_events_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})


def _remote_php() -> str:
    # The query intentionally selects no customer name, phone, address or email.
    return r'''
define("NO_KEEP_STATISTIC", true);
define("NO_AGENT_STATISTIC", true);
define("NOT_CHECK_PERMISSIONS", true);
$_SERVER["DOCUMENT_ROOT"] = $argv[1];
require $_SERVER["DOCUMENT_ROOT"] . "/bitrix/modules/main/include/prolog_before.php";
$connection = \Bitrix\Main\Application::getConnection();
$helper = $connection->getSqlHelper();
$from = $helper->forSql($argv[2]);
$to = $helper->forSql($argv[3]);
$queries = array(
    "SELECT DATE(o.DATE_INSERT) AS event_date, 'site_order' AS event_type, " .
    "b.PRODUCT_XML_ID AS product_xml_id, SUM(b.QUANTITY) AS quantity, " .
    "o.ID AS order_id, COALESCE(NULLIF(o.ACCOUNT_NUMBER, ''), CAST(o.ID AS CHAR)) AS order_number, " .
    "CASE WHEN o.CANCELED = 'Y' THEN DATE(COALESCE(o.DATE_CANCELED, o.DATE_UPDATE)) ELSE NULL END AS cancelled_at, " .
    "MAX(b.FUSER_ID) AS fuser_id, 'N' AS delay_flag " .
    "FROM b_sale_order o INNER JOIN b_sale_basket b ON b.ORDER_ID = o.ID " .
    "WHERE o.DATE_INSERT >= '" . $from . " 00:00:00' AND o.DATE_INSERT < '" . $to . " 00:00:00' " .
    "AND b.PRODUCT_XML_ID IS NOT NULL AND b.NAME LIKE '%Дисплей для %' " .
    "GROUP BY DATE(o.DATE_INSERT), b.PRODUCT_XML_ID, o.ID, o.ACCOUNT_NUMBER, " .
    "o.CANCELED, o.DATE_CANCELED, o.DATE_UPDATE",
    "SELECT DATE(b.DATE_INSERT) AS event_date, 'site_unordered_cart' AS event_type, " .
    "b.PRODUCT_XML_ID AS product_xml_id, SUM(b.QUANTITY) AS quantity, " .
    "NULL AS order_id, '' AS order_number, NULL AS cancelled_at, " .
    "b.FUSER_ID AS fuser_id, b.DELAY AS delay_flag " .
    "FROM b_sale_basket b " .
    "WHERE b.ORDER_ID IS NULL AND b.DATE_INSERT >= '" . $from . " 00:00:00' " .
    "AND b.DATE_INSERT < '" . $to . " 00:00:00' " .
    "AND b.PRODUCT_XML_ID IS NOT NULL AND b.NAME LIKE '%Дисплей для %' " .
    "GROUP BY DATE(b.DATE_INSERT), b.PRODUCT_XML_ID, b.FUSER_ID, b.DELAY"
);
foreach ($queries as $sql) {
    $result = $connection->query($sql);
    while ($row = $result->fetch()) {
        foreach (array("event_date", "cancelled_at") as $dateField) {
            if (isset($row[$dateField]) && is_object($row[$dateField]) && method_exists($row[$dateField], "format")) {
                $row[$dateField] = $row[$dateField]->format("Y-m-d");
            }
        }
        echo json_encode($row, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), "\n";
    }
}
'''.strip()


def fetch_remote_rows(
    *,
    ssh_alias: str,
    site_root: str,
    date_from: date,
    date_to: date,
) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "ssh",
            ssh_alias,
            "php",
            "-r",
            shlex.quote(_remote_php()),
            shlex.quote(site_root),
            shlex.quote(date_from.isoformat()),
            shlex.quote((date_to + timedelta(days=1)).isoformat()),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    rows: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        rendered = line.strip()
        if not rendered:
            continue
        payload = json.loads(rendered)
        if not isinstance(payload, dict):
            raise ValueError("site export returned a non-object JSON row")
        rows.append(payload)
    return rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", type=date.fromisoformat, required=True)
    parser.add_argument("--date-to", type=date.fromisoformat, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--ssh-alias", default="bitrix-box")
    parser.add_argument(
        "--site-root",
        default="/var/www/mm/data/www/master-mobile.ru",
    )
    args = parser.parse_args()
    if args.date_from > args.date_to:
        raise SystemExit("date-from must not exceed date-to")
    return args


def main() -> int:
    args = _parse_args()
    raw_rows = fetch_remote_rows(
        ssh_alias=args.ssh_alias,
        site_root=args.site_root,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    rows = normalize_export_rows(raw_rows, salt=secrets.token_bytes(32))
    write_site_events_csv(args.output_csv, rows)
    summary = {
        "output_csv": str(args.output_csv),
        "row_count": len(rows),
        "order_row_count": sum(row["event_type"] == "site_order" for row in rows),
        "cart_row_count": sum(row["event_type"] == "site_unordered_cart" for row in rows),
        "sha256": hashlib.sha256(args.output_csv.read_bytes()).hexdigest(),
        "contains_personal_data": False,
        "contains_raw_fuser_id": False,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
