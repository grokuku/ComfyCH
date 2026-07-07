"""CPU-only Modal app to sync model files into the ``comfy-models`` Volume.

Run on-demand after changing ``models.py`` — no GPU needed, no image rebuild
required.

Usage::

    modal run sync.py
"""

from __future__ import annotations

import modal

from helpers import (
    download_external_model,
    get_hf_secrets,
    hf_download,
)
from models import models, models_ext

vol = modal.Volume.from_name("comfy-models", create_if_missing=True)

# Minimal CPU image with just our local sources and huggingface_hub.
# No GPU dependencies, no ComfyUI install.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .add_local_python_source("helpers", "models", copy=True)
    .apt_install("aria2")
    .pip_install("huggingface_hub")
)

app = modal.App("comfy-sync", image=image)


@app.function(
    cpu=1,
    memory=2048,
    volumes={"/cache": vol},
    secrets=get_hf_secrets(),
)
def sync() -> None:
    """Download all models defined in ``models.py`` into the shared Volume."""
    print(f"Starting sync: {len(models)} HF models + {len(models_ext)} external models")

    for i, model in enumerate(models, start=1):
        print(f"[{i}/{len(models)}] HF: {model['repo_id']}/{model['filename']}")
        hf_download(model["repo_id"], model["filename"], model["model_dir"])

    for i, model in enumerate(models_ext, start=1):
        print(f"[{i}/{len(models_ext)}] External: {model['filename']}")
        download_external_model(model["url"], model["filename"], model["model_dir"])

    print("✅ Sync terminée !")


@app.local_entrypoint()
def main() -> None:
    """Local entrypoint — calls ``sync.remote()`` on Modal infrastructure."""
    sync.remote()
    print("✅ Sync terminée !")
