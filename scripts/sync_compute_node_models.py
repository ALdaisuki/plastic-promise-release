"""Download a pinned rerank model tree for the Windows compute node.

The Windows compute node never downloads model weights at container start; the
operator prepares an immutable read-only model directory first (compose binds
``/models`` read-only).  This script is the persisted, idempotent model sync
step used by the ``PPNodeModelSync`` scheduled task.  It pins an exact HF
revision, verifies the files the CrossEncoder needs, and never falls back to
``latest``/``main``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

DEFAULT_REQUIRED_FILES = (
    "config.json",
    "tokenizer.json",
    "vocab.json",
    "merges.txt",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "1_LogitScore/config.json",
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plastic-promise-model-sync")
    parser.add_argument("--repo-id", required=True, help="HF repo id (e.g. Qwen/Qwen3-Reranker-4B)")
    parser.add_argument("--revision", required=True, help="Exact 40-hex HF revision to pin")
    parser.add_argument("--target", required=True, help="Local destination directory")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("HF_ENDPOINT", "https://huggingface.co"),
        help="HF-compatible endpoint; set HF_ENDPOINT for a regional mirror",
    )
    parser.add_argument(
        "--filename",
        help="Download one pinned artifact instead of the default CrossEncoder tree",
    )
    parser.add_argument(
        "--sha256",
        help="Required lowercase/uppercase SHA-256 for --filename",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["HF_ENDPOINT"] = args.endpoint
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(target / ".hf-cache"))

    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError as exc:  # pragma: no cover - operator prerequisite
        print(f"huggingface_hub is required: {exc}", file=sys.stderr)
        return 1

    if args.filename:
        if not args.sha256 or len(args.sha256) != 64:
            print("--filename requires a 64-hex --sha256", file=sys.stderr)
            return 2
        try:
            int(args.sha256, 16)
        except ValueError:
            print("--sha256 must be hexadecimal", file=sys.stderr)
            return 2
        if Path(args.filename).name != args.filename:
            print("--filename must be a repository-root filename", file=sys.stderr)
            return 2
        downloaded = Path(
            hf_hub_download(
                repo_id=args.repo_id,
                filename=args.filename,
                revision=args.revision,
                local_dir=str(target),
                cache_dir=str(target / ".hf-cache"),
            )
        )
        observed = _file_sha256(downloaded)
        if observed.casefold() != args.sha256.casefold():
            print("downloaded model SHA-256 mismatch", file=sys.stderr)
            return 3
    else:
        snapshot_download(
            repo_id=args.repo_id,
            revision=args.revision,
            local_dir=str(target),
            max_workers=8,
        )
        missing = [name for name in DEFAULT_REQUIRED_FILES if not (target / name).is_file()]
        if missing:
            print(f"missing required model files: {', '.join(missing)}", file=sys.stderr)
            return 2
    print("PP_NODE_MODEL_SYNC_COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
