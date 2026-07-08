"""CPU-only Modal app to sync model files into the ``comfy-models`` Volume.

Supports two sync modes:
1. **Local models** — files detected in the local ComfyUI/models/ directory,
   selected via the Modal Gateway UI, added to the image and copied to the Volume.
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

# ── Build image with local model files ───────────────────────────────────
# In Modal 1.5.1, modal.Mount is removed. We use Image.add_local_file() instead.

_sync_image = (
    modal.Image.debian_slim(python_version="3.11")
    .add_local_python_source("helpers", "models", copy=True)
    .apt_install("aria2")
    .pip_install("huggingface_hub")
)

# Add each selected local model file to the image
if local_models_dir and models_to_sync:
    for model in models_to_sync:
        filename = model["filename"]
        model_dir = model["model_dir"]
        model_path = local_models_dir / model_dir / filename

        if not model_path.exists():
            print(f"⚠️ Local model not found: {model_path}")
            continue

        size_mb = model_path.stat().st_size / (1024 * 1024)
        print(f"📦 Adding to image: {filename} ({size_mb:.0f} MB) from {model_dir}/")
        _sync_image = _sync_image.add_local_file(
            str(model_path),
            f"/local_models/{filename}",
        )

app = modal.App("comfy-sync", image=_sync_image)


# ── Remote function: copy local models to volume + create symlinks ──────


@app.function(
    cpu=2,
    memory=4096,
    volumes={"/cache": vol},
    timeout=3600,  # 1 hour for large files
)
def upload_and_link_local_models(models_list: list[dict]) -> dict:
    """Copy model files from the image to the Volume and create symlinks.

    The local model files were added to the image via Image.add_local_file()
    and are available at /local_models/<filename>.

    Returns a dict with verification results for each file.
    """
    results = {}

    for model in models_list:
        filename = model["filename"]
        model_dir = model["model_dir"]

        src = Path(f"/local_models/{filename}")
        if not src.exists():
            print(f"  ❌ Not found in image: {filename}")
            results[filename] = {"ok": False, "error": "not in image"}
            continue

        src_size = src.stat().st_size
        size_mb = src_size / (1024 * 1024)

        # Copy to volume
        dst = Path("/cache") / filename
        print(f"  📤 Copying to volume: {filename} ({size_mb:.0f} MB)")

        # Remove existing file on volume if any
        if dst.exists() or dst.is_symlink():
            dst.unlink()

        shutil.copy2(str(src), str(dst))

        # Verify copy
        dst_size = dst.stat().st_size
        if dst_size != src_size:
            print(f"  ❌ Size mismatch: {filename} (expected {src_size}, got {dst_size})")
            results[filename] = {"ok": False, "error": "size mismatch"}
            continue

        # Create symlink in ComfyUI model directory
        target_dir = resolve_model_dir(model_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename

        if target_path.exists() or target_path.is_symlink():
            target_path.unlink()

        target_path.symlink_to(dst)
        print(f"  ✅ {filename}: copied ({size_mb:.0f} MB) + linked -> {target_path}")
        results[filename] = {"ok": True, "size_mb": round(size_mb, 1)}

    # Write manifest for workers to read at startup
    import json as _json
    manifest = {model["filename"]: model["model_dir"] for model in models_list if results.get(model["filename"], {}).get("ok")}
    manifest_path = Path("/cache") / "model_manifest.json"
    manifest_path.write_text(_json.dumps(manifest, indent=2))
    print(f"  📝 Manifest written: {len(manifest)} entries")

    # Commit volume changes
    vol.commit()
    ok_count = len([r for r in results.values() if r.get("ok")])
    print(f"  💾 Volume committed ({ok_count}/{len(models_list)} files)")
    return results


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
    """Local entrypoint — uploads local models, then syncs HuggingFace models."""

    # ── Step 1: Upload + link local models ───────────────────────────────
    if models_to_sync:
        print(f"\n{'='*60}")
        print(f"📦 Step 1: Uploading {len(models_to_sync)} local model(s) to Volume")
        print(f"{'='*60}\n")

        results = upload_and_link_local_models.remote(models_to_sync)

        ok_count = sum(1 for r in results.values() if r.get("ok"))
        fail_count = len(results) - ok_count

        print(f"\n📊 Results: {ok_count} OK, {fail_count} failed")
        for filename, info in results.items():
            status = "✅" if info.get("ok") else "❌"
            size = info.get("size_mb", 0)
            error = info.get("error", "")
            if info.get("ok"):
                print(f"  {status} {filename} ({size} MB)")
            else:
                print(f"  {status} {filename}: {error}")

        if fail_count > 0:
            print(f"\n⚠️ {fail_count} file(s) failed to upload!")

    else:
        print("\nℹ️ No local models selected in config.json")

    # ── Step 2: Download HuggingFace models (from models.py) ─────────────
    if models or models_ext:
        print(f"\n{'='*60}")
        print(f"📥 Step 2: Downloading HuggingFace/external models")
        print(f"{'='*60}\n")
        sync_hf_models.remote()

    print(f"\n{'='*60}")
    print("✅ Sync terminée !")
    print(f"{'='*60}")