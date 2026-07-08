"""
Modal Gateway — L4-only version for Modal Starter plan (no credit card).

Deploy::

    modal deploy apps/all_in_one_l4.py
"""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import modal

from image import image
from workers.l4_worker import L4Worker

app = modal.App(name="modal-comfy-gateway-l4", image=image)

# ─── Déclaration du worker L4 comme classe Modal ───


@app.cls(
    gpu=L4Worker.gpu_type,
    volumes={"/cache": modal.Volume.from_name("comfy-models", create_if_missing=True)},
    scaledown_window=L4Worker.scaledown_window,
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
class L4(L4Worker):
    pass


# ─── Modèles de données ───


class GenerateRequest(BaseModel):
    workflow: dict
    gpu: str = "L4"


# ─── Routeur FastAPI public ───

web_app = FastAPI(title="Modal ComfyUI Gateway (L4)")

# CORS — critique pour l'appel depuis l'extension JS
web_app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8188",
        "http://127.0.0.1:8188",
        "https://localhost:8188",
        "https://127.0.0.1:8188",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = os.environ.get("MODAL_GATEWAY_KEY", "dev-key-please-change")

# ─── Session tracking : dernier GPU utilisé par session ───
last_gpu: dict[str, str] = {}


@web_app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """Protège tous les endpoints sauf /health et /gpus"""
    # OPTIONS = preflight CORS → laisser passer sans auth
    if request.method == "OPTIONS":
        return await call_next(request)
    if request.url.path in ("/health", "/gpus", "/docs", "/openapi.json"):
        return await call_next(request)
    key = request.headers.get("X-API-Key")
    if not key or key != API_KEY:
        return JSONResponse(
            {"error": "Unauthorized", "message": "X-API-Key header required"},
            status_code=401,
        )
    return await call_next(request)


WORKER_MAP = {
    "L4": L4,
}


@web_app.post("/generate")
async def generate(req: GenerateRequest):
    """Dispatch un workflow vers le worker GPU, attend le résultat,
    et retourne les images générées en base64 au format attendu par l'extension JS.

    Retourne ``{"images": [{"filename": ..., "data": "<base64>"}], "job_id": ..., "gpu": ...}``.
    """
    worker_cls = WORKER_MAP.get(req.gpu)
    if not worker_cls:
        return JSONResponse(
            {
                "error": f"GPU '{req.gpu}' not supported. Use: {list(WORKER_MAP.keys())}"
            },
            status_code=400,
        )
    # Stocke le GPU utilisé par défaut pour les appels suivants
    last_gpu["default"] = req.gpu
    worker = worker_cls()

    try:
        # ── 1. Envoyer le workflow et récupérer le prompt_id ──────────────
        prompt_result = await worker.prompt.remote.aio(req.workflow)
        prompt_id = prompt_result.get("prompt_id")
        if not prompt_id:
            return JSONResponse(
                {"error": "No prompt_id returned by worker"},
                status_code=500,
            )

        # ── 2. Poller l'historique jusqu'à complétion (timeout: 5 min) ───
        max_attempts = 300  # 300 * 1s = 5 minutes
        outputs = None
        for attempt in range(max_attempts):
            await asyncio.sleep(1)
            try:
                history = await worker.history.remote.aio(prompt_id)
                if isinstance(history, dict) and prompt_id in history:
                    history_data = history[prompt_id]
                    outputs = history_data.get("outputs", {})
                    break
            except Exception:
                # Le job n'est pas encore dans l'historique — on continue
                pass
        else:
            return JSONResponse(
                {"error": "Timeout waiting for generation to complete"},
                status_code=504,
            )

        # ── 3. Récupérer les images de sortie via /view ──────────────────
        images = []
        for node_id, node_outputs in outputs.items():
            for output_key, output_data in node_outputs.items():
                if isinstance(output_data, list):
                    for item in output_data:
                        if isinstance(item, dict) and "filename" in item:
                            filename = item["filename"]
                            subfolder = item.get("subfolder", "")
                            image_type = item.get("type", "output")
                            try:
                                img_data = await worker.view.remote.aio(
                                    filename, subfolder, image_type
                                )
                                if img_data and "data" in img_data:
                                    images.append({
                                        "filename": filename,
                                        "subfolder": subfolder,
                                        "type": image_type,
                                        "data": img_data["data"],
                                    })
                            except Exception as e:
                                print(
                                    f"[Modal Gateway] Error fetching image {filename}: {e}"
                                )

        # ── 4. Retourner le résultat formaté pour l'extension JS ─────────
        return {
            "images": images,
            "job_id": prompt_id,
            "gpu": req.gpu,
        }

    except Exception as e:
        print(f"[Modal Gateway] Error in generate: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@web_app.post("/upload/image")
async def upload_image(request: Request):
    """Proxy l'upload d'image vers le worker GPU cible.

    Le GPU est soit passé en query param (?gpu=L4), soit déduit
    du dernier appel à /generate.
    """
    gpu = request.query_params.get("gpu") or last_gpu.get("default", "L4")
    worker_cls = WORKER_MAP.get(gpu)
    if not worker_cls:
        return JSONResponse({"error": f"Unknown GPU: {gpu}"}, status_code=400)

    worker = worker_cls()
    form = await request.form()
    file = form.get("image")
    if not file:
        return JSONResponse({"error": "No image file"}, status_code=400)

    content = await file.read()
    filename = form.get("filename", file.filename or "input.png")

    result = await worker.upload_image.remote.aio(content)
    return result


@web_app.get("/view")
async def view_image(
    filename: str,
    subfolder: str = "",
    view_type: str = "output",
    gpu: str | None = None,
):
    """Récupère une image générée depuis le worker GPU.

    Le worker ``view()`` retourne un dict avec ``data`` (base64),
    ``content_type`` et ``filename``. On le convertit en réponse
    binaire pour le navigateur.
    """
    import base64

    gpu = gpu or last_gpu.get("default", "L4")
    worker_cls = WORKER_MAP.get(gpu)
    if not worker_cls:
        return JSONResponse({"error": f"Unknown GPU: {gpu}"}, status_code=400)

    worker = worker_cls()
    result = await worker.view.remote.aio(filename, subfolder, view_type)
    # Le worker renvoie { "data": "<base64>", "content_type": "...", "filename": "..." }
    binary = base64.b64decode(result["data"])
    media_type = result.get("content_type", "image/png")
    return Response(content=binary, media_type=media_type)


@web_app.get("/history/{job_id}")
async def get_history(job_id: str, gpu: str | None = None):
    """Statut d'un job via l'historique ComfyUI."""
    gpu = gpu or last_gpu.get("default", "L4")
    worker_cls = WORKER_MAP.get(gpu)
    if not worker_cls:
        return JSONResponse({"error": f"Unknown GPU: {gpu}"}, status_code=400)

    worker = worker_cls()
    result = await worker.history.remote.aio(job_id)
    return result


@web_app.get("/gpus")
async def list_gpus():
    """Liste les GPUs disponibles avec leurs caractéristiques"""
    return [
        {
            "id": "L4",
            "name": "NVIDIA L4",
            "vram": "24 GB",
            "price_per_hour": "$0.80",
        },
    ]


@web_app.get("/health")
async def health():
    return {"status": "ok"}


# Point d'entrée Modal — expose le routeur FastAPI
@app.function(
    secrets=[modal.Secret.from_name("modal-gateway-key")],
)
@modal.concurrent(max_inputs=20)
@modal.asgi_app(label="gateway-l4")
def gateway():
    return web_app
