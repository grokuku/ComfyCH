# 📋 PLAN DE RÉALISATION — Modal Gateway pour ComfyUI

> Plan d'implémentation détaillé, phase par phase.
> Chaque phase est indépendante et testable.
> Durée estimée totale : ~2-3 jours de dev.

---

## 📦 Phase 0 — Préparation de l'environnement

**Objectif** : Avoir un environnement Modal fonctionnel, un compte, et les outils de base.

### 🎯 À faire

- [ ] Créer un compte Modal (modal.com/signup) → 30 $ de crédit offert
- [ ] Installer le CLI Modal et le SDK Python
  ```bash
  pip install modal
  modal setup
  ```
- [ ] Créer le Secret Hugging Face
  ```bash
  modal secret create huggingface-secret HF_TOKEN=hf_xxxxx
  ```
- [ ] Créer le Volume de modèles
  ```bash
  modal volume create comfy-models
  ```
- [ ] Vérifier que le projet existant `comfyui.py` se déploie correctement
  ```bash
  modal deploy comfyui.py
  ```

### ✅ Critères de succès

- `modal run comfyui.py` démarre ComfyUI sur Modal
- L'interface web est accessible via l'URL Modal
- La génération d'image fonctionne

---

## 🧱 Phase 1 — Découpage du monolithe : sync CPU

**Objectif** : Extraire le téléchargement des modèles du build de l'image Docker
pour pouvoir le lancer indépendamment sur un conteneur CPU pas cher.

### 🎯 À faire

- [ ] Créer `sync.py` — conteneur CPU pur qui :
  - Monte le Volume `comfy-models`
  - Télécharge tous les modèles listés dans `models.py`
  - Supporte les mêmes formats (HF + externes via aria2c)
  - Fonctionnement : `modal run sync.py`

```python
# sync.py — structure attendue
@app.function(
    cpu=1,
    memory=2048,
    volumes={"/cache": vol},
    secrets=_hf_secrets(),
)
def sync_models():
    from models import models, models_ext
    for model in models:
        hf_download(model["repo_id"], model["filename"], model["model_dir"])
    for model in models_ext:
        download_external_model(model["url"], model["filename"], model["model_dir"])
```

- [ ] Supprimer `download_all()` du build de l'image dans `comfyui.py`
- [ ] Remplacer par une vérification que les modèles existent (sans les télécharger)
- [ ] Ajouter une option `--sync` optionnelle : `modal run sync.py --sync`

### 📦 Fichiers concernés

| Fichier | Action |
|---|---|
| `sync.py` | **NOUVEAU** — conteneur CPU de sync |
| `comfyui.py` | MODIFIER — retirer `download_all()` du build |
| `helpers.py` | **NOUVEAU** — mutualiser `hf_download()`, `download_external_model()`, `resolve_model_dir()` |

### ✅ Critères de succès

- `modal run sync.py` télécharge les modèles et écrit dans le Volume
- Le build de l'image Docker ne télécharge **pas** les modèles (plus rapide)
- Un déploiement frais utilise les modèles déjà dans le Volume

---

## 🧱 Phase 2 — Refonte du worker en headless API

**Objectif** : Transformer la classe `ComfyUI` actuelle (avec `@modal.web_server`)
en un worker headless qui expose uniquement l'API REST de ComfyUI.

### 🎯 À faire

- [ ] Créer un `BaseWorker` avec la logique commune de démarrage de ComfyUI en headless

```python
# workers/base_worker.py — structure attendue
class BaseWorker:
    """Classe de base : démarre ComfyUI en headless, expose /prompt"""

    @modal.enter(snap=True)
    def start_checkpoint(self):
        # Lance comfy en mode headless (sans UI)
        self.proc = subprocess.Popen(
            "comfy launch --background -- --listen 0.0.0.0 --port 8000",
            shell=True
        )
        wait_for_port(8000, timeout=300)

    @modal.enter(snap=False)
    def start_restore(self):
        wait_for_port(8000, timeout=30)

    @modal.exit()
    def cleanup(self):
        if self.proc:
            self.proc.terminate()
```

- [ ] Supprimer `@modal.web_server(8000)` de la classe actuelle
- [ ] À la place, exposer un `@modal.web_endpoint()` qui fait proxy vers l'API ComfyUI locale

```python
# workers/base_worker.py — suite
@app.cls(...)
class ComfyUIHeadless(BaseWorker):

    @modal.web_endpoint(method="POST")
    async def generate(self, workflow_json: dict):
        """Proxy vers l'API ComfyUI locale (127.0.0.1:8000)"""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://127.0.0.1:8000/prompt",
                json={"prompt": workflow_json}
            )
            return resp.json()

    @modal.web_endpoint(method="GET")
    async def health(self):
        return {"status": "ok"}
```

- [ ] Remplacer la classe actuelle `ComfyUI` dans `comfyui.py`
- [ ] Tester avec :
  ```bash
  # Envoyer un workflow JSON à l'API Modal
  curl -X POST https://mon-app.modal.run/generate \
    -H "Content-Type: application/json" \
    -d '{"3": {"inputs": {"seed": 42, ...}}, ...}'
  ```

### 📦 Fichiers concernés

| Fichier | Action |
|---|---|
| `workers/base_worker.py` | **NOUVEAU** — classe de base headless |
| `comfyui.py` | MODIFIER — utiliser BaseWorker, supprimer web_server |
| `pyproject.toml` | VÉRIFIER — ajouter `httpx` si besoin |

### ✅ Critères de succès

- `POST /generate` avec un workflow JSON → retourne un `job_id`
- `GET /health` → `{"status": "ok"}`
- L'ancienne interface web ComfyUI n'est plus accessible (headless)
- Le snapshot GPU fonctionne toujours (démarrage < 30s)

---

## 🧱 Phase 3 — Workers multiples par GPU

**Objectif** : Créer un worker par type de GPU (L4, L40S, A100, H100) partageant
le même Volume de modèles.

### 🎯 À faire

- [ ] Créer les workers spécialisés :

```python
# workers/l4_worker.py
@app.cls(gpu="L4", volumes={"/cache": vol}, ...)
class L4Worker(ComfyUIHeadless):
    pass

# workers/l40s_worker.py
@app.cls(gpu="L40S", volumes={"/cache": vol}, ...)
class L40SWorker(ComfyUIHeadless):
    pass

# workers/a100_worker.py
@app.cls(gpu="A100-80GB", volumes={"/cache": vol}, ...)
class A100Worker(ComfyUIHeadless):
    pass

# workers/h100_worker.py
@app.cls(gpu="H100", volumes={"/cache": vol}, ...)
class H100Worker(ComfyUIHeadless):
    pass
```

- [ ] Ajuster les paramètres par worker :
  - `scaledown_window` : L4/L40S → 30s, A100/H100 → 60s (temps de chargement plus long)
  - `enable_memory_snapshot` : tous en True
  - `max_containers=1` pour tous

- [ ] Mutualiser la définition de l'image Docker dans un fichier commun
  (`image.py`) pour éviter la duplication entre workers.

### 📦 Fichiers concernés

| Fichier | Action |
|---|---|
| `workers/__init__.py` | **NOUVEAU** — package |
| `workers/l4_worker.py` | **NOUVEAU** |
| `workers/l40s_worker.py` | **NOUVEAU** |
| `workers/a100_worker.py` | **NOUVEAU** |
| `workers/h100_worker.py` | **NOUVEAU** |
| `image.py` | **NOUVEAU** — construction de l'image Docker partagée |

### ✅ Critères de succès

- Chaque worker peut être déployé indépendamment
- Tous les workers partagent le même Volume → les modèles sont là
- Chaque worker a son propre snapshot GPU

---

## 🧱 Phase 4 — Routeur API central

**Objectif** : Un point d'entrée unique qui reçoit les requêtes et les dispatch
vers le bon worker selon le GPU demandé.

### 🎯 À faire

- [ ] Créer `modal_api.py` avec un endpoint FastAPI :

```python
# modal_api.py — structure attendue
from modal import App, web_endpoint
from pydantic import BaseModel

class GenerateRequest(BaseModel):
    workflow: dict
    gpu: str = "L4"  # L4, L40S, A100, H100

# Mapping GPU → worker (avec lazy import)
WORKERS = {
    "L4": L4Worker(),
    "L40S": L40SWorker(),
    "A100": A100Worker(),
    "H100": H100Worker(),
}

@app.function()
@web_endpoint(method="POST")
async def generate(request: GenerateRequest):
    worker = WORKERS.get(request.gpu)
    if not worker:
        return {"error": f"GPU '{request.gpu}' not supported"}
    # Appel asynchrone au worker
    result = await worker.generate.remote(request.workflow)
    return result
```

- [ ] Gérer le cas où le worker n'est pas encore déployé (premier appel = warmup)
- [ ] Ajouter un endpoint `GET /gpus` qui liste les GPUs disponibles avec leurs stats

```python
@app.function()
@web_endpoint(method="GET")
async def list_gpus():
    """Retourne la liste des GPUs disponibles avec leurs tarifs"""
    return [
        {"id": "L4", "name": "NVIDIA L4", "vram": "24 GB", "price": "$0.80/h"},
        {"id": "L40S", "name": "NVIDIA L40S", "vram": "48 GB", "price": "$1.95/h"},
        {"id": "A100", "name": "NVIDIA A100 80GB", "vram": "80 GB", "price": "$2.50/h"},
        {"id": "H100", "name": "NVIDIA H100", "vram": "80 GB", "price": "$3.95/h"},
    ]
```

### ⚙️ Détail d'implémentation important

Chaque worker doit être déclaré comme un **App Modal distinct** ou alors tous
dans la même App mais avec des noms de classes différents. L'approche la plus
propre : **un fichier par App**, déployées indépendamment.

```
📦 modal-comfyui/
├── apps/
│   ├── l4_app.py        → modal deploy apps/l4_app.py
│   ├── l40s_app.py      → modal deploy apps/l40s_app.py
│   ├── a100_app.py      → modal deploy apps/a100_app.py
│   └── h100_app.py      → modal deploy apps/h100_app.py
│
└── modal_api.py          → point d'entrée unique, interroge les workers
```

### 📦 Fichiers concernés

| Fichier | Action |
|---|---|
| `modal_api.py` | **NOUVEAU** — routeur FastAPI |
| `apps/l4_app.py` | **NOUVEAU** — déploiement du worker L4 |
| `apps/l40s_app.py` | **NOUVEAU** |
| `apps/a100_app.py` | **NOUVEAU** |
| `apps/h100_app.py` | **NOUVEAU** |

### ✅ Critères de succès

- `POST /generate` avec `gpu="L40S"` → execute sur L40S
- `POST /generate` avec `gpu="A100"` → execute sur A100
- `GET /gpus` → liste les GPUs disponibles
- Erreur propre si GPU inconnu

---

## 🧱 Phase 5 — Endpoints spécialisés pour ComfyUI

**Objectif** : Exposer les endpoints ComfyUI nécessaires au-delà du simple
`/prompt` : upload d'images, visualisation, historique.

### 🎯 À faire

- [ ] Analyser les endpoints ComfyUI utilisés par l'extension JS :

| Endpoint ComfyUI | Usage | Méthode |
|---|---|---|
| `/prompt` | Envoyer un workflow | POST |
| `/upload/image` | Uploader une image source (img2img) | POST |
| `/view` | Récupérer une image générée | GET |
| `/history/{job_id}` | Statut d'un job | GET |
| `/queue` | File d'attente | GET |

- [ ] Ajouter ces endpoints dans le `BaseWorker` :

```python
@modal.web_endpoint(method="POST")
async def upload_image(self, file: bytes, ...):
    """Proxy vers /upload/image de ComfyUI"""
    ...

@modal.web_endpoint(method="GET")
async def get_image(self, filename: str, subfolder: str, type: str = "output"):
    """Proxy vers /view de ComfyUI"""
    ...
```

- [ ] Le routeur central `modal_api.py` doit aussi proxyfier ces endpoints

### ⚠️ Point d'attention : les images uploadées

Pour l'img2img, l'utilisateur upload une image depuis son ComfyUI local.
Cette image doit transiter jusqu'au worker Modal. Options :

1. **Proxy direct** : l'utilisateur upload → routeur → worker → ComfyUI distant
   → Plus simple, mais 2× le temps de transfert

2. **Upload vers le Volume** : l'image est écrite dans le Volume partagé
   → Plus rapide, mais nécessite un chemin connu des deux côtés

3. **Base64 dans le workflow** : l'image est encodée directement dans le JSON
   du workflow (ComfyUI le supporte).
   → Recommandé pour la v1 : pas de endpoints supplémentaires à exposer

### ✅ Critères de succès

- Un workflow img2img fonctionne via l'API (image en base64 dans le JSON)
- Les workflows txt2img fonctionnent
- Les images générées sont retournées au client

---

## 🧱 Phase 6 — Extension JS ComfyUI locale

**Objectif** : Créer l'extension JavaScript qui ajoute le dropdown de sélection
du GPU et intercepte "Queue Prompt" pour rediriger vers Modal.

### 🎯 À faire

- [ ] Créer la structure du custom node :

```
custom_nodes/modal_gateway/
├── __init__.py       ← Sert juste à activer l'extension web
└── web/
    ├── modal_gateway.js    ← L'extension JS principale
    └── modal_gateway.css   ← Styles du dropdown
```

- [ ] Dans `modal_gateway.js` :

```javascript
// 1. AJOUTER LE DROPDOWN DANS LA BARRE D'OUTILS
// Injection d'un élément <select> à côté du bouton Queue Prompt

function addGpuSelector() {
    const toolbar = document.querySelector(".comfyui-toolbar");
    const select = document.createElement("select");
    select.id = "gpu-selector";
    select.innerHTML = `
        <option value="local">🖥️ Rendu : Local (gratuit)</option>
        <option value="modal:l4">☁️ Modal L4 — $0.80/h</option>
        <option value="modal:l40s">☁️ Modal L40S — $1.95/h</option>
        <option value="modal:a100">☁️ Modal A100 — $2.50/h</option>
        <option value="modal:h100">☁️ Modal H100 — $3.95/h</option>
    `;
    toolbar.appendChild(select);
}

// 2. INTERCEPTER QUEUE PROMPT
// Rediriger la requête vers l'API Modal quand le mode cloud est sélectionné

function interceptQueuePrompt() {
    const api = window.api;
    const originalQueue = api.queuePrompt;

    api.queuePrompt = async function(number, workflow) {
        const mode = document.getElementById("gpu-selector").value;

        if (mode === "local") {
            return originalQueue(number, workflow); // ← normal
        }

        // Mode cloud → on envoie à Modal
        const gpu = mode.split(":")[1];
        const response = await fetch("https://api.modal.com/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ workflow, gpu })
        });

        const result = await response.json();
        // TODO: injecter les images reçues dans ComfyUI
        return result;
    };
}

// 3. STOCKER LE CHOIX DANS LOCALSTORAGE
function persistChoice() {
    const select = document.getElementById("gpu-selector");
    select.value = localStorage.getItem("gpu-mode") || "local";
    select.addEventListener("change", () => {
        localStorage.setItem("gpu-mode", select.value);
    });
}
```

- [ ] Gérer les erreurs réseau (timeout, Modal indisponible → fallback local ?)

### 📦 Fichiers concernés

| Fichier | Action |
|---|---|
| `custom_nodes/modal_gateway/__init__.py` | **NOUVEAU** |
| `custom_nodes/modal_gateway/web/modal_gateway.js` | **NOUVEAU** |
| `custom_nodes/modal_gateway/web/modal_gateway.css` | **NOUVEAU** |

### ✅ Critères de succès

- Le dropdown apparaît dans la barre d'outils de ComfyUI
- Le choix est persisté entre les sessions (localStorage)
- Mode "Local" : Queue Prompt fonctionne normalement
- Mode "L4/L40S/A100/H100" : interception et appel à l'API Modal
- Les images générées s'affichent dans ComfyUI

---

## 🧱 Phase 7 — Gestion des fichiers uploadés

**Objectif** : Gérer le cas des workflows img2img (ou vidéo) où l'utilisateur
doit uploader un fichier depuis son ComfyUI local vers le worker Modal.

### 🎯 À faire

- [ ] Dans l'extension JS, détecter les fichiers locaux dans le workflow
- [ ] Option A : Encoder les images en base64 et les inclure dans le JSON
  ```javascript
  // Avant d'envoyer, scanner le workflow pour les fichiers locaux
  function serializeWorkflow(workflow) {
      for (const [nodeId, node] of Object.entries(workflow)) {
          if (node.class_type === "LoadImage") {
              // Lire le fichier et l'encoder en base64
              const file = await loadImageFile(node.inputs.image);
              node.inputs.image_base64 = file; // champ custom
          }
      }
      return workflow;
  }
  ```
- [ ] Option B : Uploader d'abord les fichiers vers le worker Modal via
  `/upload/image`, puis envoyer le workflow avec les références

- [ ] Implémenter la gestion côté worker Modal (base64 → fichier temporaire)

### ✅ Critères de succès

- Un workflow img2img fonctionne de bout en bout
- L'image source est correctement transmise
- L'image générée est retournée et s'affiche dans ComfyUI

---

## 🧱 Phase 8 — Déploiement et documentation

**Objectif** : Automatiser le déploiement et documenter l'ensemble.

### 📖 Documentation à écrire

1. **README.md** — Présentation du projet
   - Problème résolu (12 Go VRAM, chaleur)
   - Architecture (schéma)
   - Prérequis (compte Modal, Python, ComfyUI local)

2. **INSTALL.md** — Guide d'installation
   - Configuration du compte Modal
   - Création du Secret HF
   - Création du Volume
   - Configuration de `models.py` et `plugins.py`
   - Sync des modèles : `modal run sync.py`
   - Déploiement des workers :
     ```bash
     modal deploy apps/l4_app.py
     modal deploy apps/l40s_app.py
     modal deploy apps/a100_app.py
     modal deploy apps/h100_app.py
     ```
   - Installation de l'extension JS dans ComfyUI local

3. **API.md** — Documentation de l'API
   - `POST /generate` — payload, exemples
   - `GET /gpus` — liste des GPUs disponibles
   - Exemple curl pour chaque type de GPU

4. **COST.md** — Guide des coûts
   - Barème Modal
   - Exemples de coût par rendu
   - Comment optimiser (choisir le bon GPU)
   - Le crédit Starter de 30$/mois

### 🚀 Scripts de déploiement

- [ ] Créer `deploy.sh` / `deploy.ps1` :
  ```bash
  #!/bin/bash
  echo "🚀 Déploiement des workers Modal..."
  modal deploy apps/l4_app.py
  modal deploy apps/l40s_app.py
  modal deploy apps/a100_app.py
  modal deploy apps/h100_app.py
  echo "✅ Fini !"
  ```

- [ ] Créer `deploy-all.sh` qui déploie tout en une commande

---

## 📐 Répartition par phases

### Sprint 1 — Core backend (Phases 0→2)
**Durée estimée : 1 jour**

| # | Phase | Fichiers créés | Dépend de |
|---|---|---|---|
| 0 | Préparation | — | — |
| 1 | Sync CPU | `sync.py`, `helpers.py` | 0 |
| 2 | Worker headless | `workers/base_worker.py` | 1 |

**Résultat** : Un worker Modal headless qui expose une API, avec sync CPU
indépendant. Testable en curl.

### Sprint 2 — Multi-GPU (Phases 3→4)
**Durée estimée : 0.5 jour**

| # | Phase | Fichiers créés | Dépend de |
|---|---|---|---|
| 3 | Workers multi-GPU | `workers/l4*.py`, `image.py` | 2 |
| 4 | Routeur API | `modal_api.py` | 3 |

**Résultat** : Quatre endpoints GPU fonctionnels, dispatch par le routeur.

### Sprint 3 — Frontend (Phases 5→6)
**Durée estimée : 0.5 jour**

| # | Phase | Fichiers créés | Dépend de |
|---|---|---|---|
| 5 | Endpoints ComfyUI | (dans base_worker) | 4 |
| 6 | Extension JS | `custom_nodes/modal_gateway/` | 5 |

**Résultat** : Le dropdown apparaît dans l'interface, Queue Prompt redirige
vers Modal.

### Sprint 4 — Polish (Phases 7→8)
**Durée estimée : 0.5 jour**

| # | Phase | Fichiers créés | Dépend de |
|---|---|---|---|
| 7 | Upload fichiers | (modif extension JS + worker) | 6 |
| 8 | Documentation | `README.md`, `INSTALL.md`, etc. | 7 |

**Résultat** : Projet complet, documenté, prêt à l'emploi.

---

## 🧪 Roadmap de test (à chaque phase)

```yaml
Phase 1 - Sync CPU:
  test: modal run sync.py
  vérifier: les modèles apparaissent dans le Volume

Phase 2 - Worker headless:
  test: curl -X POST https://xxx.modal.run/generate ...
  vérifier: workflow exécuté, image retournée

Phase 3 - Multi-GPU:
  test: curl avec gpu="L40S" puis gpu="H100"
  vérifier: le worker approprié est utilisé

Phase 4 - Routeur:
  test: POST /generate {"gpu": "A100", ...}
  vérifier: dispatch correct

Phase 5 - Endpoints ComfyUI:
  test: img2img workflow avec base64
  vérifier: l'image source est prise en compte

Phase 6 - Extension JS:
  test: cliquer Queue Prompt en mode "L4"
  vérifier: l'appel part vers Modal, pas vers localhost
```

---

## 🔮 Phases futures (v2)

- [ ] **Mode "Auto"** : analyse la VRAM nécessaire du workflow et choisit le GPU optimal
- [ ] **Fallback GPU** : si H100 pas dispo, tenter A100 automatiquement
- [ ] **Barre de progression** : progrès en temps réel du rendu distant
- [ ] **Cache de workflows** : éviter de renvoyer le même workflow plusieurs fois
- [ ] **Multi-utilisateurs** : partager les workers entre plusieurs personnes
- [ ] **Intégration Thunder Compute** : support d'un second provider dans le dropdown
- [ ] **Statistiques** : dashboard des coûts, nombre de rendus, GPU utilisés
