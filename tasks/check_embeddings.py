"""Preflight check for the embeddings provider used by competitor matching."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from app.services.embeddings import EmbeddingClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Check embeddings provider availability")
    parser.add_argument("--embed-model", default=None, help="Embedding model override")
    parser.add_argument("--expected-dim", type=int, default=None, help="Expected vector size")
    args = parser.parse_args()

    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        "model": args.embed_model,
        "expected_dim": args.expected_dim,
        "ok": False,
    }
    try:
        client = EmbeddingClient(model=args.embed_model)
        dim = client.preflight(expected_dim=args.expected_dim)
        payload.update({"ok": True, "model": client.model, "dim": dim})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        payload.update({"error": type(exc).__name__, "message": str(exc)})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
