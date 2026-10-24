"""Tests du gateway — sans dépendances (stubs fastapi/pydantic/modal).

Couvre :
- l'idempotence de /generate (pas de double exécution au retry)
- la sanitisation anti path traversal de /api/modal/save-local
- l'extraction AST des NODE_CLASS_MAPPINGS
- la sanitisation des noms de modèles et le rejet des liens tar (sync.py)
- le slot d'opération Modal (timeout, état exposé, libération sur slot fuité)
- l'encadrement des logs + heartbeat (le flux SSE ne reste jamais muet)
- le manifeste des custom nodes sur le volume (source de vérité des badges)

Usage ::

    python3 tests/test_gateway.py
"""
import asyncio
import importlib.util
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace

# ─────────────────────────── Stubs fastapi/pydantic/modal ──────────────────

class Header:
    def __init__(self, default="", alias=None): pass
class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code; self.detail = detail
        super().__init__(detail)
class Depends:
    def __init__(self, dep): self.dep = dep
class JSONResponse:
    def __init__(self, content, status_code=200):
        self.content = content; self.status_code = status_code
class Response:
    def __init__(self, content=b"", media_type=None):
        self.content = content; self.media_type = media_type
class CORSMiddleware: pass
class FastAPI:
    def __init__(self, title=""):
        self.title = title; self.handlers = {}
    def add_middleware(self, *a, **kw): pass
    def _route(self, method, path):
        def deco(fn):
            self.handlers[(method, path)] = fn
            return fn
        return deco
    def post(self, path, **kw): return self._route("POST", path)
    def get(self, path, **kw): return self._route("GET", path)

fastapi = types.ModuleType("fastapi")
fastapi.FastAPI = FastAPI
fastapi.Depends = Depends
fastapi.Request = object
fastapi.JSONResponse = JSONResponse
fastapi.Response = Response
fastapi.Header = Header
fastapi.HTTPException = HTTPException
fastapi.CORSMiddleware = CORSMiddleware

middleware = types.ModuleType("fastapi.middleware")
cors_mod = types.ModuleType("fastapi.middleware.cors")
cors_mod.CORSMiddleware = CORSMiddleware
middleware.cors = cors_mod
fastapi.middleware = middleware

responses = types.ModuleType("fastapi.responses")
responses.JSONResponse = JSONResponse
responses.Response = Response
fastapi.responses = responses

sys.modules["fastapi"] = fastapi
sys.modules["fastapi.middleware"] = middleware
sys.modules["fastapi.middleware.cors"] = cors_mod
sys.modules["fastapi.responses"] = responses

pydantic = types.ModuleType("pydantic")
class BaseModel: pass
pydantic.BaseModel = BaseModel
sys.modules["pydantic"] = pydantic

modal = types.ModuleType("modal")
class Secret:
    @classmethod
    def from_name(cls, name):
        s = cls(); s._name = name; return s
    @classmethod
    def from_dict(cls, d):
        s = cls(); s._dict = d; return s
    def hydrate(self): raise modal.exception.NotFoundError("nope")
class exception:
    class NotFoundError(Exception): pass

class _StubBatch:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def put_file(self, path, dest): return None

class Volume:
    def __init__(self, name=""):
        self.name = name; self.committed = False
    @classmethod
    def from_name(cls, name, create_if_missing=False):
        return cls(name)
    def commit(self): self.committed = True
    def reload(self): self.reloaded = True
    def batch_upload(self): return _StubBatch()

class Image:
    @classmethod
    def debian_slim(cls, python_version="3.11"):
        return cls()
    def add_local_python_source(self, *names, copy=False): return self
    def apt_install(self, *pkgs): return self
    def pip_install(self, *pkgs): return self

class App:
    def __init__(self, name="", image=None):
        self.name = name; self.image = image; self.functions = {}
    def function(self, **kw):
        def deco(fn):
            self.functions[fn.__name__] = fn
            return fn
        return deco
    def local_entrypoint(self, **kw):
        def deco(fn):
            self.functions[fn.__name__] = fn
            return fn
        return deco

modal.Secret = Secret
modal.exception = exception
modal.Volume = Volume
modal.Image = Image
modal.App = App
sys.modules["modal"] = modal

# ─────────────────────────── Import du code réel ───────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import auth  # noqa: E402
import gateway_router  # noqa: E402

# ─────────────────────────── Fake workers ──────────────────────────────────

class _Chain:
    def __init__(self, fn):
        self.remote = SimpleNamespace(aio=fn)

class FakeWorker:
    submit_count = 0

    def __init__(self):
        self.prompt = _Chain(self._prompt)
        self.history = _Chain(self._history)
        self.view = _Chain(self._view)
        self.upload_image = _Chain(self._upload)

    async def _prompt(self, workflow):
        FakeWorker.submit_count += 1
        return {"prompt_id": "job-1"}

    async def _history(self, job_id):
        return {"job-1": {"outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}}}}

    async def _view(self, filename, subfolder, image_type):
        return {"data": "aGVsbG8=", "content_type": "image/png", "filename": filename}

    async def _upload(self, content):
        return {"name": "uploaded.png"}

def make_router():
    return gateway_router.build_router({"L4": FakeWorker}, cors_origins=["*"], title="test")

def req(gpu="L4", request_id=""):
    return SimpleNamespace(workflow={"3": {"class_type": "KSampler", "inputs": {}}}, gpu=gpu, request_id=request_id)

PASS = 0
TOTAL = 0


def check(name, cond):
    global PASS, TOTAL
    TOTAL += 1
    if cond: PASS += 1
    print(("✅" if cond else "❌"), name)


# ─────────────────────────── Tests idempotence ─────────────────────────────

async def _test_concurrent_same_id():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    r1, r2 = await asyncio.gather(generate(req(request_id="req-A")), generate(req(request_id="req-A")))
    check("concurrent même request_id → 1 seule soumission", FakeWorker.submit_count == 1)
    check("concurrent même request_id → 2 résultats identiques (même job_id)",
          r1.get("job_id") == r2.get("job_id") == "job-1")


def _test_sequential_same_id():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    r1 = asyncio.run(generate(req(request_id="req-B")))
    r2 = asyncio.run(generate(req(request_id="req-B")))
    check("séquentiel même request_id → 1 seule soumission (cache)", FakeWorker.submit_count == 1)
    check("séquentiel même request_id → même job_id", r1.get("job_id") == r2.get("job_id") == "job-1")


def _test_different_ids():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    asyncio.run(generate(req(request_id="req-C")))
    asyncio.run(generate(req(request_id="req-D")))
    check("request_id différents → 2 soumissions", FakeWorker.submit_count == 2)


def _test_no_request_id():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    asyncio.run(generate(req(request_id="")))
    asyncio.run(generate(req(request_id="")))
    check("sans request_id → comportement d'origine (2 soumissions)", FakeWorker.submit_count == 2)


def _test_unknown_gpu():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    resp = asyncio.run(generate(req(gpu="H100", request_id="req-E")))
    check("GPU inconnu → 400 (pas de soumission)",
          isinstance(resp, JSONResponse) and resp.status_code == 400 and FakeWorker.submit_count == 0)


def _test_cache_after_success():
    router = make_router()
    FakeWorker.submit_count = 0
    generate = router.handlers[("POST", "/generate")]
    r1 = asyncio.run(generate(req(request_id="req-F")))
    r2 = asyncio.run(generate(req(request_id="req-F")))
    check("même id après succès → cache (pas de 2e soumission)",
          FakeWorker.submit_count == 1 and r2.get("job_id") == "job-1")


# ─────────────────────── Robustesse idempotence (M2) ───────────────────────

class HangingWorker(FakeWorker):
    """Worker dont la soumission ne rend jamais la main (leader bloqué)."""

    async def _prompt(self, workflow):
        FakeWorker.submit_count += 1
        await asyncio.Event().wait()


async def _test_follower_timeout():
    """Un follower ne doit pas attendre à vie si le leader meurt."""
    router = make_router()
    generate = router.handlers[("POST", "/generate")]
    rid = "req-FOLLOWER-TIMEOUT"
    entry = {"event": asyncio.Event(), "result": None, "error": None}
    gateway_router._idempotency_inflight[rid] = entry

    old_timeout = gateway_router._FOLLOWER_WAIT_TIMEOUT_SECONDS
    gateway_router._FOLLOWER_WAIT_TIMEOUT_SECONDS = 0.05
    try:
        resp = await generate(req(request_id=rid))
    finally:
        gateway_router._FOLLOWER_WAIT_TIMEOUT_SECONDS = old_timeout

    check("follower : timeout du leader → 504",
          isinstance(resp, JSONResponse) and resp.status_code == 504)
    check("follower : timeout du leader → entrée inflight nettoyée",
          rid not in gateway_router._idempotency_inflight)


async def _test_leader_cancelled_wakes_followers():
    """Une annulation du leader clôt l'entrée au lieu de la laisser fuiter."""
    router = gateway_router.build_router(
        {"L4": HangingWorker}, cors_origins=["*"], title="test"
    )
    generate = router.handlers[("POST", "/generate")]
    rid = "req-LEADER-CANCEL"

    task = asyncio.create_task(generate(req(request_id=rid)))
    entry = None
    for _ in range(200):
        await asyncio.sleep(0.005)
        entry = gateway_router._idempotency_inflight.get(rid)
        if entry is not None:
            break
    # Laisser la tâche entrer dans le try (await du worker bloquant)
    await asyncio.sleep(0.02)
    check("leader : entrée inflight créée avant annulation", entry is not None)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    check("leader annulé → entrée inflight nettoyée",
          rid not in gateway_router._idempotency_inflight)
    check("leader annulé → event levé + erreur renseignée",
          entry is not None and entry["event"].is_set() and entry["error"] is not None)


async def _test_idempotency_finish_idempotent():
    """_idempotency_finish doit être appelable deux fois sans casse."""
    rid = "req-FINISH-TWICE"
    entry = {"event": asyncio.Event(), "result": None, "error": None}
    gateway_router._idempotency_inflight[rid] = entry
    r1 = gateway_router._idempotency_finish(rid, entry, {"job_id": "j"}, None)
    r2 = gateway_router._idempotency_finish(rid, entry, {"job_id": "j"}, None)
    check("finish deux fois → même résultat, pas d'exception",
          r1 == r2 == {"job_id": "j"} and entry["event"].is_set())
    check("finish → entrée inflight retirée",
          rid not in gateway_router._idempotency_inflight)


# ─────────────────────────── Tests masquage config (F3) ────────────────────

# ─────────────────── Tests slot d'opération Modal (BUG 1) ──────────────

def _test_op_slot_state():
    """Le slot expose l'opération en cours et sa durée, et se libère."""
    import time as _time

    comfych = _load_comfych_module()
    comfych.web = _FakeWeb

    check("slot : libre au départ", comfych._op_in_progress is False)
    check("slot : acquisition → True", comfych._try_acquire_op("sync") is True)
    check("slot : opération en cours mémorisée",
          comfych._op_in_progress is True
          and comfych._op_current_name == "sync"
          and comfych._op_started_at > 0)
    check("slot : seconde acquisition refusée", comfych._try_acquire_op("deploy") is False)
    check("slot : refus ne change pas l'opération courante",
          comfych._op_current_name == "sync")

    busy = comfych._op_busy_response()
    payload = busy.payload
    check("409 : contenu explicite (opération + elapsed_seconds)",
          busy.status == 409
          and payload["ok"] is False
          and payload["operation"] == "sync"
          and isinstance(payload["elapsed_seconds"], int)
          and "sync" in payload["error"]
          and "depuis" in payload["error"])

    comfych._release_op()
    check("slot : libération → état remis à zéro",
          comfych._op_in_progress is False
          and comfych._op_current_name == ""
          and comfych._op_started_at == 0.0)
    check("slot : libération idempotente",
          comfych._release_op() is None and comfych._op_in_progress is False)
    check("slot : elapsed_seconds = 0 sans opération",
          comfych._op_elapsed_seconds() == 0
          and comfych._op_busy_response().payload["elapsed_seconds"] == 0)

    # Slot occupé mais récent → refus
    comfych._try_acquire_op("sync_user")
    comfych._op_started_at = _time.time() - 10
    check("slot : occupé < stale → toujours refusé",
          comfych._try_acquire_op("sync") is False)

    # Slot fuité (plus vieux que _OP_STALE_SECONDS) → reprise automatique
    comfych._op_started_at = _time.time() - (comfych._OP_STALE_SECONDS + 5)
    check("slot fuité → libération automatique + réacquisition",
          comfych._try_acquire_op("deploy") is True
          and comfych._op_current_name == "deploy")
    comfych._release_op()
    check("slot : stale > timeout (invariant)",
          comfych._OP_STALE_SECONDS > comfych._OP_TIMEOUT_SECONDS)


async def _test_op_reset_keeps_new_slot():
    """Après un reset, la fin de l'ancienne tâche ne libère pas le nouveau slot."""
    comfych = _load_comfych_module()
    comfych._try_acquire_op("sync")
    old_task = asyncio.create_task(asyncio.sleep(0))
    comfych._task_op_tokens[old_task] = comfych._op_token

    comfych._release_op()                 # POST /api/modal/op/reset
    comfych._try_acquire_op("deploy")     # nouvelle opération acceptée
    comfych._on_op_task_done(old_task)    # l'ancienne tâche se termine enfin

    check("reset : la fin de l'ancienne op ne libère pas le nouveau slot",
          comfych._op_in_progress is True and comfych._op_current_name == "deploy")
    check("reset : jeton de la tâche oublié par le callback",
          old_task not in comfych._task_op_tokens)
    await old_task
    comfych._release_op()


async def _test_op_timeout_kills_process():
    """Un subprocess bloqué est tué au timeout, status=1 → l'UI se débloque."""
    comfych = _load_comfych_module()
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "op.log"
    status_path = tmp / "op.status"
    comfych._log_path = lambda: log_path
    comfych._status_path = lambda: status_path
    comfych.CONFIG_PATH = tmp / "config.json"
    comfych._OP_TIMEOUT_SECONDS = 0.2

    await comfych._run_async_inner(
        "unit-test", [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    logs = log_path.read_text() if log_path.exists() else ""
    check("timeout : subprocess tué et status=1 écrit",
          status_path.exists() and status_path.read_text() == "1")
    check("timeout : message de timeout dans les logs", "timeout" in logs)


async def _test_op_stream_success():
    """Le streaming des logs fonctionne et le code de retour est écrit."""
    comfych = _load_comfych_module()
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "op.log"
    status_path = tmp / "op.status"
    comfych._log_path = lambda: log_path
    comfych._status_path = lambda: status_path
    comfych.CONFIG_PATH = tmp / "config.json"

    await comfych._run_async_inner(
        "unit-test", [sys.executable, "-c", "print('hello-modal')"]
    )
    check("stream : ligne du subprocess écrite dans les logs",
          "hello-modal" in (log_path.read_text() if log_path.exists() else ""))
    check("stream : status = code de retour (0)",
          status_path.exists() and status_path.read_text() == "0")


async def _test_op_log_framing():
    """Le log est encadré (amorce + ligne de fin) : le SSE n'est jamais muet."""
    comfych = _load_comfych_module()
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "op.log"
    status_path = tmp / "op.status"
    comfych._log_path = lambda: log_path
    comfych._status_path = lambda: status_path
    comfych.CONFIG_PATH = tmp / "config.json"

    await comfych._run_async_inner(
        "unit-test", [sys.executable, "-c", "print('corps')"]
    )
    logs = log_path.read_text() if log_path.exists() else ""
    check("log : en-tête d'opération écrit avant le lancement",
          "Opération: unit-test" in logs)
    check("log : commande annoncée avant l'import modal",
          "Lancement de: " in logs and "Initialisation de modal" in logs)
    check("log : en-tête avant la sortie du subprocess",
          "corps" in logs and logs.index("Opération: unit-test") < logs.index("corps"))
    check("log : ligne de fin avec le code de retour",
          "Terminé (code 0)" in logs)


async def _test_stream_heartbeat():
    """Un subprocess muet produit des heartbeats (sortie bufferisée de modal)."""
    comfych = _load_comfych_module()
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "op.log"
    comfych._HEARTBEAT_SECONDS = 0.2

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import time; time.sleep(0.6); print('apres-silence')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await asyncio.wait_for(
        comfych._stream_process(process, log_path, time.time()), timeout=15
    )
    await process.wait()

    logs = log_path.read_text() if log_path.exists() else ""
    check("heartbeat : « toujours en cours » écrit pendant le silence",
          "toujours en cours" in logs)
    check("heartbeat : la sortie réelle est écrite après le silence",
          "apres-silence" in logs)


# ───────────── Tests manifeste custom nodes sur le volume (BUG 2) ───────

def _nodes_archive(names):
    """Construit un tar.gz contenant ``<node>/__init__.py`` pour chaque node."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            info = tarfile.TarInfo(name + "/__init__.py")
            info.size = 0
            tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


def _stage_upload(root, names, archive_name="nodes.tar.gz"):
    """Dépose une archive dans ``<root>/_upload/`` (comme le batch_upload local)."""
    upload = Path(root) / "_upload"
    upload.mkdir(parents=True, exist_ok=True)
    (upload / archive_name).write_bytes(_nodes_archive(names))
    return archive_name


def _test_custom_nodes_manifest():
    import json as json_mod

    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())
    # Contenu périmé qui doit être remplacé par le swap
    (root / "old_node").mkdir()
    (root / "stale_file.txt").write_text("x")
    sync_mod._CUSTOM_NODES_ROOT = str(root)

    archive_name = _stage_upload(root, ["node_b", "node_a"])
    result = sync_mod.extract_custom_nodes_from_volume(archive_name)
    check("manifeste : sync retourne les nodes triés",
          result.get("status") == "ok" and result.get("nodes") == ["node_a", "node_b"])
    check("manifeste : ancien contenu du volume remplacé",
          not (root / "old_node").exists())
    check("manifeste : archive temporaire + dossier _upload supprimés",
          not (root / "_upload").exists())
    check("manifeste : dossier _upload jamais listé comme un node",
          "_upload" not in result.get("nodes", []))

    manifest = root / sync_mod.NODES_MANIFEST_NAME
    check("manifeste : fichier écrit sur le volume", manifest.is_file())
    data = json_mod.loads(manifest.read_text()) if manifest.is_file() else {}
    check("manifeste : contenu {nodes, synced_at}",
          data.get("nodes") == ["node_a", "node_b"]
          and isinstance(data.get("synced_at"), str) and bool(data["synced_at"]))

    read_back = sync_mod.get_custom_nodes_manifest()
    check("manifeste : lecture distante = ce qui a été écrit",
          read_back.get("nodes") == ["node_a", "node_b"]
          and read_back.get("synced_at") == data.get("synced_at"))

    empty_root = Path(tempfile.mkdtemp())
    sync_mod._CUSTOM_NODES_ROOT = str(empty_root)
    check("manifeste : absent → {nodes: [], synced_at: None}",
          sync_mod.get_custom_nodes_manifest() == {"nodes": [], "synced_at": None})

    (empty_root / sync_mod.NODES_MANIFEST_NAME).write_text("pas du json {{{ ")
    check("manifeste : corrompu → fallback vide",
          sync_mod.get_custom_nodes_manifest() == {"nodes": [], "synced_at": None})


def _test_extract_volume_security():
    """Nom d'archive invalide / archive absente → refus explicite."""
    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())
    sync_mod._CUSTOM_NODES_ROOT = str(root)

    check("extraction : nom d'archive avec traversal rejeté",
          all(_raises(sync_mod.extract_custom_nodes_from_volume, n)
              for n in ("../evil.tar.gz", "sub/nodes.tar.gz", "a\\b.tar.gz", "", ".", "..")))

    try:
        sync_mod.extract_custom_nodes_from_volume("absent.tar.gz")
        check("extraction : archive absente → pas d'exception", False)
    except FileNotFoundError:
        check("extraction : archive absente → pas d'exception", True)
    except Exception:  # noqa: BLE001
        check("extraction : archive absente → pas d'exception", False)


def _test_update_local_config():
    import json as json_mod

    sync_mod = _load_sync_module()
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "config.json"
    sync_mod._CONFIG_PATH = cfg

    sync_mod._update_local_config({"synced_custom_nodes": ["a"]})
    check("config locale : clé synced_custom_nodes écrite",
          cfg.is_file() and json_mod.loads(cfg.read_text())["synced_custom_nodes"] == ["a"])

    sync_mod._update_local_config({"last_sync_custom_nodes": "2026-01-01T00:00:00"})
    data = json_mod.loads(cfg.read_text())
    check("config locale : mise à jour fusionnée (pas de perte de clés)",
          data.get("synced_custom_nodes") == ["a"]
          and data.get("last_sync_custom_nodes") == "2026-01-01T00:00:00")

    cfg.write_text("pas du json {{{ ")
    sync_mod._update_local_config({"synced_custom_nodes": ["b"]})
    check("config locale : JSON corrompu → réécrite sans exception",
          json_mod.loads(cfg.read_text())["synced_custom_nodes"] == ["b"])

    blocker = tmp / "afile"
    blocker.write_text("x")
    sync_mod._CONFIG_PATH = blocker / "config.json"  # mkdir impossible
    try:
        sync_mod._update_local_config({"synced_custom_nodes": ["c"]})
        check("config locale : écriture impossible → pas d'exception", True)
    except Exception:  # noqa: BLE001 — le sync ne doit jamais casser
        check("config locale : écriture impossible → pas d'exception", False)


def _load_comfych_module():
    """Importe __init__.py (PromptServer absent → routes non enregistrées)."""
    spec = importlib.util.spec_from_file_location("comfych", str(PROJECT_ROOT / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeWeb:
    """Remplace l'alias ``web`` (aiohttp) absent hors ComfyUI."""

    @staticmethod
    def json_response(payload, status=200):
        return SimpleNamespace(payload=payload, status=status, content=payload)


def _test_mask_config():
    spec = importlib.util.spec_from_file_location("comfych", str(PROJECT_ROOT / "__init__.py"))
    comfych = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comfych)

    src = {
        "api_url": "https://xxx.modal.run",
        "api_key": "super-secret",
        "allowed_hosts": ["comfy.example.com"],
    }
    masked = comfych._mask_config(src)
    check("mask : api_key jamais renvoyée en clair",
          masked["api_key"] == "" and "super-secret" not in str(masked))
    check("mask : api_key_set=true quand une clé existe", masked["api_key_set"] is True)
    check("mask : autres clés intactes",
          masked["api_url"] == "https://xxx.modal.run"
          and masked["allowed_hosts"] == ["comfy.example.com"])
    check("mask : api_key_set=false quand aucune clé",
          comfych._mask_config({"api_url": ""})["api_key_set"] is False)
    check("mask : config d'origine non mutée", src["api_key"] == "super-secret")


# ─────────────────────────── Tests path traversal ──────────────────────────

def _test_sanitize_save_path():
    spec = importlib.util.spec_from_file_location("comfych", str(PROJECT_ROOT / "__init__.py"))
    comfych = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comfych)
    s = comfych._sanitize_save_path

    tmp = Path(tempfile.mkdtemp())
    out = tmp / "output"
    out.mkdir()

    check("fichier simple valide", s(out, "", "img.png") == (out / "img.png").resolve())
    check("sous-dossier valide", s(out, "sub/dir", "img.png") == (out / "sub/dir/img.png").resolve())
    check("subfolder '..' rejeté", s(out, "../etc", "img.png") is None)
    check("subfolder absolu rejeté", s(out, "/etc", "img.png") is None)
    check("subfolder '..' seul rejeté", s(out, "..", "img.png") is None)
    check("subfolder avec '..' interne rejeté", s(out, "sub/../sub2", "img.png") is None)
    check("filename '../x' rejeté", s(out, "", "../img.png") is None)
    check("filename avec slash rejeté", s(out, "", "a/b.png") is None)
    check("filename absolu rejeté", s(out, "", "/etc/passwd") is None)
    check("filename '..' rejeté", s(out, "", "..") is None)
    check("filename vide rejeté", s(out, "", "") is None)
    check("filename antislash rejeté", s(out, "", "a\\b.png") is None)

    try:
        victim = Path(tempfile.mkdtemp())
        (out / "link").symlink_to(victim, target_is_directory=True)
        check("symlink sortant rejeté", s(out, "link", "img.png") is None)
    except OSError:
        print("ℹ️  symlinks non supportés ici — test sauté")


# ─────────────────── Tests sanitisation sync.py (path traversal) ────────────

def _load_sync_module():
    spec = importlib.util.spec_from_file_location("comfych_sync", str(PROJECT_ROOT / "sync.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _raises(fn, *args, **kwargs):
    """True si fn(*args) lève ValueError."""
    try:
        fn(*args, **kwargs)
    except ValueError:
        return True
    return False


def _test_safe_model_filename():
    sync_mod = _load_sync_module()
    safe = sync_mod._safe_model_filename

    check("filename simple accepté", safe("model.safetensors") == "model.safetensors")
    check("filename avec espaces accepté", safe("my model.safetensors") == "my model.safetensors")
    bad_names = ["", ".", "..", "../x", "sub/x", "a\\b", "/etc/passwd", "..\\x"]
    check("filenames dangereux rejetés",
          all(_raises(safe, n) for n in bad_names))

    check("_model_dest : chemin valide sous /cache",
          sync_mod._model_dest("model.safetensors") == Path("/cache") / "model.safetensors")
    check("_model_dest : filename dangereux rejeté",
          all(_raises(sync_mod._model_dest, n) for n in bad_names))


# ─────────────────── Tests rejet des liens dans l'archive ──────────────────

def _tar_members(members):
    """Construit un tar.gz et retourne ses membres (aller-retour réel)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for m in members:
            tar.addfile(m)
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue()), mode="r:gz") as tar:
        return tar.getmembers()


def _test_tar_member_validation():
    import tarfile

    sync_mod = _load_sync_module()
    validate = sync_mod._validate_tar_members
    staging = str(Path(tempfile.mkdtemp()) / ".staging")

    symlink = tarfile.TarInfo("innocent.txt")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "/etc/passwd"
    check("tar : membre symlink rejeté",
          _raises(validate, _tar_members([symlink]), staging))

    hardlink = tarfile.TarInfo("innocent.txt")
    hardlink.type = tarfile.LNKTYPE
    hardlink.linkname = "../../etc/passwd"
    check("tar : membre hardlink rejeté",
          _raises(validate, _tar_members([hardlink]), staging))

    absolute = tarfile.TarInfo("/etc/passwd")
    check("tar : chemin absolu rejeté",
          _raises(validate, _tar_members([absolute]), staging))

    parent = tarfile.TarInfo("../evil.txt")
    check("tar : composant '..' rejeté",
          _raises(validate, _tar_members([parent]), staging))

    directory = tarfile.TarInfo("node_a")
    directory.type = tarfile.DIRTYPE
    regular = tarfile.TarInfo("node_a/__init__.py")
    regular.size = 0
    check("tar : membres normaux acceptés",
          validate(_tar_members([directory, regular]), staging) is None)


# ─────────────── Tests sélection des nodes / garde-fous de taille ─────────

def _make_node(custom_nodes_dir, name, files=("__init__.py",)):
    """Crée ``<custom_nodes_dir>/<name>/`` avec les fichiers relatifs *files*."""
    node = Path(custom_nodes_dir) / name
    node.mkdir(parents=True, exist_ok=True)
    for rel in files:
        target = node / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    return node


def _test_node_exclusions():
    """Le gateway lui-même, ``user`` et les dossiers techniques sont exclus."""
    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())

    _make_node(root, "ComfyUI-Manager")
    _make_node(root, "user")
    _make_node(root, sync_mod.PROJECT_ROOT_NAME)
    _make_node(root, ".hidden_node")
    _make_node(root, "__pycache__")
    _make_node(root, "node_modules")
    (root / "config.json").write_text("{}")  # fichier, pas un node

    check("exclusions : PROJECT_ROOT_NAME déduit du dossier de sync.py",
          sync_mod.PROJECT_ROOT_NAME == PROJECT_ROOT.name)

    kept, skipped = sync_mod._select_node_dirs(root)
    kept_names = [Path(d).name for d in kept]
    skipped_names = {name for name, _ in skipped}
    check("exclusions : node normal conservé", kept_names == ["ComfyUI-Manager"])
    check("exclusions : gateway ComfyCH exclu",
          sync_mod.PROJECT_ROOT_NAME not in kept_names)
    check("exclusions : dossier 'user' exclu", "user" not in kept_names)
    check("exclusions : dossiers cachés et techniques exclus",
          {".hidden_node", "__pycache__", "node_modules"}.isdisjoint(kept_names))
    check("exclusions : chaque exclusion est motivée",
          skipped_names == {"user", sync_mod.PROJECT_ROOT_NAME, ".hidden_node",
                            "__pycache__", "node_modules"}
          and all(bool(reason) for _, reason in skipped))

    heavy_exts = ["model.safetensors", "w.ckpt", "x.pt", "x.pth", "x.bin",
                  "x.gguf", "x.onnx", "x.pkl", "x.h5", "x.msgpack", "x.zip",
                  "x.tar", "x.gz", "x.7z", "mod.pyc", "mod.pyo"]
    check("exclusions : extensions lourdes rejetées",
          all(sync_mod._is_excluded_file(n) for n in heavy_exts))
    check("exclusions : fichiers de code conservés",
          not sync_mod._is_excluded_file("__init__.py")
          and not sync_mod._is_excluded_file("requirements.txt")
          and not sync_mod._is_excluded_file("nodes.py"))
    check("exclusions : dossiers techniques ignorés où qu'ils soient",
          all(sync_mod._is_excluded_dir(d) for d in
              ["__pycache__", ".git", "node_modules", ".venv", "venv", ".cache",
               "dist", "build", "models", "output", "input", ".mypy_cache",
               ".pytest_cache", ".ruff_cache"]))


def _test_node_size_report():
    """Taille par node (fichiers retenus seulement) + top 5 décroissant."""
    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())

    small = _make_node(root, "node_small", files=())
    (small / "payload.dat").write_bytes(b"x" * 10)
    mid = _make_node(root, "node_mid", files=())
    (mid / "payload.dat").write_bytes(b"x" * 500)
    big = _make_node(root, "node_big", files=())
    (big / "payload.dat").write_bytes(b"x" * 3000)
    # Ces fichiers ne doivent PAS être comptés (extension lourde / dossier exclu)
    (big / "model.safetensors").write_bytes(b"y" * 10000)
    (big / "models").mkdir()
    (big / "models" / "weights.dat").write_bytes(b"z" * 20000)
    (big / "__pycache__").mkdir()
    (big / "__pycache__" / "junk.dat").write_bytes(b"w" * 30000)

    kept, _ = sync_mod._select_node_dirs(root)
    report = sync_mod._node_size_report(kept)
    sizes = dict(report)

    check("tailles : rapport trié décroissant",
          [name for name, _ in report] == ["node_big", "node_mid", "node_small"])
    check("tailles : seuls les fichiers archivés sont comptés",
          sizes["node_big"] == 3000 and sizes["node_mid"] == 500
          and sizes["node_small"] == 10)
    check("tailles : _node_dir_size cohérent avec le rapport",
          sync_mod._node_dir_size(big) == sizes["node_big"])

    many = [(f"node_{i:02d}", i) for i in range(7)]
    msg = sync_mod._archive_too_big_message(999 * 1024 * 1024, many)
    check("tailles : top 5 seulement dans le message",
          msg is not None and msg.count("     - ") == 5)


def _test_archive_guards():
    """Garde-fou par fichier et abandon propre si l'archive est trop grosse."""
    import tarfile

    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())
    node = _make_node(root, "node_ok", files=("__init__.py", "sub/mod.py"))
    (node / "big.bin").write_bytes(b"h" * 10)  # extension lourde → exclue
    oversized = node / "payload.dat"
    with open(oversized, "wb") as f:  # fichier sparse, instantané
        f.truncate(sync_mod.MAX_FILE_MB * 1024 * 1024 + 1)

    archive = Path(tempfile.mkdtemp()) / "nodes.tar.gz"
    stats = sync_mod._build_custom_nodes_archive([node], archive)
    check("archive : fichier > MAX_FILE_MB ignoré et compté",
          stats["skipped_large"] == 1)
    check("archive : taille retournée = taille du fichier",
          stats["size"] == archive.stat().st_size and stats["size"] > 0)

    with tarfile.open(archive, mode="r:gz") as tar:
        names = set(tar.getnames())
        members = tar.getmembers()
    check("archive : contenu filtré (lourds exclus)",
          {"node_ok/__init__.py", "node_ok/sub/mod.py"} <= names
          and "node_ok/big.bin" not in names
          and "node_ok/payload.dat" not in names)
    check("archive : la validation de membres accepte l'archive produite",
          sync_mod._validate_tar_members(
              members, str(Path(tempfile.mkdtemp()) / ".staging")) is None)

    heavy = sync_mod.MAX_ARCHIVE_MB * 1024 * 1024
    msg = sync_mod._archive_too_big_message(heavy + 1, [("a", 100 * 1024 * 1024)])
    check("garde-fou : archive au-dessus du seuil → message, pas d'exception",
          isinstance(msg, str) and "trop volumineuse" in msg)
    check("garde-fou : message explicite (taille + maximum)",
          "(maximum 500 MB)" in msg)
    check("garde-fou : conseil + nodes les plus lourds listés",
          "modèles ou de caches" in msg and "- a : 100 MB" in msg)
    check("garde-fou : taille à la limite → aucun message",
          sync_mod._archive_too_big_message(heavy) is None)
    check("garde-fou : jamais d'exception quel que soit le volume",
          sync_mod._archive_too_big_message(10 ** 15, []) is not None)

    check("résumé : liste tronquée à 10 + « et N autres »",
          sync_mod._format_node_list([f"n{i}" for i in range(12)])
          .endswith("… et 2 autre(s)")
          and sync_mod._format_node_list(["a", "b"]) == "a, b")


# ─────────────────────────── Tests extraction AST ──────────────────────────

def _test_extract_class_mappings():
    spec = importlib.util.spec_from_file_location("comfych", str(PROJECT_ROOT / "__init__.py"))
    comfych = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(comfych)

    tmp = Path(tempfile.mkdtemp())
    good = tmp / "good.py"
    good.write_text(
        'NODE_CLASS_MAPPINGS = {"MyNode": MyNode, "OtherNode": OtherNode}\n'
        '# "FakeNode" dans un commentaire ne doit pas matcher\n'
        'SOME_OTHER = {"NotANode": 1}\n'
    )
    mappings = comfych._extract_class_mappings(good)
    check("AST : clés réelles extraites", mappings == {"MyNode", "OtherNode"})
    check("AST : commentaire ignoré", "FakeNode" not in mappings)
    check("AST : dict non-NODE_CLASS_MAPPINGS ignoré", "NotANode" not in mappings)

    bad = tmp / "bad.py"
    bad.write_text("ceci n'est pas du python valide {{{")
    check("AST : fichier invalide → set vide", comfych._extract_class_mappings(bad) == set())


# ─────────────────────────── Main ──────────────────────────────────────────

def main():
    asyncio.run(_test_concurrent_same_id())
    _test_sequential_same_id()
    _test_different_ids()
    _test_no_request_id()
    _test_unknown_gpu()
    _test_cache_after_success()
    asyncio.run(_test_follower_timeout())
    asyncio.run(_test_leader_cancelled_wakes_followers())
    asyncio.run(_test_idempotency_finish_idempotent())
    _test_mask_config()
    _test_op_slot_state()
    asyncio.run(_test_op_reset_keeps_new_slot())
    asyncio.run(_test_op_timeout_kills_process())
    asyncio.run(_test_op_stream_success())
    asyncio.run(_test_op_log_framing())
    asyncio.run(_test_stream_heartbeat())
    _test_custom_nodes_manifest()
    _test_extract_volume_security()
    _test_update_local_config()
    _test_sanitize_save_path()
    _test_safe_model_filename()
    _test_tar_member_validation()
    _test_node_exclusions()
    _test_node_size_report()
    _test_archive_guards()
    _test_extract_class_mappings()

    print(f"\n{'='*50}")
    print(f"Résultat : {PASS}/{TOTAL} tests OK")
    sys.exit(0 if PASS == TOTAL else 1)


if __name__ == "__main__":
    main()
