#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asr_runtime import load_whisper_model_with_repair


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe one audio file with faster-whisper.")
    parser.add_argument("audio_path", help="Path to input audio file.")
    parser.add_argument(
        "--model", default="large-v3", help="Model name/path (default: %(default)s)."
    )
    parser.add_argument(
        "--language", default="ru", help="Language hint or 'auto' (default: %(default)s)."
    )
    parser.add_argument("--device", default="cpu", help="Device: cpu/cuda (default: %(default)s).")
    parser.add_argument(
        "--compute-type", default="int8", help="Compute type (default: %(default)s)."
    )
    parser.add_argument(
        "--beam-size", type=int, default=5, help="Beam size (default: %(default)s)."
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # pragma: no cover
        print(f"missing faster-whisper dependency: {exc}", file=sys.stderr)
        return 2

    language = None if args.language == "auto" else args.language

    def build():
        return WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=4,
            num_workers=1,
        )

    try:
        model = load_whisper_model_with_repair(build, args.model)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    segments_iter, _info = model.transcribe(
        args.audio_path,
        language=language,
        beam_size=args.beam_size,
        vad_filter=True,
        condition_on_previous_text=True,
        word_timestamps=False,
    )
    text = "\n".join(
        (getattr(segment, "text", "") or "").strip() for segment in segments_iter
    ).strip()
    if not text:
        print("empty transcript", file=sys.stderr)
        return 3
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
