"""[DEPRECATED — replaced by ``apps/all_in_one.py``]

ComfyUI on Modal — headless API worker.

This module is **no longer deployed directly**.  Use ``apps/all_in_one.py``
which provides the same functionality plus multi-GPU support, a public
FastAPI router with API-key authentication, and CORS headers.

New deploy command::

    modal deploy apps/all_in_one.py

Legacy deploy (still works, not maintained)::

    modal deploy comfyui.py

Sync models (CPU-only, run separately)::

    modal run sync.py
"""

from __future__ import annotations

import os

import modal

from image import image
from workers.base_worker import ComfyWorker

# ── Configuration ────────────────────────────────────────────────────────

GPU_TYPE = os.getenv("MODAL_GPU", "L4")

# Volume harmonisé avec sync.py — tous les workers utilisent "comfy-models"
vol = modal.Volume.from_name("comfy-models", create_if_missing=True)

print(
    "[INFO] Model sync is handled by ``modal run sync.py`` on a CPU container. "
    "Models are stored in the ``comfy-models`` volume."
)

# ── App & worker registration ───────────────────────────────────────────

app = modal.App(name="modal-comfyui", image=image)


@app.cls(
    max_containers=1,
    gpu=GPU_TYPE,
    volumes={"/cache": vol},
    scaledown_window=60,  # idle 1 minute before shutdown
    enable_memory_snapshot=True,
    experimental_options={"enable_gpu_snapshot": True},
)
@modal.concurrent(max_inputs=10)
class ComfyUI(ComfyWorker):
    """Headless ComfyUI worker with GPU snapshot.

    Inherits all lifecycle hooks and web endpoints from ``ComfyWorker``.
    The ``@app.cls(...)`` decorator wires up the Modal infrastructure
    (GPU, volume, snapshot), while ``@modal.concurrent`` limits concurrent
    requests to 10.
    """
    pass
