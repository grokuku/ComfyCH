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

import base64
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

import modal

# ── Emplacements (constantes patchables en test) ─────────────────────────

CUSTOM_NODES_VOLUME_PATH = Path("/custom_nodes")
"""Volume Modal qui porte le code des custom nodes (monté dans les 3 apps)."""

COMFY_CUSTOM_NODES_DIR = Path("/root/comfy/ComfyUI/custom_nodes")
"""Dossier scanné par ComfyUI — une seule fois, à son démarrage."""

COMFY_LOG_PATH = Path("/tmp/comfyui.log")
"""Sortie stdout/stderr de ComfyUI (tracebacks des custom nodes inclus)."""

COMFY_LOG_MAX_BYTES = 2 * 1024 * 1024
"""Fenêtre de lecture de ``logs_tail`` (2 Mio) — un node bavard ne doit pas
faire charger des centaines de Mo en mémoire au worker."""


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
        """Prepare the filesystem from the volumes, then launch ComfyUI.

        Contrainte forte : ComfyUI ne scanne ``custom_nodes/`` qu'UNE SEULE
        FOIS, au moment de son démarrage. Tout symlink créé après le lancement
        est ignoré — le node reste introuvable (``missing_node_type``) alors
        même qu'il est bien présent sur le volume. D'où l'ordre imposé :
        liens/copies d'abord, ``subprocess.Popen`` ensuite.

        This method runs *before* the GPU snapshot is taken, so the snapshot
        captures ComfyUI in a fully-loaded state ready to serve requests.
        """
        # ── Préparer le système de fichiers AVANT de lancer ComfyUI ──
        # ComfyUI ne scanne custom_nodes/ qu'une seule fois à son démarrage :
        # tout symlink créé après le lancement est ignoré (missing_node_type).
        self._link_models_from_manifest()
        self._sync_custom_nodes_from_volume()
        self._restore_uploads_from_volume()
        self._sync_user_settings_from_volume()

        self.proc = self._launch_comfy()
        wait_for_port(8000, timeout=300)

    def _launch_comfy(self):
        """Lance ComfyUI headless en redirigeant stdout/stderr vers COMFY_LOG_PATH.

        Le log permet de diagnostiquer les erreurs d'import des custom nodes
        (tracebacks, dépendances manquantes) qui n'apparaissent nulle part
        ailleurs : sans lui on ne voit que ``missing_node_type`` côté gateway.

        Le fichier est ouvert en append (les redémarrages s'ajoutent au lieu
        de s'écraser) et ``start_new_session=True`` détache le process du
        groupe courant pour qu'il survive à son parent.

        Returns
        -------
        subprocess.Popen
            Le process ComfyUI, à mémoriser sur ``self.proc``.
        """
        COMFY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(COMFY_LOG_PATH, "a") as log_file:
            log_file.write(
                f"\n===== ComfyUI start {datetime.now().isoformat()} =====\n"
            )
            log_file.flush()
            return subprocess.Popen(
                "comfy launch --background -- --listen 0.0.0.0 --port 8000",
                shell=True,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _restore_uploads_from_volume(self):
        """Copy user-uploaded images from the volume to the ComfyUI input dir.

        Les images uploadées via /upload/image sont persistées dans
        /cache/uploads (volume) pour survivre au redémarrage du container —
        sinon une image uploadée sur un worker serait perdue si le container
        s'arrête (scaledown) avant le /generate.
        """
        uploads_dir = Path("/cache/uploads")
        input_dir = Path("/root/comfy/ComfyUI/input")
        if not uploads_dir.is_dir():
            return

        input_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for f in uploads_dir.iterdir():
            if f.is_file() and not f.name.startswith("."):
                dst = input_dir / f.name
                if not dst.exists():
                    shutil.copy2(str(f), str(dst))
                    count += 1

        print(f"[ComfyWorker] Restored {count} upload(s) from volume")

    def _sync_user_settings_from_volume(self):
        """Copy user settings from the mounted volume to the ComfyUI user/ directory."""
        vol_path = Path("/user-settings")
        user_path = Path("/root/comfy/ComfyUI/user")

        if not vol_path.is_dir():
            print("[ComfyWorker] No user-settings volume mounted")
            return

        if not any(vol_path.iterdir()):
            print("[ComfyWorker] User settings volume is empty — skipping")
            return

        import shutil
        user_path.mkdir(parents=True, exist_ok=True)

        # Copy each item from the volume to the user directory
        count = 0
        for item in vol_path.iterdir():
            dest = user_path / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest), dirs_exist_ok=True)
                count += 1
            elif item.is_file():
                shutil.copy2(str(item), str(dest))
                count += 1

        print(f"[ComfyWorker] Synced {count} items from user-settings volume")

    def _link_models_from_manifest(self):
        """Read /cache/model_manifest.json and create symlinks in ComfyUI model dirs."""
        import json
        manifest_path = Path("/cache") / "model_manifest.json"
        if not manifest_path.exists():
            print("[ComfyWorker] No model manifest found — skipping symlink creation")
            return

        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[ComfyWorker] Failed to read manifest: {e}")
            return

        models_root = Path("/root/comfy/ComfyUI/models")
        linked = 0
        for filename, model_dir in manifest.items():
            cache_file = Path("/cache") / filename
            if not cache_file.exists():
                print(f"[ComfyWorker] Model file not in volume: {filename}")
                continue

            target_dir = models_root / model_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / filename

            if target_path.exists() or target_path.is_symlink():
                target_path.unlink()

            target_path.symlink_to(cache_file)
            linked += 1

        print(f"[ComfyWorker] Created {linked} model symlinks from manifest")

    def _sync_custom_nodes_from_volume(self) -> int:
        """Symlink each custom node from /custom_nodes volume into ComfyUI.

        Le code des nodes vit sur le volume (plus installé dans l'image). Au
        démarrage on symlink chaque dossier node dans ComfyUI, on installe ses
        dépendances pip (requirements.txt — généralement déjà présentes via le
        build de l'image, donc idempotent et rapide) et on exécute son
        install.py s'il existe. Les échecs sont loggés en warning sans jamais
        faire échouer le démarrage du worker.

        Returns
        -------
        int
            Nombre de symlinks **créés** par cet appel (les nodes déjà présents
            ne sont pas comptés : ComfyUI les a déjà chargés au démarrage
            courant). Un retour > 0 signifie donc que ComfyUI doit être
            (re)démarré pour voir ces nouveaux nodes.
        """
        vol_path = CUSTOM_NODES_VOLUME_PATH
        comfy_path = COMFY_CUSTOM_NODES_DIR

        if not vol_path.is_dir():
            print(
                "[ComfyWorker] ⚠️ volume /custom_nodes NON MONTÉ — aucun custom node "
                "ne sera chargé. Redéployez (modal deploy apps/all_in_one.py)."
            )
            return 0

        names = sorted(
            p.name for p in vol_path.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        if not names:
            print("[ComfyWorker] /custom_nodes est vide — aucun node à lier")
            return 0

        print(f"[ComfyWorker] Nodes trouvés sur le volume : {names}")

        comfy_path.mkdir(parents=True, exist_ok=True)
        linked = 0
        existing = 0
        for name in names:
            item = vol_path / name
            dst = comfy_path / name
            if dst.exists() or dst.is_symlink():
                existing += 1
            else:
                try:
                    dst.symlink_to(item)
                    linked += 1
                except OSError as e:
                    print(f"[ComfyWorker] ⚠️ Impossible de lier {name} : {e}")
                    continue

            # Install node deps + run install.py (best-effort)
            self._install_node_requirements(item)
            self._run_node_install_script(item)

        print(f"[ComfyWorker] Custom nodes : {linked} lié(s), {existing} déjà présent(s)")
        return linked

    def _install_node_requirements(self, node_dir: Path) -> None:
        """Install a custom node's pip dependencies from its requirements.txt.

        Best-effort : pip est idempotent, donc re-installer des deps déjà
        cuites dans l'image au build est un no-op rapide. Les échecs sont
        loggés, jamais levés.
        """
        req_file = node_dir / "requirements.txt"
        if not req_file.is_file():
            return
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                capture_output=True, timeout=600,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
                print(f"[ComfyWorker] ⚠️ Failed to install deps for {node_dir.name}: {detail[-500:]}")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[ComfyWorker] ⚠️ Error installing deps for {node_dir.name}: {e}")

    def _run_node_install_script(self, node_dir: Path) -> None:
        """Run a custom node's install.py if present (best-effort)."""
        install_py = node_dir / "install.py"
        if not install_py.is_file():
            return
        try:
            result = subprocess.run(
                [sys.executable, str(install_py)],
                capture_output=True, timeout=600,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
                print(f"[ComfyWorker] ⚠️ install.py failed for {node_dir.name}: {detail[-500:]}")
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[ComfyWorker] ⚠️ Error running install.py for {node_dir.name}: {e}")

    @modal.enter(snap=False)
    def start_restore(self) -> None:
        """Restore the filesystem from the volumes, then ensure ComfyUI is up.

        Le snapshot contient ComfyUI déjà lancé. Les liens sont donc créés ici
        AVANT le test du port : si de nouveaux custom nodes ont été
        synchronisés depuis la prise du snapshot, le delta est connu avant de
        décider du redémarrage (ComfyUI ne rescanne ``custom_nodes/`` qu'à son
        démarrage).

        Si ComfyUI ne répond pas (crash avant le snapshot), il est relancé.
        """
        self._link_models_from_manifest()
        self._restore_uploads_from_volume()
        self._sync_user_settings_from_volume()
        new_nodes = self._sync_custom_nodes_from_volume()

        comfy_already_running = False
        try:
            wait_for_port(8000, timeout=30)
            comfy_already_running = True
            print("[ComfyWorker] ComfyUI is running from snapshot")
        except (TimeoutError, OSError):
            print("[ComfyWorker] ComfyUI not responding after snapshot — cold restarting...")

        # ComfyUI n'a scanné custom_nodes/ qu'au moment du snapshot :
        # si de nouveaux nodes ont été synchronisés depuis, il faut le relancer.
        if comfy_already_running and new_nodes > 0:
            print(
                f"[ComfyWorker] {new_nodes} nouveau(x) node(s) détecté(s) — "
                "redémarrage de ComfyUI pour les charger"
            )
            self._shutdown_comfy()
            comfy_already_running = False

        if not comfy_already_running:
            self.proc = self._launch_comfy()
            wait_for_port(8000, timeout=300)
            print("[ComfyWorker] ComfyUI (re)démarré — custom nodes chargés")

        print("[ComfyWorker] App restored from snapshot!")

    def _shutdown_comfy(self, timeout: float = 10) -> None:
        """Arrête proprement le process ComfyUI (terminate, puis kill en secours)."""
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        self.proc = None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                print("[ComfyWorker] ComfyUI ne s'est pas arrêté proprement — kill")
            print("[ComfyWorker] ComfyUI process terminated")
        except (ProcessLookupError, OSError) as e:
            print(f"[ComfyWorker] ComfyUI shutdown: {e}")

    @modal.exit()
    def cleanup(self) -> None:
        """Terminate the ComfyUI process on container shutdown."""
        self._shutdown_comfy()

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
        # ── Extraire extra_data avant le filtrage (il contient les images base64) ──
        extra_data = workflow.pop("extra_data", None) or {}

        # Restaurer les images embarquées (<4 Mo) dans le dossier input de ComfyUI
        for img in extra_data.get("images", []) or []:
            try:
                await self._restore_embedded_image(img)
            except Exception as e:
                print(f"[ComfyWorker] Failed to restore embedded image: {e}")

        # Filter out non-node keys (metadata like "workflow", "extra_data", "version", etc.)
        clean_workflow = {
            k: v for k, v in workflow.items()
            if isinstance(v, dict) and "class_type" in v
        }
        print(f"[ComfyWorker] Workflow: {len(workflow)} keys in, {len(clean_workflow)} kept. Keys filtered out: {set(workflow.keys()) - set(clean_workflow.keys())}")
        print(f"[ComfyWorker] Clean workflow keys: {list(clean_workflow.keys())}")
        print(f"[ComfyWorker] Has SaveImage? {'SaveImage' in str(clean_workflow)}")
        if len(clean_workflow) != len(workflow):
            print(f"[ComfyWorker] Filtered {len(workflow) - len(clean_workflow)} non-node keys from workflow")

        # Passthrough de extra_data SANS la clé "images" (déjà restaurées ci-dessus)
        payload = {"prompt": clean_workflow, "client_id": "modal-gateway"}
        passthrough_extra = {k: v for k, v in extra_data.items() if k != "images"}
        if passthrough_extra:
            payload["extra_data"] = passthrough_extra

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:8000/prompt",
                json=payload,
            )
            if resp.status_code != 200:
                error_text = resp.text
                print(f"[ComfyWorker] /prompt error {resp.status_code}: {error_text}")
                return {"error": f"ComfyUI returned {resp.status_code}: {error_text}"}
            return resp.json()

    async def _restore_embedded_image(self, img: dict) -> None:
        """Restore a base64-embedded image into ComfyUI's input folder.

        The JS extension stores small (<4 Mo) images base64-encoded inside
        ``extra_data.images[]``. ComfyUI's ``/prompt`` cannot read those
        directly, so each image is re-uploaded via ``/upload/image`` before
        submitting the prompt.

        Raises
        ------
        RuntimeError
            If ComfyUI's /upload/image returns a non-200 status.
        """
        imagebase64 = img.get("imagebase64", "")
        if not imagebase64:
            return
        content = base64.b64decode(imagebase64)
        filename = img.get("filename") or img.get("name") or "image.png"
        subfolder = img.get("subfolder", "")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:8000/upload/image",
                files={"image": (filename, content)},
                data={
                    "subfolder": subfolder,
                    "type": "input",
                    "overwrite": "true",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"/upload/image returned {resp.status_code}: {resp.text}"
                )

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
            result = resp.json()

        # ── Persist uploaded file to the shared volume ──────────────────
        # L'image est copiée dans /cache/uploads (volume) pour survivre au
        # scaledown/restart du container. Les workers la restaurent dans leur
        # dossier input au démarrage (_restore_uploads_from_volume).
        try:
            filename = result.get("name", "")
            if filename:
                input_root = Path("/root/comfy/ComfyUI/input")
                src = (input_root / subfolder / filename) if subfolder else (input_root / filename)
                if src.is_file():
                    uploads_dir = Path("/cache/uploads")
                    uploads_dir.mkdir(parents=True, exist_ok=True)
                    dst = uploads_dir / filename
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    shutil.copy2(str(src), str(dst))
                    vol = modal.Volume.from_name("comfy-models")
                    vol.commit()
                    print(f"[ComfyWorker] Upload persisted to volume: {filename}")
        except Exception as e:
            print(f"[ComfyWorker] Upload persist failed (non-fatal): {e}")

        return result

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

    @modal.method()
    async def logs_tail(self, lines: int = 200) -> dict:
        """Retourne les N dernières lignes du log ComfyUI du worker.

        Le log (``COMFY_LOG_PATH``) contient la sortie de ComfyUI : c'est le
        seul endroit où apparaissent les tracebacks d'import des custom nodes,
        les dépendances pip manquantes ou les erreurs réseau d'un node (401,
        timeout…) qui se traduisent côté client par un ``missing_node_type``
        ou un HTTP 500 sans détail.

        Parameters
        ----------
        lines : int, optional
            Nombre de lignes demandées (borné à 2000, minimum 1).

        Returns
        -------
        dict
            ``{"lines": [...], "path": str, "size": int}`` ou, en cas de
            problème, ``{"lines": [], "path": str, "error": str}``.
        """
        try:
            if not COMFY_LOG_PATH.is_file():
                return {
                    "lines": [],
                    "path": str(COMFY_LOG_PATH),
                    "error": "log file not found",
                }
            size = COMFY_LOG_PATH.stat().st_size
            # Lecture bornée : on ne charge que la fin du fichier (une fenêtre
            # suffit largement pour 2000 lignes) et on jette la première ligne
            # si elle est probablement coupée par le seek.
            with open(COMFY_LOG_PATH, "rb") as f:
                if size > COMFY_LOG_MAX_BYTES:
                    f.seek(size - COMFY_LOG_MAX_BYTES)
                    f.readline()
                content = f.read().decode("utf-8", errors="replace")
            tail = content.splitlines()[-max(1, min(int(lines), 2000)):]
            return {"lines": tail, "path": str(COMFY_LOG_PATH), "size": size}
        except (OSError, TypeError, ValueError) as e:
            return {"lines": [], "path": str(COMFY_LOG_PATH), "error": str(e)}
