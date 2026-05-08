"""Compute embeddings for products and competitor items."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CompetitorItem, Product
from app.services.embedding_utils import (
    compose_competitor_text,
    compose_product_text,
    text_hash,
)
from app.services.embeddings import EmbeddingClient


def _load_index(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {"meta": {}, "items": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_index(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _batch(iterable: list[tuple[int, str]], batch_size: int) -> Iterable[list[tuple[int, str]]]:
    for idx in range(0, len(iterable), batch_size):
        yield iterable[idx : idx + batch_size]


def _prepare_product_rows(
    session: Session,
    limit: int | None,
) -> list[tuple[int, str, str]]:
    query = select(Product)
    if limit:
        query = query.limit(limit)
    rows = []
    for product in session.execute(query).scalars():
        text = compose_product_text(
            product.name,
            product.brand,
            product.category,
            product.quality,
            product.display_type,
            product.display_quality,
            product.display_construction,
            product.display_refresh_rate_hz,
            product.display_screen_kit,
            product.display_has_frame,
            product.display_has_touch,
            product.display_has_ic_pad,
            product.display_has_binding_no_solder,
            product.display_backlight,
            product.display_matrix_tags,
            product.display_modification_status,
            product.color,
            product.article,
        )
        if not text:
            continue
        rows.append((product.id, text, product.article))
    return rows


def _prepare_competitor_rows(
    session: Session,
    limit: int | None,
    min_llm_confidence: float,
) -> list[tuple[int, str]]:
    query = select(CompetitorItem)
    if limit:
        query = query.limit(limit)
    rows = []
    for item in session.execute(query).scalars():
        if item.llm_confidence is not None and float(item.llm_confidence) < min_llm_confidence:
            continue
        text = compose_competitor_text(item.normalized_title or item.name, item.attrs_json)
        if not text:
            continue
        rows.append((item.id, text))
    return rows


def _update_embeddings(
    *,
    target_name: str,
    rows: list[tuple[int, str]],
    index_path: Path,
    embeddings_dir: Path,
    matrix_prefix: str,
    client: EmbeddingClient,
    batch_size: int,
    overwrite: bool,
    only_changed: bool,
    normalize: bool,
) -> None:
    index = _load_index(index_path)
    items: dict[str, dict] = index.get("items", {})
    matrix = None
    meta = index.get("meta", {})
    matrix_file = meta.get("matrix_file")
    legacy_path = embeddings_dir / f"{matrix_prefix}_embeddings.npy"
    matrix_path = embeddings_dir / matrix_file if matrix_file else legacy_path
    if matrix_path.exists() and items:
        matrix = np.load(matrix_path)

    model = client.model
    if overwrite:
        items = {}
        matrix = None

    to_compute: list[tuple[int, str]] = []
    for row_id, text in rows:
        key = str(row_id)
        digest = text_hash(text)
        if not only_changed:
            to_compute.append((row_id, text))
            continue
        existing = items.get(key)
        if not existing or existing.get("text_hash") != digest:
            to_compute.append((row_id, text))

    if not to_compute:
        return

    for chunk in _batch(to_compute, batch_size):
        ids = [row_id for row_id, _ in chunk]
        texts = [text for _, text in chunk]
        embeddings = client.embed_texts(texts)
        if not embeddings:
            continue
        vecs = np.array(embeddings, dtype=np.float32)
        if normalize:
            vecs = _normalize(vecs)

        append_vecs = []
        for idx, row_id in enumerate(ids):
            key = str(row_id)
            digest = text_hash(texts[idx])
            existing = items.get(key)
            if (
                existing
                and existing.get("row") is not None
                and matrix is not None
                and not overwrite
            ):
                row_idx = existing["row"]
                if row_idx < matrix.shape[0]:
                    matrix[row_idx] = vecs[idx]
                else:
                    append_vecs.append((key, digest, vecs[idx]))
            else:
                append_vecs.append((key, digest, vecs[idx]))

        if append_vecs:
            new_vectors = np.array([vec for _, _, vec in append_vecs], dtype=np.float32)
            if matrix is None:
                matrix = new_vectors
                start_idx = 0
            else:
                start_idx = matrix.shape[0]
                matrix = np.vstack([matrix, new_vectors])
            for offset, (key, digest, _) in enumerate(append_vecs):
                items[key] = {"row": start_idx + offset, "text_hash": digest}

    if matrix is None:
        return

    model_slug = (model or "embeddings").replace("/", "_")
    matrix_file = f"{matrix_prefix}_{model_slug}_{int(matrix.shape[1])}.npy"
    matrix_path = embeddings_dir / matrix_file

    index["meta"] = {
        "model": model,
        "dim": int(matrix.shape[1]),
        "updated_at": datetime.now(UTC).isoformat(),
        "normalized": bool(normalize),
        "target": target_name,
        "matrix_file": matrix_file,
    }
    index["items"] = items

    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(matrix_path, matrix)
    _save_index(index_path, index)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute embeddings for products/competitor items."
    )
    parser.add_argument("--target", choices=["products", "competitors", "both"], default="products")
    parser.add_argument("--limit", type=int, help="Limit records")
    parser.add_argument("--batch-size", type=int, default=None, help="Embedding batch size")
    parser.add_argument("--overwrite", action="store_true", help="Recompute everything")
    parser.add_argument(
        "--only-changed", action="store_true", help="Compute only changed (default)"
    )
    parser.add_argument(
        "--min-llm-confidence", type=float, default=None, help="Min LLM confidence for competitors"
    )
    parser.add_argument("--no-normalize", action="store_true", help="Store non-normalized vectors")
    parser.add_argument("--embed-model", default=None, help="Embedding model override")
    parser.add_argument("--embeddings-dir", default=None, help="Embeddings directory")
    args = parser.parse_args()

    settings = get_settings()
    embeddings_dir = Path(args.embeddings_dir or settings.embeddings_dir)
    batch_size = args.batch_size or settings.embeddings_batch_size
    min_llm_conf = (
        args.min_llm_confidence
        if args.min_llm_confidence is not None
        else settings.matching_min_llm_confidence
    )

    client = EmbeddingClient(model=args.embed_model, batch_size=batch_size)
    normalize_vectors = not args.no_normalize

    engine = create_engine(settings.database_url)
    with Session(engine) as session:
        if args.target in {"products", "both"}:
            product_rows = _prepare_product_rows(session, args.limit)
            _update_embeddings(
                target_name="products",
                rows=[(row_id, text) for row_id, text, _ in product_rows],
                index_path=embeddings_dir / "our_catalog_index.json",
                embeddings_dir=embeddings_dir,
                matrix_prefix="our_catalog",
                client=client,
                batch_size=batch_size,
                overwrite=args.overwrite,
                only_changed=True if args.only_changed or not args.overwrite else False,
                normalize=normalize_vectors,
            )
        if args.target in {"competitors", "both"}:
            competitor_rows = _prepare_competitor_rows(session, args.limit, min_llm_conf)
            _update_embeddings(
                target_name="competitor_items",
                rows=competitor_rows,
                index_path=embeddings_dir / "competitor_items_index.json",
                embeddings_dir=embeddings_dir,
                matrix_prefix="competitor_items",
                client=client,
                batch_size=batch_size,
                overwrite=args.overwrite,
                only_changed=True if args.only_changed or not args.overwrite else False,
                normalize=normalize_vectors,
            )


if __name__ == "__main__":
    main()
