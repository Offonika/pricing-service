from __future__ import annotations

import json

import pytest

from tasks import run_expertise_sync


def test_run_expertise_sync_retries_only_failed_and_allows_partial_bitrix_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[str, bool | None]] = []

    def fake_onec_sync() -> dict[str, int]:
        calls.append(("onec", None))
        return {"created": 0, "updated": 10, "fetched": 10}

    def fake_bitrix_sync(*, only_failed: bool = False) -> dict[str, int]:
        calls.append(("bitrix", only_failed))
        return {"scanned": 5, "synced": 4, "errors": 1, "disabled": 0}

    monkeypatch.setattr(run_expertise_sync, "run_expertise_onec_sync", fake_onec_sync)
    monkeypatch.setattr(run_expertise_sync, "run_expertise_bitrix_sync", fake_bitrix_sync)

    with pytest.raises(SystemExit) as exc_info:
        run_expertise_sync.main()

    assert exc_info.value.code == 0
    assert calls == [("onec", None), ("bitrix", True)]
    payload = json.loads(capsys.readouterr().out)
    assert payload["bitrix_retry_sync"]["errors"] == 1


def test_run_expertise_sync_fails_when_bitrix_retry_cannot_sync_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_expertise_sync,
        "run_expertise_onec_sync",
        lambda: {"created": 0, "updated": 10, "fetched": 10},
    )
    monkeypatch.setattr(
        run_expertise_sync,
        "run_expertise_bitrix_sync",
        lambda *, only_failed=False: {"scanned": 5, "synced": 0, "errors": 5, "disabled": 0},
    )

    with pytest.raises(SystemExit) as exc_info:
        run_expertise_sync.main()

    assert exc_info.value.code == 1
