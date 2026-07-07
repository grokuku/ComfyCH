# Guide d'installation

Installation complète de **Modal Gateway for ComfyUI** — du compte Modal jusqu'au dropdown dans votre interface ComfyUI.

---

## 1. Prérequis

- **Compte Modal** → Créez-vous un compte gratuit sur [modal.com](https://modal.com)  
  30 $/mois de crédit offert (plan Starter), pas de frais fixes.
- **ComfyUI** installé localement ([guide officiel](https://github.com/comfyanonymous/ComfyUI))
- **Python 3.11+** avec [`uv`](https://docs.astral.sh/uv/#installation) installé
- **Git** installé
- **Token Hugging Face** (recommandé) → [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)

---

## 2. Cloner et configurer le projet

```bash
# Cloner le dépôt
git clone <url-du-depot>
cd modal-comfyui

# Installer les dépendances Python locales (modal, httpx, fastapi)
uv sync
```

### Configurer les modèles

```bash
cp models.example.py models.py
```

Éditez `models.py` pour lister les modèles à télécharger. Deux types :

- **Hugging Face** — via `repo_id` + `filename`
- **Externe** (CivitAI, etc.) — via `url` directe

```python
# Exemple models.py
models = [
    {
        "repo_id": "black-forest-labs/FLUX.1-dev",
        "filename": "flux1-dev.safetensors",
        "model_dir": "diffusion_models",
    },
    {
        "repo_id": "comfyanonymous/flux_text_encoders",
        "filename": "clip_l.safetensors",
        "model_dir": "text_encoders",
    },
]

models_ext = [
    {
        "url": "https://civitai.com/api/download/models/...",
        "filename": "mon-lora.safetensors",
        "model_dir": "loras",
    },
]
```

### Configurer les custom nodes

```bash
cp plugins.example.py plugins.py
```

Éditez `plugins.py` pour ajouter les custom nodes ComfyUI nécessaires à vos workflows.

```python
# Exemple plugins.py
comfy_plugins = [
    # IDs du ComfyUI Registry (pas les noms)
    "ComfyUI-WanVideoWrapper",
]

comfy_plugins_ext = [
    # Plugins installés depuis Git
    {
        "url": "https://github.com/author/custom-node.git",
        "branch": "main",
        "requirements": ["requirements.txt"],
    },
]
```

> **Astuce** : Si vous avez un fichier `workflow_api.json` à la racine du projet, les dépendances sont automatiquement détectées et installées à la construction de l'image.

---

## 3. Configurer Modal

### 3.1 Authentification

```bash
modal setup
```
Suivez les instructions dans le navigateur pour connecter votre compte Modal.

### 3.2 Créer les secrets

```bash
# Token Hugging Face (obligatoire pour les modèles gated)
modal secret create huggingface-secret HF_TOKEN=hf_votre_token_ici

# Clé API pour sécuriser l'accès au Gateway
modal secret create modal-gateway-key MODAL_GATEWAY_KEY=votre-cle-secrete-tres-longue
```

> **Important** : La clé API (`MODAL_GATEWAY_KEY`) protège vos endpoints `/generate`, `/upload/image`, etc. Choisissez une chaîne longue et aléatoire.

### 3.3 Créer le volume de modèles

```bash
modal volume create comfy-models
```

Ce volume stocke les poids des modèles et est partagé entre tous les workers GPU.  
Le premier To est gratuit, au-delà ~9¢/Go/mois.

---

## 4. Synchroniser les modèles

```bash
modal run sync.py
```

Cette commande lance un **conteneur CPU** (~5¢/h) qui télécharge tous les modèles listés dans `models.py` vers le volume `comfy-models`.

- Les modèles sont **cachés** dans `/cache` (réutilisés entre les runs)
- Des **symlinks** pointent vers les dossiers attendus par ComfyUI
- Le téléchargement est unique : si un fichier existe déjà dans le cache, il est ignoré

Durée indicative : 5-30 minutes selon le nombre et la taille des modèles.

---

## 5. Déployer l'API

```bash
modal deploy apps/all_in_one.py
```

Cette commande :

1. **Construit l'image Docker** : installe ComfyUI, les custom nodes, les dépendances
2. **Déploie le routeur FastAPI** : accessible publiquement
3. **Déploie les 4 workers GPU** : L4, L40S, A100, H100

Une fois terminé, **notez l'URL** retournée. Elle ressemble à :

```
https://votre-compte--modal-comfy-gateway-gateway.modal.run
```

> **⚠️ Gardez cette URL et votre clé API sous la main** — vous en aurez besoin pour configurer l'extension JS.

### Options avancées

- **Mode dev** : `modal serve apps/all_in_one.py` (URL temporaire, idéal pour les tests)
- **Logs** : `modal logs modal-comfy-gateway` pour voir les logs en temps réel

---

## 6. Installer l'extension dans ComfyUI

```bash
# Depuis la racine du projet
cp -r custom_nodes/modal_gateway /chemin/vers/ComfyUI/custom_nodes/
```

Sur Windows, utilisez l'Explorateur ou :

```cmd
xcopy /E /I custom_nodes\modal_gateway C:\chemin\vers\ComfyUI\custom_nodes\modal_gateway
```

---

## 7. Configurer l'extension JS

> ⚠️ **Étape OBLIGATOIRE** — L'extension ne fonctionnera pas sans cette configuration.

Éditez le fichier **`ComfyUI/custom_nodes/modal_gateway/web/modal_gateway.js`** et modifiez les deux constantes en haut du fichier :

```javascript
const CONFIG = {
    // ─── À MODIFIER APRÈS DÉPLOIEMENT ─────────────────────────────

    /** URL de votre API déployée (sans slash final) */
    API_URL: 'https://votre-compte--modal-comfy-gateway-gateway.modal.run',

    /** Clé API définie dans le secret modal-gateway-key */
    API_KEY: 'votre-cle-secrete-tres-longue',

    // ─── Fin des paramètres à modifier ────────────────────────────
};
```

- L'`API_URL` est l'URL retournée par `modal deploy apps/all_in_one.py`
- L'`API_KEY` doit correspondre exactement à la valeur du secret `modal-gateway-key`

---

## 8. Redémarrer ComfyUI

1. Arrêtez complètement ComfyUI (Ctrl+C / fermez la fenêtre)
2. Relancez-le :

```bash
# Depuis le dossier ComfyUI
python main.py
```

3. Ouvrez `http://localhost:8188` dans votre navigateur
4. Vous devriez voir apparaître le **sélecteur de GPU** 🎮 dans la barre d'outils

---

## ✅ Vérification

1. **Dropdown visible** ? → L'extension est bien chargée
2. **Sélectionnez "L4 — $0.80/h"** → le mode cloud est activé
3. **Cliquez sur Queue Prompt** → une notification "☁️ Transfert des fichiers..." apparaît
4. **Patientez 10-30s** → le GPU démarre (snapshot), le workflow s'exécute
5. **L'image apparaît dans le canvas** → tout fonctionne 🎉

Si vous voyez une erreur, ouvrez la console développeur (F12) de votre navigateur — les logs `Modal Gateway:` y sont visibles.

---

## 🔄 Mettre à jour

```bash
# 1. Pull des dernières sources
git pull

# 2. Redéployer l'API (nouvelle image construite automatiquement)
modal deploy apps/all_in_one.py

# 3. Mettre à jour l'extension (si modifiée)
cp -r custom_nodes/modal_gateway /chemin/vers/ComfyUI/custom_nodes/

# 4. Redémarrer ComfyUI
```

---

## ❓ Dépannage

| Problème | Solution |
|---|---|
| **Dropdown absent** | Vérifiez que l'extension est bien dans `custom_nodes/`, rechargez la page |
| **Erreur 401** | L'`API_KEY` dans le JS ne correspond pas au secret Modal |
| **Erreur 502** | Le worker n'a pas démarré — vérifiez les logs avec `modal logs modal-comfy-gateway` |
| **Timeout** | Le GPU met du temps à démarrer (premier déploiement = pas de snapshot). Réessayez. |
| **Modèle manquant** | Relancez `modal run sync.py` pour synchroniser les nouveaux modèles |
| **Image non affichée** | Vérifiez la console JS (F12) pour les erreurs CORS ou réseau |
