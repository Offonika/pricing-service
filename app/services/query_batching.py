"""Small deterministic helpers for SQL queries with expanding parameters."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

SQL_SERVER_SAFE_EXPANDING_BATCH_SIZE = 1000


def normalized_text_batches(
    values: Sequence[object],
    *,
    batch_size: int = SQL_SERVER_SAFE_EXPANDING_BATCH_SIZE,
) -> tuple[tuple[str, ...], ...]:
    """Return sorted unique non-empty strings in stable SQL-safe batches."""

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    normalized = sorted({str(value or "").strip() for value in values if str(value or "").strip()})
    return tuple(
        tuple(normalized[offset : offset + batch_size])
        for offset in range(0, len(normalized), batch_size)
    )


def load_text_mapping_in_batches[ValueT](
    values: Sequence[object],
    loader: Callable[[tuple[str, ...]], Mapping[str, ValueT]],
    *,
    batch_size: int = SQL_SERVER_SAFE_EXPANDING_BATCH_SIZE,
) -> dict[str, ValueT]:
    """Load and merge disjoint code-keyed results without exceeding RPC limits."""

    result: dict[str, ValueT] = {}
    for batch in normalized_text_batches(values, batch_size=batch_size):
        result.update(loader(batch))
    return result
