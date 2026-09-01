# 🗺️ Roadmap — Modal Gateway pour ComfyUI

> Fichier de référence : architecture, décisions, et prochaines étapes.
> Dernière mise à jour : août 2026
>
> **État actuel** : le projet est **opérationnel**. Les sections « Architecture »,
> « Structure du projet » et « Étapes » ci-dessous reflètent le code réel ; voir
> aussi le `README.md` pour l'état des lieux à jour.

## ✅ Fonctionnalités livrées

- **Routeur FastAPI partagé** (`gateway_router.py`) : `/generate` (idempotent via
  `request_id`), `/upload/image`, `/view`, `/history`, `/gpus`, `/health` —
  factorisé pour les 3 variantes d'app (`apps/all_in_one*.py` : 4 / 3 / 1 GPU).
- **Auth du gateway** : clé API `X-API-Key` (Secret Modal `comfy-gateway-secret`,
  fail-closed 401/503), clé générable et saisissable dans les paramètres.
- **Token Modal de compte** saisissable dans l'UI (remplace `modal token set` en CLI).
- **Extension JS** : dropdown GPU persistant (localStorage), file d'attente avec
  badge, interception de Queue Prompt, overlay d'affichage des images, sauvegarde
  locale automatique dans `ComfyUI/output/`, modale de configuration complète
  (connexion, token, modèles, plugins, user settings, sync, deploy, logs SSE).
- **Sync** : modèles locaux (sélectionnés dans l'UI) + HuggingFace/externes vers
  le volume partagé ; user settings synchronisés ; uploads d'images persistés
  dans le volume (survivent au scaledown des workers).
- **Sécurité & fiabilité** : anti path traversal sur `/save-local`, idempotence
  (pas de double facturation), verrous sur les opérations Modal, subprocess en
  thread (event loop non bloquée), tests automatisés (`tests/test_gateway.py`).
- **Sync modèles par upload chunké** : les modèles locaux sont streamés par chunks
  de 100 Mo directement dans le volume (plus de rebuild d'image à chaque sync).
- **Custom nodes synchronisés par volume** : packaging tar.gz (protection path
  traversal + swap atomique) vers le volume `comfy-custom-nodes`, symlinkés par
  les workers au démarrage — plus besoin de redeploy pour changer un node.

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

### Option A — ComfyUI headless sur Modal (choisie et déployée)

Le backend Modal fait tourner un vrai ComfyUI **headless** (pas d'UI web). Les
workers exposent l'API REST de ComfyUI via des méthodes proxy
(`workers/base_worker.py`) appelées par le routeur FastAPI (`gateway_router.py`).

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
Déclencheur : l'utilisateur modifie models.py, sélectionne des modèles locaux
             dans l'UI (bouton « Sync Models »), ou lance `modal run sync.py`

1. Conteneur CPU (1 core, ~5¢/h) démarre sur Modal
2. Copie les modèles locaux sélectionnés (embarqués dans l'image via config.json)
   et télécharge les modèles listés dans models.py
   (HuggingFace via huggingface_hub, externes via aria2c) ;
   symlinks créés dans /cache + manifest (model_manifest.json)
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

Options du dropdown (implémentées dans `web/modal_gateway.js`) :

```
[ 🎮 ▼ ]

├── 🖥️  Local (gratuit)         → GPU local (RTX 3060)
├── ☁️  L4 — $0.80/h            → 24 GB VRAM — tests, petits rendus
├── ☁️  L40S — $1.95/h          → 48 GB VRAM — FLUX, workflows moyens
├── ☁️  A100 80GB — $2.50/h     → 80 GB VRAM — Wan2.2, vidéo
└── ☁️  H100 — $3.95/h          → 80 GB VRAM — grosse prod, urgences
```

> 💡 Le prix à l'heure est affiché directement dans le menu → transparence totale.

### Comportement (implémenté)

- Le dropdown conserve son choix entre les sessions (localStorage)
- "Local" sélectionné → Queue Prompt fonctionne normalement (pas d'interception)
- Mode Modal sélectionné → l'extension intercepte `queuePrompt`, sérialise le
  workflow (images locales encodées en base64 ou uploadées via `/upload/image`),
  l'envoie à `https://xxx.modal.run/generate` avec la clé `X-API-Key` et un
  `request_id` d'idempotence, puis affiche les images reçues dans un overlay
  (et les sauvegarde dans `ComfyUI/output/`)
- Une **file d'attente** (badge 🎮 cliquable) traite les rendus un par un ; en
  cas d'erreur réseau, la requête est retentée avec le même `request_id` — le
  serveur ne relance pas un job déjà exécuté (pas de double facturation)

### Modale de configuration (⚙️)

- **Connexion API Modal** : URL du gateway + clé API (bouton « générer », hint
  de la commande `modal secret create comfy-gateway-secret API_KEY=<clé>`)
- **Token Modal (compte)** : Token ID + Token Secret du site Modal — remplace
  `modal token set` en CLI (credentials stockés par le CLI, pas dans la config)
- **Modèles** : détection des modèles locaux, sélection (filtre « workflow
  seulement »), puis « Sync Models »
- **User Settings** : statut du dossier `user/` local + sync vers le volume
- **Custom Nodes** : détection des nodes locaux (git / non-git), sélection —
  un redeploy est nécessaire pour prendre effet
- **Déploiement** : bouton « Deploy API » + statut
- **Statut Modal** : CLI installé, authentifié, volume, secret, API configurée
- **Logs** : flux SSE en direct des opérations (sync/deploy)

---

## 📁 Structure du projet (actuelle)

```
ComfyCH/
│
├── __init__.py                ← Extension ComfyUI : routes /api/modal/*
│                                (config, status, sync, deploy, token, logs SSE,
│                                 détection modèles/plugins, save-local)
├── web/modal_gateway.js       ← Extension JS : dropdown, file d'attente, modale
│
├── gateway_router.py          ← Routeur FastAPI partagé (auth + idempotence)
├── auth.py                    ← Clé API du gateway (Secret Modal)
├── image.py                   ← Image Docker des workers (pas de téléchargement de modèles)
├── sync.py                    ← Sync CPU (modèles locaux + HF + user settings)
├── helpers.py                 ← Téléchargements + secrets HuggingFace
├── models.py / plugins.py     ← Listes de modèles / custom nodes
│
├── apps/
│   ├── all_in_one.py          ← App 4 GPU (L4, L40S, A100, H100)
│   ├── all_in_one_lite.py     ← App 3 GPU (sans H100)
│   └── all_in_one_l4.py       ← App L4 seul (plan Starter)
│
├── workers/
│   ├── base_worker.py         ← ComfyWorker : ComfyUI headless, proxy API,
│   │                            snapshots, uploads persistés dans le volume
│   ├── l4_worker.py … h100_worker.py  ← GPU + scaledown par classe
│
├── vendor_nodes/
│   └── reverse_proxy_fix/     ← Fix sauvegarde de workflows derrière le proxy Modal
└── tests/test_gateway.py      ← Tests sans dépendances (25 checks)
```

---

## 🐣 Étapes de réalisation (ordre suggéré)

### Étape 1 — Backend Modal headless ✅ Terminé
- [x] Partir du projet actuel (`comfyui.py`)
- [x] Supprimer `@modal.web_server(8000)` (plus d'UI web)
- [x] Exposer l'API REST de ComfyUI (`/prompt`, `/upload/image`, etc.) via le worker headless + routeur FastAPI
- [x] Tester avec un appel curl depuis la machine locale : envoyer un workflow → recevoir une image
- [x] Documenter l'URL d'API (README)

### Étape 2 — Workers multiples ✅ Terminé
- [x] Créer une classe de base `ComfyWorker` avec le Volume partagé
- [x] Créer les sous-classes pour chaque GPU : `L4Worker`, `L40SWorker`, `A100Worker`, `H100Worker`
- [x] Routeur API qui reçoit `{gpu, workflow}` et dispatch vers le bon worker
- [x] Gérer le snapshot : `@modal.enter(snap=True/False)` + fallback cold start

### Étape 3 — Sync CPU ✅ Terminé
- [x] Créer `sync.py` : conteneur CPU qui télécharge les modèles dans le Volume
- [ ] Lancer automatiquement la synchro avant le premier déploiement (première exécution manuelle pour l'instant)
- [x] Lancer la synchro manuellement via `modal run sync.py` ou le bouton « Sync Models » de l'UI

### Étape 4 — Extension JS locale ✅ Terminé
- [x] Créer `modal_gateway.js` qui ajoute le dropdown dans l'interface ComfyUI
- [x] Intercepter Queue Prompt quand le mode Modal est sélectionné
- [x] Gérer la sérialisation du workflow (images locales incluses)
- [x] Gérer la réponse (overlay d'affichage + sauvegarde locale)
- [x] Afficher le statut de la requête (file d'attente, notifications)
- [x] Stocker le dernier choix dans localStorage

### Étape 5 — Affinements (restants)
- [ ] Fallback GPU : si H100 pas dispo, essayer A100 automatiquement
- [ ] Indicateur de coût estimé avant de lancer le rendu
- [ ] Mode "auto" : choisir le GPU selon la taille du workflow (détection de la VRAM nécessaire)
- [ ] Barre de progression pendant le rendu distant
- [x] Gérer les erreurs réseau proprement (timeout, retry + idempotence `request_id`)

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
- [x] Comment gérer les workflows qui utilisent des fichiers locaux (images uploadées) ?
      → ✅ Réglé : les uploads sont persistés dans le volume (`/cache/uploads`)
        et restaurés au démarrage de chaque worker (`_restore_uploads_from_volume`).
- [ ] Faut-il synchroniser automatiquement les plugins entre local et Modal ?
      (actuellement : détection + sélection manuelle dans l'UI, redeploy requis)
- [x] Comment exposer l'API Modal de façon sécurisée ? — Clé API `X-API-Key`
      (Secret Modal `comfy-gateway-secret`, fail-closed 401/503) + `/generate`
      idempotent (request_id) pour éviter la double facturation.
- [ ] Faut-il un mode "fallback automatique" où Modal est utilisé si la VRAM locale est insuffisante ?
- [ ] Comment gérer le démontage des fichiers dans ComfyUI quand on passe d'un worker à l'autre ?

---

## 📚 Références

- Projet existant : `github.com/caru-ini/modal-comfyui`
- API REST ComfyUI : `/prompt`, `/upload/image`, `/view`
- Documentation Modal Volumes : `modal.com/docs/guide/volumes`
- Documentation Modal GPU : `modal.com/docs/guide/gpu`
- Prix Modal : `modal.com/pricing`
