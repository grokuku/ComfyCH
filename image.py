"""Shared Docker image for ComfyUI workers.

Extracted from ``comfyui.py`` so that multiple worker variants (L4, L40S,
A100, H100) can reuse the same image definition without duplication.

Model downloads have been moved to ``sync.py`` (CPU-only, runs on a cheap
container).  This image **must not** download any models.
"""

from __future__ import annotations

import json
from pathlib import Path

import modal

from plugins import comfy_plugins

try:
    from plugins import comfy_plugins_ext
except ImportError:
    comfy_plugins_ext = []

custom_nodes_local = []

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
if _CONFIG_PATH.exists():
    try:
        _saved = json.loads(_CONFIG_PATH.read_text())
        if _saved.get("custom_nodes"):
            comfy_plugins = list(_saved["custom_nodes"])
        if _saved.get("custom_nodes_ext"):
            comfy_plugins_ext = list(_saved["custom_nodes_ext"])
        if _saved.get("custom_nodes_local"):
            custom_nodes_local = list(_saved["custom_nodes_local"])
    except (json.JSONDecodeError, OSError):
        pass

root_dir = Path(__file__).parent


def _build_image() -> modal.Image:
    """Build and return the ComfyUI Docker image (no model downloads)."""
    image = (
        modal.Image.debian_slim(python_version="3.11")
        .add_local_python_source("image", "helpers", "auth", "gateway_router", "workers", "models", "plugins", copy=True)
        .apt_install("git", "git-lfs", "libgl1-mesa-dev", "libglib2.0-0", "aria2")
        .pip_install_from_requirements(str(root_dir / "requirements_comfy.txt"))
        .run_commands("comfy --skip-prompt install --nvidia")
        .run_commands("git lfs install")
    )

    # ── Optional workflow dependencies ──────────────────────────────────
    workflow_file_path = root_dir / "workflow_api.json"
    if workflow_file_path.exists():
        image = image.add_local_file(
            workflow_file_path, "/root/workflow_api.json", copy=True
        ).run_commands("comfy node install-deps --workflow=/root/workflow_api.json")
    else:
        print(
            "Warning: workflow_api.json not found. "
            "API endpoint might not work without a workflow."
        )

    # ── Custom node dependencies (pip) — code lives on the volume ──────
    # Install pip deps of selected custom nodes (read local requirements.txt)
    # so the worker can load node code from the volume without a redeploy.
    local_custom_nodes_dir = root_dir.parent  # ComfyUI/custom_nodes
    selected_names = set()
    for p in comfy_plugins_ext:
        url = p.get("url", "") if isinstance(p, dict) else str(p)
        name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if name:
            selected_names.add(name)
    selected_names.update(custom_nodes_local)
    selected_names.update(comfy_plugins)  # registry ids (best-effort)
    for name in sorted(selected_names):
        req_file = local_custom_nodes_dir / name / "requirements.txt"
        if req_file.is_file():
            image = image.add_local_file(str(req_file), f"/deps/{name}/requirements.txt", copy=True)
            image = image.run_commands(
                f"pip install -r /deps/{name}/requirements.txt || "
                f"echo '⚠️ Failed to install deps for {name}'"
            )

    # ── Reverse-proxy fix so workflow save works behind Modal's edge proxy
    image = image.add_local_dir(
        root_dir / "vendor_nodes" / "reverse_proxy_fix",
        "/root/comfy/ComfyUI/custom_nodes/reverse_proxy_fix",
        copy=True,
    )

    return image


# ── Single shared instance ───────────────────────────────────────────────
image = _build_image()
