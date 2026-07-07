# 🗺️ Roadmap — Modal Gateway pour ComfyUI

> Fichier de référence : architecture, décisions, et prochaines étapes.
> Dernière mise à jour : juillet 2026

---

## 🎯 Vision

Permettre à ComfyUI (installé localement) de déporter sélectivement les calculs GPU vers Modal,
via un simple interrupteur dans l'interface. L'utilisateur garde une UI locale réactive pour la
création de workflows et le développement de custom nodes, et ne sollicite le cloud que pour
l'exécution des rendus lourds.

---

## 🧠 Problème résolu

- **Contrainte VRAM** : 12 Go de VRAM locale suffisent pour beaucoup de workflows, mais bloquent
  FLUX.1, Wan2.2, ou les gros batches.
- **Contrainte thermique** : Le serveur IA est dans le salon. En été (canicule, pas de clim),
  faire tourner le GPU local chauffe la pièce de façon insupportable. Le cloud permet de
  délocaliser la chaleur.
- **Contrainte financière** : Pas besoin de GPU cloud pour la navigation/l'édition — seulement
  pour les rendus ponctuels. Le paiement à la seconde de Modal est idéal.

---

## 🏗️ Architecture retenue

### Principe général

```
┌── Machine locale (salon) ──────────────────┐
│                                            │
│  ComfyUI (installation normale)             │
│  ├── Interface web locale (réactive)        │
│  ├── Custom nodes en développement          │
│  ├── GPU local (RTX 3060 12 Go)            │
│  └── Extension JS : Modal Gateway           │
│       ├── Dropdown dans la barre d'outils   │
│       └── Interception du "Queue Prompt"    │
│                                            │
└──────────┬─────────────────────────────────┘
           │ HTTP (workflow JSON sérialisé)
           ▼
┌── ☁️ Modal ─────────────────────────────────┐
│                                            │
│  Volume "comfy-models" (stockage partagé)   │
│  ├── Checkpoints, LoRAs, VAEs, etc.        │
│  └── Accessible par TOUS les workers       │
│                                            │
│  API Router (/generate)                    │
│  ├── /generate POST → dispatch vers worker │
│  │   selon le GPU choisi                   │
│  └── Retourne les images générées          │
│                                            │
│  Workers headless (ComfyUI sans UI web)    │
│  ├── L4   → workflows légers               │
│  ├── L40S → FLUX, workflows moyens         │
│  ├── A100 → Wan2.2, vidéo                 │
│  └── H100 → grosse prod, urgences          │
│                                            │
│  Sync (CPU, ~5¢/h)                        │
│  └── Télécharge les modèles dans le Volume │
│      sans GPU, au plus bas coût            │
│                                            │
└────────────────────────────────────────────┘
```

### Option A — ComfyUI headless sur Modal (recommandée)

Le backend Modal fait tourner un vrai ComfyUI, mais **sans interface web** (`@modal.web_server`
désactivé). Seule l'API REST de ComfyUI est exposée via `@modal.web_endpoint()`.

**Pourquoi c'est le choix retenu :**
- ✅ L'API de ComfyUI est déjà complète : envoie un `workflow_api.json`, reçois les images
- ✅ Compatibilité totale avec les custom nodes (mêmes plugins que le local)
- ✅ Le projet `modal-comfyui` existant fait déjà 90% du boulot (image Docker, plugins, modèles)
- ✅ L'utilisateur garde son ComfyUI local avec ses propres plugins en développement

### Ce qu'on change par rapport au projet actuel

| Dans le projet existant | Dans notre version |
|---|---|
| `@modal.web_server(8000)` | ❌ Supprimé (pas d'UI web distante) |
| Lancement de ComfyUI avec UI | ✅ Lancé en headless, API seulement |
| Les modèles dans `/cache` | ✅ Conservé (Volume partagé) |
| Les plugins via `plugins.py` | ✅ Conservé (même logique) |
| GPU fixe (L4) | ✅ Multiple workers (L4, L40S, A100, H100) |

---

## 🔄 Cycle de vie complet

### Phase 1 — Sync (setup initial + ajouts de modèles)

```
Déclencheur : l'utilisateur modifie models.py
             ou lance explicitement la synchro

1. Conteneur CPU (1 core, ~5¢/h) démarre sur Modal
2. Télécharge TOUS les modèles listés dans models.py
   (HuggingFace via huggingface_hub, externes via aria2c)
3. Écrit dans le Volume "comfy-models"
4. Conteneur s'arrête
5. ✅ Modèles disponibles pour tous les workers GPU

Coût : ~5-10¢ par synchro (selon le volume à télécharger)
Stockage :~9¢/Go/mois (1er To gratuit)
```

### Phase 2 — Rendu local

```
Hiver, 15°C dans le salon, pas besoin de chauffer le GPU

1. Utilisateur sélectionne "Local" dans le dropdown Modal Gateway
2. Queue Prompt fonctionne normalement
3. Le workflow s'exécute sur la RTX 3060 locale (12 Go VRAM)
4. ✅ Gratuit, ça chauffe un peu, c'est l'hiver
```

### Phase 3 — Rendu distant

```
Été, 38°C, le salon est un four

1. Utilisateur sélectionne "L40S", "A100" ou "H100" dans le dropdown
2. Queue Prompt → l'extension JS intercepte la requête
3. Le workflow est sérialisé en JSON et envoyé à l'API Modal
4. Modal :
   a. Démarre le worker GPU demandé (ou restore un snapshot)
   b. Les modèles sont déjà dans le Volume → pas de download
   c. Le worker exécute le workflow complet (headless)
   d. Renvoie les images générées
5. Les images arrivent dans ComfyUI local comme si c'était local
6. ✅ Le salon n'a pas chauffé d'un degré
```

---

## 🖥️ Interface utilisateur

### Dropdown dans la barre d'outils ComfyUI

Emplacement : dans la barre d'outils de ComfyUI, juste à côté du bouton "Queue Prompt".

Options du dropdown (définitives, après réflexion) :

```
[ 🖥️ Rendu ▼ ]

├── 🔴 Local             → GPU local (RTX 3060) — gratuit
├── 🟢 Modal L4          → $0.80/h  — 24 GB VRAM — tests, petits rendus
├── 🟡 Modal L40S        → $1.95/h  — 48 GB VRAM — FLUX, workflows moyens
├── 🟠 Modal A100 80GB   → $2.50/h  — 80 GB VRAM — Wan2.2, vidéo
└── 🔴 Modal H100        → $3.95/h  — 80 GB VRAM — grosse prod, urgences
```

> 💡 Les couleurs indiquent le niveau de coût.
> L'utilisateur voit le prix à l'heure directement dans le menu → transparence totale.

### Comportement attendu

- Le dropdown conserve son choix entre les sessions (stocké dans localStorage)
- Quand "Local" est sélectionné : Queue Prompt fonctionne normalement (pas d'interception)
- Quand un mode Modal est sélectionné :
  1. L'extension JS intercepte `POST /prompt`
  2. Sérialise le workflow en JSON
  3. Envoie à `https://api.modal.com/generate`
  4. Attend le résultat
  5. Injecte les images reçues dans ComfyUI

---

## 📁 Structure du projet (à venir)

```
modal-comfyui/
│
├── modal_api.py              ← Routeur API FastAPI sur Modal
├── workers/
│   ├── base_worker.py        ← Classe de base (Volume, snapshot, etc.)
│   ├── l4_worker.py          ← Worker GPU L4
│   ├── l40s_worker.py        ← Worker GPU L40S
│   ├── a100_worker.py        ← Worker GPU A100
│   └── h100_worker.py        ← Worker GPU H100
│
├── sync.py                   ← Script CPU pour télécharger les modèles
├── models.py                 ← Liste des modèles (copie de models.example.py)
├── plugins.py                ← Liste des plugins (copie de plugins.example.py)
│
├── custom_nodes/
│   └── modal_gateway/        ← Extension côté ComfyUI local
│       ├── __init__.py       ← (vide ou minime, juste pour l'activation)
│       └── web/
│           └── modal_gateway.js  ← L'extension JS qui ajoute le dropdown
│                                   et intercepte Queue Prompt
│
└── vendor_nodes/
    └── reverse_proxy_fix/    ← Conservé pour l'API Modal
```

---

## 🐣 Étapes de réalisation (ordre suggéré)

### Étape 1 — Backend Modal headless ✅ Prioritaire
- [x] Partir du projet actuel (`comfyui.py`)
- [ ] Supprimer `@modal.web_server(8000)` (plus d'UI web)
- [ ] Exposer l'API REST de ComfyUI (`/prompt`, `/upload/image`, etc.) via `@modal.web_endpoint()` ou un wrapper FastAPI
- [ ] Tester avec un appel curl depuis la machine locale : envoyer un workflow → recevoir une image
- [ ] Documenter l'URL d'API

### Étape 2 — Workers multiples ✅ Prioritaire
- [ ] Créer une classe de base `ComfyUIWorker` avec le Volume partagé
- [ ] Créer les sous-classes pour chaque GPU : `L4Worker`, `L40SWorker`, `A100Worker`, `H100Worker`
- [ ] Routeur API qui reçoit `{gpu_type, workflow_json}` et dispatch vers le bon worker
- [ ] Gérer le snapshot : idéalement un snapshot par type de GPU (L4 ≠ H100)

### Étape 3 — Sync CPU 💡 Important
- [ ] Créer `sync.py` : conteneur CPU qui télécharge les modèles dans le Volume
- [ ] Lancer automatiquement la synchro avant le premier déploiement
- [ ] Option : lancer la synchro manuellement via `modal run sync.py`

### Étape 4 — Extension JS locale 💡 Important
- [ ] Créer `modal_gateway.js` qui ajoute le dropdown dans l'interface ComfyUI
- [ ] Intercepter `POST /prompt` quand le mode Modal est sélectionné
- [ ] Gérer la sérialisation du workflow
- [ ] Gérer la réponse (images reçues → injectées dans ComfyUI)
- [ ] Afficher le statut de la requête (en cours, terminé, erreur)
- [ ] Stocker le dernier choix dans localStorage

### Étape 5 — Affinements 💡 Bonus
- [ ] Fallback GPU : si H100 pas dispo, essayer A100 automatiquement
- [ ] Indicateur de coût estimé avant de lancer le rendu
- [ ] Mode "auto" : choisir le GPU selon la taille du workflow (détection de la VRAM nécessaire)
- [ ] Barre de progression pendant le rendu distant
- [ ] Gérer les erreurs réseau proprement (timeout, retry)

---

## 💰 Budget estimé

### Coûts fixes (mensuels)

| Élément | Coût |
|---|---|
| Volume (modèles) | 0-5 $/mois selon la taille (1 To gratuit) |
| Plan Starter Modal | 0 $/mois (30 $ de crédit offert) |

### Coûts variables (usage)

| Usage | Durée typique | Coût |
|---|---|---|
| Sync des modèles (CPU) | ~30 min (une fois) | ~2.5 ¢ |
| Rendu SDXL sur L4 | ~30 sec | ~0.6 ¢ |
| Rendu FLUX sur L40S | ~1 min | ~3.2 ¢ |
| Rendu Wan2.2 sur A100 | ~2-3 min | ~12 ¢ |
| Rendu Wan2.2 sur H100 | ~2-3 min | ~20 ¢ |

> Avec les 30 $/mois de crédit Starter, ça représente des **centaines de rendus** par mois.

---

## 🔄 Alternatives explorées (juillet 2026)

> Conclusion : **Modal reste le meilleur choix** pour l'intégration programmatique
> via extension JS + API. Thunder Compute et RunPod pourraient être des plans B
> intéressants si le besoin évolue.

### Tableau comparatif des services

| Critère | Modal ⭐ | RunPod | Vast.ai | Thunder Compute ⛈️ |
|---|---|---|---|---|
| **Billing** | ✅ à la seconde | ✅ à la seconde | ❌ à l'heure | ⚠️ à la minute |
| **SDK Python / API** | ✅ Excellent | ⚠️ API Pods | ❌ Limité | ❌ CLI seulement |
| **Templates ComfyUI** | ❌ Manuel | ✅ One-click | ❌ Manuel | ✅ One-click |
| **Snapshots GPU** | ✅ Oui | ❌ Non | ❌ Non | ✅ Snapshots d'instance |
| **Volumes persistants** | ✅ Modal Volumes | ✅ Network Volumes | ❌ Local | ✅ Snapshots |
| **GPU détaché immédiatement** | ❌ 60s de scaledown | ✅ Instantané | ✅ Instantané | ✅ GPU-over-TCP |
| **Multi-workers (L4→H100)** | ✅ Volume partagé | ❌ Instances séparées | ❌ Instances séparées | ❌ Instances séparées |
| **Crédit gratuit** | ✅ 30 $/mois | ❌ Non | ❌ Non | ❌ Non |
| **Maturité / Communauté** | ✅ ✅ ✅ | ✅ ✅ ✅ | ✅ ✅ | ⚠️ Très récent |

### Prix détaillés

| GPU | Modal | RunPod (Secure) | Vast.ai | Thunder Compute |
|---|---|---|---|---|
| L4 (24GB) | **$0.80/h** | — | $0.32/h | — |
| L40S (48GB) | **$1.95/h** | ~$1.00/h | $0.47/h | $0.79/h (L40) |
| A100 80GB | **$2.50/h** | $1.39/h | $0.51/h | $1.09/h |
| H100 80GB | **$3.95/h** | $2.89/h | $2.00/h | $2.19/h |
| RTX 4090 (24GB) | — | $0.69/h | $0.35/h | — |
| RTX A6000 (48GB) | — | ~$0.60/h | $0.39/h | **$0.35/h** |

### Coût réel par rendu (avec billing à la seconde/minute)

| Usage | Durée | Modal | RunPod | Vast.ai¹ | Thunder² |
|---|---|---|---|---|---|
| Rendu SDXL L4 | 30s | $0.006 | — | $0.32 | — |
| Rendu FLUX L40S | 1min | $0.032 | $0.017 | $0.47 | $0.013 |
| Wan2.2 A100 | 3min | $0.125 | $0.070 | $0.51 | $0.055 |
| Wan2.2 H100 | 3min | $0.198 | $0.145 | $2.00 | $0.110 |

> ¹ Vast.ai est à l'heure → même pour 30s tu paies l'heure entière. Prix réel bien plus élevé.
> ² Thunder Compute est à la minute → 1min minimum même pour 30s de rendu.

### Quand choisir quoi ?

| Scénario | Service recommandé | Raison |
|---|---|---|
| **Extension JS + API automatisée** (notre projet) | **Modal** | SDK Python, volumes partagés, multi-workers |
| **Usage manuel via navigateur** (pas d'intégration) | **RunPod** ou **Thunder** | Templates ComfyUI, moins cher, paiement à l'usage |
| **Budget ultra-serré, OK avec l'instabilité** | **Vast.ai** | Prix plancher, mais fiabilité aléatoire |
| **Gros volume de rendus quotidiens** | **RunPod Community** ou **Thunder** | Moins cher à l'heure que Modal pour des sessions longues |
| **Petits tests rapides (< 1min)** | **Modal** | Per-second, pas de minimum |

### ✅ Verdict

**Modal reste le choix #1 pour ce projet** pour 3 raisons :

1. **SDK Python** : tout s'écrit en code, l'intégration avec une extension JS est
   naturelle (API REST → appel fetch depuis le navigateur)
2. **30 $/mois de crédit** : pour un usage modéré, le coût réel est **zéro**
3. **Architecture multi-workers avec Volume partagé** : un seul point d'entrée API
   qui dispatch vers L4/L40S/A100/H100 selon le choix du dropdown

**Thunder Compute** serait un excellent plan B si :
- Ils sortent un SDK Python / une API
- Le besoin devient plus "usage manuel" que "programmatique"

**RunPod** serait intéressant comme plan C si le Community Cloud (pas cher)
s'avérait suffisamment fiable pour de la prod.

---

## ❓ Questions en suspens

- [ ] Faut-il un snapshot GPU par type de GPU, ou un seul snapshot partagé ?
- [ ] Comment gérer les workflows qui utilisent des fichiers locaux (images uploadées) ?
- [ ] Faut-il synchroniser automatiquement les plugins entre local et Modal ?
- [ ] Comment exposer l'API Modal de façon sécurisée (token d'accès) ?
- [ ] Faut-il un mode "fallback automatique" où Modal est utilisé si la VRAM locale est insuffisante ?
- [ ] Comment gérer le démontage des fichiers dans ComfyUI quand on passe d'un worker à l'autre ?

---

## 📚 Références

- Projet existant : `github.com/caru-ini/modal-comfyui`
- API REST ComfyUI : `/prompt`, `/upload/image`, `/view`
- Documentation Modal Volumes : `modal.com/docs/guide/volumes`
- Documentation Modal GPU : `modal.com/docs/guide/gpu`
- Prix Modal : `modal.com/pricing`
