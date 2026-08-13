"""
Modal Gateway — Single App with 4 GPU workers + public router.

Deploy::

    modal deploy apps/all_in_one.py

Replace l'ancienne commande ``modal deploy comfyui.py``.

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
from workers.h100_worker import H100Worker

app = modal.App(
    name="modal-comfy-gateway",
    image=image,
    secrets=auth.get_modal_secrets(),
)

# ─── Déclaration des 4 workers comme classes Modal ───


@app.cls(
    gpu=L4Worker.gpu_type,
    volumes={
        "/cache": modal.Volume.from_name("comfy-models", create_if_missing=True),
        "/user-settings": modal.Volume.from_name("comfy-user-settings", create_if_missing=True),
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
    },
    scaledown_window=A100Worker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class A100(A100Worker):
    pass


@app.cls(
    gpu=H100Worker.gpu_type,
    volumes={
        "/cache": modal.Volume.from_name("comfy-models", create_if_missing=True),
        "/user-settings": modal.Volume.from_name("comfy-user-settings", create_if_missing=True),
    },
    scaledown_window=H100Worker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class H100(H100Worker):
    pass


# ─── Routeur FastAPI public ───

WORKER_MAP = {
    "L4": L4,
    "L40S": L40S,
    "A100": A100,
    "H100": H100,
}

web_app = build_router(
    worker_map=WORKER_MAP,
    cors_origins=["*"],
    title="Modal ComfyUI Gateway",
)


# Point d'entrée Modal — expose le routeur FastAPI
@app.function()
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label="gateway")
def gateway():
    return web_app
