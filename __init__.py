"""
Modal Gateway — Backend Configuration UI
Ajoute des routes API au ComfyUI PromptServer pour :
- Lancer la sync des modèles (modal run sync.py)
- Lancer le déploiement (modal deploy apps/all_in_one.py)
- Configurer l'URL de l'API et la clé d'authentification
- Vérifier le statut de Modal
"""
from __future__ import annotations

import asyncio
import ast
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

# La racine du projet est le dossier de ce __init__.py
# custom_nodes/modal_gateway/ = PROJECT_ROOT
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

# Dossier des fichiers web (JS, CSS) pour ComfyUI
# Requis depuis les versions récentes de ComfyUI (2024+)
WEB_DIRECTORY = "./web"

# ─── Gestion de la configuration ───

DEFAULT_CONFIG = {
    "api_url": "",
    "api_key": "",
    "last_sync": None,
    "last_deploy": None,
    "last_user_sync": None,
    "custom_nodes": [],
    "custom_nodes_ext": [],
    "custom_nodes_local": [],
    "models_to_sync": [],
    "local_output_dir": "",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_CONFIG)


_CONFIG_LOCK = threading.Lock()


def save_config(config: dict) -> None:
    with _CONFIG_LOCK:
        current = load_config()
        current.update(config)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(current, indent=2))


# ─── Helpers ───

LOG_DIR = Path(__file__).resolve().parent / "logs"


def _log_path() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / "last_operation.log"


def _status_path() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / "last_operation.status"


_OP_LOCK = asyncio.Lock()  # sérialise les opérations Modal (sync/deploy/sync-user)


async def _run_async(operation: str, cmd: list[str]):
    """Exécute une commande modal et écrit les logs + status.

    Sérialisée par _OP_LOCK : une seule opération à la fois — les opérations
    partagent les mêmes fichiers de logs et le même volume.
    """
    async with _OP_LOCK:
        await _run_async_inner(operation, cmd)


async def _run_async_inner(operation: str, cmd: list[str]):
    """Exécute réellement la commande (appelée sous _OP_LOCK)."""
    log_path = _log_path()
    status_path = _status_path()

    # Nettoyer les logs précédents
    log_path.write_text("")
    if status_path.exists():
        status_path.unlink()

    project_dir = str(PROJECT_ROOT)

    # S'assurer que le CLI modal est dans le PATH
    env = os.environ.copy()
    env["PATH"] = f"{os.path.dirname(sys.executable)}:{env.get('PATH', '')}"

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=project_dir,
            env=env,
        )
    except (OSError, ValueError) as e:
        # modal absent ou commande invalide → logguer et marquer l'échec
        # (sinon le SSE attendrait un status qui ne viendrait jamais)
        with open(log_path, "a") as f:
            f.write(f"ERROR: impossible de lancer la commande: {e}\n")
        status_path.write_text("1")
        return

    # Lire et écrire les logs en temps réel
    async for line in process.stdout:
        decoded = line.decode().rstrip()
        with open(log_path, "a") as f:
            f.write(decoded + "\n")

    await process.wait()

    # Écrire le code de retour
    status_path.write_text(str(process.returncode))

    # Mettre à jour last_sync ou last_deploy dans la config
    config = load_config()
    now = datetime.now().isoformat()
    if process.returncode == 0:
        if operation == "sync":
            config["last_sync"] = now
        elif operation == "deploy":
            config["last_deploy"] = now
        elif operation == "sync_user":
            config["last_user_sync"] = now
        save_config(config)


def _check_modal_installed() -> bool:
    try:
        result = subprocess.run(
            ["modal", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_modal_auth() -> bool:
    try:
        result = subprocess.run(
            ["modal", "profile", "current"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_volume_exists() -> bool:
    try:
        result = subprocess.run(
            ["modal", "volume", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return "comfy-models" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _check_secret_exists() -> bool:
    """Check that the gateway API-key Secret exists on the Modal account."""
    try:
        result = subprocess.run(
            ["modal", "secret", "list"],
            capture_output=True, text=True, timeout=10,
        )
        return "comfy-gateway-secret" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _detect_custom_nodes() -> list[dict]:
    """Scan the local ComfyUI custom_nodes/ directory for installed nodes."""
    custom_nodes_dir = PROJECT_ROOT.parent
    results: list[dict] = []

    if not custom_nodes_dir.is_dir():
        print(f"[Modal Gateway] custom_nodes dir not found: {custom_nodes_dir}")
        return results

    for entry in sorted(custom_nodes_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(".") or name.startswith("__"):
            continue
        if name == PROJECT_ROOT.name:
            continue

        git_config_path = entry / ".git" / "config"
        git_url = None
        has_git = False

        if git_config_path.is_file():
            has_git = True
            try:
                content = git_config_path.read_text(errors="replace")
                in_origin = False
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("[remote"):
                        in_origin = '"origin"' in stripped
                        continue
                    if in_origin and stripped.startswith("url"):
                        parts = stripped.split("=", 1)
                        if len(parts) == 2:
                            git_url = parts[1].strip()
                        break
            except OSError:
                pass

        results.append({
            "name": name,
            "git_url": git_url,
            "has_git": has_git,
        })

    return results


def _detect_local_models() -> list[dict]:
    """Scan the local ComfyUI models/ directory for installed model files.

    Looks for .safetensors, .ckpt, .pt, .bin, .gguf, .pth files in each
    subdirectory of ComfyUI/models/.

    Returns a list of {"filename": str, "model_dir": str, "size_mb": float}.
    """
    # PROJECT_ROOT = custom_nodes/modal_gateway
    # PROJECT_ROOT.parent = custom_nodes
    # PROJECT_ROOT.parent.parent = ComfyUI
    models_dir = PROJECT_ROOT.parent.parent / "models"

    MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".bin", ".gguf", ".pth"}
    results: list[dict] = []

    if not models_dir.is_dir():
        print(f"[Modal Gateway] models dir not found: {models_dir}")
        return results

    for subdir in sorted(models_dir.iterdir()):
        if not subdir.is_dir():
            continue
        # Skip non-model dirs
        if subdir.name.startswith(".") or subdir.name.startswith("__"):
            continue
        for file in sorted(subdir.iterdir()):
            if file.is_file() and file.suffix.lower() in MODEL_EXTENSIONS:
                try:
                    size_mb = round(file.stat().st_size / (1024 * 1024), 1)
                except OSError:
                    size_mb = 0
                results.append({
                    "filename": file.name,
                    "model_dir": subdir.name,
                    "size_mb": size_mb,
                })

    return results


def _extract_class_mappings(init_path: Path) -> set[str]:
    """Extrait les clés de NODE_CLASS_MAPPINGS d'un __init__.py via AST.

    Évite le matching par sous-chaîne (faux positifs : commentaires, chaînes
    de doc, etc.).
    """
    try:
        tree = ast.parse(init_path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return set()

    mappings: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS"):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    mappings.add(key.value)
    return mappings


def _match_nodes_to_class_types(class_types: list[str]) -> list[str]:
    """Match workflow class_types to local custom node directories.

    Parse chaque custom node's __init__.py (AST) et extrait les vraies clés de
    NODE_CLASS_MAPPINGS — pas de matching par sous-chaîne.
    Retourne une liste de noms de dossiers de nodes fournissant l'un des
    class_types donnés.
    """
    custom_nodes_dir = PROJECT_ROOT.parent
    matches: list[str] = []

    if not custom_nodes_dir.is_dir():
        return matches

    wanted = set(class_types)
    if not wanted:
        return matches

    for entry in sorted(custom_nodes_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("__"):
            continue
        if entry.name == PROJECT_ROOT.name:
            continue

        init_path = entry / "__init__.py"
        if not init_path.is_file():
            continue

        if _extract_class_mappings(init_path) & wanted:
            matches.append(entry.name)

    return matches


def _sanitize_save_path(output_dir: Path, subfolder: str, filename: str) -> Path | None:
    """Valide un couple (subfolder, filename) contre le path traversal.

    Retourne le chemin cible résolu s'il est valide et situé sous *output_dir*,
    sinon None. Rejette : chemins absolus, composants ``..``/``.`` dans
    subfolder, filename avec séparateurs ou ``..``, et toute sortie du dossier
    (y compris via symlinks, grâce à ``resolve()``).
    """
    try:
        base = output_dir.resolve()
    except OSError:
        return None

    # subfolder : ni chemin absolu, ni composant ".." ou "."
    if subfolder:
        sub_path = Path(subfolder)
        if sub_path.is_absolute() or any(p in ("..", ".") for p in sub_path.parts):
            return None
        target_dir = base.joinpath(*sub_path.parts)
    else:
        target_dir = base

    # filename : un seul composant, sans séparateur de chemin
    if (
        "/" in filename
        or "\\" in filename
        or filename in ("", ".", "..")
        or Path(filename).name != filename
    ):
        return None

    try:
        target = (target_dir / filename).resolve()
    except OSError:
        return None
    if not target.is_relative_to(base):
        return None
    return target


# ─── Initialisation : patcher PromptServer.add_routes ───

try:
    from server import PromptServer
    from aiohttp import web
except ImportError:
    # Pas dans ComfyUI, tout va bien
    PromptServer = None  # type: ignore
    web = None

def _request_guard(request):
    """Retourne ``None`` si la requête est autorisée, sinon une réponse 403.

    Protège les endpoints locaux contre drive-by / DNS rebinding en
    vérifiant le Host et l'Origin de la requête. Seules les origines
    locales (loopback, réseau privé, ``.local``) et same-origin sont
    acceptées.
    """
    host = request.headers.get("Host", "")
    origin = request.headers.get("Origin", "")

    def _hostname(netloc: str) -> str:
        n = netloc.strip().lower()
        if n.startswith("["):  # IPv6 [::1]:port
            return n.split("]")[0] + "]"
        if ":" in n:  # hostname:port
            return n.rsplit(":", 1)[0]
        return n

    def _is_local(hostname: str) -> bool:
        h = hostname.strip().lower()
        if h in ("localhost", "::1", "[::1]") or h.endswith(".local"):
            return True
        if h.startswith("127."):
            return True
        if h.startswith("10.") or h.startswith("192.168."):
            return True
        if h.startswith("172."):
            try:
                return 16 <= int(h.split(".")[1]) <= 31
            except (IndexError, ValueError):
                return False
        return False

    if host:
        if not _is_local(_hostname(host)):
            return web.json_response(
                {"error": "Forbidden: non-local Host"}, status=403
            )

    if origin:
        origin_hostname = ""
        try:
            origin_hostname = _hostname(urlsplit(origin).netloc)
        except ValueError:
            origin_hostname = ""

        if origin_hostname:
            if host and _hostname(host) == origin_hostname:
                pass  # same-origin OK (notamment accès LAN)
            elif _is_local(origin_hostname):
                pass  # origine locale OK
            else:
                return web.json_response(
                    {"error": "Forbidden: non-local Origin"}, status=403
                )

    return None


if PromptServer is not None:
    _original_add_routes = PromptServer.add_routes

    def _add_routes_with_modal(self):
        """Appelé pendant l'init de ComfyUI, AVANT que le router soit frozen."""
        # D'abord les routes originales de ComfyUI
        result = _original_add_routes(self)

        # ── GET /api/modal/config ──
        async def get_config(request):
            config = load_config()
            return web.json_response(config)

        # ── POST /api/modal/config ──
        async def post_config(request):
            try:
                data = await request.json()
            except Exception:
                data = {}
            save_config(data)
            return web.json_response({"ok": True, "config": load_config()})

        # ── GET /api/modal/status ──
        async def get_status(request):
            # Les checks subprocess tournent dans un thread pour ne pas
            # bloquer l'event loop de ComfyUI (modal list peut prendre 10s)
            status = {
                "modal_installed": await asyncio.to_thread(_check_modal_installed),
                "modal_authenticated": await asyncio.to_thread(_check_modal_auth),
                "volume_exists": await asyncio.to_thread(_check_volume_exists),
                "secret_exists": await asyncio.to_thread(_check_secret_exists),
            }
            config = load_config()
            status["last_sync"] = config.get("last_sync")
            status["last_deploy"] = config.get("last_deploy")
            status["api_configured"] = bool(config.get("api_url") and config.get("api_key"))
            return web.json_response(status)

        # ── POST /api/modal/sync — lance la sync en arrière-plan ──
        async def post_sync(request):
            if _OP_LOCK.locked():
                return web.json_response(
                    {"ok": False, "error": "Une opération Modal est déjà en cours"},
                    status=409,
                )
            asyncio.create_task(_run_async("sync", ["modal", "run", "sync.py"]))
            return web.json_response({"ok": True, "message": "Sync lancée"})

        # ── POST /api/modal/deploy — lance le déploiement ──
        async def post_deploy(request):
            if _OP_LOCK.locked():
                return web.json_response(
                    {"ok": False, "error": "Une opération Modal est déjà en cours"},
                    status=409,
                )
            asyncio.create_task(_run_async("deploy", ["modal", "deploy", "apps/all_in_one.py"]))
            return web.json_response({"ok": True, "message": "Déploiement lancé"})

        # ── POST /api/modal/token — enregistre le token Modal (remplace modal token set en CLI) ──
        async def post_token(request):
            """Configure les credentials Modal du serveur (Token ID + Token Secret du site Modal).

            Exécute ``modal token set`` qui vérifie les credentials (--verify par défaut)
            puis confirme avec ``modal token info``. Les credentials sont stockés par le
            CLI Modal lui-même (~/.modal), pas dans config.json.
            """
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

            token_id = str(data.get("token_id", "")).strip()
            token_secret = str(data.get("token_secret", "")).strip()
            if not token_id or not token_secret:
                return web.json_response(
                    {"ok": False, "error": "token_id and token_secret are required"},
                    status=400,
                )

            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["modal", "token", "set", "--token-id", token_id, "--token-secret", token_secret],
                    capture_output=True, text=True, timeout=60,
                )
            except FileNotFoundError:
                return web.json_response({"ok": False, "error": "modal CLI not installed"}, status=500)
            except subprocess.TimeoutExpired:
                return web.json_response({"ok": False, "error": "modal token set timed out"}, status=500)

            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                return web.json_response(
                    {"ok": False, "error": f"modal token set failed: {detail[:500]}"},
                    status=500,
                )

            # Confirmer que le token fonctionne
            authenticated = False
            try:
                info = await asyncio.to_thread(
                    subprocess.run,
                    ["modal", "token", "info"],
                    capture_output=True, text=True, timeout=15,
                )
                authenticated = info.returncode == 0
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

            print(f"[Modal Gateway] Token Modal enregistré (authentifié: {authenticated})")
            return web.json_response({"ok": True, "authenticated": authenticated})

        # ── GET /api/modal/logs — récupère les logs de la dernière opération ──
        async def get_logs(request):
            log_path = _log_path()
            if log_path.exists():
                return web.json_response({"logs": log_path.read_text()})
            return web.json_response({"logs": ""})

        # ── GET /api/modal/logs/stream — SSE pour logs en temps réel ──
        async def get_logs_stream(request):
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            await response.prepare(request)

            log_path = _log_path()
            last_size = log_path.stat().st_size if log_path.exists() else 0
            keepalive_count = 0

            try:
                while True:  # No more 5-minute limit
                    await asyncio.sleep(0.5)
                    keepalive_count += 1

                    if log_path.exists():
                        current_size = log_path.stat().st_size
                        if current_size > last_size:
                            with open(log_path, "r") as f:
                                f.seek(last_size)
                                new_content = f.read()
                                last_size = current_size
                                for line in new_content.split("\n"):
                                    if line.strip():
                                        try:
                                            await response.write(
                                                f"data: {json.dumps({'line': line.rstrip()})}\n\n".encode()
                                            )
                                        except (ConnectionResetError, ConnectionAbortedError):
                                            return response
                                    keepalive_count = 0

                    # Send keepalive every 30 seconds to prevent proxy timeout
                    if keepalive_count >= 60:  # 60 * 0.5s = 30s
                        try:
                            await response.write(f": keepalive\n\n".encode())
                        except (ConnectionResetError, ConnectionAbortedError):
                            return response
                        keepalive_count = 0

                    status_path = _status_path()
                    if status_path.exists():
                        status = status_path.read_text().strip()
                        try:
                            await response.write(
                                f"event: done\ndata: {json.dumps({'code': status})}\n\n".encode()
                            )
                        except (ConnectionResetError, ConnectionAbortedError):
                            pass
                        break
            except (ConnectionResetError, ConnectionAbortedError):
                pass
            return response

        async def get_plugins_detect(request):
            plugins = _detect_custom_nodes()
            return web.json_response({"plugins": plugins})

        async def get_plugins(request):
            config = load_config()
            return web.json_response({
                "custom_nodes": config.get("custom_nodes", []),
                "custom_nodes_ext": config.get("custom_nodes_ext", []),
                "custom_nodes_local": config.get("custom_nodes_local", []),
            })

        async def post_plugins(request):
            try:
                data = await request.json()
            except Exception:
                return web.json_response(
                    {"ok": False, "error": "Invalid JSON"}, status=400
                )
            custom_nodes = data.get("custom_nodes", [])
            custom_nodes_ext = data.get("custom_nodes_ext", [])
            custom_nodes_local = data.get("custom_nodes_local", [])
            if not isinstance(custom_nodes, list):
                return web.json_response(
                    {"ok": False, "error": "custom_nodes must be a list"}, status=400
                )
            if not isinstance(custom_nodes_ext, list):
                return web.json_response(
                    {"ok": False, "error": "custom_nodes_ext must be a list"}, status=400
                )
            if not isinstance(custom_nodes_local, list):
                return web.json_response(
                    {"ok": False, "error": "custom_nodes_local must be a list"}, status=400
                )
            save_config({
                "custom_nodes": custom_nodes,
                "custom_nodes_ext": custom_nodes_ext,
                "custom_nodes_local": custom_nodes_local,
            })
            return web.json_response({
                "ok": True,
                "custom_nodes": custom_nodes,
                "custom_nodes_ext": custom_nodes_ext,
                "custom_nodes_local": custom_nodes_local,
            })

        # ── GET /api/modal/models/detect — scan local models ──
        async def get_models_detect(request):
            models = _detect_local_models()
            return web.json_response({"models": models})

        # ── GET /api/modal/models/select — get saved model selection ──
        async def get_models_select(request):
            config = load_config()
            return web.json_response({
                "models_to_sync": config.get("models_to_sync", []),
            })

        # ── POST /api/modal/models/select — save model selection ──
        async def post_models_select(request):
            try:
                data = await request.json()
            except Exception:
                return web.json_response(
                    {"ok": False, "error": "Invalid JSON"}, status=400
                )
            models_to_sync = data.get("models_to_sync", [])
            if not isinstance(models_to_sync, list):
                return web.json_response(
                    {"ok": False, "error": "models_to_sync must be a list"}, status=400
                )
            save_config({"models_to_sync": models_to_sync})
            return web.json_response({
                "ok": True,
                "models_to_sync": models_to_sync,
            })

        # ── POST /api/modal/plugins/match — match class_types to nodes ──
        async def post_plugins_match(request):
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
            class_types = data.get("class_types", [])
            if not isinstance(class_types, list):
                return web.json_response({"ok": False, "error": "class_types must be a list"}, status=400)
            matches = _match_nodes_to_class_types(class_types)
            return web.json_response({"matches": matches})

        # ── POST /api/modal/save-local — save image to local filesystem ──
        async def post_save_local(request):
            try:
                data = await request.json()
            except Exception:
                return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

            filename = data.get("filename", "")
            subfolder = data.get("subfolder", "")
            image_type = data.get("type", "output")
            base64_data = data.get("data", "")

            if not filename or not base64_data:
                return web.json_response({"ok": False, "error": "Missing filename or data"}, status=400)

            import base64 as b64mod

            # Determine local output directory
            config = load_config()
            output_dir_str = config.get("local_output_dir", "")
            if output_dir_str:
                output_dir = Path(output_dir_str)
            else:
                # Auto-detect: PROJECT_ROOT = custom_nodes/ComfyCH
                # ComfyUI root = PROJECT_ROOT.parent.parent
                # output dir = ComfyUI root / "output"
                comfy_root = PROJECT_ROOT.parent.parent
                output_dir = comfy_root / "output"

            # ── Sanitisation anti path traversal ───────────────────────
            target_path = _sanitize_save_path(output_dir, subfolder, filename)
            if target_path is None:
                return web.json_response(
                    {"ok": False, "error": "Invalid filename or subfolder"},
                    status=400,
                )

            target_dir = target_path.parent
            target_dir.mkdir(parents=True, exist_ok=True)

            # Write the file
            try:
                file_data = b64mod.b64decode(base64_data)
                target_path.write_bytes(file_data)
                print(f"[Modal Gateway] Saved locally: {target_path}")
                return web.json_response({
                    "ok": True,
                    "path": str(target_path),
                })
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=500)

        # ── GET /api/modal/user-settings/status ──
        async def get_user_settings_status(request):
            user_dir = PROJECT_ROOT.parent.parent / "user"
            if not user_dir.is_dir():
                return web.json_response({"exists": False, "path": str(user_dir)})
            total_files = 0
            total_size = 0
            for f in user_dir.rglob("*"):
                if f.is_file():
                    total_files += 1
                    total_size += f.stat().st_size
            return web.json_response({
                "exists": True,
                "path": str(user_dir),
                "file_count": total_files,
                "size_mb": round(total_size / (1024 * 1024), 1),
            })

        # ── POST /api/modal/sync-user ──
        async def post_sync_user(request):
            if _OP_LOCK.locked():
                return web.json_response(
                    {"ok": False, "error": "Une opération Modal est déjà en cours"},
                    status=409,
                )
            asyncio.create_task(_run_async("sync_user", ["modal", "run", "sync.py"]))
            return web.json_response({"ok": True, "message": "Sync user settings lancée"})

        # Ensuite nos routes Modal Gateway
        routes = [
            ("GET", "/api/modal/config", get_config),
            ("POST", "/api/modal/config", post_config),
            ("GET", "/api/modal/plugins/detect", get_plugins_detect),
            ("GET", "/api/modal/plugins", get_plugins),
            ("POST", "/api/modal/plugins", post_plugins),
            ("POST", "/api/modal/plugins/match", post_plugins_match),
            ("GET", "/api/modal/models/detect", get_models_detect),
            ("GET", "/api/modal/models/select", get_models_select),
            ("POST", "/api/modal/models/select", post_models_select),
            ("GET", "/api/modal/status", get_status),
            ("POST", "/api/modal/sync", post_sync),
            ("POST", "/api/modal/deploy", post_deploy),
            ("POST", "/api/modal/token", post_token),
            ("GET", "/api/modal/logs", get_logs),
            ("GET", "/api/modal/logs/stream", get_logs_stream),
            ("POST", "/api/modal/save-local", post_save_local),
            ("GET", "/api/modal/user-settings/status", get_user_settings_status),
            ("POST", "/api/modal/sync-user", post_sync_user),
        ]
        for method, path, handler in routes:
            async def _guarded(request, _handler=handler):
                denied = _request_guard(request)
                if denied is not None:
                    return denied
                return await _handler(request)
            self.app.router.add_route(method, path, _guarded)

        print(f"[Modal Gateway] Routes API configurées ({len(routes)} endpoints)")
        return result

    PromptServer.add_routes = _add_routes_with_modal

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
