from __future__ import annotations

from tasks.report_assortment_status_consistency import compare_catalog_vs_decisions


def test_consistent_status_is_not_reported_as_divergent() -> None:
    catalog = [{"nomenclature_code": "РБ0001", "assortment_status": "Матричный"}]
    decisions = [{"nomenclature_code": "РБ0001", "manual_status": "matrix"}]

    report = compare_catalog_vs_decisions(catalog, decisions)

    assert report["decisions_compared"] == 1
    assert report["consistent_count"] == 1
    assert report["divergent_count"] == 0
    assert report["divergent"] == []


def test_divergent_status_between_catalog_and_decision_is_flagged() -> None:
    catalog = [{"nomenclature_code": "РБ0002", "assortment_status": "ПРОДАЖА"}]
    decisions = [{"nomenclature_code": "РБ0002", "manual_status": "do_not_order"}]

    report = compare_catalog_vs_decisions(catalog, decisions)

    assert report["divergent_count"] == 1
    assert report["divergent"] == [
        {
            "nomenclature_code": "РБ0002",
            "decision_status": "do_not_order",
            "catalog_status": "sale",
        }
    ]


def test_no_change_decisions_are_not_compared() -> None:
    catalog = [{"nomenclature_code": "РБ0003", "assortment_status": "Плод"}]
    decisions = [{"nomenclature_code": "РБ0003", "manual_status": "no_change"}]

    report = compare_catalog_vs_decisions(catalog, decisions)

    assert report["decisions_compared"] == 0
    assert report["divergent"] == []


def test_decision_without_matching_catalog_row_is_reported_separately() -> None:
    catalog: list[dict] = []
    decisions = [{"nomenclature_code": "РБ0004", "manual_status": "nonliquid"}]

    report = compare_catalog_vs_decisions(catalog, decisions)

    assert report["decision_without_catalog_count"] == 1
    assert report["decision_without_catalog"] == [
        {"nomenclature_code": "РБ0004", "decision_status": "nonliquid"}
    ]
    assert report["divergent"] == []
