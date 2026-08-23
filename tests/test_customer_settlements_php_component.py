from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "integrations/master_mobile_site/customer_settlements"
PHP = shutil.which("php")


def test_component_bundle_is_server_side_and_fail_closed() -> None:
    client = (
        BUNDLE / "local/components/mastermobile/customer.settlements/lib/client.php"
    ).read_text(encoding="utf-8")
    component = (BUNDLE / "local/components/mastermobile/customer.settlements/class.php").read_text(
        encoding="utf-8"
    )
    page = (BUNDLE / "personal/settlements/index.php").read_text(encoding="utf-8")
    template = (
        BUNDLE
        / "local/components/mastermobile/customer.settlements/templates/.default/template.php"
    ).read_text(encoding="utf-8")

    assert "Authorization: Bearer " in client
    assert "site_user_id" not in page
    assert "active_secret" not in template
    assert "BX_COMPOSITE_CACHE" in page
    assert "CACHE_TYPE' => 'N'" in page
    assert "Cache-Control: private, no-store" in component
    assert "К оплате" in template
    assert "Ваш аванс" in template
    assert "Задолженности нет" in template
    assert "Обновить" not in template


def test_php_adapter_hardening_and_eligibility_cache_are_explicit() -> None:
    client = (
        BUNDLE / "local/components/mastermobile/customer.settlements/lib/client.php"
    ).read_text(encoding="utf-8")
    menu_visibility = (
        BUNDLE / "local/include/personal/customer_settlements_menu_visibility.php"
    ).read_text(encoding="utf-8")
    page = (BUNDLE / "personal/settlements/index.php").read_text(encoding="utf-8")
    template = (
        BUNDLE
        / "local/components/mastermobile/customer.settlements/templates/.default/template.php"
    ).read_text(encoding="utf-8")

    assert "CURLOPT_FOLLOWLOCATION => false" in client
    assert "CURLOPT_CONNECTTIMEOUT_MS => $probe ? 500 : 2000" in client
    assert "CURLOPT_TIMEOUT_MS => $probe ? 1000 : 3000" in client
    assert "CURLOPT_SSL_VERIFYPEER => true" in client
    assert "CURLOPT_SSL_VERIFYHOST => 2" in client
    assert "allowed_hosts" in client
    assert "mock_allowed_user_hashes" in client
    assert "hash_hmac('sha256', $siteUserId, $salt)" in client
    assert "hash_equals" in client
    assert "|eligibility" in client
    assert "MM_CUSTOMER_SETTLEMENTS_ELIGIBILITY" in menu_visibility
    assert "time() + 300" in menu_visibility
    assert "customer-settlements-eligibility|" in menu_visibility
    assert "Composite\\Engine::setEnable(false)" in page
    assert "(float)" not in template


def test_menu_patch_places_settlements_after_orders() -> None:
    patch = (BUNDLE / "dev-personal-menu.patch").read_text(encoding="utf-8")
    assert patch.index('"ORDERS_LINK"') < patch.index('"SETTLEMENTS_LINK"')
    assert "SETTLEMENTS_VISIBLE" in patch
    assert "Взаиморасчёты" in patch


@pytest.mark.skipif(PHP is None, reason="php binary is not installed")
def test_php_contract_fixture_matches_backend_vector() -> None:
    fixture = BUNDLE / "fixtures/assertion_fixture.php"
    result = subprocess.run(
        [str(PHP), str(fixture)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "assertionMatches": True,
        "status": "available",
        "state": "zero",
        "amount": "0.00",
    }


@pytest.mark.skipif(PHP is None, reason="php binary is not installed")
@pytest.mark.parametrize("path", sorted(BUNDLE.rglob("*.php")))
def test_php_sources_lint(path: Path) -> None:
    result = subprocess.run(
        [str(PHP), "-l", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
