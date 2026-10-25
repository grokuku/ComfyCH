"""Tests du gateway — sans dépendances (stubs fastapi/pydantic/modal).

Couvre :
- l'idempotence de /generate (pas de double exécution au retry)
- la sanitisation anti path traversal de /api/modal/save-local
- l'extraction AST des NODE_CLASS_MAPPINGS
- la sanitisation des noms de modèles et le rejet des liens tar (sync.py)
- le slot d'opération Modal (timeout, état exposé, libération sur slot fuité)
- l'encadrement des logs + heartbeat (le flux SSE ne reste jamais muet)
- le manifeste des custom nodes sur le volume (source de vérité des badges)
- le filtrage des custom nodes (workflows/LUTS/images de démo, garde-fou images)

Usage ::

    python3 tests/test_gateway.py
"""
import asyncio
import contextlib
import importlib.util
import io
import os
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
    def add_local_file(self, *a, **kw): return self
    def add_local_dir(self, *a, **kw): return self
    def apt_install(self, *pkgs): return self
    def pip_install(self, *pkgs): return self
    def pip_install_from_requirements(self, *a, **kw): return self
    def run_commands(self, *cmds, **kw): return self

class App:
    def __init__(self, name="", image=None, secrets=None):
        self.name = name; self.image = image; self.secrets = secrets; self.functions = {}
        self.cls_defs = {}
    def function(self, **kw):
        def deco(fn):
            self.functions[fn.__name__] = fn
            return fn
        return deco
    def cls(self, **kw):
        def deco(cls_):
            self.cls_defs[cls_.__name__] = cls_
            return cls_
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


def _passthrough_decorator(*a, **kw):
    """``@modal.enter(snap=True)`` / ``@modal.exit()`` / ``@modal.method()``."""
    def deco(fn):
        return fn
    return deco


modal.enter = _passthrough_decorator
modal.exit = _passthrough_decorator
modal.method = _passthrough_decorator
modal.concurrent = _passthrough_decorator
modal.asgi_app = _passthrough_decorator
sys.modules["modal"] = modal

httpx = types.ModuleType("httpx")


class _StubAsyncClient:
    """Client httpx factice — aucun worker n'émet de requête dans ces tests."""

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False
    async def post(self, *a, **kw): raise AssertionError("no HTTP call expected")
    async def get(self, *a, **kw): raise AssertionError("no HTTP call expected")


httpx.AsyncClient = _StubAsyncClient
sys.modules["httpx"] = httpx

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

    # Contenu lourd livré avec les nodes (mesuré : Workflows 375 MB, LUTS
    # 102 MB, images 17 MB chez vrgamedevgirl) : jamais du code.
    check("exclusions : dossiers de contenu exclus (workflows, luts, images…)",
          all(sync_mod._is_excluded_dir(d) for d in
              ["Workflows", "workflows", "workflow", "LUTS", "luts", "lut",
               "images", "image", "assets", "screenshots", "screenshot",
               "media", "videos", "video", "audio", "sounds", "fonts"]))
    check("exclusions : le dossier UI `web` et le code restent inclus",
          not any(sync_mod._is_excluded_dir(d) for d in
                  ["web", "js", "nodes", "src", "lib", "utils",
                   "image_utils", "assets_loader", "luts_reader", "nodes_api"]))
    check("exclusions : dossiers de contenu composés exclus (example_workflows…)",
          all(sync_mod._is_excluded_dir(d) for d in
              ["example_workflows", "example_workflows2", "workflows_v2",
               "previews_old", "my_screenshots"]))
    check("exclusions : un node nommé « …Workflows » n'est pas éliminé",
          sync_mod._node_skip_reason("ComfyUI-Workflows") is None
          and sync_mod._node_skip_reason("ComfyUI-example-nodes") is None
          and sync_mod._node_skip_reason("workflows") is not None)

    content_exts = ["lut.cube", "img.exr", "img.hdr", "x.tiff", "x.tif",
                    "x.bmp", "run.log", "x.tmp", "x.bak", "x.orig", "x.rej",
                    "demo.ipynb", "doc.pdf", "doc.docx", "sheet.xlsx",
                    "slides.pptx"]
    check("exclusions : extensions de contenu lourd rejetées",
          all(sync_mod._is_excluded_file(n) for n in content_exts))
    check("exclusions : `.ico` et `.json` conservés (favicon d'UI, presets)",
          not sync_mod._is_excluded_file("favicon.ico")
          and not sync_mod._is_excluded_file("presets.json"))


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


# ───────────── Tests garde-fou images volumineuses / résumé des exclusions ─

def _test_image_size_guard():
    """Une image > ``MAX_IMAGE_MB`` est écartée ; une icône d'UI est gardée."""
    import tarfile

    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())
    node = _make_node(root, "node_imgs", files=("__init__.py",))

    check("images : MAX_IMAGE_MB module-level exposé",
          sync_mod.MAX_IMAGE_MB == 2)
    check("images : décision par nom seul → conservée (taille inconnue)",
          sync_mod._file_exclusion_reason("shot.png") is None)

    limit = sync_mod.MAX_IMAGE_MB * 1024 * 1024
    check("images : 3 MB exclue",
          sync_mod._file_exclusion_reason("sample.png", 3 * 1024 * 1024)
          == sync_mod._SKIP_LARGE_IMAGE)
    check("images : 100 Ko conservée",
          sync_mod._file_exclusion_reason("icon.png", 100 * 1024) is None)
    check("images : exactement MAX_IMAGE_MB conservée",
          sync_mod._file_exclusion_reason("edge.jpg", limit) is None)
    check("images : garde-fou limité aux images",
          sync_mod._file_exclusion_reason("node.py", 3 * 1024 * 1024) is None)

    # Bout en bout : le tar contient l'icône, pas la capture.
    (node / "icon.png").write_bytes(b"i" * (100 * 1024))
    (node / "shot.png").write_bytes(b"s" * (3 * 1024 * 1024))
    archive = Path(tempfile.mkdtemp()) / "nodes.tar.gz"
    stats = sync_mod._build_custom_nodes_archive([node], archive)

    check("images : grande image comptée dans les stats",
          stats["skipped_large_images"] == 1 and stats["image_exts"] == [".png"])
    with tarfile.open(archive, mode="r:gz") as tar:
        names = set(tar.getnames())
    check("images : icône conservée, capture exclue",
          "node_imgs/icon.png" in names and "node_imgs/shot.png" not in names)

    check("résumé : nouveaux compteurs affichés",
          sync_mod._archive_exclusion_summary_lines(stats)[-1]
          == "  ⊘ 1 image(s) > 2 MB ignorée(s) (.png)")


def _test_archive_summary_lines():
    """Le résumé de packaging mentionne chaque catégorie d'exclusion."""
    sync_mod = _load_sync_module()

    check("résumé : rien à signaler → aucune ligne",
          sync_mod._archive_exclusion_summary_lines({}) == [])
    check("résumé : catégories vides → aucune ligne",
          sync_mod._archive_exclusion_summary_lines(
              {"skipped_demo_dirs": 0, "skipped_media": 0,
               "skipped_large_images": 0, "skipped_large": 0}) == [])

    lines = sync_mod._archive_exclusion_summary_lines({
        "skipped_demo_dirs": 3,
        "demo_dir_names": ["LUTS", "Workflows", "images"],
        "skipped_media": 3,
        "media_exts": [".mp4", ".psd", ".wav"],
        "skipped_large_images": 12,
        "image_exts": [".jpg", ".png"],
        "skipped_large": 1,
    })
    check("résumé : quatre lignes (dossiers, médias, images, gros fichiers)",
          len(lines) == 4)
    check("résumé : dossiers de démonstration nommés",
          lines[0] == "  ⊘ LUTS/, Workflows/, images/ exclu(s) "
                      "(3 dossier(s), contenu de démonstration)")
    check("résumé : médias listés avec leurs extensions",
          lines[1] == "  ⊘ 3 fichier(s) média/démo ignoré(s) (.mp4 .psd .wav)")
    check("résumé : images volumineuses comptées",
          lines[2] == "  ⊘ 12 image(s) > 2 MB ignorée(s) (.jpg .png)")
    check("résumé : fichiers > MAX_FILE_MB signalés",
          lines[3] == f"⚠️ 1 fichier(s) > {sync_mod.MAX_FILE_MB} MB ignoré(s)")


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


# ─────────── Tests cycle de vie du worker (ordre des custom nodes) ──────────

def _load_base_worker():
    """Importe workers/base_worker.py (modal + httpx stubbés en tête de fichier)."""
    spec = importlib.util.spec_from_file_location(
        "comfych_base_worker", str(PROJECT_ROOT / "workers" / "base_worker.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    """Process ComfyUI factice — aucun lancement réel."""

    def __init__(self, exit_code=None):
        self.terminated = False
        self.killed = False
        self.exit_code = exit_code

    def terminate(self): self.terminated = True
    def wait(self, timeout=None): return 0
    def kill(self): self.killed = True
    def poll(self): return self.exit_code


def _fake_subprocess(calls, label):
    """Module ``subprocess`` factice qui enregistre chaque Popen dans *calls*.

    ``kwargs`` conserve les mots-clés de chaque lancement (stdout/stderr),
    utilisés par les checks de redirection des logs ComfyUI. ``args`` conserve
    les arguments positionnels (commande de lancement de ComfyUI).
    """
    fake = types.ModuleType("fake_subprocess")

    class TimeoutExpired(Exception): pass

    fake.STDOUT = -2  # même valeur sentinelle que le vrai subprocess
    fake.kwargs = []
    fake.args = []

    def Popen(*args, **kwargs):
        calls.append(label)
        fake.args.append(args)
        fake.kwargs.append(kwargs)
        return _FakeProc()

    fake.TimeoutExpired = TimeoutExpired
    fake.Popen = Popen
    return fake


def _test_start_checkpoint_order():
    """ComfyUI ne scanne custom_nodes/ qu'au démarrage → liants d'abord."""
    bw = _load_base_worker()
    calls = []

    class W(bw.ComfyWorker):
        def _link_models_from_manifest(self): calls.append("link_models")
        def _sync_custom_nodes_from_volume(self):
            calls.append("custom_nodes")
            return 0
        def _restore_uploads_from_volume(self): calls.append("uploads")
        def _sync_user_settings_from_volume(self): calls.append("user_settings")

    real_subprocess, real_wait = bw.subprocess, bw.wait_for_port
    real_log, real_build = bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH
    bw.subprocess = _fake_subprocess(calls, "popen")
    bw.wait_for_port = lambda port, timeout=60: calls.append(f"wait:{timeout}")
    bw.COMFY_LOG_PATH = Path(tempfile.mkdtemp()) / "comfyui.log"
    bw.WORKER_BUILD_ID_PATH = Path(tempfile.mkdtemp()) / "worker_build_id"
    try:
        worker = W()
        worker.start_checkpoint()
    finally:
        bw.subprocess, bw.wait_for_port = real_subprocess, real_wait
        bw.COMFY_LOG_PATH = real_log
        bw.WORKER_BUILD_ID_PATH = real_build

    helpers = ["link_models", "custom_nodes", "uploads", "user_settings"]
    check("start_checkpoint : les 4 helpers tournent avant Popen",
          calls[:4] == helpers and "popen" in calls)
    check("start_checkpoint : Popen avant l'attente du port",
          calls.index("popen") < calls.index("wait:300"))
    check("start_checkpoint : le process est mémorisé sur self.proc",
          isinstance(getattr(worker, "proc", None), _FakeProc))


def _test_worker_build_id():
    """Empreinte du code = détecteur de redéploiement (anti-snapshot périmé)."""
    bw = _load_base_worker()

    build_id = bw.WORKER_BUILD_ID
    check("WORKER_BUILD_ID : non vide", isinstance(build_id, str) and build_id != "")
    check("WORKER_BUILD_ID : 16 caractères hexadécimaux",
          len(build_id) == 16 and all(c in "0123456789abcdef" for c in build_id.lower()))
    check("WORKER_BUILD_ID : stable pour un même code",
          bw._compute_worker_build_id() == build_id or bool(
              os.environ.get("COMFYCH_BUILD_ID")))

    # ── start_checkpoint : l'empreinte est gravée avant tout le reste ──
    calls = []

    class W(bw.ComfyWorker):
        def _link_models_from_manifest(self):
            calls.append("link_models")
            written_at_first_helper.append(build_path.exists())
        def _sync_custom_nodes_from_volume(self): return 0
        def _restore_uploads_from_volume(self): calls.append("uploads")
        def _sync_user_settings_from_volume(self): calls.append("user_settings")

    tmp = Path(tempfile.mkdtemp())
    build_path = tmp / "nested" / "worker_build_id"
    written_at_first_helper: list[bool] = []
    real = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH)
    bw.subprocess = _fake_subprocess(calls, "popen")
    bw.wait_for_port = lambda port, timeout=60: calls.append(f"wait:{timeout}")
    bw.COMFY_LOG_PATH = tmp / "comfyui.log"
    bw.WORKER_BUILD_ID_PATH = build_path
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            W().start_checkpoint()
        check("start_checkpoint : empreinte écrite sur le volume",
              build_path.is_file() and build_path.read_text() == build_id)
        check("start_checkpoint : empreinte loguée", f"build id: {build_id}" in out.getvalue())
        check("start_checkpoint : empreinte écrite avant les helpers ("
              "sinon un snapshot interrompu resterait non identifié)",
              written_at_first_helper == [True])
    finally:
        bw.subprocess, bw.wait_for_port = real[0], real[1]
        bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH = real[2], real[3]

    # ── _launch_comfy : l'en-tête porte l'empreinte ──
    log_path = tmp / "launch.log"
    bw.COMFY_LOG_PATH = log_path
    bw.subprocess = _fake_subprocess([], "popen")
    try:
        bw.ComfyWorker()._launch_comfy()
    finally:
        bw.subprocess, bw.COMFY_LOG_PATH = real[0], real[2]
    check("_launch_comfy : build_id dans l'en-tête du log",
          f"build_id: {build_id}" in log_path.read_text())

    # ── start_restore : snapshot identique → pas de redémarrage ──
    def _restore(snapshot_id, new_nodes=0, proc_exit_code=None):
        calls = []

        class R(bw.ComfyWorker):
            def _link_models_from_manifest(self): calls.append("link_models")
            def _restore_uploads_from_volume(self): calls.append("uploads")
            def _sync_user_settings_from_volume(self): calls.append("user_settings")
            def _sync_custom_nodes_from_volume(self):
                calls.append("custom_nodes")
                return new_nodes
            def _shutdown_comfy(self, timeout=10): calls.append("shutdown")
            def _launch_comfy(self):
                calls.append("popen")
                return _FakeProc(exit_code=proc_exit_code)

        path = Path(tempfile.mkdtemp()) / "worker_build_id"
        if snapshot_id is not None:
            path.write_text(snapshot_id)
        saved = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH)
        bw.subprocess = _fake_subprocess(calls, "popen")
        bw.wait_for_port = lambda port, timeout=60: calls.append(f"wait:{timeout}")
        bw.COMFY_LOG_PATH = Path(tempfile.mkdtemp()) / "comfyui.log"
        bw.WORKER_BUILD_ID_PATH = path
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                R().start_restore()
        finally:
            bw.subprocess, bw.wait_for_port = saved[0], saved[1]
            bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH = saved[2], saved[3]
        return calls, path, out.getvalue()

    calls, path, _ = _restore(snapshot_id=build_id)
    check("start_restore : snapshot à jour → aucun redémarrage",
          "shutdown" not in calls and "popen" not in calls and "wait:30" in calls)

    calls, path, out = _restore(snapshot_id="deadbeefdeadbeef")
    check("start_restore : empreinte différente → process du snapshot tué",
          "shutdown" in calls)
    check("start_restore : empreinte différente → ComfyUI relancé par le nouveau code",
          "popen" in calls and calls[-1] == "wait:300")
    check("start_restore : empreinte différente → le port n'est pas testé ("
          "il répondrait avec l'ancien ComfyUI)",
          "wait:30" not in calls)
    check("start_restore : empreinte réécrite après relance",
          path.read_text() == build_id)
    check("start_restore : redéploiement signalé dans les logs",
          "Code du worker modifié depuis le snapshot" in out)

    calls, path, out = _restore(snapshot_id=None)  # snapshot d'une version antérieure
    check("start_restore : snapshot sans empreinte → considéré comme périmé",
          "shutdown" in calls and "popen" in calls)
    check("start_restore : snapshot sans empreinte → avertissement logué",
          "snapshot=inconnu" in out)

    # Process relancé mort-né (port 8000 déjà tenu par l'ancien ComfyUI du
    # snapshot) : on ne doit surtout pas graver l'empreinte — le code qui
    # tourne reste l'ancien.
    calls, path, out = _restore(snapshot_id="deadbeefdeadbeef", proc_exit_code=1)
    check("start_restore : relance morte-née → avertissement port squatté",
          "port 8000 est probablement tenu" in out)
    check("start_restore : relance morte-née → empreinte NON gravée",
          not path.exists() or path.read_text() != build_id)


def _test_custom_nodes_sync():
    """Comptage des liens + logs de diagnostic (volume absent / vide / plein)."""
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())
    real_vol, real_dir = bw.CUSTOM_NODES_VOLUME_PATH, bw.COMFY_CUSTOM_NODES_DIR
    bw.COMFY_CUSTOM_NODES_DIR = tmp / "comfy_custom_nodes"

    def _sync():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = bw.ComfyWorker()._sync_custom_nodes_from_volume()
        return result, buf.getvalue()

    try:
        bw.CUSTOM_NODES_VOLUME_PATH = tmp / "missing"
        rc, out = _sync()
        check("custom nodes : volume absent → 0", rc == 0)
        check("custom nodes : volume absent → avertissement explicite",
              "NON MONTÉ" in out and "modal deploy" in out)
        check("custom nodes : volume absent → rien créé",
              not bw.COMFY_CUSTOM_NODES_DIR.exists())

        empty = tmp / "empty"
        empty.mkdir()
        bw.CUSTOM_NODES_VOLUME_PATH = empty
        rc, out = _sync()
        check("custom nodes : volume vide → 0", rc == 0)
        check("custom nodes : volume vide → log dédié", "est vide" in out)

        vol = tmp / "nodes"
        _make_node(vol, "node_a")
        _make_node(vol, "node_b")
        bw.CUSTOM_NODES_VOLUME_PATH = vol
        rc, out = _sync()
        check("custom nodes : 2 nouveaux liens comptés", rc == 2)
        check("custom nodes : noms des nodes loggués",
              "node_a" in out and "node_b" in out)
        try:
            check("custom nodes : symlinks créés dans ComfyUI",
                  (bw.COMFY_CUSTOM_NODES_DIR / "node_a").is_symlink()
                  and (bw.COMFY_CUSTOM_NODES_DIR / "node_b").is_symlink())
        except OSError:
            print("ℹ️  symlinks non supportés ici — test sauté")

        rc, out = _sync()
        check("custom nodes : déjà présents → 0 nouveau", rc == 0)
        check("custom nodes : log lié(s)/déjà présent(s)",
              "0 lié(s), 2 déjà présent(s)" in out)

        # ── Deps des nodes : installées AU BUILD, plus au démarrage ──
        # 30 × ``pip install -r`` ≈ 141-187 s de préparation par container :
        # l'étape ne doit plus tourner que sur demande (COMFYCH_INSTALL_DEPS=1).
        vol2 = tmp / "nodes_deps"
        _make_node(vol2, "node_req", files=("requirements.txt", "install.py"))
        bw.CUSTOM_NODES_VOLUME_PATH = vol2
        bw.COMFY_CUSTOM_NODES_DIR = tmp / "comfy_deps"
        calls = []
        real_req = bw.ComfyWorker._install_node_requirements
        real_inst = bw.ComfyWorker._run_node_install_script
        real_flag = bw.INSTALL_NODE_DEPS_AT_STARTUP
        bw.ComfyWorker._install_node_requirements = lambda self, p: calls.append("pip")
        bw.ComfyWorker._run_node_install_script = lambda self, p: calls.append("install")
        try:
            bw.INSTALL_NODE_DEPS_AT_STARTUP = False
            rc, out = _sync()
            check("custom nodes : deps NON réinstallées au démarrage (défaut)", calls == [])
            check("custom nodes : rc inchangé malgré l'étape ignorée", rc == 1)
            check("custom nodes : étape deps ignorée → log explicite",
                  "COMFYCH_INSTALL_DEPS=0" in out)

            bw.INSTALL_NODE_DEPS_AT_STARTUP = True
            rc, out = _sync()
            check("custom nodes : COMFYCH_INSTALL_DEPS=1 rétablit pip + install.py",
                  calls == ["pip", "install"])
            check("custom nodes : étape deps rétablie → pas de log d'ignorance",
                  "COMFYCH_INSTALL_DEPS=0" not in out)
        finally:
            bw.INSTALL_NODE_DEPS_AT_STARTUP = real_flag
            bw.ComfyWorker._install_node_requirements = real_req
            bw.ComfyWorker._run_node_install_script = real_inst

        # ── Chronométrage par node : top 5 seulement si la préparation coûte ──
        bw.CUSTOM_NODES_VOLUME_PATH = vol
        bw.COMFY_CUSTOM_NODES_DIR = tmp / "comfy_timed"
        sync_log = tmp / "logs" / "comfyui.log"
        real_log = bw.COMFY_LOG_PATH
        bw.COMFY_LOG_PATH = sync_log
        try:
            rc, out = _sync()
            check("custom nodes : préparation rapide → aucun top de lenteurs",
                  not sync_log.exists())
            check("custom nodes : durée par node mesurée sans erreur", rc == 2)
        finally:
            bw.COMFY_LOG_PATH = real_log
    finally:
        bw.CUSTOM_NODES_VOLUME_PATH, bw.COMFY_CUSTOM_NODES_DIR = real_vol, real_dir


def _test_start_restore_restart():
    """Redémarrage de ComfyUI uniquement si de nouveaux nodes ont été liés."""
    bw = _load_base_worker()

    def _run(new_nodes, port_ok=True, snapshot_build_id=bw.WORKER_BUILD_ID):
        calls = []

        class W(bw.ComfyWorker):
            def _link_models_from_manifest(self): calls.append("link_models")
            def _restore_uploads_from_volume(self): calls.append("uploads")
            def _sync_user_settings_from_volume(self): calls.append("user_settings")
            def _sync_custom_nodes_from_volume(self):
                calls.append("custom_nodes")
                return new_nodes
            def _shutdown_comfy(self, timeout=10): calls.append("shutdown")

        def fake_wait(port, timeout=60):
            calls.append(f"wait:{timeout}")
            if not port_ok and timeout == 30:
                raise TimeoutError("snapshot KO")

        real_subprocess, real_wait = bw.subprocess, bw.wait_for_port
        real_log, real_build = bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH
        bw.subprocess = _fake_subprocess(calls, "popen")
        bw.wait_for_port = fake_wait
        bw.COMFY_LOG_PATH = Path(tempfile.mkdtemp()) / "comfyui.log"
        build_path = Path(tempfile.mkdtemp()) / "worker_build_id"
        if snapshot_build_id is not None:
            build_path.write_text(snapshot_build_id)
        bw.WORKER_BUILD_ID_PATH = build_path
        try:
            W().start_restore()
        finally:
            bw.subprocess, bw.wait_for_port = real_subprocess, real_wait
            bw.COMFY_LOG_PATH = real_log
            bw.WORKER_BUILD_ID_PATH = real_build
        return calls, build_path

    calls, _ = _run(new_nodes=2)
    check("start_restore : symlinks faits avant le test du port",
          calls.index("custom_nodes") < calls.index("wait:30"))
    check("start_restore : nouveaux nodes → ComfyUI arrêté", "shutdown" in calls)
    check("start_restore : nouveaux nodes → relance + attente du port",
          "popen" in calls and calls[-1] == "wait:300")

    calls, _ = _run(new_nodes=0)
    check("start_restore : aucun nouveau node → pas de redémarrage",
          "shutdown" not in calls and "popen" not in calls)
    check("start_restore : snapshot sain → pas d'attente de relance",
          "wait:300" not in calls)

    calls, _ = _run(new_nodes=0, port_ok=False)
    check("start_restore : ComfyUI muet → cold restart",
          "popen" in calls and "wait:300" in calls and "shutdown" not in calls)


def _test_shutdown_comfy():
    """terminate → wait, kill en secours, no-op sans process."""
    bw = _load_base_worker()
    worker = bw.ComfyWorker()

    proc = _FakeProc()
    worker.proc = proc
    worker._shutdown_comfy()
    check("_shutdown_comfy : terminate + oubli du process",
          proc.terminated and not proc.killed and worker.proc is None)

    class HangingProc(_FakeProc):
        def wait(self, timeout=None):
            raise bw.subprocess.TimeoutExpired("comfy launch", timeout)

    hanging = HangingProc()
    worker.proc = hanging
    worker._shutdown_comfy(timeout=0.1)
    check("_shutdown_comfy : kill si arrêt trop long", hanging.killed)

    worker.proc = None
    worker._shutdown_comfy()
    check("_shutdown_comfy : sans process → no-op", True)

    proc = _FakeProc()
    worker.proc = proc
    worker.cleanup()
    check("cleanup : passe par _shutdown_comfy",
          proc.terminated and worker.proc is None)


# ───────── Tests du chronométrage du démarrage (wait_for_port, timing) ─────

class _FakeSocket:
    """``socket.create_connection`` factice piloté par un scénario de ports."""
    def __init__(self, fail_times):
        self.calls = 0
        self.fail_times = fail_times

    def create_connection(self, address, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionRefusedError("port fermé")
        return self  # objet-contexte minimal : c'est le "succès"

    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _catch_timeout(fn):
    """Renvoie l'exception levée par ``fn()`` (``None`` si aucune) — stdout muet."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except BaseException as e:  # noqa: BLE001 — l'assertion vérifie le type
        return e
    return None


def _test_wait_for_port():
    """wait_for_port : durée retournée, progression loguée, TimeoutError à l'échéance."""
    bw = _load_base_worker()
    port = 59999  # aucun serveur réel : seuls les OSError comptent

    # ── Port qui répond (monkeypatch de socket.create_connection) ──
    real_socket, real_sleep = bw.socket, time.sleep
    fake = _FakeSocket(fail_times=0)
    bw.socket = types.SimpleNamespace(create_connection=fake.create_connection)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            elapsed = bw.wait_for_port(port, timeout=5)
    finally:
        bw.socket = real_socket
    check("wait_for_port : retourne un float >= 0 quand le port répond",
          isinstance(elapsed, float) and elapsed >= 0)
    check("wait_for_port : port joignable → un seul essai", fake.calls == 1)
    check("wait_for_port : disponibilité loguée",
          f"port {port} prêt" in buf.getvalue())

    # ── Port muet : timeout court → TimeoutError, jamais d'attente infinie ──
    # ``progress_every=0.3`` : la progression est logguée à cette période, donc
    # il faut au moins une période dans le budget de ``timeout`` pour qu'une
    # ligne apparaisse (la période par défaut est de 5 s).
    real_socket = bw.socket
    bw.socket = types.SimpleNamespace(
        create_connection=lambda address, timeout=None: (_ for _ in ()).throw(
            ConnectionRefusedError("port fermé")
        )
    )
    started = time.time()
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            bw.wait_for_port(port, timeout=1.2, progress_every=0.3)
        raised = None
    except BaseException as e:  # noqa: BLE001 — on vérifie le type ensuite
        raised = e
    finally:
        bw.socket = real_socket
    check("wait_for_port : TimeoutError quand le port ne répond pas",
          isinstance(raised, TimeoutError))
    check("wait_for_port : timeout respecté (~1.2 s, pas 60 s)",
          time.time() - started < 5)
    check("wait_for_port : progression loguée pendant l'attente",
          f"en attente du port {port}…" in buf.getvalue())

    # ── Progression périodique : une ligne toutes ~0.3 s ici ──
    real_socket = bw.socket
    bw.socket = types.SimpleNamespace(
        create_connection=lambda address, timeout=None: (_ for _ in ()).throw(
            OSError("port fermé")
        )
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            try:
                bw.wait_for_port(port, timeout=1.5, progress_every=0.3)
            except TimeoutError:
                pass
    finally:
        bw.socket = real_socket
    check("wait_for_port : progress_every borne la fréquence des lignes",
          buf.getvalue().count("en attente du port") >= 2)

    # ── timeout/progress_every dégénérés : jamais d'attente infinie ──
    real_socket = bw.socket
    bw.socket = types.SimpleNamespace(
        create_connection=lambda address, timeout=None: (_ for _ in ()).throw(
            OSError("port fermé")
        )
    )
    started = time.time()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                bw.wait_for_port(port, timeout=0.4, progress_every=0)
            except TimeoutError:
                pass
    finally:
        bw.socket = real_socket
    check("wait_for_port : progress_every=0 ne bloque pas ni ne divise par zéro",
          time.time() - started < 5)

    # ── wait_for_port réel (aucun fake) : socket local, échéance respectée ──
    check("wait_for_port : TimeoutError sur un port réellement fermé",
          isinstance(_catch_timeout(lambda: bw.wait_for_port(port, timeout=1.0)),
                     TimeoutError))


def _test_startup_timing_blocks():
    """Blocs de timing : phases mesurées, mode explicite, TOTAL, log + stdout."""
    bw = _load_base_worker()

    # ── Bloc de timing isolé (append + stdout + total retourné) ──
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "nested" / "comfyui.log"
    phases = [
        ("préparation (volumes, symlinks, deps)", 12.4),
        ("lancement du process ComfyUI", 0.0),
        ("attente du port 8000 (ready)", 47.3),
    ]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        total = bw.write_timing_block(
            "démarrage (checkpoint)", phases,
            mode=bw.STARTUP_MODE_CHECKPOINT, path=log_path,
        )
    check("write_timing_block : retourne le TOTAL", abs(total - 59.7) < 0.05)
    written = log_path.read_text() if log_path.is_file() else ""
    check("write_timing_block : bloc écrit dans le log (dossier créé)",
          "timing démarrage (checkpoint)" in written)
    check("write_timing_block : bloc dupliqué sur stdout du worker",
          "TOTAL" in buf.getvalue() and "préparation" in buf.getvalue())
    check("write_timing_block : phases attendues dans le bloc",
          "préparation" in written and "attente du port 8000" in written
          and "TOTAL" in written)
    check("write_timing_block : mode affiché sur sa propre ligne",
          f"mode{'':<34} : " in written and bw.STARTUP_MODE_CHECKPOINT in written)
    check("write_timing_block : format aligné « label : 12.4s »",
          f"  {'préparation (volumes, symlinks, deps)':<38} :  12.4s" in written)
    check("write_timing_block : TOTAL aligné",
          f"  {'TOTAL':<38} :  59.7s" in written)
    check("write_timing_block : séparateur de 44 ─",
          ("  " + "─" * 44) in written)

    # ── Top des nodes les plus lents (garde-fou de la préparation) ──
    slow_log = tmp / "slow.log"
    with contextlib.redirect_stdout(io.StringIO()):
        slow_total = bw.write_slowest_nodes_block(
            [("comfyui-easy-use", 2.1), ("RES4LYF", 1.8), ("node_a", 0.2),
             ("node_b", 0.1), ("node_c", 0.05), ("node_lent", 9.0)],
            path=slow_log,
        )
    slow_written = slow_log.read_text() if slow_log.is_file() else ""
    slow_lines = [
        line for line in slow_written.splitlines()
        if " : " in line and "TOTAL" not in line
    ]
    check("write_slowest_nodes_block : total renvoyé", abs(slow_total - 13.25) < 0.01)
    check("write_slowest_nodes_block : bloc écrit dans le log",
          "top 5 nodes les plus lents (préparation)" in slow_written)
    check("write_slowest_nodes_block : 5 nodes au maximum", len(slow_lines) == 5)
    check("write_slowest_nodes_block : les plus lents listés en premier",
          slow_written.index("node_lent") < slow_written.index("comfyui-easy-use"))
    check("write_slowest_nodes_block : node le plus lent absent du top 5 exclu",
          "node_c" not in slow_written)

    fast_log = tmp / "fast.log"
    with contextlib.redirect_stdout(io.StringIO()):
        fast_total = bw.write_slowest_nodes_block(
            [("node_a", 0.01), ("node_b", 0.02)], path=fast_log
        )
    check("write_slowest_nodes_block : sous le seuil → rien d'écrit",
          abs(fast_total - 0.03) < 0.001 and not fast_log.exists())

    slow_blocker = tmp / "fichier-bloquant"
    slow_blocker.write_text("x")
    with contextlib.redirect_stdout(io.StringIO()) as slow_buf:
        blocked_total = bw.write_slowest_nodes_block(
            [("node_lent", 42.0)], path=slow_blocker / "x"
        )
    check("write_slowest_nodes_block : OSError toléré (total renvoyé)",
          abs(blocked_total - 42.0) < 0.01
          and "top des nodes lents non écrit" in slow_buf.getvalue())

    # Un log non inscriptible ne doit jamais faire échouer un démarrage.
    # ``blocker`` est un FICHIER : il ne peut pas servir de dossier parent à
    # ``blocker / "x"``, donc ``mkdir`` échoue avec un OSError.
    blocker = tmp / "pas-un-dossier"
    blocker.write_text("x")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        total = bw.write_timing_block(
            "démarrage (checkpoint)", phases, path=blocker / "x"
        )
    check("write_timing_block : OSError toléré (total renvoyé, avertissement)",
          abs(total - 59.7) < 0.05 and "timing non écrit" in buf.getvalue())
    check("write_timing_block : phases non numériques → 0.0 (jamais d'exception)",
          bw.write_timing_block("t", [("x", None), ("y", "boom")], path=tmp) == 0.0)

    # ── start_checkpoint : 3 phases + TOTAL dans le log et sur stdout ──
    class C(bw.ComfyWorker):
        def _link_models_from_manifest(self): pass
        def _sync_custom_nodes_from_volume(self): return 0
        def _restore_uploads_from_volume(self): pass
        def _sync_user_settings_from_volume(self): pass

    checkpoint_log = Path(tempfile.mkdtemp()) / "comfyui.log"
    real = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH)
    bw.subprocess = _fake_subprocess([], "popen")
    bw.wait_for_port = lambda port, timeout=60, progress_every=5.0: 1.0
    bw.COMFY_LOG_PATH = checkpoint_log
    bw.WORKER_BUILD_ID_PATH = Path(tempfile.mkdtemp()) / "worker_build_id"
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            C().start_checkpoint()
    finally:
        bw.subprocess, bw.wait_for_port = real[0], real[1]
        bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH = real[2], real[3]
    block = checkpoint_log.read_text() if checkpoint_log.is_file() else ""
    check("start_checkpoint : bloc de timing écrit dans le log",
          "timing démarrage (checkpoint)" in block)
    check("start_checkpoint : bloc de timing sur stdout",
          "timing démarrage (checkpoint)" in out.getvalue())
    check("start_checkpoint : 3 phases + TOTAL",
          all(k in block for k in (
              "préparation (volumes, symlinks, deps)",
              "lancement du process ComfyUI",
              "attente du port 8000 (ready)",
              "TOTAL",
          )))
    check("start_checkpoint : attente du port mesurée via wait_for_port",
          f"  {'attente du port 8000 (ready)':<38} :   1.0s" in block)
    check("start_checkpoint : durée totale résumée sur stdout",
          "Démarrage ComfyUI terminé en" in out.getvalue())

    # ── start_restore : le mode retenu est explicite dans le bloc ──
    def _restore_block(snapshot_id, port_ok=True, new_nodes=0):
        log = Path(tempfile.mkdtemp()) / "comfyui.log"
        build = Path(tempfile.mkdtemp()) / "worker_build_id"
        if snapshot_id is not None:
            build.write_text(snapshot_id)

        class R(bw.ComfyWorker):
            def _link_models_from_manifest(self): pass
            def _restore_uploads_from_volume(self): pass
            def _sync_user_settings_from_volume(self): pass
            def _sync_custom_nodes_from_volume(self): return new_nodes
            def _launch_comfy(self): return _FakeProc()

        def fake_wait(port, timeout=60, progress_every=5.0):
            if timeout == 30 and not port_ok:
                raise TimeoutError("snapshot muet")
            return 0.5

        saved = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH)
        bw.subprocess = _fake_subprocess([], "popen")
        bw.wait_for_port = fake_wait
        bw.COMFY_LOG_PATH = log
        bw.WORKER_BUILD_ID_PATH = build
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                R().start_restore()
        finally:
            bw.subprocess, bw.wait_for_port = saved[0], saved[1]
            bw.COMFY_LOG_PATH, bw.WORKER_BUILD_ID_PATH = saved[2], saved[3]
        return (log.read_text() if log.is_file() else ""), out.getvalue()

    block, out = _restore_block(bw.WORKER_BUILD_ID)
    check("start_restore : bloc « timing restauration (snapshot) »",
          "timing restauration (snapshot)" in block)
    check("start_restore : snapshot réutilisé → « restauré depuis snapshot »",
          "restauré depuis snapshot" in block)
    check("start_restore : 4 phases mesurées + TOTAL",
          all(k in block for k in (
              "vérification build id",
              "préparation (volumes, symlinks, deps)",
              "arrêt éventuel de ComfyUI",
              "attente/relance ComfyUI",
              "TOTAL",
          )))

    block, _ = _restore_block("deadbeefdeadbeef")
    check("start_restore : code modifié → « redémarrage forcé (code modifié) »",
          bw.STARTUP_MODE_CODE_CHANGED in block
          and "code modifié" in block)

    block, _ = _restore_block(bw.WORKER_BUILD_ID, new_nodes=1)
    check("start_restore : nouveaux nodes → « nouveaux custom nodes » dans le bloc",
          "nouveaux custom nodes" in block)

    block, _ = _restore_block(bw.WORKER_BUILD_ID, port_ok=False)
    check("start_restore : port muet → « cold start » dans le bloc",
          "cold start" in block)

    # ── _timed_shutdown : le coût de l'arrêt est réellement mesuré ──
    worker = bw.ComfyWorker()
    worker.proc = _FakeProc()
    check("_timed_shutdown : renvoie une durée >= 0",
          isinstance(worker._timed_shutdown(), float) and worker.proc is None)
    check("_timed_shutdown : sans process → 0.0",
          bw.ComfyWorker()._timed_shutdown() == 0.0)


# ─────────── Tests chronométrage du démarrage de ComfyUI ────────────────

def _test_wait_for_port_timing():
    """``wait_for_port`` mesure l'attente, la renvoie et logue la progression."""
    bw = _load_base_worker()

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    def _open(address, timeout=1):
        return _Conn()

    def _never(address, timeout=1):
        raise OSError("connexion refusée")

    real_socket = bw.socket
    try:
        # ── Port déjà ouvert : mesure ~0 s, mais bien un float >= 0 ──
        bw.socket = SimpleNamespace(create_connection=_open)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            elapsed = bw.wait_for_port(8000, timeout=5)
        check("wait_for_port : port ouvert → float >= 0",
              isinstance(elapsed, float) and elapsed >= 0)
        check("wait_for_port : port ouvert → pas d'attente artificielle",
              elapsed < 1.0)
        check("wait_for_port : durée loguée sur la stdout du worker",
              "port 8000 prêt après" in out.getvalue())

        # ── Port muet : TimeoutError, durée mesurée dans le message ──
        bw.socket = SimpleNamespace(create_connection=_never)
        started = time.time()
        try:
            bw.wait_for_port(8000, timeout=0.3)
            error = None
        except TimeoutError as e:
            error = e
        took = time.time() - started
        check("wait_for_port : port muet → TimeoutError", isinstance(error, TimeoutError))
        check("wait_for_port : port muet → message avec la durée mesurée",
              "after " in str(error) and str(error).endswith("s"))
        check("wait_for_port : port muet → le timeout court est respecté",
              took < 3)

        # ── Logs de progression : un démarrage long n'est pas un silence ──
        # Période courte : le budget ci-dessus ne couvre qu'une fraction de la
        # période par défaut (5 s), donc aucune ligne n'apparaîtrait.
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                bw.wait_for_port(8000, timeout=0.5, progress_every=0.1)
            except TimeoutError:
                pass
        check("wait_for_port : progression loguée pendant l'attente",
              "en attente du port 8000" in out.getvalue())
    finally:
        bw.socket = real_socket


def _test_startup_timing_block():
    """Le bloc « timing » du démarrage est écrit dans le log, phases nommées."""
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "comfyui.log"

    class W(bw.ComfyWorker):
        def _link_models_from_manifest(self): pass
        def _sync_custom_nodes_from_volume(self): return 0
        def _restore_uploads_from_volume(self): pass
        def _sync_user_settings_from_volume(self): pass

    commits = []
    real = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH,
            bw.WORKER_BUILD_ID_PATH, bw.commit_log_volume)
    bw.subprocess = _fake_subprocess([], "popen")
    bw.wait_for_port = lambda port, timeout=60: 12.5
    bw.COMFY_LOG_PATH = log
    bw.WORKER_BUILD_ID_PATH = tmp / "worker_build_id"
    bw.commit_log_volume = lambda: commits.append(True)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            W().start_checkpoint()
    finally:
        (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH,
         bw.WORKER_BUILD_ID_PATH, bw.commit_log_volume) = real

    body = log.read_text()
    check("timing : bloc écrit dans le log ComfyUI",
          "timing démarrage (checkpoint)" in body)
    check("timing : phases nommées (préparation, lancement, attente du port)",
          all(label in body for label in (
              "préparation (volumes, symlinks, deps)",
              "lancement du process ComfyUI",
              "attente du port 8000 (ready)",
              "TOTAL")))
    check("timing : la phase d'attente reprend la mesure de wait_for_port",
          bw.format_timing_line("attente du port 8000 (ready)", 12.5) in body)
    check("timing : mode affiché (snapshot en préparation)",
          f"mode" in body and bw.STARTUP_MODE_CHECKPOINT in body)
    check("timing : bloc commité sur le volume dans la foulée", len(commits) >= 1)
    check("timing : durée totale annoncée sur la stdout du worker",
          "Démarrage ComfyUI terminé en 12.5s" in out.getvalue())

    # ── Les phases sont bien ordonnées et le TOTAL est leur somme ──
    block = [ln for ln in body.splitlines() if ln.strip()]
    total_line = next((ln for ln in block if "TOTAL" in ln), "")
    check("timing : TOTAL présent sous forme de durée",
          total_line.strip().endswith("s"))


def _test_restore_timing_mode():
    """Le timing de restauration dit si le snapshot a servi ou non."""
    bw = _load_base_worker()

    def _run(snapshot_id, new_nodes=0, port_ok=True):
        calls = []

        class R(bw.ComfyWorker):
            def _link_models_from_manifest(self): pass
            def _restore_uploads_from_volume(self): pass
            def _sync_user_settings_from_volume(self): pass
            def _sync_custom_nodes_from_volume(self): return new_nodes
            def _shutdown_comfy(self, timeout=10): calls.append("shutdown")

        def fake_wait(port, timeout=60):
            calls.append(f"wait:{timeout}")
            if not port_ok and timeout == 30:
                raise TimeoutError("port muet")
            return 4.0 if timeout == 30 else 41.2

        tmp = Path(tempfile.mkdtemp())
        log = tmp / "comfyui.log"
        build_path = tmp / "worker_build_id"
        if snapshot_id is not None:
            build_path.write_text(snapshot_id)
        real = (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH,
                bw.WORKER_BUILD_ID_PATH)
        bw.subprocess = _fake_subprocess(calls, "popen")
        bw.wait_for_port = fake_wait
        bw.COMFY_LOG_PATH = log
        bw.WORKER_BUILD_ID_PATH = build_path
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                R().start_restore()
        finally:
            (bw.subprocess, bw.wait_for_port, bw.COMFY_LOG_PATH,
             bw.WORKER_BUILD_ID_PATH) = real
        return log.read_text(), out.getvalue()

    body, out = _run(bw.WORKER_BUILD_ID)
    check("timing restauration : toutes les phases présentes",
          all(label in body for label in (
              "timing restauration (snapshot)",
              "vérification build id",
              "arrêt éventuel de ComfyUI",
              "préparation (volumes, symlinks, deps)",
              "attente/relance ComfyUI",
              "TOTAL")))
    check("timing restauration : snapshot réutilisé → mode explicite",
          bw.STARTUP_MODE_SNAPSHOT in body and "restauré depuis snapshot" in body)
    check("timing restauration : snapshot → attente du port 30 s mesurée",
          bw.format_timing_line("attente/relance ComfyUI", 4.0) in body)
    check("timing restauration : mode aussi sur la stdout du worker",
          f"({bw.STARTUP_MODE_SNAPSHOT})" in out)

    body, out = _run("deadbeefdeadbeef")
    check("timing restauration : code modifié → mode redémarrage",
          bw.STARTUP_MODE_CODE_CHANGED in body)
    check("timing restauration : redémarrage → attente de relance mesurée",
          bw.format_timing_line("attente/relance ComfyUI", 41.2) in body)

    body, _ = _run(bw.WORKER_BUILD_ID, new_nodes=2)
    check("timing restauration : nouveaux nodes → mode redémarrage",
          bw.STARTUP_MODE_NEW_NODES in body)

    body, out = _run(bw.WORKER_BUILD_ID, port_ok=False)
    check("timing restauration : cold start → mode explicite",
          bw.STARTUP_MODE_COLD in body)
    # Le cold start cumule les deux attentes : port muet (≈30 s) + relance.
    check("timing restauration : cold start → les deux attentes comptées",
          "attente/relance ComfyUI" in body
          and bw.STARTUP_MODE_COLD in out)


# ─────────── Tests capture des logs ComfyUI (diagnostic custom nodes) ──────

def _test_launch_comfy_redirect():
    """ComfyUI est lancé avec stdout/stderr redirigés vers COMFY_LOG_PATH."""
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())
    log_path = tmp / "nested" / "comfyui.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("ancienne sortie\n")  # doit être conservée (append)

    fake = _fake_subprocess([], "popen")
    real_subprocess, real_log = bw.subprocess, bw.COMFY_LOG_PATH
    bw.subprocess, bw.COMFY_LOG_PATH = fake, log_path
    try:
        proc1 = bw.ComfyWorker()._launch_comfy()
        proc2 = bw.ComfyWorker()._launch_comfy()
    finally:
        bw.subprocess, bw.COMFY_LOG_PATH = real_subprocess, real_log

    check("_launch_comfy : process retourné (à mémoriser sur self.proc)",
          isinstance(proc1, _FakeProc) and isinstance(proc2, _FakeProc))
    check("_launch_comfy : stdout redirigé vers COMFY_LOG_PATH",
          len(fake.kwargs) == 2
          and all(Path(kw["stdout"].name) == log_path for kw in fake.kwargs))
    check("_launch_comfy : log ouvert en append",
          all(kw["stdout"].mode == "a" for kw in fake.kwargs))
    check("_launch_comfy : stderr fusionné dans stdout",
          all(kw.get("stderr") == fake.STDOUT for kw in fake.kwargs))
    check("_launch_comfy : process détaché du groupe parent",
          all(kw.get("start_new_session") is True for kw in fake.kwargs))

    content = log_path.read_text()
    check("_launch_comfy : en-tête horodaté écrit à chaque lancement",
          content.count("===== ComfyUI start ") == 2)
    check("_launch_comfy : ancienne sortie conservée (append)",
          content.startswith("ancienne sortie\n"))


def _test_launch_comfy_direct():
    """ComfyUI est lancé via main.py — pas via `comfy launch` qui daemonise.

    `comfy launch --background` daemonise le serveur et détourne sa stdout vers
    son log interne : notre COMFY_LOG_PATH ne contient alors que l'en-tête du
    wrapper et les erreurs d'import des nodes (`IMPORT FAILED`, ModuleNotFound)
    restent invisibles. On vérifie donc le lancement direct de main.py.
    """
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())
    comfy_root = tmp / "ComfyUI"
    nodes_dir = comfy_root / "custom_nodes"
    nodes_dir.mkdir(parents=True)
    for name in ("node_a", "node_b", "node_c"):
        (nodes_dir / name).mkdir()
    (comfy_root / "main.py").write_text("# point d'entrée ComfyUI\n")
    (comfy_root / "comfyui_version.py").write_text('__version__ = "0.3.40"\n')

    fake = _fake_subprocess([], "popen")
    real = (
        bw.subprocess, bw.COMFY_ROOT, bw.COMFY_LOG_PATH, bw.COMFY_CUSTOM_NODES_DIR
    )
    log_path = tmp / "logs" / "comfyui.log"
    bw.subprocess, bw.COMFY_ROOT = fake, comfy_root
    bw.COMFY_LOG_PATH, bw.COMFY_CUSTOM_NODES_DIR = log_path, nodes_dir
    try:
        proc = bw.ComfyWorker()._launch_comfy()
    finally:
        (
            bw.subprocess, bw.COMFY_ROOT, bw.COMFY_LOG_PATH, bw.COMFY_CUSTOM_NODES_DIR
        ) = real

    check("_launch_comfy direct : process retourné", isinstance(proc, _FakeProc))
    check("_launch_comfy direct : main.py lancé avec sys.executable",
          len(fake.args) == 1
          and fake.args[0][0] == [
              sys.executable, str(comfy_root / "main.py"),
              "--listen", "0.0.0.0", "--port", "8000",
          ])
    kwargs = fake.kwargs[0] if fake.kwargs else {}
    check("_launch_comfy direct : pas de shell (aucun wrapper comfy-cli)",
          not kwargs.get("shell"))
    check("_launch_comfy direct : cwd = racine ComfyUI",
          kwargs.get("cwd") == str(comfy_root))
    check("_launch_comfy direct : PYTHONUNBUFFERED=1 (logs temps réel)",
          (kwargs.get("env") or {}).get("PYTHONUNBUFFERED") == "1")
    check("_launch_comfy direct : PYTHONIOENCODING=utf-8",
          (kwargs.get("env") or {}).get("PYTHONIOENCODING") == "utf-8")
    check("_launch_comfy direct : stdout/stderr toujours capturés dans le log",
          Path(kwargs["stdout"].name) == log_path
          and kwargs.get("stderr") == fake.STDOUT
          and kwargs.get("start_new_session") is True)

    content = log_path.read_text()
    cmd_line = (
        f"commande: {sys.executable} {comfy_root / 'main.py'} "
        "--listen 0.0.0.0 --port 8000"
    )
    check("_launch_comfy direct : commande exacte loguée", cmd_line in content)
    check("_launch_comfy direct : nombre de custom_nodes logué",
          "custom_nodes: 3 dossier(s)" in content)
    check("_launch_comfy direct : présence + version de ComfyUI loguées",
          "main.py: présent (ComfyUI 0.3.40)" in content)

    # ── Repli : main.py absent → wrapper comfy-cli (comportement historique)
    empty_root = tmp / "vide"
    empty_root.mkdir()
    fake2 = _fake_subprocess([], "popen")
    bw.subprocess, bw.COMFY_ROOT = fake2, empty_root
    bw.COMFY_LOG_PATH = tmp / "logs" / "repli.log"
    bw.COMFY_CUSTOM_NODES_DIR = empty_root / "custom_nodes"
    try:
        proc2 = bw.ComfyWorker()._launch_comfy()
    finally:
        (
            bw.subprocess, bw.COMFY_ROOT, bw.COMFY_LOG_PATH, bw.COMFY_CUSTOM_NODES_DIR
        ) = real

    kwargs2 = fake2.kwargs[0] if fake2.kwargs else {}
    checkar = str(fake2.args[0][0]) if fake2.args and fake2.args[0] else ""
    check("_launch_comfy repli : comfy launch utilisé si main.py absent",
          isinstance(proc2, _FakeProc)
          and checkar == "comfy launch --background -- --listen 0.0.0.0 --port 8000"
          and kwargs2.get("shell") is True)
    repli = (tmp / "logs" / "repli.log").read_text()
    check("_launch_comfy repli : repli + absence de main.py logués",
          "repli comfy-cli" in repli and "custom_nodes: 0 dossier(s)" in repli)


def _test_logs_tail_cli_fallback():
    """logs_tail : log principal minuscule → complété par le log de comfy-cli."""
    bw = _load_base_worker()
    worker = bw.ComfyWorker()
    tmp = Path(tempfile.mkdtemp())

    log = tmp / "comfyui.log"
    log.write_text(
        "===== ComfyUI start 2026-10-25T10:04:26 =====\n"
        "Launching ComfyUI from: /root/comfy/ComfyUI\n"
        "ComfyUI is successfully launched in the background.\n"
    )  # < 2 Kio → inexploitable
    cli_log = tmp / "user" / "comfyui.log"
    cli_log.parent.mkdir(parents=True)
    cli_log.write_text(
        "\n".join(
            ["Total VRAM 23028 MB", "IMPORT FAILED: ComfyUI-AI-Helper", "ModuleNotFoundError: requests"]
        ) + "\n"
    )

    real = (bw.COMFY_LOG_PATH, bw.COMFY_CLI_LOG_CANDIDATES)
    bw.COMFY_LOG_PATH, bw.COMFY_CLI_LOG_CANDIDATES = log, (cli_log,)
    try:
        res = asyncio.run(worker.logs_tail(50))
        check("logs_tail : complément comfy-cli ajouté quand le log est minuscule",
              res.get("fallback_path") == str(cli_log)
              and "IMPORT FAILED: ComfyUI-AI-Helper" in res.get("lines", []))
        check("logs_tail : séparateur explicite avant le log interne",
              any(
                  line.startswith("───── log interne comfy-cli")
                  and str(cli_log) in line
                  for line in res.get("lines", [])
              ))
        check("logs_tail : log principal conservé avant le complément",
              res.get("lines", [""])[0].startswith("===== ComfyUI start")
              and res.get("size") == log.stat().st_size)
        # Le complément comfy-cli ne doit pas écraser l'âge : seul le log
        # principal dit quand un container a réellement démarré.
        check("logs_tail : mtime/age_seconds du log principal conservés",
              isinstance(res.get("mtime"), float)
              and isinstance(res.get("age_seconds"), int)
              and abs(res["mtime"] - log.stat().st_mtime) < 2)

        # Log principal exploitable → aucun complément (le log fait foi).
        big = tmp / "gros.log"
        big.write_text("x" * (bw.COMFY_CLI_LOG_MIN_BYTES + 1) + "\n")
        bw.COMFY_LOG_PATH = big
        res = asyncio.run(worker.logs_tail(50))
        check("logs_tail : log principal suffisant → pas de complément",
              "fallback_path" not in res
              and not any("log interne comfy-cli" in line for line in res["lines"]))

        # Aucun log comfy-cli présent → résultat inchangé.
        bw.COMFY_LOG_PATH = log
        bw.COMFY_CLI_LOG_CANDIDATES = (tmp / "absent" / "comfyui.log",)
        res = asyncio.run(worker.logs_tail(50))
        check("logs_tail : log interne absent → résultat inchangé",
              "fallback_path" not in res and "error" not in res
              and res["lines"][-1] == "ComfyUI is successfully launched in the background.")

        # La fonction CPU partagée garde son contrat (aucun complément).
        bw.COMFY_CLI_LOG_CANDIDATES = (cli_log,)
        res = bw.read_log_tail(log, 50)
        check("read_log_tail (CPU) : pas de complément comfy-cli",
              "fallback_path" not in res
              and not any("log interne comfy-cli" in line for line in res["lines"]))
    finally:
        bw.COMFY_LOG_PATH, bw.COMFY_CLI_LOG_CANDIDATES = real

    paths = bw._fallback_cli_log_paths()
    check("_fallback_cli_log_paths : ~ développé, racine comfy en tête",
          len(paths) == 3
          and str(paths[0]) == "/root/comfy/ComfyUI/user/comfyui.log"
          and all("~" not in str(p) for p in paths))


def _test_logs_tail():
    """logs_tail : fichier absent → error, N dernières lignes, bornes."""
    bw = _load_base_worker()
    worker = bw.ComfyWorker()
    tmp = Path(tempfile.mkdtemp())
    real_log = bw.COMFY_LOG_PATH

    try:
        bw.COMFY_LOG_PATH = tmp / "absent.log"
        res = asyncio.run(worker.logs_tail(10))
        check("logs_tail : log absent → error explicite",
              res.get("error") == "log file not found" and res.get("lines") == [])
        check("logs_tail : log absent → chemin retourné",
              res.get("path") == str(bw.COMFY_LOG_PATH))

        log = tmp / "comfyui.log"
        log.write_text("\n".join(f"ligne {i}" for i in range(1, 11)) + "\n")
        bw.COMFY_LOG_PATH = log
        res = asyncio.run(worker.logs_tail(3))
        check("logs_tail : 3 dernières lignes du log",
              res.get("lines") == ["ligne 8", "ligne 9", "ligne 10"])
        check("logs_tail : taille du log exposée",
              res.get("size") == log.stat().st_size)
        check("logs_tail : pas d'error quand le log existe", "error" not in res)

        big = tmp / "big.log"
        big.write_text("\n".join(f"l{i}" for i in range(1, 2501)) + "\n")
        bw.COMFY_LOG_PATH = big
        res = asyncio.run(worker.logs_tail(10 ** 6))
        check("logs_tail : borne haute à 2000 lignes",
              len(res.get("lines", [])) == 2000
              and res["lines"][-1] == "l2500")
        res = asyncio.run(worker.logs_tail(-5))
        check("logs_tail : borne basse à 1 ligne", len(res.get("lines", [])) == 1)

        # Fichier plus gros que la fenêtre de lecture : seules les dernières
        # lignes sont chargées, la ligne coupée par le seek doit être jetée.
        real_window = bw.COMFY_LOG_MAX_BYTES
        bw.COMFY_LOG_MAX_BYTES = 64
        try:
            res = asyncio.run(worker.logs_tail(2000))
        finally:
            bw.COMFY_LOG_MAX_BYTES = real_window
        lines = res.get("lines", [])
        check("logs_tail : fenêtre de lecture bornée (fin du log seul)",
              lines[-1] == "l2500" and len(lines) < 2000)
        check("logs_tail : taille exposée = fichier entier malgré la fenêtre",
              res.get("size") == big.stat().st_size)
        check("logs_tail : aucune ligne tronquée par le seek",
              all(line.startswith("l") and line[1:].isdigit() for line in lines))
    finally:
        bw.COMFY_LOG_PATH = real_log


def _test_worker_logs_route():
    """La route GET /logs relaie logs_tail du worker (et gère les erreurs)."""
    class LogsWorker:
        asked = []

        def __init__(self):
            self.logs_tail = _Chain(self._logs_tail)

        async def _logs_tail(self, lines):
            LogsWorker.asked.append(lines)
            return {"lines": ["derniere ligne"], "path": "/tmp/comfyui.log", "size": 9}

    class BoomWorker(LogsWorker):
        async def _logs_tail(self, lines):
            raise RuntimeError("container injoignable")

    router = gateway_router.build_router(
        {"L4": LogsWorker}, cors_origins=["*"], title="logs"
    )
    check("router : route GET /logs exposée", ("GET", "/logs") in router.handlers)

    logs = router.handlers[("GET", "/logs")]
    result = asyncio.run(logs(lines=50))
    check("router : /logs retourne les lignes du worker",
          result.get("lines") == ["derniere ligne"])
    check("router : /logs transmet le nombre de lignes au worker",
          LogsWorker.asked == [50])

    bad = asyncio.run(logs(gpu="A100"))
    check("router : /logs GPU inconnu → 400",
          isinstance(bad, JSONResponse) and bad.status_code == 400)

    boom = gateway_router.build_router(
        {"L4": BoomWorker}, cors_origins=["*"], title="logs"
    )
    failed = asyncio.run(boom.handlers[("GET", "/logs")](lines=10))
    check("router : /logs worker en erreur → 500 avec message",
          isinstance(failed, JSONResponse) and failed.status_code == 500
          and "container injoignable" in failed.content.get("error", ""))

    # ── Chemin principal : lecture par la fonction CPU (logs_reader) ──────
    # Le worker GPU ne doit surtout pas être instancié : l'appeler réveillerait
    # un container GPU (cold start 1-3 min) pour un simple coup d'œil au log.
    cpu_asked = []

    class CpuLogsReader:
        def __init__(self):
            self.remote = SimpleNamespace(aio=self._read)

        async def _read(self, lines):
            cpu_asked.append(lines)
            return {"lines": ["ligne cpu"], "path": "/cache/logs/comfyui.log", "size": 42}

    class NeverTouchedWorker(LogsWorker):
        def __init__(self):
            raise AssertionError("aucun worker GPU ne doit être instancié")

    LogsWorker.asked = []
    cpu_router = gateway_router.build_router(
        {"L4": NeverTouchedWorker},
        cors_origins=["*"],
        title="logs",
        logs_reader=CpuLogsReader(),
    )
    result = asyncio.run(cpu_router.handlers[("GET", "/logs")](lines=42))
    check("router : /logs lit via la fonction CPU quand logs_reader est fourni",
          result.get("lines") == ["ligne cpu"])
    check("router : /logs transmet lines à la fonction CPU", cpu_asked == [42])
    check("router : /logs ne touche pas aux workers GPU (pas de cold start)",
          LogsWorker.asked == [])

    bad_cpu = asyncio.run(cpu_router.handlers[("GET", "/logs")](gpu="A100"))
    check("router : /logs GPU inconnu → 400 même avec logs_reader",
          isinstance(bad_cpu, JSONResponse) and bad_cpu.status_code == 400)

    class FailingLogsReader:
        def __init__(self):
            self.remote = SimpleNamespace(aio=self._read)

        async def _read(self, lines):
            raise RuntimeError("volume indisponible")

    failing = gateway_router.build_router(
        {"L4": LogsWorker},
        cors_origins=["*"],
        title="logs",
        logs_reader=FailingLogsReader(),
    )
    failed_cpu = asyncio.run(failing.handlers[("GET", "/logs")](lines=10))
    check("router : /logs fonction CPU en erreur → 500 avec message",
          isinstance(failed_cpu, JSONResponse) and failed_cpu.status_code == 500
          and "volume indisponible" in failed_cpu.content.get("error", ""))


def _test_read_log_tail():
    """read_log_tail : lecteur partagé (worker GPU + fonction CPU) borné."""
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())

    check("COMFY_LOG_PATH : log persisté sur le volume /cache",
          str(bw.COMFY_LOG_PATH).startswith("/cache/"))

    missing = tmp / "absent.log"
    res = bw.read_log_tail(str(missing), 10)
    check("read_log_tail : fichier absent → error explicite",
          res.get("error") == "log file not found" and res.get("lines") == [])
    check("read_log_tail : chemin str accepté et retourné tel quel",
          res.get("path") == str(missing))

    log = tmp / "comfyui.log"
    log.write_text("\n".join(f"ligne {i}" for i in range(1, 21)) + "\n")
    res = bw.read_log_tail(log, 5)
    check("read_log_tail : 5 dernières lignes (chemin Path)",
          res.get("lines") == [f"ligne {i}" for i in range(16, 21)])
    check("read_log_tail : taille exposée", res.get("size") == log.stat().st_size)

    # Péremption : mtime/age_seconds exposés. Un ``modal deploy`` ne démarre
    # aucun container — sans cet âge, l'UI laisse croire que le code déployé
    # tourne alors que le log peut dater du dernier container.
    aged = tmp / "aged.log"
    aged.write_text("ligne figée\n")
    fixed = time.time() - 7200
    os.utime(aged, (fixed, fixed))
    res = bw.read_log_tail(aged, 5)
    check("read_log_tail : mtime exposé (epoch du fichier)",
          isinstance(res.get("mtime"), float) and abs(res["mtime"] - fixed) < 2)
    check("read_log_tail : age_seconds cohérent avec mtime (~2 h)",
          isinstance(res.get("age_seconds"), int)
          and abs(res["age_seconds"] - 7200) <= 2)

    absent = bw.read_log_tail(tmp / "absent2.log", 5)
    check("read_log_tail : fichier absent → mtime/age_seconds à None",
          absent.get("error") == "log file not found"
          and absent.get("mtime") is None
          and absent.get("age_seconds") is None)

    big = tmp / "big.log"
    big.write_text("\n".join(f"l{i}" for i in range(1, 2501)) + "\n")
    res = bw.read_log_tail(big, 10 ** 6)
    check("read_log_tail : borne haute à 2000 lignes",
          len(res.get("lines", [])) == 2000 and res["lines"][-1] == "l2500")
    res = bw.read_log_tail(big, -5)
    check("read_log_tail : borne basse à 1 ligne", len(res.get("lines", [])) == 1)

    res = bw.read_log_tail(big, 2000, max_bytes=64)
    lines = res.get("lines", [])
    check("read_log_tail : fenêtre de lecture bornée (fin du log seul)",
          lines[-1] == "l2500" and len(lines) < 2000)
    check("read_log_tail : taille exposée = fichier entier malgré la fenêtre",
          res.get("size") == big.stat().st_size)
    check("read_log_tail : aucune ligne tronquée par le seek",
          all(line.startswith("l") and line[1:].isdigit() for line in lines))


def _test_log_rotation():
    """rotate_log : le log sur volume ne grossit pas sans fin."""
    bw = _load_base_worker()
    tmp = Path(tempfile.mkdtemp())

    small = tmp / "small.log"
    small.write_text("x" * 100 + "\n")
    bw.rotate_log(small)
    check("rotate_log : petit log laissé intact",
          small.read_text() == "x" * 100 + "\n")

    absent = tmp / "absent.log"
    bw.rotate_log(absent)
    check("rotate_log : fichier absent → no-op", not absent.exists())

    log = tmp / "comfyui.log"
    log.write_text("".join(f"ligne-{i:05d}\n" for i in range(500)))
    before = log.stat().st_size
    real_max, real_keep = bw.COMFY_LOG_MAX_BYTES, bw.COMFY_LOG_KEEP_BYTES
    bw.COMFY_LOG_MAX_BYTES, bw.COMFY_LOG_KEEP_BYTES = 1024, 256
    try:
        bw.rotate_log(log)
    finally:
        bw.COMFY_LOG_MAX_BYTES, bw.COMFY_LOG_KEEP_BYTES = real_max, real_keep

    after = log.stat().st_size
    content = log.read_text()
    check("rotate_log : log trop gros tronqué", after < before and after <= 512)
    check("rotate_log : dernières lignes conservées",
          content.rstrip().endswith("ligne-00499"))
    check("rotate_log : première ligne (coupée par le seek) jetée",
          content.startswith("ligne-"))

    # La rotation est appliquée automatiquement au lancement de ComfyUI.
    fake = _fake_subprocess([], "popen")
    real_subprocess, real_log = bw.subprocess, bw.COMFY_LOG_PATH
    log.write_text("".join(f"ligne-{i:05d}\n" for i in range(500)))
    bw.subprocess, bw.COMFY_LOG_PATH = fake, log
    bw.COMFY_LOG_MAX_BYTES, bw.COMFY_LOG_KEEP_BYTES = 1024, 256
    try:
        bw.ComfyWorker()._launch_comfy()
    finally:
        bw.subprocess, bw.COMFY_LOG_PATH = real_subprocess, real_log
        bw.COMFY_LOG_MAX_BYTES, bw.COMFY_LOG_KEEP_BYTES = real_max, real_keep
    check("_launch_comfy : rotation appliquée avant l'append",
          log.stat().st_size < before + 200
          and log.read_text().count("===== ComfyUI start ") == 1)


def _test_apps_logs_reader():
    """Les 3 apps exposent read_worker_logs (CPU) et la câblent au routeur."""
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "comfyui.log"

    for app_file in ("all_in_one.py", "all_in_one_lite.py", "all_in_one_l4.py"):
        spec = importlib.util.spec_from_file_location(
            "comfych_app_" + app_file.replace(".py", ""),
            str(PROJECT_ROOT / "apps" / app_file),
        )
        app_mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(app_mod)
        except Exception as e:  # pragma: no cover — imports non stubbés
            check(f"{app_file} : import sans erreur ({type(e).__name__}: {e})", False)
            continue

        check(f"{app_file} : read_worker_logs déclarée comme fonction Modal",
              "read_worker_logs" in app_mod.app.functions)
        check(f"{app_file} : read_worker_logs montée sur /cache",
              isinstance(app_mod.MODELS_VOLUME, Volume))

        real_path = app_mod.COMFY_LOG_PATH
        try:
            app_mod.COMFY_LOG_PATH = tmp / "absent.log"
            res = app_mod.read_worker_logs(10)
            check(f"{app_file} : log absent → message « worker pas encore démarré »",
                  res.get("lines") == []
                  and "pas encore démarré" in res.get("error", ""))

            log.write_text("\n".join(f"ligne {i}" for i in range(1, 31)) + "\n")
            app_mod.COMFY_LOG_PATH = log
            res = app_mod.read_worker_logs(3)
            check(f"{app_file} : read_worker_logs renvoie les N dernières lignes",
                  res.get("lines") == ["ligne 28", "ligne 29", "ligne 30"])
            check(f"{app_file} : read_worker_logs expose la taille du log",
                  res.get("size") == log.stat().st_size)
            check(f"{app_file} : read_worker_logs borne le nombre de lignes",
                  len(app_mod.read_worker_logs(10 ** 6).get("lines", [])) == 30)
        finally:
            app_mod.COMFY_LOG_PATH = real_path


def _test_extension_worker_logs_route():
    """La route locale GET /api/modal/worker-logs relaie {api_url}/logs.

    ``server`` et ``aiohttp`` sont stubbés le temps de l'import : sans eux,
    ``__init__.py`` n'enregistre aucune route (cas des autres tests).
    """
    registered = {}

    class _Router:
        def add_route(self, method, path, handler):
            registered[(method, path)] = handler

    class _App:
        def __init__(self): self.router = _Router()

    class PromptServer:
        def __init__(self): self.app = _App()
        def add_routes(self): return None  # remplacée par _add_routes_with_modal

    class _Web:
        @staticmethod
        def json_response(payload, status=200):
            return SimpleNamespace(payload=payload, status=status, content=payload)

    server_mod = types.ModuleType("server")
    server_mod.PromptServer = PromptServer
    aiohttp_mod = types.ModuleType("aiohttp")
    aiohttp_mod.web = _Web

    sys.modules["server"] = server_mod
    sys.modules["aiohttp"] = aiohttp_mod
    try:
        comfych = _load_comfych_module()
        prompt_server = PromptServer()
        comfych.PromptServer.add_routes(prompt_server)
    finally:
        sys.modules.pop("server", None)
        sys.modules.pop("aiohttp", None)

    handler = registered.get(("GET", "/api/modal/worker-logs"))
    check("route locale : /api/modal/worker-logs enregistrée (gardée)",
          handler is not None)
    if handler is None:
        return

    tmp = Path(tempfile.mkdtemp())
    real_config, real_urllib = comfych.load_config, comfych.urllib
    urls = []

    class _FakeResp:
        def __init__(self, body, status=200):
            self._body = body; self.status = status
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    class _FakeUrlopen:
        body = b'{"lines": ["tb import"], "path": "/tmp/comfyui.log", "size": 9}'
        status = 200
        @classmethod
        def __call__(cls, req, timeout=None):
            urls.append((req.full_url, req.headers.get("X-API-Key")))
            return _FakeResp(cls.body, cls.status)

    class _FakeRequest:
        @staticmethod
        def __call__(url, headers=None):
            return SimpleNamespace(full_url=url, headers=headers or {})

    fake_urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(Request=_FakeRequest(), urlopen=_FakeUrlopen()),
        error=types.SimpleNamespace(HTTPError=RuntimeError, URLError=OSError),
    )

    def _fake_config(cfg):
        return lambda: dict(cfg)

    async def _call(query):
        return await handler(SimpleNamespace(
            rel_url=SimpleNamespace(query=query), headers={}
        ))

    try:
        comfych.urllib = fake_urllib
        comfych.load_config = _fake_config({"api_url": "", "api_key": "k"})
        resp = asyncio.run(_call({}))
        check("route locale : api_url absente → 400",
              resp.status == 400 and resp.payload["ok"] is False)

        comfych.load_config = _fake_config(
            {"api_url": "https://x.modal.run/", "api_key": "k"}
        )
        resp = asyncio.run(_call({"lines": "300", "gpu": "L40S"}))
        check("route locale : lignes du worker relayées",
              resp.payload.get("lines") == ["tb import"])
        check("route locale : URL /logs + gpu transmis au gateway",
              urls and urls[0][0] == "https://x.modal.run/logs?lines=300&gpu=L40S")
        check("route locale : clé API envoyée dans X-API-Key",
              bool(urls) and urls[0][1] == "k")

        resp = asyncio.run(_call({"lines": "pas-un-nombre"}))
        check("route locale : lines invalide → 200 lignes par défaut",
              urls[-1][0] == "https://x.modal.run/logs?lines=200")
    finally:
        comfych.urllib, comfych.load_config = real_urllib, real_config


# ─────────────── Filtre des sous-modules git cachés (régression) ───────────

def _test_hidden_submodules_preserved():
    """Un dossier caché n'est plus exclu en bloc : les sous-modules survivent.

    Régression de production : « [VRGDG] 1 failed submodule(s):
    .VRGDG_LTXLoopingSampler / No module named 'ComfyUI-LTXVideo' » causée par
    l'ancienne règle « tout dossier commençant par . est exclu ».
    """
    import tarfile

    sync_mod = _load_sync_module()
    root = Path(tempfile.mkdtemp())
    node = _make_node(root, "vrgamedevgirl", files=("__init__.py", "nodes.py"))

    sub = node / ".VRGDG_LTXLoopingSampler"
    sub.mkdir()
    (sub / "__init__.py").write_text("x")
    (sub / "sampler.py").write_text("x")
    (sub / "_C.cpython-311-x86_64-linux-gnu.so").write_bytes(b"s" * 16)
    (node / ".git").mkdir()
    (node / ".git" / "config").write_text("x")
    (node / ".venv").mkdir()
    (node / ".venv" / "pyvenv.cfg").write_text("x")
    (node / ".DS_Store").write_bytes(b"x")
    (node / ".gitignore").write_text("x")
    (node / ".gitmodules").write_text("x")
    (node / ".code_cache.py").write_text("x")

    check("filtre : sous-module git caché CONSERVÉ",
          sync_mod._dir_exclusion_reason(".VRGDG_LTXLoopingSampler") is None)
    check("filtre : autres dossiers cachés conservés",
          all(sync_mod._dir_exclusion_reason(d) is None for d in
              [".VRGDG_LTXLoopingSampler", ".hidden_lib", ".submodule",
               ".kjnodes_patch"]))
    check("filtre : .git et .venv EXCLUS",
          sync_mod._dir_exclusion_reason(".git") == sync_mod._SKIP_HIDDEN
          and sync_mod._dir_exclusion_reason(".venv") is not None)
    check("filtre : liste explicite des dossiers cachés techniques",
          all(sync_mod._dir_exclusion_reason(d) is not None for d in
              [".git", ".github", ".venv", ".idea", ".vscode", ".cache",
               ".mypy_cache", ".pytest_cache", ".ruff_cache", ".staging"]))
    check("filtre : .git reste listé dans EXCLUDE_DIRS",
          ".git" in sync_mod.EXCLUDE_DIRS
          and ".git" in sync_mod.HIDDEN_EXCLUDE_DIRS)
    check("filtre : au niveau custom_nodes/, un dossier caché n'est pas un node",
          sync_mod._dir_exclusion_reason(".hidden_node", node_root=True) is not None)
    check("filtre : métadonnées cachées exclues (.DS_Store, .gitignore…)",
          all(sync_mod._is_excluded_file(n) for n in
              [".DS_Store", ".gitignore", ".gitmodules", ".gitattributes"]))
    check("filtre : code caché conservé (.py/.so sous dossier caché)",
          not sync_mod._is_excluded_file(".code_cache.py")
          and not sync_mod._is_excluded_file("_C.cpython-311-x86_64-linux-gnu.so"))

    archive = Path(tempfile.mkdtemp()) / "nodes.tar.gz"
    stats = sync_mod._build_custom_nodes_archive([node], archive)
    with tarfile.open(archive, mode="r:gz") as tar:
        names = set(tar.getnames())

    check("archive : le sous-module caché est archivé (import possible)",
          {"vrgamedevgirl/.VRGDG_LTXLoopingSampler/__init__.py",
           "vrgamedevgirl/.VRGDG_LTXLoopingSampler/sampler.py",
           "vrgamedevgirl/.VRGDG_LTXLoopingSampler/_C.cpython-311-x86_64-linux-gnu.so"}
          <= names)
    check("archive : .git/.venv et métadonnées restent exclus",
          not any(n.startswith("vrgamedevgirl/.git/") or
                  n.startswith("vrgamedevgirl/.venv/") for n in names)
          and "vrgamedevgirl/.DS_Store" not in names
          and "vrgamedevgirl/.gitignore" not in names)
    check("archive : sous-module conservé signalé dans les stats",
          stats["kept_hidden_dirs"] == [".VRGDG_LTXLoopingSampler"])
    check("résumé : ligne informative sur les sous-modules conservés",
          any("sous-module(s) caché(s) conservé(s)" in line
              for line in sync_mod._archive_exclusion_summary_lines(stats)))


# ────────── Champ « paquets pip supplémentaires » (config + image) ─────────

def _test_extra_pip_packages_config():
    """``extra_pip_packages`` : défaut vide, liste blanche, filtre sans 400."""
    comfych = _load_comfych_module()

    check("pip : DEFAULT_CONFIG contient extra_pip_packages (liste vide)",
          comfych.DEFAULT_CONFIG.get("extra_pip_packages") == [])

    check("pip : paquets légitimes acceptés",
          all(comfych._is_safe_pip_package(p) for p in
              ["sageattention", "mediapipe", "ultralytics", "onnxruntime-gpu",
               "mediapipe>=0.10.9", "torch==2.4.1", "foo[extra]", "x_y",
               "diffusers~=0.31"]))
    check("pip : entrées dangereuses rejetées",
          not any(comfych._is_safe_pip_package(p) for p in
                  ["x; rm -rf /", "x && reboot", "x | tee /etc/passwd",
                   "$(reboot)", "`reboot`", "x y", "x\nrm -rf /",
                   "-r /etc/passwd", "--index-url http://evil", "", "   ",
                   None, 42, ["sageattention"]]))

    accepted, rejected = comfych._filter_pip_packages(
        ["sageattention", "x; rm -rf /", "", "mediapipe"]
    )
    check("pip : filtre → valides conservés, dangereux écartés",
          accepted == ["sageattention", "mediapipe"]
          and "x; rm -rf /" in rejected)
    check("pip : doublons dédupliqués",
          comfych._filter_pip_packages(["a", " a ", "b"]) == (["a", "b"], []))
    check("pip : champ non-liste → None (champ ignoré, pas de 400)",
          comfych._filter_pip_packages("sageattention") is None
          and comfych._filter_pip_packages(None) is None)


def _test_post_config_extra_pip_packages():
    """``post_config`` accepte le champ, filtre les entrées, ne renvoie pas 400."""
    import json as json_mod

    routes = {}

    class _Router:
        def add_route(self, method, path, handler):
            routes[(method, path)] = handler

    class FakePromptServer:
        def __init__(self):
            self.app = SimpleNamespace(router=_Router())

        def add_routes(self):
            return "original"

    server_mod = types.ModuleType("server")
    server_mod.PromptServer = FakePromptServer
    aiohttp_mod = types.ModuleType("aiohttp")
    aiohttp_mod.web = _FakeWeb
    saved = {name: sys.modules.get(name) for name in ("server", "aiohttp")}
    sys.modules["server"] = server_mod
    sys.modules["aiohttp"] = aiohttp_mod
    try:
        comfych = _load_comfych_module()
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    cfg = Path(tempfile.mkdtemp()) / "config.json"
    comfych.CONFIG_PATH = cfg
    comfych._add_routes_with_modal(FakePromptServer())
    post_config = routes.get(("POST", "/api/modal/config"))
    check("pip : route POST /api/modal/config enregistrée",
          callable(post_config))

    class _FakeRequest:
        def __init__(self, payload):
            self._payload = payload
            self.headers = {"Host": "127.0.0.1:8188"}

        async def json(self):
            return self._payload

    resp = asyncio.run(post_config(_FakeRequest(
        {"extra_pip_packages": ["sageattention", "x; rm -rf /", "mediapipe"]}
    )))
    stored = json_mod.loads(cfg.read_text())["extra_pip_packages"]
    check("pip : liste valide acceptée (200, pas de 400)",
          resp.status == 200 and resp.payload["ok"] is True)
    check("pip : entrées dangereuses écartées à l'écriture",
          stored == ["sageattention", "mediapipe"])
    check("pip : commande dangereuse jamais stockée dans config.json",
          "rm -rf" not in cfg.read_text())

    # Champ inutilisable (chaîne au lieu d'une liste) : ignoré, config conservée.
    resp = asyncio.run(post_config(_FakeRequest(
        {"extra_pip_packages": "sageattention; rm -rf /"}
    )))
    check("pip : champ non-liste ignoré sans casser la requête",
          resp.status == 200
          and json_mod.loads(cfg.read_text())["extra_pip_packages"]
          == ["sageattention", "mediapipe"])

    # Les autres clés restent enregistrées normalement (compatibilité).
    asyncio.run(post_config(_FakeRequest({"api_url": "https://x.modal.run"})))
    check("pip : sauvegarde des autres clés inchangée",
          json_mod.loads(cfg.read_text())["api_url"] == "https://x.modal.run")

    # Liste vide acceptée (désactivation du champ).
    asyncio.run(post_config(_FakeRequest({"extra_pip_packages": []})))
    check("pip : liste vide acceptée",
          json_mod.loads(cfg.read_text())["extra_pip_packages"] == [])


class _RecordingImage:
    """Image Modal factice qui retient les commandes exécutées."""

    instances = []

    def __init__(self):
        self.commands = []
        _RecordingImage.instances.append(self)

    @classmethod
    def debian_slim(cls, python_version="3.11"):
        return cls()

    def add_local_python_source(self, *a, **kw): return self
    def add_local_file(self, *a, **kw): return self
    def add_local_dir(self, *a, **kw): return self
    def apt_install(self, *a, **kw): return self
    def pip_install_from_requirements(self, *a, **kw): return self

    def run_commands(self, *cmds, **kw):
        self.commands.extend(cmds)
        return self


def _test_extra_pip_install_best_effort():
    """Le build installe les paquets pip en best-effort (jamais bloquant)."""
    spec = importlib.util.spec_from_file_location(
        "comfych_image", str(PROJECT_ROOT / "image.py")
    )
    image_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(image_mod)

    check("pip image : paquets valides extraits de la config",
          image_mod._extra_pip_packages(
              {"extra_pip_packages": ["sageattention", "x; rm -rf /", 42, ""]}
          ) == ["sageattention"])
    check("pip image : config absente/corrompue → liste vide, aucune exception",
          image_mod._extra_pip_packages({}) == []
          and image_mod._extra_pip_packages({"extra_pip_packages": "sageattention"}) == []
          and image_mod._extra_pip_packages("pas un dict") == [])

    cmd = image_mod._pip_install_command("sageattention")
    check("pip image : commande = pip install <paquet>",
          cmd.startswith("pip install sageattention"))
    check("pip image : commande toujours suffixée de « || echo » (build non bloquant)",
          "|| echo" in cmd and cmd.count("|| echo") == 1)
    check("pip image : échec du paquet signalé dans le log du build",
          "build continue" in cmd)

    # Bout en bout : _build_image() avec un config.json fournissant des paquets.
    image_mod.modal = SimpleNamespace(Image=_RecordingImage)
    image_mod._read_saved_config = lambda: {
        "extra_pip_packages": ["sageattention", "mediapipe>=0.10", "x; rm -rf /"]
    }
    image_mod._build_image()
    recorded = _RecordingImage.instances[-1].commands
    pip_commands = [c for c in recorded if c.startswith("pip install sageattention")]
    check("pip image : paquet supplémentaire installé au build",
          len(pip_commands) == 1)
    check("pip image : plusieurs paquets → plusieurs commandes",
          any(c.startswith("pip install") and "mediapipe>=0.10" in c
              for c in recorded))
    check("pip image : entrée dangereuse jamais exécutée",
          not any("rm -rf" in c for c in recorded))
    check("pip image : toutes les commandes pip restent best-effort",
          all("|| echo" in c for c in recorded if c.startswith("pip install ")))


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
    _test_hidden_submodules_preserved()
    _test_extra_pip_packages_config()
    _test_post_config_extra_pip_packages()
    _test_extra_pip_install_best_effort()
    _test_image_size_guard()
    _test_archive_summary_lines()
    _test_node_size_report()
    _test_archive_guards()
    _test_extract_class_mappings()
    _test_start_checkpoint_order()
    _test_custom_nodes_sync()
    _test_start_restore_restart()
    _test_worker_build_id()
    _test_shutdown_comfy()
    _test_wait_for_port()
    _test_startup_timing_blocks()
    _test_wait_for_port_timing()
    _test_startup_timing_block()
    _test_restore_timing_mode()
    _test_launch_comfy_redirect()
    _test_launch_comfy_direct()
    _test_logs_tail_cli_fallback()
    _test_logs_tail()
    _test_read_log_tail()
    _test_log_rotation()
    _test_worker_logs_route()
    _test_apps_logs_reader()
    _test_extension_worker_logs_route()

    print(f"\n{'='*50}")
    print(f"Résultat : {PASS}/{TOTAL} tests OK")
    sys.exit(0 if PASS == TOTAL else 1)


if __name__ == "__main__":
    main()
