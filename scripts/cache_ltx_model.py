#!/usr/bin/env python3
"""Explicitly check or resumably download the configured LTX Q8 Hub snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import threading

MODEL_ID = "dgrauet/ltx-2.3-mlx-q8"
HEADROOM_BYTES = 8 * 1024**3


def hub_cache(cache_dir: Path) -> Path:
    return cache_dir if cache_dir.name == "hub" else cache_dir / "hub"


def local_snapshot(cache_dir: Path) -> Path | None:
    try:
        from huggingface_hub import snapshot_download

        path = Path(snapshot_download(repo_id=MODEL_ID, cache_dir=str(hub_cache(cache_dir)), local_files_only=True))
        weights = tuple(path.glob("*.safetensors"))
        return path if weights else None
    except Exception:
        return None


def model_file_sizes() -> list[tuple[str, int, str]]:
    from huggingface_hub import HfApi

    info = HfApi().model_info(MODEL_ID, files_metadata=True)
    files = []
    for item in info.siblings:
        size = getattr(item, "size", None)
        lfs = getattr(item, "lfs", None)
        if size is None and lfs is not None:
            size = getattr(lfs, "size", None)
        if size is None:
            raise RuntimeError(f"Hugging Face did not report the size for {getattr(item, 'rfilename', 'a repository file')}")
        lfs = getattr(item, "lfs", None)
        blob_id = getattr(lfs, "oid", None) if lfs is not None else getattr(item, "blobId", None)
        if blob_id:
            blob_id = str(blob_id).split(":")[-1]
        files.append((str(getattr(item, "rfilename", "")), int(size), str(blob_id or "")))
    if not files:
        raise RuntimeError("Hugging Face did not report repository file sizes; refusing to start a download without a disk-space estimate")
    return files


def emit_progress_class():
    from huggingface_hub.utils import tqdm as tqdm_base

    base = tqdm_base
    lock = threading.Lock()
    per_file: dict[str, int] = {}
    total_expected = int(os.environ.get("AI_VIDEO_MODEL_TOTAL_BYTES", "0"))

    class ProgressTqdm(base):
        def update(self, n=1):
            result = super().update(n)
            name = str(id(self))
            with lock:
                per_file[name] = min(int(self.n), int(self.total or self.n))
                current = sum(per_file.values())
                total = max(current, total_expected)
                print(f"DOWNLOAD_PROGRESS {current} {total}", flush=True)
            return result

    return ProgressTqdm


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--download", action="store_true", help="Explicit opt-in to downloading model weights")
    args = parser.parse_args()
    cache_dir = args.cache_dir.expanduser().resolve()
    os.environ["HF_HOME"] = str(cache_dir)

    existing = local_snapshot(cache_dir)
    if existing:
        print(f"MODEL_STATUS installed {existing}", flush=True)
        return 0
    if args.check:
        print(f"MODEL_STATUS missing {MODEL_ID}; no network request was made", flush=True)
        return 2

    # --download is an explicit consent boundary. Size lookup is metadata-only.
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    try:
        files = model_file_sizes()
    except Exception as exc:
        text = str(exc)
        if any(token in text.casefold() for token in ("gated", "401", "403", "unauthorized", "forbidden")):
            print("MODEL_AUTH_ERROR This repository requires Hugging Face access approval or login. Accept the model terms in your Hugging Face account, then run `hf auth login` and retry. No credentials were written by this setup.", file=sys.stderr, flush=True)
        else:
            print(f"MODEL_PREFLIGHT_ERROR Could not inspect model metadata: {text}", file=sys.stderr, flush=True)
        return 3
    model_cache = hub_cache(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = model_cache / "models--dgrauet--ltx-2.3-mlx-q8"
    blobs = repo_dir / "blobs"
    total = sum(size for _, size, _ in files)
    remaining = 0
    for _, size, blob_id in files:
        if not blob_id:
            remaining += size
            continue
        complete = blobs / blob_id
        incomplete = blobs / f"{blob_id}.incomplete"
        available = 0
        try:
            if complete.is_file():
                available = complete.stat().st_size
            elif incomplete.is_file():
                available = incomplete.stat().st_size
        except OSError:
            pass
        remaining += max(0, size - min(size, available))
    free = shutil.disk_usage(cache_dir).free
    required = remaining + HEADROOM_BYTES
    print(json.dumps({"model_bytes": total, "remaining_bytes": remaining,
                      "headroom_bytes": HEADROOM_BYTES, "free_bytes": free}), flush=True)
    if free < required:
        print(f"MODEL_PREFLIGHT_ERROR Need {required} free bytes (model remainder plus 8 GiB headroom); found {free}. Free disk space and rerun to resume.", file=sys.stderr, flush=True)
        return 4
    print("SETUP_STAGE:model:Downloading LTX Q8 model files with Hugging Face cache resume enabled", flush=True)
    try:
        from huggingface_hub import snapshot_download

        os.environ["AI_VIDEO_MODEL_TOTAL_BYTES"] = str(remaining)
        snapshot_download(repo_id=MODEL_ID, cache_dir=str(model_cache), local_files_only=False,
                          max_workers=1, tqdm_class=emit_progress_class())
    except Exception as exc:
        text = str(exc)
        lowered = text.casefold()
        if "gated" in lowered or "401" in lowered or "403" in lowered:
            print("MODEL_AUTH_ERROR This repository requires Hugging Face access approval or login. Accept the model terms in your Hugging Face account, then run `hf auth login` and retry. No credentials were written by this setup.", file=sys.stderr, flush=True)
        else:
            print(f"MODEL_DOWNLOAD_ERROR {text}\nPartial Hub cache files were retained; rerun to resume.", file=sys.stderr, flush=True)
        return 5
    # Verify from the cache with offline mode set; this makes no network requests.
    os.environ["HF_HOME"] = str(cache_dir)
    os.environ["HF_HUB_OFFLINE"] = "1"
    verified = local_snapshot(cache_dir)
    if not verified:
        print("MODEL_VERIFY_ERROR Download ended without a complete local safetensors snapshot; rerun to resume.", file=sys.stderr, flush=True)
        return 6
    print(f"MODEL_STATUS installed and verified offline {verified}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
