"""Tests du gateway — sans dépendances (stubs fastapi/pydantic/modal).

Couvre :
- l'idempotence de /generate (pas de double exécution au retry)
- la sanitisation anti path traversal de /api/modal/save-local
- l'extraction AST des NODE_CLASS_MAPPINGS
- la sanitisation des noms de modèles et le rejet des liens tar (sync.py)

Usage ::

    python3 tests/test_gateway.py
"""
import asyncio
import importlib.util
import sys
import tempfile
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
    _test_sanitize_save_path()
    _test_safe_model_filename()
    _test_tar_member_validation()
    _test_extract_class_mappings()

    print(f"\n{'='*50}")
    print(f"Résultat : {PASS}/{TOTAL} tests OK")
    sys.exit(0 if PASS == TOTAL else 1)


if __name__ == "__main__":
    main()
