<div align="center">

# 🎮 Modal Gateway for ComfyUI

**Déportez vos rendus ComfyUI sur GPU cloud — L4, L40S, A100 ou H100 — sans quitter votre interface locale.**

[![Modal](https://img.shields.io/badge/Modal-Cloud%20GPU-7B2FF7?logo=modal)](https://modal.com)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Workflow%20Editor-FF6B35)](https://github.com/comfyanonymous/ComfyUI)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green)]()

</div>

---

## 🔥 Problème

- VRAM limitée (12 Go sur GPU grand public)
- Chaleur en été, ventilateurs qui s'emballent
- Besoin ponctuel de GPU puissant (FLUX, Wan2.2, vidéo)
- Files d'attente interminables sur les instances partagées

## ✅ Solution

Une **extension ComfyUI** + une **API Modal headless** qui intercepte "Queue Prompt" et redirige le workflow vers le GPU cloud de votre choix. Le rendu s'affiche directement dans votre canvas local comme si rien n'avait changé.

```
┌─ Machine Locale (ComfyUI) ──────────────────────┐
│                                                   │
│  ┌─────────────────────────────────────────┐      │
│  │ 🎮  L4 — $0.80/h  ▼  [▶️ Queue Prompt] │      │
│  └─────────────────────────────────────────┘      │
│         │                                         │
│         │ POST /generate {workflow, gpu}          │
│         ▼                                         │
├─────────┼─────────────────────────────────────────┤
          │
          ▼
┌─ ☁️ Modal ────────────────────────────────────────┐
│                                                     │
│  ┌──────────┐  ┌───────────┐  ┌──────────┐  ┌───┐ │
│  │ L4Worker │  │ L40SWorker│  │A100Worker│  │H100│ │
│  │ (24GB)   │  │ (48GB)    │  │ (80GB)   │  │80GB│ │
│  │ $0.80/h  │  │ $1.95/h   │  │ $2.50/h  │  │$3.95│ │
│  └──────────┘  └───────────┘  └──────────┘  └────┘ │
│                                                     │
│  📦 Volume partagé comfy-models                     │
│  ⚡ GPU Snapshots (démarrage < 5s)                  │
│  🔌 Auto-scaledown (arrêt si inactif)               │
│                                                     │
└─────────────────────────────────────────────────────┘
```

## ✨ Fonctionnalités

| Fonctionnalité | Détail |
|---|---|
| **5 modes de rendu** | Local (gratuit) + 4 GPUs cloud (L4, L40S, A100, H100) |
| **Fallback local automatique** | Si Modal est indisponible, le rendu repasse en local |
| **img2img transparent** | Les images LoadImage sont encodées et uploadées vers le worker |
| **GPU Snapshots** | Warm start en ~5s grâce aux snapshots Modal |
| **Auto-scaledown** | Le container s'arrête après 30-60s d'inactivité |
| **Sync CPU** | Synchronisation des modèles sur un conteneur CPU (~5¢/h) |
| **API Key** | Authentification par header `X-API-Key` |
| **CORS** | Compatible avec ComfyUI sur localhost:8188 |

## 📦 Prérequis

- **Compte Modal** → [modal.com](https://modal.com) (30 $/mois de crédit offert)
- **ComfyUI** installé localement
- **Python 3.11+** avec `uv` installé
- **Hugging Face** token (recommandé pour les modèles gated)

## ⚡ Installation (3 lignes)

```bash
git clone <votre-repo> && cd modal-comfyui
uv sync                                                       # Dépendances Python
modal run sync.py                                             # Sync des modèles
modal deploy apps/all_in_one.py                               # Déploiement de l'API
```

Puis installez l'extension dans ComfyUI :

```bash
cp -r custom_nodes/modal_gateway /chemin/vers/ComfyUI/custom_nodes/
```

📖 Voir le **[guide d'installation complet](INSTALL.md)** pour les détails.

## 📚 Documentation

| Document | Description |
|---|---|
| [`INSTALL.md`](INSTALL.md) | Installation pas à pas (compte Modal, secrets, sync, déploiement) |
| [`API.md`](API.md) | Documentation complète de l'API REST |
| [`COST.md`](COST.md) | Guide des coûts et optimisation budget |
| [`models.example.py`](models.example.py) | Configuration des modèles à synchroniser |
| [`plugins.example.py`](plugins.example.py) | Configuration des custom nodes ComfyUI |

## 🏗️ Architecture du projet

```
├── apps/
│   └── all_in_one.py        # App unique : 4 workers GPU + routeur FastAPI
├── workers/
│   ├── base_worker.py       # Classe de base ComfyWorker (cycle de vie + proxy API)
│   ├── l4_worker.py         # Spécialisation GPU L4
│   ├── l40s_worker.py       # Spécialisation GPU L40S
│   ├── a100_worker.py       # Spécialisation GPU A100-80GB
│   └── h100_worker.py       # Spécialisation GPU H100
├── custom_nodes/
│   └── modal_gateway/       # Extension ComfyUI (JS + CSS)
├── helpers.py               # Fonctions de download mutualisées
├── sync.py                  # Sync CPU des modèles
├── image.py                 # Image Docker mutualisée
├── models.py                # Liste des modèles à télécharger
├── plugins.py               # Configuration des custom nodes
└── comfyui.py               # App monolithique (héritage, dépréciée)
```

## 🛠️ Stack technique

- **[Modal](https://modal.com)** — Infrastructure serverless GPU
- **[FastAPI](https://fastapi.tiangolo.com/)** — Routeur API public avec CORS
- **[ComfyUI](https://github.com/comfyanonymous/ComfyUI)** — Moteur de workflow
- **[JavaScript vanilla](https://developer.mozilla.org/en-US/docs/Web/JavaScript)** — Extension navigateur (aucune dépendance)

## 🤝 Contribuer

Les PRs sont les bienvenues ! Les axes d'amélioration :
- Support file/folder du workflow ComfyUI
- Optimisation des snapshots GPU
- Nouveaux workers (par ex. `T4`)
- Amélioration de l'UX de l'extension JS

---

<div align="center">
  <sub>Fait avec ☕ par <a href="https://github.com/caru-ini">caru-ini</a></sub>
</div>
