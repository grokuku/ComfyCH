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
custom_nodes_vol = modal.Volume.from_name("comfy-custom-nodes", create_if_missing=True)

CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB

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

# ── Build image (minimal) ─────────────────────────────────────────────
# Local models are NO LONGER baked into the image — they are uploaded in
# chunks directly to the volume (see upload_model_chunk below). The image
# only needs Python sources + aria2 + huggingface_hub for HF downloads.

_sync_image = (
    modal.Image.debian_slim(python_version="3.11")
    .add_local_python_source("helpers", "models", copy=True)
    .apt_install("aria2")
    .pip_install("huggingface_hub")
)

app = modal.App("comfy-sync", image=_sync_image)


# ── Remote functions: chunked upload of local models to the volume ──────


def _link_and_update_manifest(filename: str, model_dir: str) -> None:
    """Create the ComfyUI symlink for a model and update model_manifest.json.

    Must be called inside a Modal function with the volume mounted at /cache.
    """
    import json as _json

    dst = Path("/cache") / filename

    # Create symlink in ComfyUI model directory
    target_dir = resolve_model_dir(model_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()

    target_path.symlink_to(dst)

    # Update manifest (accumulate with existing entries)
    manifest_path = Path("/cache") / "model_manifest.json"
    existing_manifest = {}
    if manifest_path.exists():
        try:
            existing_manifest = _json.loads(manifest_path.read_text())
        except (ValueError, OSError):
            pass
    existing_manifest[filename] = model_dir
    manifest_path.write_text(_json.dumps(existing_manifest, indent=2))


@app.function(
    cpu=1,
    memory=512,
    volumes={"/cache": vol},
    timeout=60,
)
def check_model_exists(filename: str, size: int) -> dict:
    """Check whether a model file already exists on the volume with the same size."""
    dst = Path("/cache") / filename
    exists = dst.exists() and dst.stat().st_size == size
    return {"exists": exists}


@app.function(
    cpu=2,
    memory=4096,
    volumes={"/cache": vol},
    timeout=3600,
)
def upload_model_chunk(
    chunk_data: bytes,
    filename: str,
    model_dir: str,
    offset: int,
    is_last: bool,
) -> dict:
    """Append one chunk of a model file to the volume.

    Chunks are written sequentially (mode "ab" when offset > 0, "wb" for the
    first chunk). On the last chunk, the symlink + manifest are updated and
    the volume is committed.
    """
    import os

    dest = Path("/cache") / filename

    mode = "ab" if offset > 0 else "wb"
    with open(dest, mode) as f:
        f.write(chunk_data)

    if is_last:
        _link_and_update_manifest(filename, model_dir)
        vol.commit()
        return {"ok": True, "filename": filename, "size": os.path.getsize(dest)}

    return {"ok": True, "offset": offset + len(chunk_data)}


@app.function(
    cpu=1,
    memory=512,
    volumes={"/cache": vol},
    timeout=120,
)
def link_model(filename: str, model_dir: str) -> dict:
    """Re-create symlink + manifest entry for a model already on the volume."""
    _link_and_update_manifest(filename, model_dir)
    vol.commit()
    return {"ok": True, "filename": filename, "linked": True}


# ── Remote function: sync custom nodes archive to dedicated volume ──────


@app.function(
    cpu=2,
    memory=4096,
    volumes={"/custom_nodes": custom_nodes_vol},
    timeout=1800,
)
def sync_custom_nodes_to_volume(archive_data: bytes) -> dict:
    """Receive a tar.gz archive of custom nodes and extract to the volume.

    Extraction happens in a ``.staging`` directory with path traversal
    protection, then an atomic swap replaces the volume content.
    """
    import io
    import os
    import shutil as _shutil
    import tarfile

    base_path = "/custom_nodes"
    staging_dir = os.path.join(base_path, ".staging")

    # Clean any leftover staging dir
    if os.path.exists(staging_dir):
        _shutil.rmtree(staging_dir)
    os.makedirs(staging_dir)

    # Extract to staging with path traversal protection
    buf = io.BytesIO(archive_data)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        for member in tar.getmembers():
            # Reject absolute paths and parent references
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise ValueError(f"Tar member '{member.name}' contains unsafe path")
            # Verify resolved path stays within staging directory
            member_path = os.path.normpath(os.path.join(staging_dir, member.name))
            if not member_path.startswith(os.path.normpath(staging_dir) + os.sep):
                raise ValueError(f"Tar member '{member.name}' would extract outside target directory")
        # Reset buffer and extract after validation
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar2:
            tar2.extractall(path=staging_dir)

    # Swap: remove old content (except staging), move staging content into place
    for item in os.listdir(base_path):
        if item == ".staging":
            continue
        item_path = os.path.join(base_path, item)
        if os.path.isdir(item_path):
            _shutil.rmtree(item_path)
        else:
            os.remove(item_path)

    for item in os.listdir(staging_dir):
        _shutil.move(os.path.join(staging_dir, item), os.path.join(base_path, item))

    # Clean up staging
    _shutil.rmtree(staging_dir)

    custom_nodes_vol.commit()

    nodes = [d for d in os.listdir(base_path) if os.path.isdir(os.path.join(base_path, d))]
    return {"status": "ok", "nodes": sorted(nodes)}


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
def main(sync_custom_nodes: bool = False) -> None:
    """Local entrypoint — uploads local models, then syncs HuggingFace models.

    With ``--custom-nodes``, only the custom nodes archive is synced to the
    dedicated ``comfy-custom-nodes`` volume (other steps are skipped).
    """

    # ── Custom nodes only mode ─────────────────────────────────────────
    if sync_custom_nodes:
        import io
        import tarfile

        print(f"\n{'='*60}")
        print("🧩 Syncing custom nodes to volume")
        print(f"{'='*60}\n")

        custom_nodes_dir = _SCRIPT_DIR.parent  # ComfyUI/custom_nodes
        if not custom_nodes_dir.is_dir():
            print(f"❌ Custom nodes directory not found: {custom_nodes_dir}")
            return

        EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for node_dir in sorted(custom_nodes_dir.iterdir()):
                if not node_dir.is_dir():
                    continue
                if node_dir.name.startswith(".") or node_dir.name.startswith("__pycache__"):
                    continue
                for item in node_dir.rglob("*"):
                    rel_parts = item.relative_to(node_dir).parts
                    # Skip excluded dirs anywhere in the path and hidden dirs
                    if any(p in EXCLUDE_DIRS or p.startswith(".") for p in rel_parts[:-1]):
                        continue
                    if item.is_dir() and item.name in EXCLUDE_DIRS:
                        continue
                    if item.is_file() and item.suffix in (".pyc", ".pyo"):
                        continue
                    tar.add(str(item), arcname=str(Path(node_dir.name) / item.relative_to(node_dir)))

        size_mb = buf.tell() / (1024 * 1024)
        print(f"📦 Archive created: {size_mb:.1f} MB")

        result = sync_custom_nodes_to_volume.remote(buf.getvalue())
        nodes = result.get("nodes", [])
        print(f"✅ Custom nodes synced: {len(nodes)} node(s) on volume")
        for n in nodes:
            print(f"  📁 {n}")
        return

    # ── Step 1: Upload + link local models ───────────────────────────────
    if models_to_sync:
        print(f"\n{'='*60}")
        print(f"📦 Step 1: Uploading {len(models_to_sync)} local model(s) to Volume")
        print(f"{'='*60}\n")

        results = {}

        for model in models_to_sync:
            filename = model["filename"]
            model_dir = model["model_dir"]

            model_path = (local_models_dir / model_dir / filename) if local_models_dir else None
            if model_path is None or not model_path.exists():
                print(f"  ❌ Local model not found: {model_path}")
                results[filename] = {"ok": False, "error": "not found locally"}
                continue

            size = model_path.stat().st_size
            size_mb = size / (1024 * 1024)

            # Skip upload if already on the volume with the same size
            existing = check_model_exists.remote(filename, size)
            if existing.get("exists"):
                print(f"  ⏭️ Already on volume: {filename} ({size_mb:.0f} MB) — linking only")
                link_model.remote(filename, model_dir)
                results[filename] = {"ok": True, "size_mb": round(size_mb, 1), "skipped": True}
                continue

            # Upload by chunks of 100 MB
            print(f"  📤 Uploading: {filename} ({size_mb:.0f} MB)")
            offset = 0
            uploaded_mb = 0
            try:
                with open(model_path, "rb") as f:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        is_last = offset + len(chunk) >= size
                        upload_model_chunk.remote(chunk, filename, model_dir, offset, is_last)
                        offset += len(chunk)
                        uploaded_mb = offset / (1024 * 1024)
                        pct = (offset / size) * 100 if size else 100
                        print(f"     {uploaded_mb:.0f}/{size_mb:.0f} MB ({pct:.0f}%)")
            except Exception as e:  # noqa: BLE001 — report and continue with next model
                print(f"  ❌ Upload failed: {filename}: {e}")
                results[filename] = {"ok": False, "error": str(e)}
                continue

            print(f"  ✅ {filename}: uploaded ({size_mb:.0f} MB) + linked")
            results[filename] = {"ok": True, "size_mb": round(size_mb, 1)}

        ok_count = sum(1 for r in results.values() if r.get("ok"))
        fail_count = len(results) - ok_count

        print(f"\n📊 Results: {ok_count} OK, {fail_count} failed")
        for filename, info in results.items():
            status = "✅" if info.get("ok") else "❌"
            size = info.get("size_mb", 0)
            error = info.get("error", "")
            if info.get("ok"):
                skipped = " (déjà présent)" if info.get("skipped") else ""
                print(f"  {status} {filename} ({size} MB){skipped}")
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

    # ── Step 3: Sync user settings to volume ───────────────────────────────
    print(f"\n{'='*60}")
    print(f"👤 Step 3: Syncing user settings to volume")
    print(f"{'='*60}\n")

    user_vol = modal.Volume.from_name("comfy-user-settings", create_if_missing=True)

    user_dir = Path(__file__).resolve().parent.parent.parent / "user"
    if user_dir.is_dir():
        # Build a filtered list of files to upload (skip .db files)
        files_to_upload = []
        for f in user_dir.rglob("*"):
            if not f.is_file():
                continue
            # Skip database and cache files that might be locked/modified during upload
            if f.suffix.lower() in (".db", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".log", ".tmp", ".temp"):
                continue
            # Skip cache directories
            if "cache" in f.parts or "__manager" in f.parts:
                continue
            files_to_upload.append(f)

        print(f"📁 Uploading {len(files_to_upload)} user setting file(s) from {user_dir}...")

        uploaded = 0
        skipped = 0
        for f in files_to_upload:
            rel_path = f.relative_to(user_dir)
            try:
                with user_vol.batch_upload() as batch:
                    batch.put_file(str(f), f"/{rel_path}")
                uploaded += 1
            except FileExistsError:
                skipped += 1

        print(f"✅ User settings synced to volume ({uploaded} uploaded, {skipped} already present)")
    else:
        print(f"❌ Local user/ directory not found at {user_dir}")

    print(f"\n{'='*60}")
    print("✅ Sync terminée !")
    print(f"{'='*60}")