from __future__ import annotations

from app.services.query_batching import (
    load_text_mapping_in_batches,
    normalized_text_batches,
)


def test_sql_code_batches_are_stable_unique_and_below_rpc_limit() -> None:
    values = [f"CODE-{index:04d}" for index in range(2505)] + ["CODE-0001", ""]

    batches = normalized_text_batches(values)

    assert [len(batch) for batch in batches] == [1000, 1000, 505]
    assert batches[0][0] == "CODE-0000"
    assert batches[-1][-1] == "CODE-2504"
    assert all(len(batch) < 2100 for batch in batches)


def test_mapping_loader_merges_every_batch_once() -> None:
    calls: list[tuple[str, ...]] = []

    def load(batch: tuple[str, ...]) -> dict[str, int]:
        calls.append(batch)
        return {code: index for index, code in enumerate(batch)}

    result = load_text_mapping_in_batches(
        [f"CODE-{index:04d}" for index in range(2336)],
        load,
    )

    assert [len(batch) for batch in calls] == [1000, 1000, 336]
    assert len(result) == 2336
