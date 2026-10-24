"""CPU-only Modal app to sync model files into the ``comfy-models`` Volume.

Supports two sync modes:
1. **Local models** — files detected in the local ComfyUI/models/ directory,
   selected via the Modal Gateway UI, added to the image and copied to the Volume.
2. **HuggingFace models** — files listed in ``models.py``, downloaded via
   ``huggingface_hub``.

Usage::

    modal run sync.py                    # modèles locaux + HF + user settings
    modal run sync.py --sync-custom-nodes # archive des custom nodes seulement
    modal run sync.py --sync-user-only    # user settings seulement (étape 3)
"""

from __future__ import annotations

import json
import os
from datetime import datetime
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

# Nom du manifeste écrit dans le volume comfy-custom-nodes : liste des dossiers
# de nodes réellement extraits (source de vérité des badges « déjà installé »).
NODES_MANIFEST_NAME = ".nodes_manifest.json"

# Point de montage du volume des custom nodes (surchargeable en test).
_CUSTOM_NODES_ROOT = "/custom_nodes"

# Dossiers internes du volume (jamais considérés comme des nodes).
_STAGING_DIR_NAME = ".staging"
_UPLOAD_DIR_NAME = "_upload"

_CACHE_ROOT = Path("/cache")

_MB = 1024 * 1024

# ── Filtrage / garde-fous de l'archive des custom nodes ──────────────────

# Ce script EST dans le dossier du projet ComfyCH (custom_nodes/ComfyCH) : le
# nom est donc déduit du chemin, sans constante « ComfyCH » à maintenir.
PROJECT_ROOT_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))

# Dossiers de custom_nodes/ qui ne sont PAS des nodes : le gateway lui-même et
# le dossier de données ComfyUI.
EXCLUDED_NODE_NAMES = {"user", PROJECT_ROOT_NAME}

# Dossiers techniques ignorés où qu'ils soient dans l'arbre d'un node.
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".cache",
    "dist",
    "build",
    "models",
    "output",
    "input",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

# Extensions de fichiers lourds (modèles, archives, bytecode) : inutiles côté
# Modal et responsables d'archives de plusieurs Go.
EXCLUDE_FILE_EXTS = {
    ".safetensors",
    ".ckpt",
    ".pt",
    ".pth",
    ".bin",
    ".gguf",
    ".onnx",
    ".pkl",
    ".h5",
    ".msgpack",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".pyc",
    ".pyo",
}

# Garde-fous de taille : un fichier trop gros est ignoré, une archive trop
# grosse est refusée avant l'upload (message explicite, pas de crash).
MAX_FILE_MB = 50
MAX_ARCHIVE_MB = 500


# ── Path / archive validation helpers ────────────────────────────────────


def _safe_model_filename(filename: str) -> str:
    """Validate a model *filename* against path traversal and return it.

    The name must be a bare file name: no path separator (``/`` or ``\\``),
    no ``.``/``..`` component, and ``Path(filename).name`` must be the name
    itself. Raises :class:`ValueError` otherwise.
    """
    if not filename:
        raise ValueError("Model filename is empty")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Model filename '{filename}' must not contain a path separator")
    if filename in (".", ".."):
        raise ValueError(f"Model filename '{filename}' is not a valid file name")
    if Path(filename).name != filename:
        raise ValueError(f"Model filename '{filename}' is not a bare file name")
    return filename


def _model_dest(filename: str) -> Path:
    """Return the validated volume path (``/cache/<filename>``) for *filename*.

    Validates the name with :func:`_safe_model_filename` and double-checks that
    the resolved path stays under the volume root (belt + braces).
    """
    _safe_model_filename(filename)
    dest = _CACHE_ROOT / filename
    if not dest.resolve().is_relative_to(_CACHE_ROOT.resolve()):
        raise ValueError(f"Model filename '{filename}' escapes the volume root")
    return dest


def _validate_tar_members(members, staging_dir: str) -> None:
    """Validate tar members before extraction into *staging_dir*.

    Rejects, in order:

    1. links — symlink/hardlink members can have an innocuous ``name`` while
       ``linkname`` points outside the staging directory (extraction escape);
    2. absolute names and ``..`` components;
    3. any member whose resolved path would land outside *staging_dir*.

    Raises :class:`ValueError` on the first offending member.
    """
    staging_root = os.path.normpath(staging_dir)
    for member in members:
        if member.issym() or member.islnk():
            raise ValueError(f"Tar member '{member.name}' is a link — rejected")
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise ValueError(f"Tar member '{member.name}' contains unsafe path")
        member_path = os.path.normpath(os.path.join(staging_root, member.name))
        if not member_path.startswith(staging_root + os.sep):
            raise ValueError(
                f"Tar member '{member.name}' would extract outside target directory"
            )


# ── Sélection des nodes / filtrage des fichiers ──────────────────────────


def _node_skip_reason(name: str) -> str | None:
    """Raison d'exclure le dossier *name* de custom_nodes/, ou ``None``.

    Le gateway lui-même (``PROJECT_ROOT_NAME``) et ``user`` ne sont pas des
    nodes ; les dossiers cachés et techniques (``__pycache__``, ``.git``…) non
    plus.
    """
    if name == PROJECT_ROOT_NAME:
        return "gateway ComfyCH (code déjà présent dans l'image)"
    if name in EXCLUDED_NODE_NAMES:
        return "données ComfyUI, pas un custom node"
    if name.startswith("."):
        return "dossier caché"
    if name in EXCLUDE_DIRS:
        return "dossier technique"
    return None


def _select_node_dirs(custom_nodes_dir) -> tuple[list, list]:
    """Sépare les nodes retenus des dossiers exclus.

    Retourne ``(node_dirs, skipped)`` où *skipped* est une liste de couples
    ``(nom, raison)``.
    """
    kept: list[Path] = []
    skipped: list[tuple[str, str]] = []
    for entry in sorted(Path(custom_nodes_dir).iterdir()):
        if not entry.is_dir():
            continue
        reason = _node_skip_reason(entry.name)
        if reason:
            skipped.append((entry.name, reason))
        else:
            kept.append(entry)
    return kept, skipped


def _is_excluded_dir(name: str) -> bool:
    """True si le dossier *name* doit être ignoré (technique ou caché)."""
    return name in EXCLUDE_DIRS or name.startswith(".")


def _is_excluded_file(name: str) -> bool:
    """True si le fichier *name* doit être ignoré (caché ou extension lourde)."""
    if name.startswith("."):
        return True
    return Path(name).suffix.lower() in EXCLUDE_FILE_EXTS


def _iter_node_files(node_dir):
    """Énumère ``(dossier, [fichiers retenus])`` pour *node_dir*.

    Les dossiers exclus sont élagués (``os.walk`` ne descend pas dedans) et les
    liens symboliques ignorés : ils seraient de toute façon rejetés à
    l'extraction par :func:`_validate_tar_members`.
    """
    node_dir = Path(node_dir)
    for root, dirs, filenames in os.walk(node_dir):
        dirs[:] = sorted(
            d for d in dirs
            if not _is_excluded_dir(d) and not os.path.islink(os.path.join(root, d))
        )
        kept = []
        for name in sorted(filenames):
            if _is_excluded_file(name):
                continue
            file_path = Path(root) / name
            if file_path.is_symlink():
                continue
            kept.append(file_path)
        yield Path(root), kept


def _node_dir_size(node_dir) -> int:
    """Taille (octets) des fichiers de *node_dir* réellement archivés."""
    total = 0
    for _root, files in _iter_node_files(node_dir):
        for file_path in files:
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _node_size_report(node_dirs) -> list[tuple[str, int]]:
    """Tailles des nodes retenus, triées du plus lourd au plus léger."""
    sizes = [(Path(d).name, _node_dir_size(d)) for d in node_dirs]
    return sorted(sizes, key=lambda item: item[1], reverse=True)


def _build_custom_nodes_archive(
    node_dirs, archive_path, *, max_file_mb: int = MAX_FILE_MB
) -> dict:
    """Écrit un tar.gz des *node_dirs* dans *archive_path* (sur disque).

    Les mêmes exclusions que :func:`_node_dir_size` s'appliquent, plus le
    garde-fou par fichier (``max_file_mb``) dont les rejets sont comptés.

    Retourne ``{"files": n, "skipped_large": n, "size": octets}``.
    """
    import tarfile

    limit = max_file_mb * _MB
    files_count = 0
    skipped_large = 0

    with tarfile.open(archive_path, mode="w:gz") as tar:
        for node_dir in node_dirs:
            node_dir = Path(node_dir)
            # Entrée racine du node, même si le dossier est vide.
            tar.add(str(node_dir), arcname=node_dir.name, recursive=False)
            for root, files in _iter_node_files(node_dir):
                rel_root = root.relative_to(node_dir)
                if rel_root != Path("."):
                    tar.add(
                        str(root),
                        arcname=str(Path(node_dir.name) / rel_root),
                        recursive=False,
                    )
                for file_path in files:
                    try:
                        size = file_path.stat().st_size
                    except OSError:
                        continue
                    if size > limit:
                        skipped_large += 1
                        continue
                    arcname = str(Path(node_dir.name) / rel_root / file_path.name)
                    tar.add(str(file_path), arcname=arcname)
                    files_count += 1

    return {
        "files": files_count,
        "skipped_large": skipped_large,
        "size": os.path.getsize(archive_path),
    }


def _format_mb(size_bytes: float) -> str:
    """Formate une taille en MB (une décimale sous 10 MB, entier au-dessus)."""
    mb = size_bytes / _MB
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def _archive_too_big_message(
    size_bytes: int, node_sizes=(), *, max_archive_mb: int = MAX_ARCHIVE_MB
) -> str | None:
    """Message d'abandon si *size_bytes* dépasse la limite, sinon ``None``.

    Ne lève jamais : l'appelant imprime le message et s'arrête proprement.
    """
    if size_bytes <= max_archive_mb * _MB:
        return None

    lines = [
        f"❌ Archive trop volumineuse : {_format_mb(size_bytes)} "
        f"(maximum {max_archive_mb} MB)",
        "   Vérifiez que vos custom nodes ne contiennent pas de modèles ou de caches.",
    ]
    ranked = list(node_sizes)[:5]
    if ranked:
        lines.append("   Nodes les plus lourds :")
        for name, node_bytes in ranked:
            lines.append(f"     - {name} : {_format_mb(node_bytes)}")
        lines.append(
            f"   Total des nodes : {_format_mb(sum(b for _, b in node_sizes))}"
        )
    return "\n".join(lines)


def _format_node_list(names, limit: int = 10) -> str:
    """Résumé lisible des nodes (« a, b, … et N autre(s) »)."""
    names = list(names)
    shown = names[:limit]
    text = ", ".join(shown)
    remaining = len(names) - len(shown)
    if remaining > 0:
        text += f" … et {remaining} autre(s)"
    return text

# ── Read config.json for local models to sync ────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _SCRIPT_DIR / "config.json"


def _update_local_config(updates: dict) -> None:
    """Merge *updates* into the local ``config.json`` (best-effort).

    Called from the local entrypoint (``modal run``) so the ComfyUI backend can
    serve facts about the remote volumes without a Modal round-trip.
    A read-only/missing ``config.json`` must never break a successful sync, hence
    the broad exception guard.
    """
    try:
        current = {}
        if _CONFIG_PATH.exists():
            try:
                loaded = json.loads(_CONFIG_PATH.read_text())
                if isinstance(loaded, dict):
                    current = loaded
            except (json.JSONDecodeError, OSError):
                current = {}
        current.update(updates)
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(current, indent=2))
    except OSError as e:  # noqa: BLE001 — le sync lui-même a réussi
        print(f"⚠️ Impossible d'écrire {_CONFIG_PATH}: {e}")


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

    dst = _model_dest(filename)

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
    dst = _model_dest(filename)
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
    dest = _model_dest(filename)

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
    _model_dest(filename)  # validate the name before touching the volume
    _link_and_update_manifest(filename, model_dir)
    vol.commit()
    return {"ok": True, "filename": filename, "linked": True}


# ── Volume custom nodes : extraction / swap / manifeste ─────────────────


def _extract_archive_to_staging(archive_path: str, staging_dir: str) -> None:
    """Valide puis extrait l'archive tar.gz *archive_path* dans *staging_dir*.

    Les liens, chemins absolus, ``..`` et échappements sont rejetés par
    :func:`_validate_tar_members` AVANT toute écriture.
    """
    import shutil as _shutil
    import tarfile

    if os.path.exists(staging_dir):
        _shutil.rmtree(staging_dir)
    os.makedirs(staging_dir)

    with tarfile.open(archive_path, mode="r:gz") as tar:
        _validate_tar_members(tar.getmembers(), staging_dir)
        tar.extractall(path=staging_dir)


def _swap_volume_content(base_path: str, staging_dir: str) -> list[str]:
    """Remplace le contenu de *base_path* par celui de *staging_dir*.

    Les dossiers internes (``.staging``, ``_upload``) sont préservés. Retourne
    la liste triée des nodes (dossiers) présents après le swap.
    """
    import shutil as _shutil

    internal = {_STAGING_DIR_NAME, _UPLOAD_DIR_NAME}

    # Suppression de l'ancien contenu (sauf dossiers internes)
    for item in os.listdir(base_path):
        if item in internal:
            continue
        item_path = os.path.join(base_path, item)
        if os.path.isdir(item_path) and not os.path.islink(item_path):
            _shutil.rmtree(item_path)
        else:
            os.remove(item_path)

    for item in os.listdir(staging_dir):
        _shutil.move(os.path.join(staging_dir, item), os.path.join(base_path, item))

    _shutil.rmtree(staging_dir)

    return sorted(
        d for d in os.listdir(base_path)
        if d not in internal
        and not d.startswith(".")
        and os.path.isdir(os.path.join(base_path, d))
    )


def _write_nodes_manifest(base_path: str, nodes: list[str]) -> None:
    """Écrit ``.nodes_manifest.json`` : nodes RÉELLEMENT présents sur le volume.

    Source de vérité des badges « déjà installé » dans l'UI (le volume, pas la
    sélection). À écrire AVANT le ``commit()`` pour être persisté avec le swap.
    """
    manifest_path = os.path.join(base_path, NODES_MANIFEST_NAME)
    with open(manifest_path, "w") as f:
        json.dump({"nodes": nodes, "synced_at": datetime.now().isoformat()}, f, indent=2)


@app.function(
    cpu=2,
    memory=4096,
    volumes={"/custom_nodes": custom_nodes_vol},
    timeout=1800,
)
def extract_custom_nodes_from_volume(archive_name: str) -> dict:
    """Extrait sur le volume une archive tar.gz déjà uploadée (streaming).

    L'archive est lue depuis ``/custom_nodes/_upload/<archive_name>`` : plus
    aucun contenu binaire ne transite par un argument RPC (l'ancien transport
    ``bytes`` faisait sérialiser plusieurs Go côté client → OOM/SIGKILL).

    Même pipeline qu'avant : validation des membres, extraction en
    ``.staging``, swap atomique, manifeste, ``commit()``. L'archive temporaire
    et le dossier ``_upload`` sont supprimés AVANT le commit (sinon la
    suppression ne serait pas persistée).
    """
    import shutil as _shutil

    name = str(archive_name or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"Nom d'archive invalide : {archive_name!r}")

    base_path = _CUSTOM_NODES_ROOT
    upload_dir = os.path.join(base_path, _UPLOAD_DIR_NAME)
    archive_path = os.path.join(upload_dir, name)
    staging_dir = os.path.join(base_path, _STAGING_DIR_NAME)

    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Archive introuvable sur le volume : {archive_path}")

    _extract_archive_to_staging(archive_path, staging_dir)
    nodes = _swap_volume_content(base_path, staging_dir)

    # Le swap préserve ``_upload`` : on le supprime explicitement (archive
    # consommée et éventuels restes d'un run précédent).
    try:
        if os.path.isdir(upload_dir):
            _shutil.rmtree(upload_dir)
    except OSError as e:  # noqa: BLE001 — le sync lui-même a réussi
        print(f"⚠️ Nettoyage de {upload_dir} impossible : {e}")

    _write_nodes_manifest(base_path, nodes)
    custom_nodes_vol.commit()

    return {"status": "ok", "nodes": nodes}


# ── Remote function: read the custom nodes manifest from the volume ─────


@app.function(
    cpu=1,
    memory=512,
    volumes={"/custom_nodes": custom_nodes_vol},
    timeout=60,
)
def get_custom_nodes_manifest() -> dict:
    """Read ``/custom_nodes/.nodes_manifest.json`` from the volume.

    Returns ``{"nodes": [...], "synced_at": <iso>}``, or an empty list and
    ``None`` when the manifest is absent (volume never synced by this code).
    """
    custom_nodes_vol.reload()
    manifest_path = os.path.join(_CUSTOM_NODES_ROOT, NODES_MANIFEST_NAME)
    if not os.path.isfile(manifest_path):
        return {"nodes": [], "synced_at": None}
    try:
        with open(manifest_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"nodes": [], "synced_at": None}
    nodes = data.get("nodes") if isinstance(data, dict) else None
    return {
        "nodes": nodes if isinstance(nodes, list) else [],
        "synced_at": data.get("synced_at") if isinstance(data, dict) else None,
    }


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
def main(sync_custom_nodes: bool = False, sync_user_only: bool = False) -> None:
    """Local entrypoint — uploads local models, then syncs HuggingFace models.

    With ``--sync-custom-nodes``, only the custom nodes archive is synced to
    the dedicated ``comfy-custom-nodes`` volume (other steps are skipped).
    With ``--sync-user-only``, only step 3 (user settings) runs.
    """

    def _sync_user_settings() -> None:
        """Step 3 — upload the local ``user/`` directory to its Volume."""
        print(f"\n{'='*60}")
        print(f"👤 Syncing user settings to volume")
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
            # Un SEUL batch_upload pour tous les fichiers : chaque
            # ``with user_vol.batch_upload()`` ouvre une session réseau, la
            # rouvrir par fichier rendait l'upload très lent.
            try:
                with user_vol.batch_upload() as batch:
                    for f in files_to_upload:
                        rel_path = f.relative_to(user_dir)
                        try:
                            batch.put_file(str(f), f"/{rel_path}")
                            uploaded += 1
                        except FileExistsError:
                            skipped += 1
            except FileExistsError:
                # Batch refusé d'emblée (ex. conflit signalé hors put_file)
                skipped += 1

            print(f"✅ User settings synced to volume ({uploaded} uploaded, {skipped} already present)")
        else:
            print(f"❌ Local user/ directory not found at {user_dir}")

    # ── User settings only mode ────────────────────────────────────────
    if sync_user_only:
        _sync_user_settings()
        print(f"\n{'='*60}")
        print("✅ Sync terminée !")
        print(f"{'='*60}")
        return

    # ── Custom nodes only mode ─────────────────────────────────────────
    if sync_custom_nodes:
        import tempfile

        # NOTE : tous les ``print`` de cette branche utilisent ``flush=True``.
        # Sans flush, la sortie est bufferisée quand stdout n'est pas un TTY
        # (cas de ``modal run`` lancé par le gateway) : l'UI resterait muette
        # pendant tout le packaging, qui est la partie la plus longue.
        print(f"\n{'='*60}", flush=True)
        print("🧩 Syncing custom nodes to volume", flush=True)
        print(f"{'='*60}\n", flush=True)

        custom_nodes_dir = _SCRIPT_DIR.parent  # ComfyUI/custom_nodes
        if not custom_nodes_dir.is_dir():
            print(f"❌ Custom nodes directory not found: {custom_nodes_dir}", flush=True)
            return

        print(f"📦 Packaging des custom nodes depuis {custom_nodes_dir}…", flush=True)

        # Sélection : le gateway lui-même et le dossier de données ``user`` ne
        # sont pas des nodes ; les dossiers techniques/cachés non plus.
        node_dirs, skipped = _select_node_dirs(custom_nodes_dir)
        for name, reason in skipped:
            print(f"  ⊘ {name} exclu ({reason})", flush=True)
        print(f"  → {len(node_dirs)} node(s) inclus", flush=True)
        if not node_dirs:
            print("❌ Aucun custom node à synchroniser", flush=True)
            return

        # Tailles calculées AVANT le packaging : sert au résumé et au diagnostic
        # en cas d'archive trop volumineuse.
        node_sizes = _node_size_report(node_dirs)
        total = _format_mb(sum(size for _, size in node_sizes))
        print(f"  → {total} de fichiers retenus (modèles, caches et binaires exclus)", flush=True)

        # Archive écrite sur disque (jamais en RAM : plusieurs centaines de Mo).
        tmp_archive = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        tmp_path = tmp_archive.name
        tmp_archive.close()
        archive_name = f"custom_nodes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.tar.gz"

        try:
            stats = _build_custom_nodes_archive(node_dirs, tmp_path)
            size_bytes = stats["size"]

            if stats["skipped_large"]:
                print(
                    f"⚠️ {stats['skipped_large']} fichier(s) > {MAX_FILE_MB} MB ignoré(s)",
                    flush=True,
                )
            print(f"✅ Archive créée : {_format_mb(size_bytes)}", flush=True)

            too_big = _archive_too_big_message(size_bytes, node_sizes)
            if too_big:
                print(too_big, flush=True)
                return

            names = [Path(d).name for d in node_dirs]
            print(
                f"📦 Archive : {_format_mb(size_bytes)} — {len(names)} node(s)\n"
                f"   {_format_node_list(names)}",
                flush=True,
            )

            # Upload streaming : ``batch_upload`` envoie le fichier octet par
            # octet vers le volume. L'ancien transport passait l'archive
            # entière en argument RPC (sérialisation de plusieurs Go → OOM /
            # SIGKILL côté client).
            print("☁️  Upload de l'archive vers le volume…", flush=True)
            with custom_nodes_vol.batch_upload() as batch:
                batch.put_file(tmp_path, f"/{_UPLOAD_DIR_NAME}/{archive_name}")

            print("🔧 Extraction sur Modal…", flush=True)
            result = extract_custom_nodes_from_volume.remote(archive_name)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        nodes = result.get("nodes", [])
        print(f"✅ Sync terminé : {len(nodes)} node(s) sur le volume", flush=True)
        for n in nodes:
            print(f"  📁 {n}", flush=True)

        # Le volume est la source de vérité : on en garde une copie locale dans
        # config.json pour que le backend ComfyUI puisse répondre instantanément
        # à GET /api/modal/plugins/installed (sans appel Modal).
        if result.get("status") == "ok":
            _update_local_config({
                "synced_custom_nodes": nodes,
                "last_sync_custom_nodes": datetime.now().isoformat(),
            })
            print(f"📝 Manifeste local mis à jour ({len(nodes)} node(s))", flush=True)
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
    _sync_user_settings()

    print(f"\n{'='*60}")
    print("✅ Sync terminée !")
    print(f"{'='*60}")