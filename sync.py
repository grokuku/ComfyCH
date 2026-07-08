"""CPU-only Modal app to sync model files into the ``comfy-models`` Volume.

Supports two sync modes:
1. **Local models** — files detected in the local ComfyUI/models/ directory,
   selected via the Modal Gateway UI, and uploaded to the Volume.
2. **HuggingFace models** — files listed in ``models.py``, downloaded via
   ``huggingface_hub``.

Usage::

    modal run sync.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import modal

from helpers import (
    download_external_model,
    get_hf_secrets,
    hf_download,
    resolve_model_dir,
)
from models import models, models_ext

vol = modal.Volume.from_name("comfy-models", create_if_missing=True)

# Minimal CPU image with just our local sources and huggingface_hub.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .add_local_python_source("helpers", "models", copy=True)
    .apt_install("aria2")
    .pip_install("huggingface_hub")
)

app = modal.App("comfy-sync", image=image)

# ── Read config.json for local models to sync ────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _SCRIPT_DIR / "config.json"

models_to_sync: list[dict] = []
if _CONFIG_PATH.exists():
    try:
        _cfg = json.loads(_CONFIG_PATH.read_text())
        models_to_sync = _cfg.get("models_to_sync", [])
    except (json.JSONDecodeError, OSError):
        pass

# ── Find local ComfyUI models directory ──────────────────────────────────

_LOCAL_MODELS_CANDIDATES = [
    _SCRIPT_DIR.parent.parent / "models",   # custom_nodes/modal_gateway -> ComfyUI -> models
    _SCRIPT_DIR.parent / "models",          # if at project root
]

local_models_dir: Path | None = None
for candidate in _LOCAL_MODELS_CANDIDATES:
    if candidate.is_dir():
        local_models_dir = candidate
        break


# ── Remote function: clear + verify volume files ─────────────────────────


@app.function(
    cpu=1,
    memory=2048,
    volumes={"/cache": vol},
)
def manage_volume_files(filenames: list[str], mode: str = "clear") -> dict:
    """Clear existing files from volume or verify they exist.

    mode="clear": delete files that already exist (to avoid FileExistsError on upload)
    mode="verify": check that files exist and report their sizes
    """
    results = {}
    for filename in filenames:
        path = Path("/cache") / filename
        if mode == "clear":
            if path.exists():
                path.unlink()
                print(f"  🗑️ Deleted existing: {filename}")
            results[filename] = {"cleared": True}
        elif mode == "verify":
            exists = path.exists()
            size_mb = round(path.stat().st_size / (1024 * 1024), 1) if exists else 0
            results[filename] = {"exists": exists, "size_mb": size_mb}
            if exists:
                print(f"  ✅ Verified: {filename} ({size_mb} MB)")
            else:
                print(f"  ❌ MISSING: {filename}")
    vol.commit()
    return results


# ── Remote function: create symlinks for uploaded local models ───────────


@app.function(
    cpu=1,
    memory=2048,
    volumes={"/cache": vol},
)
def link_local_models(models_list: list[dict]) -> None:
    """Create symlinks in ComfyUI model dirs for files already in the Volume."""
    for model in models_list:
        filename = model["filename"]
        model_dir = model["model_dir"]

        cache_path = Path("/cache") / filename
        if not cache_path.exists():
            print(f"  ⚠️ Not in volume: {filename} — skipping")
            continue

        target_dir = resolve_model_dir(model_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        if target_path.exists() or target_path.is_symlink():
            target_path.unlink()

        target_path.symlink_to(cache_path)
        print(f"  ✅ Linked: {filename} -> {target_path}")


# ── Remote function: download HuggingFace + external models ──────────────


@app.function(
    cpu=1,
    memory=2048,
    volumes={"/cache": vol},
    secrets=get_hf_secrets(),
)
def sync_hf_models() -> None:
    """Download all models defined in ``models.py`` into the shared Volume."""
    print(f"HuggingFace models: {len(models)} | External: {len(models_ext)}")

    for i, model in enumerate(models, start=1):
        print(f"[{i}/{len(models)}] HF: {model['repo_id']}/{model['filename']}")
        hf_download(model["repo_id"], model["filename"], model["model_dir"])

    for i, model in enumerate(models_ext, start=1):
        print(f"[{i}/{len(models_ext)}] External: {model['filename']}")
        download_external_model(model["url"], model["filename"], model["model_dir"])


# ── Local entrypoint: orchestrate the full sync ──────────────────────────


@app.local_entrypoint()
def main() -> None:
    """Local entrypoint — uploads local models, then links them on the Volume."""

    # ── Step 1: Upload local model files to the Volume ───────────────────
    if local_models_dir and models_to_sync:
        filenames = [m["filename"] for m in models_to_sync]

        # 1a. Clear existing files to avoid FileExistsError
        print(f"🧹 Clearing existing files on Volume...")
        manage_volume_files.remote(filenames, mode="clear")

        # 1b. Upload local files
        print(f"📦 Uploading {len(models_to_sync)} local model(s) to Volume...")
        total_mb = 0
        with vol.batch_upload() as batch:
            for model in models_to_sync:
                filename = model["filename"]
                model_dir = model["model_dir"]
                model_path = local_models_dir / model_dir / filename

                if not model_path.exists():
                    print(f"  ⚠️ Not found locally: {model_path}")
                    continue

                size_mb = model_path.stat().st_size / (1024 * 1024)
                total_mb += size_mb
                print(f"  📤 Adding to batch: {filename} ({size_mb:.0f} MB)")
                batch.put_file(model_path, filename)

        print(f"  📦 Total to upload: {total_mb:.0f} MB — uploading to Modal...")

        # 1c. Verify files are on the volume
        print(f"🔍 Verifying uploaded files...")
        verification = manage_volume_files.remote(filenames, mode="verify")
        all_ok = True
        for filename, info in verification.items():
            if not info.get("exists"):
                print(f"  ❌ UPLOAD FAILED: {filename} not found on volume!")
                all_ok = False
        if all_ok:
            print(f"  ✅ All {len(filenames)} file(s) verified on Volume")
        else:
            print(f"  ⚠️ Some files missing — sync may be incomplete")
    else:
        if not models_to_sync:
            print("ℹ️ No local models selected in config.json")
        if not local_models_dir:
            print("ℹ️ Local ComfyUI models/ directory not found")

    # ── Step 2: Create symlinks on the Volume ────────────────────────────
    if models_to_sync:
        print("🔗 Creating symlinks in ComfyUI model directories...")
        link_local_models.remote(models_to_sync)

    # ── Step 3: Download HuggingFace models (from models.py) ─────────────
    if models or models_ext:
        print("📥 Downloading HuggingFace/external models...")
        sync_hf_models.remote()

    print("\n✅ Sync terminée !")