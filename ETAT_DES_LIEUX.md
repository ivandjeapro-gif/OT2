# OreTracking V2 — état des lieux

Arrêté au 2026-10-28. Distingue ce qui est **vérifié en conditions réelles** de ce qui est seulement écrit.

---

## 1. Composants

| Composant | Fichier | Hébergement | État |
|---|---|---|---|
| SaaS (pages + API SQLite) | [`server/server.py`](server/server.py) | local `:8100` | en service |
| Passerelle Senor Geology | [`Server_Senor_Geology.py`](Server_Senor_Geology.py) | local `:8101` | en service |
| **Serveur_OT** | [`server_OT`](server_OT) | Apps Script | **déployé, opérationnel** |
| Serveur_SG (cerveau cloud) | [`server_SG`](server_SG) | Apps Script | écrit, **non déployé** |
| Base + fichiers | Google Sheets + Drive `OT_Test` | Google | en service |

**Pages** : `home`, `live`, `alerts`, `inbox`, `settings`, `sync`, `senor`, `digplan`.

---

## 2. Serveur_OT — en production

Sert déjà les tablettes et les applis mobiles. Jamais interrompu, hors l'incident de démarrage (code collé avant les propriétés).

**Configuration** : 2 propriétés saisies à la main (`SPREADSHEET_ID`, `TOKEN`), toutes les autres écrites par le serveur. Aucun secret dans le code.

| Action | Rôle |
|---|---|
| `blasts` · `referentiels` · `affectation` · `plan` | existant, sert les tablettes |
| `evenement` · `notification` · `donnees` | existant |
| `sante` | diagnostic + `prochaine_etape` |
| `init_drive` | crée `OT_Test/{Plans,Sources,Pistes,Exports,Tablettes}` et **enregistre les 5 IDs** |
| `pistes_upload` | reçoit le GeoJSON du SaaS, **enregistre `FICHIER_PISTES_ID`** |
| `dig_plan` · `blast_upload` · `dig_plan_suppr` | plans de dig |
| `dig_referentiel` (GET/POST) | sources et teneurs |

**Onglets ajoutés** : `dig_plan`, `dig_blocs`, `dig_sources`, `dig_teneurs`. Les onglets de production (`blasts`, `blocs`, `cartes`, `flotte`…) n'ont **pas** été modifiés.

### Vérifié en réel

- `?action=blasts` rend les 4 blasts de production, plans résolus sur Drive
- Appel **navigateur → Apps Script sans blocage CORS**
- `configuration: "ok"` — toutes propriétés posées
- Référentiel écrit par le serveur : 2 sources, 3 teneurs
- Arborescence Drive créée sous `OT_Test`

---

## 3. Page Dig Plan

Planche unique **A4** (ratio 1.414 mesuré), deux onglets dans la même zone :

- **Map** — affiche uniquement le JPG importé, rien de généré
- **Progress** — contours de tous les blocs, **surlignage temps réel** du bloc en cours de minage (excavatrice `OMEX` détectée par point-dans-polygone, rafraîchi toutes les 4 s)

**Formulaire** : `Name` + 4 champs **fichier** (`Name_dxf`, `Name_Geosum`, `Name_servey`, `Name_Map`) dont les libellés suivent le champ `Name`. Boutons Enregistrer/Annuler affichés **uniquement** en création ou modification ; champs verrouillés en consultation.

**Deux backends interchangeables** via [`js/ot-config.js`](js/ot-config.js), pilotés depuis Settings :
- passerelle locale `:8101` — **vérifié**
- Serveur_OT en ligne — **pas encore vérifié de bout en bout**

### Vérifié en réel

- Tous les boutons testés par clic : sélection, création, modification, suppression, annulation, onglets, champs fichier
- Enregistrement → `PUT` → **persistance en base** confirmée
- Filtrage des blasts par source ; blast créé sous WestPit n'apparaît pas sous Main Pit
- Carte stockée en **URL** et non en base64 : cellule la plus longue = 45 caractères
- Parseurs DXF/CSV du navigateur : **10/10 cas identiques** au Python
- Géométrie de `server_SG` : **23/23 cas identiques** au Python

---

## 4. Données réelles branchées

Sources issues de [`data/Mining Areas.csv`](data/Mining%20Areas.csv) — règle retenue : une source = une fosse d'extraction.

- **Main Pit**, **WestPit** (les deux marquées « Or » dans le CSV)
- Teneurs : F5M (5-7), F2M (2-5), F4M (4-6), rattachées à Main Pit
- `RomPad*` et `WasteDump*` sont des destinations, pas des sources

---

## 5. Écarts connus

| Écart | Conséquence |
|---|---|
| **Pistes en lon/lat, zones en UTM** | le Dijkstra renvoie `null` ; `pistes:"ok"` ne signifie que « fichier lisible » |
| `server_SG` jamais déployé | le cerveau cloud n'existe pas encore en service |
| 3 correctifs en attente de redéploiement | comptage des plans, `"VIDE"` au lieu d'un `"ok"` trompeur, `force=1` sur `init_drive` |
| `dumping_areas` pollué | restes de tests (`ROMPAD HEX` à x=500000) ; `Main Pit` y figure à tort |
| WestPit sans teneur | à configurer depuis Senor Geology |
| GeoTIFF des pistes non décodé | écart d'audit toujours ouvert |
| Anti-rejeu HMAC = fenêtre 120 s | une trame interceptée reste rejouable dans ce délai |
| Voix off non jouée | messages publiés, rien ne les lit côté tablette |
| Aucune sauvegarde du Sheet | à prévoir avant production |

---

## 6. Contraintes structurantes

**Apps Script ne peut pas appeler la passerelle** (NAT, pas d'IP publique). Les échanges vont toujours passerelle → cloud ; l'inverse se fait par interrogation périodique. Ce n'est pas une limite d'Apps Script : tout cloud aurait la même.

**Quota.** ~560 appels/jour en test (2 h) = **15 % du palier gratuit**. Tient jusqu'à ~12 h d'exploitation/jour ; au-delà, compte Workspace nécessaire.

**Jamais dans une cellule Sheets** : un fichier binaire (plafond 50 000 caractères). Les fichiers vont dans Drive, la base ne garde que l'URL.

---

## 7. Prochaines étapes

1. Basculer Dig Plan sur Serveur_OT et créer un blast de bout en bout *(en cours)*
2. Convertir le GeoJSON des pistes en UTM — débloque le calcul de parcours
3. Regrouper les 3 correctifs en un redéploiement
4. Déployer `server_SG` (positions, statuts, affectation)
5. Nettoyer `dumping_areas` et réconcilier avec `Mining Areas.csv`
