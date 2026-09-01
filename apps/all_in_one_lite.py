"""
Modal Gateway — Lite version with L4 / L40S / A100 workers only (no H100).

Deploy::

    modal deploy apps/all_in_one_lite.py

Le routeur FastAPI (routes, idempotence, auth) est défini dans
``gateway_router.py`` — seul le mapping GPU -> worker change ici.
"""

from __future__ import annotations

import modal

import auth
from gateway_router import build_router
from image import image
from workers.l4_worker import L4Worker
from workers.l40s_worker import L40SWorker
from workers.a100_worker import A100Worker

app = modal.App(
    name="modal-comfy-gateway-lite",
    image=image,
    secrets=auth.get_modal_secrets(),
)

# ─── Déclaration des 3 workers comme classes Modal ───


@app.cls(
    gpu=L4Worker.gpu_type,
    volumes={
        "/cache": modal.Volume.from_name("comfy-models", create_if_missing=True),
        "/user-settings": modal.Volume.from_name("comfy-user-settings", create_if_missing=True),
        "/custom_nodes": modal.Volume.from_name("comfy-custom-nodes", create_if_missing=True),
    },
    scaledown_window=L4Worker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class L4(L4Worker):
    pass


@app.cls(
    gpu=L40SWorker.gpu_type,
    volumes={
        "/cache": modal.Volume.from_name("comfy-models", create_if_missing=True),
        "/user-settings": modal.Volume.from_name("comfy-user-settings", create_if_missing=True),
        "/custom_nodes": modal.Volume.from_name("comfy-custom-nodes", create_if_missing=True),
    },
    scaledown_window=L40SWorker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class L40S(L40SWorker):
    pass


@app.cls(
    gpu=A100Worker.gpu_type,
    volumes={
        "/cache": modal.Volume.from_name("comfy-models", create_if_missing=True),
        "/user-settings": modal.Volume.from_name("comfy-user-settings", create_if_missing=True),
        "/custom_nodes": modal.Volume.from_name("comfy-custom-nodes", create_if_missing=True),
    },
    scaledown_window=A100Worker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class A100(A100Worker):
    pass


# ─── Routeur FastAPI public ───

WORKER_MAP = {
    "L4": L4,
    "L40S": L40S,
    "A100": A100,
}

web_app = build_router(
    worker_map=WORKER_MAP,
    cors_origins=[
        "http://localhost:8188",
        "http://127.0.0.1:8188",
        "https://localhost:8188",
        "https://127.0.0.1:8188",
    ],
    title="Modal ComfyUI Gateway (Lite)",
)


# Point d'entrée Modal — expose le routeur FastAPI
@app.function()
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label="gateway-lite")
def gateway():
    return web_app
