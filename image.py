"""Shared Docker image for ComfyUI workers.

Extracted from ``comfyui.py`` so that multiple worker variants (L4, L40S,
A100, H100) can reuse the same image definition without duplication.

Model downloads have been moved to ``sync.py`` (CPU-only, runs on a cheap
container).  This image **must not** download any models.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import modal

from plugins import comfy_plugins

try:
    from plugins import comfy_plugins_ext
except ImportError:
    comfy_plugins_ext = []

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
if _CONFIG_PATH.exists():
    try:
        _saved = json.loads(_CONFIG_PATH.read_text())
        if _saved.get("custom_nodes"):
            comfy_plugins = list(_saved["custom_nodes"])
        if _saved.get("custom_nodes_ext"):
            comfy_plugins_ext = list(_saved["custom_nodes_ext"])
    except (json.JSONDecodeError, OSError):
        pass

root_dir = Path(__file__).parent


def _build_image() -> modal.Image:
    """Build and return the ComfyUI Docker image (no model downloads)."""
    image = (
        modal.Image.debian_slim(python_version="3.11")
        .add_local_python_source("image", "helpers", "workers", "models", "plugins", copy=True)
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

    # ── Built-in custom nodes (ComfyUI Registry) ────────────────────────
    if comfy_plugins:
        image = image.run_commands("comfy node install " + " ".join(comfy_plugins))

    # ── External custom nodes (from git) ────────────────────────────────
    for plugin in comfy_plugins_ext:
        image = _install_ext_plugin(image, plugin)

    # ── Reverse-proxy fix so workflow save works behind Modal's edge proxy
    image = image.add_local_dir(
        root_dir / "vendor_nodes" / "reverse_proxy_fix",
        "/root/comfy/ComfyUI/custom_nodes/reverse_proxy_fix",
        copy=True,
    )

    return image


def _install_ext_plugin(image: modal.Image, plugin: dict) -> modal.Image:
    """Install one external custom node from git into ComfyUI's custom_nodes.

    Supports optional ``branch``, ``requirements`` (a list of requirement
    files), an ``install`` script (.py), and ``ext_deps`` (a list of extra pip
    packages). User-supplied values are shell-quoted before use.
    """
    nodes_dir = "/root/comfy/ComfyUI/custom_nodes"
    url = plugin["url"]
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    work_dir = f"{nodes_dir}/{shlex.quote(name)}"

    branch = plugin.get("branch", "").strip()
    branch_opt = f"--branch {shlex.quote(branch)} " if branch else ""
    image = image.run_commands(
        f"cd {nodes_dir} && git clone --recurse-submodules --single-branch "
        f"{branch_opt}{shlex.quote(url)}"
    )

    requirements = plugin.get("requirements") or []
    if requirements:
        files = " ".join(f"-r {shlex.quote(f)}" for f in requirements)
        # --no-deps so a node's requirements can't pull a CPU-only torch over
        # the CUDA build; use "ext_deps" below to add back what's needed.
        image = image.run_commands(
            f"cd {work_dir} && uv pip install --no-deps "
            f"--python $(command -v python) --compile-bytecode {files}"
        )

    install = plugin.get("install", "").strip()
    if install:
        if install.endswith(".py"):
            image = image.run_commands(
                f"cd {work_dir} && python {shlex.quote(install)}"
            )
        else:
            print(f"Unsupported installation script: {install}")

    ext_deps = plugin.get("ext_deps") or []
    if ext_deps:
        image = image.uv_pip_install(ext_deps, extra_options="--no-deps")

    return image


# ── Single shared instance ───────────────────────────────────────────────
image = _build_image()
