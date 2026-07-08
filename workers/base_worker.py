"""Headless ComfyUI worker — exposes the ComfyUI REST API via Modal web endpoints.

La classe de base ``ComfyWorker`` définit les class-variables ``gpu_type``
et ``scaledown_window`` que les sous-classes spécialisées surchargent.

Usage (Phase 4 — ``apps/all_in_one.py``)
----------------------------------------
::

    from workers.l4_worker import L4Worker
    from image import image

    app = modal.App("my-comfy-app", image=image)
    vol = modal.Volume.from_name("comfy-models", create_if_missing=True)

    @app.cls(
        max_containers=1,
        gpu=L4Worker.gpu_type,            # ← variable de classe
        volumes={"/cache": vol},
        scaledown_window=L4Worker.scaledown_window,  # ← variable de classe
        enable_memory_snapshot=True,
        experimental_options={"enable_gpu_snapshot": True},
    )
    @modal.concurrent(max_inputs=10)
    class MyL4Worker(L4Worker):
        pass
"""

from __future__ import annotations

import socket
import subprocess
import time

import httpx

import modal

# ── Helpers ──────────────────────────────────────────────────────────────


def wait_for_port(port: int, timeout: int = 60) -> None:
    """Block until the port on ``127.0.0.1`` is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return  # port is open — ComfyUI is ready
        except OSError:
            time.sleep(0.5)
    raise TimeoutError(f"ComfyUI never became ready on port {port}")


# ── Base worker ─────────────────────────────────────────────────────────


class ComfyWorker:
    """Headless ComfyUI worker — classe de base.

    Les sous-classes surchargent ``gpu_type`` et ``scaledown_window``
    pour choisir le GPU et le délai de mise à l'échelle.

    Le décorateur ``@app.cls(gpu=cls.gpu_type, ...)`` est appliqué
    dans l'app qui importe la classe (ex. ``apps/all_in_one.py``).

    Starts ComfyUI in the background on container start (with GPU snapshot
    support) and exposes its REST API via ``@modal.web_endpoint`` methods
    that proxy requests to ``http://127.0.0.1:8000``.
    """

    # ── Paramètres GPU (surchargés par les sous-classes) ──────────────
    gpu_type: str = "L4"
    """Type de GPU Modal (``L4``, ``L40S``, ``A100-80GB``, ``H100``)."""

    scaledown_window: int = 60
    """Nombre de secondes d'inactivité avant l'arrêt du container."""

    # ── Lifecycle ───────────────────────────────────────────────────────

    @modal.enter(snap=True)
    def start_checkpoint(self) -> None:
        """Launch ComfyUI headless and wait for it to be ready.

        This method runs *before* the GPU snapshot is taken, so the snapshot
        captures ComfyUI in a fully-loaded state ready to serve requests.
        """
        self.proc = subprocess.Popen(
            "comfy launch --background -- --listen 0.0.0.0 --port 8000",
            shell=True,
        )
        wait_for_port(8000, timeout=300)

    @modal.enter(snap=False)
    def start_restore(self) -> None:
        """Wait for ComfyUI to be ready after restoring from a snapshot."""
        wait_for_port(8000, timeout=30)
        print("[ComfyWorker] App restored from snapshot!")

    @modal.exit()
    def cleanup(self) -> None:
        """Terminate the ComfyUI process on container shutdown."""
        proc = getattr(self, "proc", None)
        if proc is not None:
            try:
                proc.terminate()
                print("[ComfyWorker] ComfyUI process terminated")
            except (ProcessLookupError, OSError):
                pass

    # ── Proxy methods (called by the router via .remote()) ───────────────

    @modal.method()
    async def prompt(self, workflow: dict) -> dict:
        """Proxy to ComfyUI's ``POST /prompt``.

        Accepts a workflow JSON object and returns the prompt result
        (including the ``prompt_id``).  Called by the router via ``.remote()``.

        Non-node keys (metadata like ``"workflow"``, ``"extra_data"``,
        ``"version"``, etc.) are filtered out so that ComfyUI does not
        try to parse them as nodes and fail with a ``missing_node_type``
        error.
        """
        # Filter out non-node keys (metadata like "workflow", "extra_data", "version", etc.)
        clean_workflow = {
            k: v for k, v in workflow.items()
            if isinstance(v, dict) and "class_type" in v
        }
        if len(clean_workflow) != len(workflow):
            print(f"[ComfyWorker] Filtered {len(workflow) - len(clean_workflow)} non-node keys from workflow")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:8000/prompt",
                json={"prompt": clean_workflow, "client_id": "modal-gateway"},
            )
            if resp.status_code != 200:
                error_text = resp.text
                print(f"[ComfyWorker] /prompt error {resp.status_code}: {error_text}")
                return {"error": f"ComfyUI returned {resp.status_code}: {error_text}"}
            return resp.json()

    @modal.method()
    async def history(self, job_id: str) -> dict:
        """Proxy to ComfyUI's ``GET /history/{job_id}``.

        Query parameter ``job_id`` is the prompt ID returned by ``/prompt``.
        Returns the execution history for that prompt.  Called via ``.remote()``.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:8000/history/{job_id}",
            )
            if resp.status_code != 200:
                error_text = resp.text
                print(f"[ComfyWorker] /history error {resp.status_code}: {error_text}")
                return {"error": f"ComfyUI returned {resp.status_code}: {error_text}"}
            return resp.json()

    @modal.method()
    async def upload_image(self, image: bytes, subfolder: str = "", image_type: str = "input") -> dict:
        """Proxy to ComfyUI's ``POST /upload/image``.

        Accepts raw image bytes and optional metadata.  Returns the uploaded
        image reference that can be used in a workflow.  Called via ``.remote()``.

        Parameters
        ----------
        image : bytes
            Raw image file content (PNG, JPG, etc.).
        subfolder : str, optional
            Subfolder within the ComfyUI input directory.
        image_type : str, optional
            Type of image folder (``"input"``, ``"output"``, ``"temp"``).
            Defaults to ``"input"``.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:8000/upload/image",
                files={"image": image},
                data={
                    "subfolder": subfolder,
                    "type": image_type,
                },
            )
            if resp.status_code != 200:
                error_text = resp.text
                print(f"[ComfyWorker] /upload/image error {resp.status_code}: {error_text}")
                return {"error": f"ComfyUI returned {resp.status_code}: {error_text}"}
            return resp.json()

    @modal.method()
    async def view(self, filename: str, subfolder: str = "", view_type: str = "output") -> dict:
        """Proxy to ComfyUI's ``GET /view``.

        Retrieves a generated image or file from the ComfyUI output/input
        directories.  Called via ``.remote()``.

        Parameters
        ----------
        filename : str
            Name of the file to retrieve.
        subfolder : str, optional
            Subfolder within the type directory.
        view_type : str, optional
            Type of folder (``"output"``, ``"input"``, ``"temp"``).
            Defaults to ``"output"``.

        Returns
        -------
        dict
            A dict with ``"data"`` (base64-encoded file content),
            ``"content_type"``, and ``"filename"`` so the client can
            reconstruct the binary response.
        """
        import base64

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "http://127.0.0.1:8000/view",
                params={
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": view_type,
                },
            )
            if resp.status_code != 200:
                error_text = resp.text
                print(f"[ComfyWorker] /view error {resp.status_code}: {error_text}")
                return {"data": "", "content_type": "", "filename": filename, "error": f"ComfyUI /view returned {resp.status_code}: {error_text}"}
            return {
                "data": base64.b64encode(resp.content).decode("ascii"),
                "content_type": resp.headers.get("content-type", "application/octet-stream"),
                "filename": filename,
            }
