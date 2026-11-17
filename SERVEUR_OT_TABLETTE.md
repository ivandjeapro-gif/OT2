# Serveur_OT (Apps Script) — Fonctions du contrat tablette

> **Mise a jour 08/11 — reponses au rapport tablette (§5, D1, D2, D3)**
>
> - **§5 mapping ore** : chaque ligne de `ore_dig_bloc` porte maintenant
>   `blast` (numero du blast) et `carte` (code RL complet), en plus de
>   `option label` (nom du bloc, pas un rang). Le numero de bloc affichable
>   = `option label` ; le rang du polygone = `polygone` (1..N).
> - **D1** = Option A confirmee : `blasts` lit `ONGLET_ORE_DIG_BLOC` (deja
>   filtre), avec repli sur `ONGLET_DIG_BLOC` si vide.
> - **D2** = Option JPG confirmee : les plans viennent de `blast_upload`
>   (map_contenu -> Drive). Le nom du fichier depose = code carte exact.
> - **D3** = confirme : les 9 types sont dans `TYPES_ACCEPTES` (L.698 de
>   server_OT), avec validations GPS_UPDATE / POSITION_PERLE / MOYENNE_PERLE.

## Contexte

Ce document inventorie **toutes les actions que la tablette Spotter appelle réellement**
(code Flutter lu dans `lib/data/`), et l'état d'implémentation dans `server_OT`.
Il sert de feuille de route de conformité : une action manquante ou un format
non conforme fait tourner la tablette dans le vide (ou pire, la fait retenir
ses événements en file à l'infini).

La configuration vit dans les **Propriétés du script** (Script Properties) —
aucun identifiant n'est codé en dur dans les fonctions :

| Propriété | Usage | Défaut (si absente) |
|---|---|---|
| `SPREADSHEET_ID` | classeur cible | `CONFIG.SPREADSHEET_ID` |
| `ONGLET_DIG_BLOC` | onglet des polygones (Base de données Dig Plan) | `dig_bloc` |
| `ONGLET_ORE_DIG_BLOC` | onglet des blocs minerais lus par la tablette | `ore_dig_bloc` |

---

## A. GET (lectures)

### 1. `?action=referentiels` — ✅ implémenté
- Appelé par : `Synchronisation.unePasseReferentiels()` (~40 s) + au login.
- Réponse attendue : `ok`, `referentiels_version`, `profils[]`, `equipements[]` (spotters), `dts[]` (flotte).
- **Format tablette** : `dts[] = {role, type, id, actif}` — le serveur OT expose déjà
  ce format (`canon_flotte` en sortie).

### 2. `?action=blasts` — ✅ implémenté (en cours d'enrichissement)
Le plus critique. Format exact attendu par la tablette :
```
ok
blasts[] : {
  numero, carte (legacy), cartes[], libelle_carte,
  nombre_blocs, plan_url,
  blocs_par_carte[] : { carte, flitch, blocs[], plan_url }
}
```
⚠️ La tablette **ne lit que `blocs_par_carte[].blocs`** — numéros **déjà filtrés**
(pas de stérile "W") : le serveur doit filtrer.

**Ajout du 08/11** : les polygones du CSV "Base de données" Dig Plan arrivent
dans l'onglet `ONGLET_DIG_BLOC` via `?action=onglet_upload`, les blocs
qualifiés dans `ONGLET_ORE_DIG_BLOC`. La tablette lit `ore_dig_bloc` pour
les blocs précis. Voir la section "Onglets blocs" plus bas.

### 3. `?action=plans` — ✅ implémenté
- Appelé par : `TelechargerPlans.unePasse()`.
- Réponse : `ok, dossier, nombre, plans[] : {carte, id, nom, url}` (URL Drive directe).
- **Une liste vide est une information** (purge locale) ; `ok:false` = ne rien toucher.

### 4. `?action=sante` — ✅ implémenté
- Écran Paramètres / diagnostic de la tablette.
- Réponse : `ok, service, protocole, onglets{}, plans, base_url`.

### 5. `?action=notifications&depuis=<id>` — ✅ implémenté
- Ordres du Boss, incrémental par `id`.
- Réponse : `ok, notifications[], derniere_id`.

### 6. `?action=affectation` — ✅ implémenté
- Mission en cours du DT.
- Réponse : `ok, affectation {destination, dumping_area, mission, alerte, parcours}, valide_le`.

---

## B. POST (écritures)

### 7. `?action=evenement` — ✅ implémenté
- Appelé par : `TransmetteurHttp.Envoyer()` — la file locale de la tablette.
- Corps = enveloppe CDC :
```json
{
  "event_id":      "uuid-v4",
  "gateway_id":    "SP-001",
  "source_device": "SP-001",
  "event_type":    "...",
  "timestamp":     "2026-11-08T08:24:42Z",
  "vers_protocole":"1.0",
  "payload":       { ... }
}
```
- Réponse attendue :
```json
{ "ok": true, "ack": { "event_id": "...", "status": "SYNCED"|"DOUBLON", "received_at": "..." } }
```
- `SYNCED` **et** `DOUBLON` marquent l'événement comme envoyé (idempotence).
- **Types obligatoires à accepter** (sinon 400 → la tablette renvoie à l'infini) :
  - `GPS_UPDATE`
  - `LINE_ADD`
  - `BLAST_LOCK`
  - `BLAST_UNLOCK`
  - `BLAST_SEND`
  - `BLE_RELAY`
  - `POSITION_PERLE`
  - `MOYENNE_PERLE`
  - `CHARGEMENT`

---

## C. Onglets blocs (ajout du 08/11)

Ces actions sont appelées **par la passerelle** (server.py), pas par la tablette
directement. Elles alimentent les onglets que la tablette lit.

### `POST ?action=onglet_upload` — ✅ implémenté (fonction `ongletBlocsUpload`)
Corps :
```json
{ "numero": "119", "cible": "blocs"|"ore", "lignes": [ [entete...], [ligne], ... ] }
```
- `lignes[0]` = entête CSV (`option label, polygone, hole, id, x, y, ...`)
- lignes suivantes = **une ligne par sommet** (`blocs`) ou **par bloc qualifié** (`ore`)
- Comportement : purge d'abord les lignes de CE blast (repérées par la colonne
  `polygone`), crée l'onglet au besoin (nom venant des propriétés), puis ajoute.

### `POST ?action=onglet_purge` — ✅ implémenté (fonction `ongletBlocsPurge`)
Query : `?action=onglet_purge&numero=119`
- Purge **intégrale** des lignes du blast dans les **deux** onglets
  (`ONGLET_DIG_BLOC` + `ONGLET_ORE_DIG_BLOC`).
- Appelé à la **suppression** d'un blast : la tablette ne doit plus voir ni
  polygones ni blocs d'un blast supprimé.

### Fonctions associées
| Fonction | Rôle |
|---|---|
| `propOu(cle, defaut)` | valeur de Script Property avec repli sur `CONFIG` |
| `classeurBlocs()` | Spreadsheet déduit des propriétés |
| `ongletDigBloc(cible)` | nom d'onglet (`blocs`→`dig_bloc`, `ore`→`ore_dig_bloc`) |
| `feuilleBlocs(cible)` | feuille cible (créée au besoin) |
| `ongletBlocsUpload(json)` | écriture/replacement des lignes d'un blast |
| `ongletBlocsPurge(params)` | purge intégrale des deux onglets |

---

## D. Actions internes (passerelle ↔ classeur, hors tablette)

| Action | Rôle | Statut |
|---|---|---|
| `blast_upload` | écrire un blast + ses blocs + la carte (Drive) | ✅ existant |
| `dig_plan_suppr` | retirer un blast du classeur | ✅ existant |
| `dig_plan` | lecture des blasts (miroir inverse) | ✅ existant |
| `dig_referentiel` | sources/teneurs/DA (miroir inverse) | ✅ existant |
| `donnees` (POST) | écriture générique d'un onglet | ✅ existant |
| `donnees` (GET) | lecture brute d'un onglet | ✅ existant |
| `sante` | diagnostic de déploiement | ✅ existant |
| `migrer` | structuration des onglets | ✅ existant |
| `diagnostic` | anomalies des référentiels | ✅ existant |
| `notification` | écriture d'une notification | ✅ existant |

---

## E. Points de vigilance (à retester après déploiement)

1. **Redeploiement obligatoire** après toute modification du script :
   *Déployer > Modifier > Nouvelle version > Déployer* — sinon Google exécute
   l'ancienne version et les nouvelles actions renvoient « action POST inconnue ».
2. **`evenement` doit accepter les 9 types** listés plus haut. Un type refusé
   fait boucler la file de la tablette indéfiniment (400 → retry).
3. **`blocs_par_carte[].blocs` doit être pré-filtré** (pas de "W") : le filtrage
   minerai/stérile est une responsabilité du **serveur**, pas de la tablette.
4. **`onglet_upload` purge-avant-écriture** garantit l'intégrité en cas de
   re-push d'un même blast (pas de doublon de sommets).
5. **Une liste `plans` vide est une information** (purge côté tablette) :
   ne jamais renvoyer `ok:false` par précaution — ça bloque la purge.
6. **Aucune valeur en dur** : les onglets blocs viennent des Script Properties
   (`ONGLET_DIG_BLOC`, `ONGLET_ORE_DIG_BLOC`), repli sur les défauts.
