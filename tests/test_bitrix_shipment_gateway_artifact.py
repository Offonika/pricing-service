from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "infra/bitrix/mm_sale_shipment_gateway.php"
LEGACY_SYNC = ROOT / "infra/bitrix/mm_site_cdek_track_sync.php"


def test_shipment_gateway_is_authenticated_idempotent_and_exact() -> None:
    source = GATEWAY.read_text(encoding="utf-8")

    assert "hash_equals($configuredToken, $providedToken)" in source
    assert "mmShipmentHandleEnsure" in source
    assert "shipment_key_conflict" in source
    assert "mmShipmentHandleUpdateTracking" in source
    assert "mmShipmentFind($order->getShipmentCollection(), $shipmentId)" in source
    assert "tracking_readback_mismatch" in source


def test_legacy_track_sync_fails_closed_for_multiple_shipments() -> None:
    source = LEGACY_SYNC.read_text(encoding="utf-8")

    assert "hasMultiplePhysicalShipments" in source
    assert "multiple physical shipments require exact shipment_id" in source
    assert "count($physical) !== 1" in source
    assert "$physical[0]->setField('TRACKING_NUMBER', $track);" in source
