# API REST — Modal ComfyUI Gateway

Documentation complète des endpoints exposés par le routeur FastAPI déployé sur Modal.

**URL de base** : `https://votre-compte--modal-comfy-gateway-gateway.modal.run`

---

## Authentification

Tous les endpoints **sauf** `/health`, `/gpus`, `/docs` et `/openapi.json` nécessitent une clé API.

| Header | Valeur |
|---|---|
| `X-API-Key` | La clé définie dans le secret Modal `modal-gateway-key` |

**Exemple avec curl :**

```bash
curl -H "X-API-Key: votre-cle-secrete" https://xxx.modal.run/generate
```

Si la clé est absente ou incorrecte, le serveur répond :

```json
{
  "error": "Unauthorized",
  "message": "X-API-Key header required"
}
```

---

## Endpoints

### `POST /generate` — Lancer un workflow

Redirige un workflow ComfyUI vers le worker GPU de votre choix. Le worker démarre le workflow, génère les images et retourne les résultats encodés en base64.

**Requête :**

```json
{
  "workflow": { ... },
  "gpu": "L40S"
}
```

| Champ | Type | Requis | Défaut | Description |
|---|---|---|---|---|
| `workflow` | `object` | ✅ | — | Workflow ComfyUI au format API (JSON export depuis l'éditeur) |
| `gpu` | `string` | ❌ | `"L4"` | GPU cible : `L4`, `L40S`, `A100` ou `H100` |

**Réponse (succès) :**

```json
{
  "images": [
    {
      "data": "iVBORw0KGgo...",
      "filename": "ComfyUI_00001_.png",
      "subfolder": "",
      "type": "output"
    }
  ],
  "job_id": "a1b2c3d4-e5f6-..."
}
```

**Réponse (erreur) :**

```json
{
  "error": "GPU 'T4' not supported. Use: ['L4', 'L40S', 'A100', 'H100']"
}
```

**Exemple curl :**

```bash
curl -X POST https://xxx.modal.run/generate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: votre-cle" \
  -d '{
    "workflow": {
      "3": {
        "class_type": "KSampler",
        "inputs": {
          "seed": 42,
          "steps": 20,
          "cfg": 7,
          "sampler_name": "euler",
          "scheduler": "normal",
          "denoise": 1,
          "model": ["4", 0],
          "positive": ["6", 0],
          "negative": ["7", 0],
          "latent_image": ["5", 0]
        }
      }
    },
    "gpu": "L4"
  }'
```

---

### `POST /upload/image` — Uploader une image source

Transfère une image (PNG, JPG, etc.) vers le worker GPU. Nécessaire pour le mode img2img lorsque l'image est trop volumineuse pour l'encodage base64 inline.

**Requête :**

```
POST /upload/image?gpu=L40S
Content-Type: multipart/form-data

image: <fichier binaire>
```

| Paramètre | Type | Requis | Description |
|---|---|---|---|
| `gpu` (query) | `string` | ❌ | GPU cible (par défaut : dernier GPU utilisé ou `L4`) |
| `image` (body) | `file` | ✅ | Fichier image (PNG, JPG, WebP, etc.) |
| `overwrite` (body) | `string` | ❌ | `"true"` pour écraser un fichier existant |

**Réponse :**

```json
{
  "name": "mon_image.png",
  "subfolder": "",
  "type": "input"
}
```

**Exemple curl :**

```bash
curl -X POST https://xxx.modal.run/upload/image?gpu=L40S \
  -H "X-API-Key: votre-cle" \
  -F "image=@/chemin/vers/image.png" \
  -F "overwrite=true"
```

---

### `GET /view` — Récupérer une image générée

Télécharge une image depuis le répertoire output (ou input) d'un worker GPU.

**Requête :**

```
GET /view?filename=ComfyUI_00001_.png&subfolder=&gpu=L40S
```

| Paramètre | Type | Requis | Défaut | Description |
|---|---|---|---|---|
| `filename` | `string` | ✅ | — | Nom du fichier à récupérer |
| `subfolder` | `string` | ❌ | `""` | Sous-dossier dans le répertoire |
| `view_type` | `string` | ❌ | `"output"` | Type : `output`, `input` ou `temp` |
| `gpu` | `string` | ❌ | Dernier GPU utilisé | GPU cible |

**Réponse :** Binaire (image/png, image/jpeg, etc.)

**Exemple curl :**

```bash
curl -X GET "https://xxx.modal.run/view?filename=ComfyUI_00001_.png&subfolder=&gpu=L4" \
  -H "X-API-Key: votre-cle" \
  -o image_generée.png
```

---

### `GET /history/{job_id}` — Statut et résultat d'un job

Consulte l'historique d'exécution d'un prompt via l'API ComfyUI. Utile pour le polling asynchrone.

**Requête :**

```
GET /history/a1b2c3d4-e5f6-...
```

| Paramètre | Type | Requis | Défaut | Description |
|---|---|---|---|---|
| `job_id` (path) | `string` | ✅ | — | L'ID retourné par `/generate` |
| `gpu` (query) | `string` | ❌ | Dernier GPU utilisé | GPU cible |

**Réponse (job en cours) :**

```json
{
  "a1b2c3d4-e5f6-...": {
    "status": "pending"
  }
}
```

**Réponse (job terminé) :**

```json
{
  "a1b2c3d4-e5f6-...": {
    "status": "completed",
    "outputs": { ... }
  }
}
```

**Réponse (job inconnu ou expiré) :**

```json
{}
```

**Exemple curl :**

```bash
curl -X GET "https://xxx.modal.run/history/a1b2c3d4-e5f6-7890" \
  -H "X-API-Key: votre-cle"
```

---

### `GET /gpus` — Lister les GPUs disponibles (public)

Retourne la liste des GPUs disponibles avec leurs caractéristiques et prix. Aucune authentification requise.

**Requête :**

```
GET /gpus
```

**Réponse :**

```json
[
  {
    "id": "L4",
    "name": "NVIDIA L4",
    "vram": "24 GB",
    "price_per_hour": "$0.80"
  },
  {
    "id": "L40S",
    "name": "NVIDIA L40S",
    "vram": "48 GB",
    "price_per_hour": "$1.95"
  },
  {
    "id": "A100",
    "name": "NVIDIA A100 80GB",
    "vram": "80 GB",
    "price_per_hour": "$2.50"
  },
  {
    "id": "H100",
    "name": "NVIDIA H100",
    "vram": "80 GB",
    "price_per_hour": "$3.95"
  }
]
```

**Exemple curl :**

```bash
curl https://xxx.modal.run/gpus
```

---

### `GET /health` — Healthcheck (public)

Vérifie que le routeur FastAPI est opérationnel.

**Requête :**

```
GET /health
```

**Réponse :**

```json
{
  "status": "ok"
}
```

**Exemple curl :**

```bash
curl https://xxx.modal.run/health
```

---

## Diagramme de flux

```
Extension JS (navigateur)
       │
       │ POST /generate {workflow, gpu:"L40S"}
       │ X-API-Key: ***
       ▼
┌─────────────────────────────┐
│   Routeur FastAPI (public)  │
│   - Vérifie API Key         │
│   - Vérifie GPU disponible  │
│   - Dispatch vers le worker │
└──────────┬──────────────────┘
           │
           │ Appel Modal .remote()
           ▼
┌─────────────────────────────┐
│   Worker GPU (ex: L40S)     │
│   - Snapshot → ComfyUI prêt │
│   - POST /prompt (interne)  │
│   - Génération d'image      │
│   - Retour des résultats    │
└──────────┬──────────────────┘
           │
           │ JSON {images: [{data: base64, ...}], job_id}
           ▼
    Image affichée dans le canvas ComfyUI
```

---

## Code HTTP

| Code | Signification |
|---|---|
| `200` | Succès |
| `400` | GPU invalide ou paramètre manquant |
| `401` | Clé API manquante ou incorrecte |
| `500` | Erreur interne (worker injoignable, timeout, etc.) |

---

## Remarques

- **Timeout** : La génération d'image est limitée à 120s par le timeout du worker. Pour des workflows plus longs (vidéo), augmentez le timeout ou utilisez le polling via `/history`.
- **GPU Snapshots** : Le premier appel à un worker froid peut prendre 5-15s (démarrage ComfyUI). Les appels suivants sont quasi-instantanés grâce aux snapshots GPU.
- **CORS** : Les origines autorisées sont `http://localhost:8188`, `http://127.0.0.1:8188`, `https://localhost:8188`, `https://127.0.0.1:8188`.
