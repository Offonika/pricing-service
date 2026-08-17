"""Strict two-step evaluator for display assortment lifecycle v2 candidates.

``select-training`` receives only February-June candidate metrics and freezes one
candidate. ``evaluate-holdout`` accepts only that exact candidate for the July
holdout. Both commands are read-only with respect to databases, 1C and orders;
they only replace the explicitly requested JSON artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.services.assortment_lifecycle_v2_backtest import (
    evaluate_selected_holdout,
    select_training_candidate,
)
from app.services.assortment_lifecycle_v2_policy import (
    DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    load_assortment_lifecycle_v2_policy,
)


def _cmd_select_training(args: argparse.Namespace) -> int:
    policy = load_assortment_lifecycle_v2_policy(args.policy_json)
    result = select_training_candidate(
        _load_object(args.training_results_json),
        policy=policy,
        policy_sha256=_sha256(args.policy_json),
    )
    _write_result(args.output_json, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["selected_candidate_id"] else 2


def _cmd_evaluate_holdout(args: argparse.Namespace) -> int:
    policy = load_assortment_lifecycle_v2_policy(args.policy_json)
    result = evaluate_selected_holdout(
        _load_object(args.selection_json),
        _load_object(args.holdout_results_json),
        policy=policy,
        policy_sha256=_sha256(args.policy_json),
    )
    _write_result(args.output_json, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["decision"] == "eligible_for_diff_review" else 3


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    training = subcommands.add_parser(
        "select-training",
        help="Select one candidate from configured training dates without holdout data",
    )
    training.add_argument("--training-results-json", type=Path, required=True)
    training.add_argument(
        "--policy-json",
        type=Path,
        default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    )
    training.add_argument("--output-json", type=Path, required=True)
    training.set_defaults(func=_cmd_select_training)

    holdout = subcommands.add_parser(
        "evaluate-holdout",
        help="Evaluate only the candidate frozen by select-training",
    )
    holdout.add_argument("--selection-json", type=Path, required=True)
    holdout.add_argument("--holdout-results-json", type=Path, required=True)
    holdout.add_argument(
        "--policy-json",
        type=Path,
        default=DEFAULT_ASSORTMENT_LIFECYCLE_V2_POLICY_PATH,
    )
    holdout.add_argument("--output-json", type=Path, required=True)
    holdout.set_defaults(func=_cmd_evaluate_holdout)
    return parser.parse_args(argv)


def _load_object(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"{path} must contain a JSON object")
    return payload


def _write_result(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
