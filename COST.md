# Guide des coûts Modal

Comprenez et maîtrisez votre budget cloud pour Modal Gateway.

---

## 💰 Crédit gratuit

**Plan Starter** : 30 $/mois de crédit offert, **sans frais fixes**.

- Valable à la fois pour le GPU et le CPU
- Crédit renouvelé chaque mois
- Pas d'engagement — vous pouvez arrêter à tout moment
- Au-delà du crédit : tarification à l'usage (pay-as-you-go)

> **Astuce** : Avec 30 $/mois, un utilisateur modéré ne dépasse jamais le crédit offert.

---

## 💻 Tarifs GPU (Modal, juillet 2026)

| GPU | VRAM | Prix/h | Idéal pour |
|---|---|---|---|
| **L4** | 24 Go | **0,80 $** | SDXL, SD1.5, petits rendus, tests |
| **L40S** | 48 Go | **1,95 $** | FLUX, SD3.5, workflows moyens, ControlNet |
| **A100 80GB** | 80 Go | **2,50 $** | Wan2.2, vidéo, gros batches, très haute résolution |
| **H100** | 80 Go | **3,95 $** | Grosse production, urgences, entraînement léger |

> **Note** : Les prix Modal sont à la seconde — vous ne payez que le temps réel d'exécution, pas l'heure entamée.

---

## 📊 Coût réel par rendu

Estimation basée sur des workflows typiques (temps GPU réel, scaledown inclus) :

| Usage | GPU | Temps GPU | Coût par rendu |
|---|---|---|---|
| **SDXL** (1024×1024, 20 steps) | L4 | ~30s | **~0,6 ¢** |
| **SDXL** avec 2 ControlNet | L4 | ~60s | **~1,3 ¢** |
| **FLUX.1-dev** (1024×1024, 25 steps) | L40S | ~60s | **~3,2 ¢** |
| **FLUX.1-pro** (ultra qualité) | L40S | ~2 min | **~6,5 ¢** |
| **Wan2.2** (vidéo 5s, 480p) | A100 | ~3 min | **~12 ¢** |
| **Wan2.2** (vidéo 10s, 720p) | A100 | ~6 min | **~25 ¢** |
| **SD3.5** (1024×1024) | L40S | ~45s | **~2,4 ¢** |

---

## 🎯 Avec 30 $/mois

| Usage intensif | Nombre de rendus |
|---|---|
| SDXL (L4) | **~5 000 rendus** |
| FLUX (L40S) | **~940 rendus** |
| Wan2.2 5s (A100) | **~250 vidéos** |
| Mix 50% SDXL + 50% FLUX | **~2 000 rendus** |

---

## 📦 Stockage Volume

Les volumes Modal servent à stocker les poids des modèles.

| Métrique | Tarif |
|---|---|
| **1er To** | **Gratuit** |
| Au-delà | ~9 ¢/Go/mois |

**Coût typique** : Un ensemble de modèles FLUX + SDXL + Wan2.2 pèse environ 50-80 Go → **gratuit**.

---

## ⚙️ Optimisations pour réduire la facture

### 1. Choisir le bon GPU

Le piège le plus fréquent : utiliser un H100 pour du SDXL. C'est 5× plus cher pour le même résultat.

| Si vous faites... | Prenez... | Économie |
|---|---|---|
| SDXL / SD1.5 | **L4** (0,80 $/h) | — |
| FLUX / SD3.5 | **L40S** (1,95 $/h) | −51% vs H100 |
| Vidéo Wan2.2 | **A100** (2,50 $/h) | −37% vs H100 |
| Urgence / prod | **H100** (3,95 $/h) | — |

### 2. Scaledown window

```python
# Dans apps/all_in_one.py
scaledown_window = L4Worker.scaledown_window  # 30s pour L4/L40S
# scaledown_window = A100Worker.scaledown_window  # 60s pour A100/H100
```

- **30s** suffit pour une utilisation interactive (SDXL, FLUX)
- **60s** recommandé pour les workflows longs (vidéo)
- Plus la fenêtre est courte, moins vous payez d'inactivité

### 3. GPU Snapshots

Les snapshots GPU (activés par défaut) réduisent le temps de démarrage de ~30s à ~5s. Moins de temps GPU = moins cher.

```python
enable_memory_snapshot=True
experimental_options={"enable_gpu_snapshot": True}
```

### 4. Sync CPU (pas de GPU)

```bash
modal run sync.py
```

La synchronisation des modèles s'exécute sur un **conteneur CPU** (1 CPU, 2 Go RAM) à **~5 ¢/h** au lieu de 0,80 à 3,95 $/h.

### 5. Mode local par défaut

L'extension JS garde le mode "Local (gratuit)" par défaut. Vous ne dépensez rien tant que vous ne sélectionnez pas un GPU cloud dans le dropdown.

### 6. Pas de frais fixes

Modal ne facture aucun coût fixe mensuel. Si vous n'utilisez pas le service, votre facture est **0 $** (dans la limite du crédit).

---

## 🔍 Surveillance de vos coûts

1. **Dashboard Modal** → [modal.com/dashboard](https://modal.com/dashboard) — visualisation des coûts par app
2. **Logs en temps réel** :
   ```bash
   modal logs modal-comfy-gateway
   ```
3. **Alertes de budget** : Configurez des seuils dans votre compte Modal (Settings > Budget)

---

## 💡 Scénarios réels

### Utilisateur loisir (10 $/mois)
- 200 rendus SDXL sur L4
- 50 rendus FLUX sur L40S
- **Coût : ~7 $** *(dans le crédit gratuit)*

### Utilisateur régulier (25 $/mois)
- 500 rendus SDXL sur L4
- 200 rendus FLUX sur L40S
- 20 vidéos Wan2.2 sur A100
- **Coût : ~22 $** *(dans le crédit gratuit)*

### Utilisateur intensif (50 $/mois)
- 1000 rendus SDXL sur L4
- 500 rendus FLUX sur L40S
- 50 vidéos Wan2.2 sur A100
- **Coût : ~48 $** *(30 $ de crédit + 18 $ facturés)*

---

## 📝 Résumé

```
┌─────────────────────────────────────────────────┐
│           Budget mensuel Modal                  │
├─────────────────────────────────────────────────┤
│  Crédit gratuit        30 $                     │
│                                                 │
│  Usage typique         5-25 $                   │
│  Reste dans le crédit  ✅ Oui                    │
│                                                 │
│  Clé pour économiser   🎯 Choisir le bon GPU    │
│  Pire erreur           🚫 H100 pour du SDXL     │
└─────────────────────────────────────────────────┘
```
