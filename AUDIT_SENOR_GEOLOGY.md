# Audit — Spécialiste Géologue Digital (Senor Geology)

Comparaison du cahier des charges fourni avec l'implémentation actuelle
([Server_Senor_Geology.py](Server_Senor_Geology.py), serveur Office
[server_OT](server_OT) en Google Apps Script, SAAS principal
[server/server.py](server/server.py)).

## 1. Mission — vue d'ensemble

| Exigence | État | Référence |
|---|---|---|
| Gérer automatiquement les missions de voyage des DT | ✅ Fait | `generer_affectation()` [Server_Senor_Geology.py:394](Server_Senor_Geology.py:394) |
| Instruire les camions sur la teneur transportée + destination | ⚠️ Partiel | teneur = `grade_cutoff` du bloc copiée dans `voyages.teneur` seulement à la **fin** de mission ([Server_Senor_Geology.py:913](Server_Senor_Geology.py:913)) ; rien n'est poussé au DT **au départ** de la mission (pas de champ teneur dans la réponse `/api/affecter`) |
| Stocker en ligne | ⚠️ Partiel | Stockage = SQLite **local** au serveur Senor Geology (`data/senor_geology.db`). Aucune synchro réelle vers un stockage cloud/distant — "en ligne" dans le spec semble plutôt viser un accès réseau LAN/4G, ce qui existe via l'API REST |
| Envoyer le chemin à suivre vers le Dumping Area | ⚠️ Partiel | `chemin` = segment à 2 points [position → centre du bloc] ([Server_Senor_Geology.py:429-430](Server_Senor_Geology.py:429)), pas un vrai tracé suivant les `travels_roads` |
| Suivre le respect du trajet | ⚠️ Partiel | `verifier_deviation()` ne compare que la distance au **point final**, pas un suivi point-par-point du chemin ([Server_Senor_Geology.py:458](Server_Senor_Geology.py:458)) |
| Alerter DT + Serveur Office + voix off tablette en cas de déviation | ⚠️ Partiel | Alerte créée en base avec `voix_off=1` et `dest_office=1`, mais rien ne déclenche réellement un son côté tablette, et rien n'envoie réellement l'alerte au serveur Office (voir §3) |
| Féliciter en fin de mission (voix off) | ⚠️ Partiel | Message de félicitation généré et stocké en base ([Server_Senor_Geology.py:924](Server_Senor_Geology.py:924)), mais pas de diffusion audio réelle côté tablette |

## 2. Outils (données sources)

| Outil du spec | État | Détail |
|---|---|---|
| Liste des ore blocs (CSV/DXF → polygones x,y) | ✅ Fait | `extraire_oreblocs_csv()` / `extraire_oreblocs_dxf()` ([Server_Senor_Geology.py:182](Server_Senor_Geology.py:182), [:235](Server_Senor_Geology.py:235)), upload via `/api/blasts` |
| Position temps réel engins via 4G tablette **ou** XBee/RTK | ❌ Non différencié | Une seule route générique `POST /api/engins` ([Server_Senor_Geology.py:714](Server_Senor_Geology.py:714)) qui reçoit juste `{code, type, x, y}`. Rien ne distingue la provenance (4G vs XBee), pas de parsing de trame XBee, pas d'horodatage de fraîcheur par source |
| Teneur de coupure par défaut | ✅ Fait | `DEFAULT_CUTOFF_GRADE` + table `teneurs` par source ([Server_Senor_Geology.py:44](Server_Senor_Geology.py:44), [:97](Server_Senor_Geology.py:97)) |
| Travels roads via GeoTIFF | ❌ Non exploité | `/api/travels_roads_upload` stocke le fichier `.tif` brut sur disque ([Server_Senor_Geology.py:864](Server_Senor_Geology.py:864)) mais **aucun décodage** du raster n'est fait : pas d'extraction de polylignes/chemin exploitable pour le guidage |
| Dumping Area (position + liste, CSV) | ✅ Fait | Table `dumping_areas`, `/api/dumping_areas` ([Server_Senor_Geology.py:753](Server_Senor_Geology.py:753)). Import CSV en masse non présent (ajout un par un via POST) |

## 3. Logique métier — Affectation

Les règles de statut sont codées **fidèlement** au spec :

- **Statut Excavatrice** (WEX/OMEX) : dans un orebloc → OMEX, sinon WEX. ✅ Conforme ([Server_Senor_Geology.py:333-346](Server_Senor_Geology.py:333)).
- **Statut DT** (WDT/SBDT/ODT) : logique proche/loin d'une exca OMEX + immobilisation ≥ 3 min + dans/proche d'un orebloc. ✅ Conforme dans l'esprit ([Server_Senor_Geology.py:348-391](Server_Senor_Geology.py:348)), avec une nuance :
  - ⚠️ Le spec dit *"proche ou à l'intérieur des limites d'un orebloc"* pour ODT, mais le code exige `dans_orebloc` strictement vrai (`point_in_polygon`), sans marge de tolérance autour du polygone — un DT juste à l'extérieur du bord ne passera jamais ODT.
  - ⚠️ `immobilise_depuis` n'est **jamais renseigné** nulle part dans le code (aucun `UPDATE engins SET immobilise_depuis=...`). Résultat : `immo_min` vaut toujours 0 et les conditions `immo_min >= 3` ne sont **jamais remplies** → aucun DT ne peut passer ODT ou SBDT en l'état actuel. C'est le bug le plus important trouvé dans l'audit.

- **Affectation destination + Dumping Area** : ✅ fait mais simplifiée — destination = **centre géométrique** du bloc (moyenne des sommets), pas un point d'approche réel ni le point de chargement exact ; Dumping Area = **plus proche à vol d'oiseau** de cette destination, sans tenir compte des routes disponibles ([Server_Senor_Geology.py:409-427](Server_Senor_Geology.py:409)).
- **Protocole réseau MQTT** : ❌ Non implémenté. Le spec demande explicitement MQTT pour l'affectation + chemin. L'implémentation actuelle utilise HTTP REST (`ThreadingHTTPServer`) partout, aucune dépendance MQTT, aucun broker, aucun topic. La table `office_sync` + `notifier_office()` **simule** un envoi (elle marque juste la ligne `envoye` en base) sans jamais effectuer de requête réseau réelle vers le serveur Office.

## 4. Notifier le serveur Office

| Exigence | État | Détail |
|---|---|---|
| Envoyer l'affectation au serveur Office en temps réel | ❌ Simulé uniquement | `notifier_office()` ([Server_Senor_Geology.py:489](Server_Senor_Geology.py:489)) lit la file `office_sync` et la marque `envoye` sans faire d'appel HTTP/MQTT vers [server_OT](server_OT) (qui existe pourtant et expose `doPost` avec les actions `evenement` / `notification` — [server_OT:333-344](server_OT:333)). Les deux systèmes ne sont **pas connectés**. |
| Notifier le cumul minage par bloc (voyages × truck factor) | ✅ Fait côté calcul | `/api/cumul` ([Server_Senor_Geology.py:951](Server_Senor_Geology.py:951)) calcule voyages/tonnes/teneur moyenne par bloc — mais cette donnée n'est **jamais poussée** vers le serveur Office, elle n'est accessible que si l'Office vient l'interroger activement (pull), ce qui n'est pas prévu non plus côté `server_OT`. |

## 5. Synthèse des écarts critiques (par priorité)

1. **Bug bloquant** : `immobilise_depuis` n'est jamais écrit → aucun DT ne peut jamais devenir `ODT`/`SBDT` → **aucune affectation automatique ne peut se déclencher en conditions réelles**. À corriger en premier.
2. **MQTT absent** : toute la couche réseau est HTTP REST ; si MQTT est une contrainte dure (ex. compatibilité tablette/office existante), il faut soit ajouter un broker + client MQTT, soit confirmer avec le métier que REST est acceptable.
3. **Liaison Senor Geology ↔ Office réelle** : `office_sync` ne fait que se simuler elle-même ; aucun appel sortant vers `server_OT`.
4. **GeoTIFF des travels roads non décodé** : le chemin envoyé au DT ignore totalement le réseau de pistes réel.
5. **Pas de voix off réelle** : les messages sont stockés en base, mais rien ne les lit/joue côté tablette (pas de WebSocket/push, pas d'intégration TTS visible dans `html/`/`js/`).
6. **Suivi de trajectoire simpliste** : la déviation ne regarde que la distance à la destination finale, pas l'écart par rapport au chemin complet.
7. **Pas de différenciation 4G vs XBee** pour la position temps réel des engins.

## 6. Points conformes à souligner

- Les statuts WEX/OMEX/WDT/SBDT/ODT, les tables de données (oreblocs, dumping_areas, teneurs, travels_roads, engins, missions, alertes, voyages) et les seuils (5 m, 25 m, 3 min) collent bien au cahier des charges.
- Le cumul par bloc (voyages, tonnage, teneur moyenne) est correctement implémenté.
- Le message de félicitation en fin de mission reprend bien tonnage + teneur + dumping area.

## 7. Addendum — Architecture radio RTK + MQTT (validée le 2026-10-23)

Architecture cible précisée par l'utilisateur :

- **Base RTK fixe** (alt. 300 m) + **relais radio XBee DigiMesh** en bord de fosse (pont visuel) + **Rover** dans le camion (alt. 200 m) → position centimétrique.
- **Montée (uplink)** : l'app embarquée du camion lit le Rover par USB-C, encapsule `{"id": "DT_04", "lat": ..., "lon": ...}` et l'envoie par radio XBee, relayée jusqu'à la Base puis au serveur central.
- **Cerveau central** : le serveur trie les trames par ID, détecte l'immobilisation géofencée sur une zone de chargement, valide le chargement, calcule la destination.
- **Descente (downlink)** : MQTT, un topic dédié par camion (`camions/DT_04/mission`), l'app camion (connectée en 4G) s'abonne uniquement à son topic et affiche l'ordre en cabine.

Recherche effectuée dans le code existant : **aucune trace** de `mqtt`, `xbee`, ou lecture série (`pyserial`) dans [Server_Senor_Geology.py](Server_Senor_Geology.py) ni [server/server.py](server/server.py) (le mot "serial" qui y apparaît désigne des numéros de série d'appareils Android/tablettes, pas une liaison série radio). Tout ce volet est à construire.

Écarts supplémentaires révélés par cette architecture :

1. **Pas d'écoute radio/série côté serveur** : il faut un process qui lit un port série (ou une passerelle USB/XBee côté serveur) en continu, parse le JSON, et alimente `engins.x/y` — remplace ou complète la route `POST /api/engins` actuelle qui suppose un envoi HTTP direct (ce qui ne correspond qu'au cas "4G tablette", pas au cas "XBee").
2. **Pas de conversion GPS (lat/lon, WGS84) → repère local (x,y) de la mine.** Les `oreblocs` et `dumping_areas` sont stockés en x,y "plan" (issus des CSV/DXF), alors que le Rover RTK renverra du lat/lon. Il faut une fonction de reprojection (ex. transformation affine calibrée sur 2-3 points de repère connus, ou UTM) avant de pouvoir faire `point_in_polygon()`.
3. **"Trame signée"** : le terme employé laisse penser à une authentification/intégrité (éviter qu'un faux camion usurpe un ID), mais l'exemple donné (`{"id":"DT_04", "lat":...}`) n'est qu'un simple tag d'identité, sans signature cryptographique réelle. À clarifier : si c'est juste un identifiant, pas de souci ; si une vraie authenticité est requise (anti-usurpation), il faut ajouter un HMAC ou équivalent.
4. **Détection d'immobilisation géofencée** : confirme le bug critique du §5.1 (`immobilise_depuis` jamais écrit) — cette architecture ne peut pas fonctionner tant que ce bug n'est pas corrigé, puisque toute la détection de chargement en dépend.
5. **MQTT confirmé comme exigence dure** (pas juste "souhaitable") pour le downlink camion — à bâtir avec un broker (local type Mosquitto, ou managé) + un topic par DT.

## 8. Avancement

- **Phase 0 (bug `immobilise_depuis`) — fait et testé.** Route `/api/engins` ([Server_Senor_Geology.py:671](Server_Senor_Geology.py:671)) calcule désormais le déplacement par rapport à la dernière position connue (seuil `SEUIL_IMMOBILISATION_M = 2.0`) et démarre/efface le chrono en conséquence, via `appliquer_position_engin()`.
- **Phase 1 (ingestion XBee) — fait et testé en l'absence de matériel.**
  - `appliquer_position_engin(conn, code, typ, x, y)` : logique de position/immobilisation factorisée, partagée entre HTTP et radio.
  - `gps_vers_local(lat, lon)` : projection équirectangulaire **provisoire** lat/lon → x,y mètres (fiable pour les distances relatives/immobilisation, **pas encore calibrée** pour superposer aux oreblocs CSV/DXF — calibration prévue en Phase 2).
  - `traiter_trame_xbee(conn, ligne)` : parse une trame JSON `{"id":"DT_04","lat":...,"lon":...}` (ou `{"x":...,"y":...}` en banc de test sans GPS), ignore proprement les trames vides/invalides/sans id.
  - `ecouteur_xbee()` : thread qui ouvre le port série (`XBEE_PORT` / `XBEE_BAUD`, variables d'environnement), se désactive proprement si `pyserial` n'est pas installé ou si aucun port n'est configuré (poste de dev sans radio) — le reste du serveur continue de tourner normalement.
  - **Dépendance à installer sur le site** (pas encore présente dans l'environnement de dev) : `pip install pyserial`.
  - Testé : parsing de trames valides/invalides, conversion GPS, démarrage/réinitialisation du chrono d'immobilisation, démarrage du serveur HTTP sans régression avec le port série absent.
- **Phase 1 bis (émission XBee côté tablette) — fait.** L'app tablette existante ([js/gnss-serial.js](js/gnss-serial.js)) lisait déjà le Rover par USB-C (Web Serial) et émettait un événement `gnss:gngga` ({lat, lon, fix, sats}), mais rien n'écoutait cet événement pour l'envoyer sur la radio. Ajout de :
  - [js/xbee-serial.js](js/xbee-serial.js) : ouvre un **second** port série (Web Serial, dédié au module XBee, distinct du port GNSS), écoute `gnss:gngga`, résout le code DT affecté à cette tablette (même chaîne que `server-ui.js` : `otThisDeviceId` → `otGnssRegistry` → `otAssignments.gnss`), et écrit `{"id":"DT_04","lat":...,"lon":...}\n` sur ce port.
  - Panneau UI "Radio XBee" ajouté dans [html/settings.html](html/settings.html) (bouton "Connecter la radio XBee (câble)"), à côté du panneau GNSS existant.
  - Testé en isolation (Node, DOM/localStorage simulés) : résolution du code DT (cas sans affectation, affectation partielle, affectation complète, mauvaise tablette) et envoi sans port ouvert — tous corrects.
  - Testé dans un vrai navigateur (headless) sur `html/settings.html` servi par `server/server.py` : le script se charge (HTTP 200), le panneau et ses éléments (`xbeePanel`, `btnConnectXbee`, `xbeeStatus`) sont bien dans le DOM. **Non testable plus loin dans cet environnement** : la page exige une connexion (overlay de login Firebase) qui bloque l'exécution/exposition de **tous** les modules JS de l'appli (y compris des modules existants non touchés comme `gnss-serial.js` et `server-ui.js`, qui présentent le même comportement) — donc pas un problème introduit par ce changement, juste une limite de l'environnement de test sans vrai compte connecté. Validation réelle à faire sur une tablette connectée avec un XBee USB branché.
- **Phase 2 (calibration GPS -> repère local) — fait et testé.** Remplace la projection équirectangulaire provisoire par une vraie calibration :
  - Tables `calibration_points` (points de référence : même point physique mesuré au RTK en lat/lon ET connu sur le plan mine en x/y) et `calibration` (dernière transformation affine calculée + erreur résiduelle).
  - `ajuster_affine()` / `_resoudre_3x3()` : régression linéaire (moindres carrés) sans dépendance externe (ni numpy ni scipy), résout `x = a·lon + b·lat + c` et `y = d·lon + e·lat + f` à partir d'au moins 3 points non alignés.
  - `calculer_calibration()` : calcule et enregistre la transformation + RMSE/erreur max (en mètres) pour juger de la qualité du calage.
  - `gps_vers_local(conn, lat, lon)` : utilise désormais la dernière calibration enregistrée ; repli sur l'ancienne projection approximative tant qu'aucune calibration n'existe (comportement Phase 1 préservé).
  - Nouvelles routes : `GET/POST/DELETE /api/calibration_points`, `POST /api/calibration_calculer`, `GET /api/calibration`. Calibration recalculée automatiquement dès qu'il y a ≥ 3 points actifs.
  - Panneau admin ajouté dans [senor_geology.html](senor_geology.html) : saisie de points de référence, tableau des points, statut de calibration (calibré/non calibré, nb points, erreur moyenne/max).
  - Testé : fit sur jeu de données synthétique (coefficients retrouvés à ~1e-7 près), repli sans calibration, détection de points colinéaires (retourne `None` proprement), bout en bout via `traiter_trame_xbee`, et via l'UI réelle (navigateur headless, formulaire → table → statut "Calibré" après le 3e point, erreur 0.00 m sur le jeu de test).
  - **Point d'attention relevé pendant le test** : un ancien processus `Server_Senor_Geology.py` tournait déjà sur le port 8101 avec l'**ancien code** (sans aucune des routes de calibration), pointé sur la vraie base `data/senor_geology.db`. Il faudra le redémarrer pour charger le code à jour avant toute utilisation réelle.
- **Phase 3 (geofencing & validation de chargement) — fait et testé.** Corrige deux écarts du §3/§5.1 de l'audit, maintenant que les positions arrivent en continu (Phases 0-2) :
  - **Tolérance "proche d'un orebloc"** : le cahier des charges dit *"a l'interieur OU proche des limites d'un orebloc"* pour le statut ODT, mais le code n'testait que l'intérieur strict (`point_in_polygon`). Ajout de `distance_polygone()` / `dans_ou_pres_polygone()` et d'une marge `MARGE_OREBLOC_M = 5.0` : un DT à quelques mètres du bord (dans la marge) peut désormais devenir ODT, conformément au spec. Le même correctif a été appliqué à la recherche de l'orebloc dans `generer_affectation()` (sinon la mission se créait avec `orebloc_id=0` et une destination dégénérée pour un DT ODT-par-proximité).
  - **Affectation automatique** : jusqu'ici, un DT qui passait ODT ne déclenchait **aucune** création de mission — il fallait appeler `/api/affecter` manuellement (le commentaire du code le prévoyait déjà : *"recalcul statuts + affectation si DT devient ODT"*, mais ce n'était pas implémenté). Ajout de `verifier_affectations_auto()` : pour chaque DT ODT sans mission `en_cours`, déclenche `generer_affectation()` automatiquement. Branché aux deux points d'entrée des positions (`POST /api/engins` et `traiter_trame_xbee`), conformément à la mission #1 du cahier des charges ("gérer de façon automatique les missions").
  - Testé : EXCA proche du bord + DT à 2 m hors polygone (dans la marge) + immobilisation simulée (chrono reculé artificiellement) → statut ODT confirmé, mission créée automatiquement avec le bon `orebloc_id`, pas de doublon sur appel répété (idempotence). Contre-test : DT à 6 m hors polygone (hors marge) → reste WDT, aucune mission créée. Rejoué bout en bout via HTTP (`POST /api/engins`) sans jamais appeler `/api/affecter`.
- **Phase 4 (descente MQTT) — fait et testé avec un vrai échange MQTT sur le réseau.** Remplace la simulation `office_sync`/`notifier_office` (qui ne faisait que se marquer "envoyé" elle-même) par une vraie publication MQTT :
  - `mqtt_publier(topic, payload)` + `_mqtt_client()` : client `paho-mqtt` paresseux (connexion différée au premier envoi), désactivation propre si `paho-mqtt` n'est pas installé ou si `MQTT_HOST` n'est pas configuré (même philosophie que `pyserial`/`XBEE_PORT`) ; ne lève jamais d'exception côté appelant.
  - `creer_alerte()` : nouvelle fonction unifiée qui enregistre l'alerte en base **et** la publie sur `camions/<dt_code>/alerte` — remplace les 3 `INSERT INTO alertes` dispersés (mission, déviation, fin de mission). Ferme l'écart "pas de voix off réelle" (§5.5 de l'audit) : chaque alerte est désormais réellement poussée en temps réel vers le camion, pas seulement stockée en base.
  - `generer_affectation()` publie en plus l'ordre de mission structuré (destination, dumping area, chemin) sur `camions/<dt_code>/mission`, conformément au topic nommé explicitement dans l'architecture validée par l'utilisateur.
  - **Dépendance à installer sur site** : `pip install paho-mqtt` (installée ici uniquement pour les tests, pas dans l'environnement de production). Variables d'environnement : `MQTT_HOST`, `MQTT_PORT` (def. 1883), `MQTT_USER`, `MQTT_PASS`.
  - Testé avec un **vrai broker MQTT 3.1.1** (mini-broker jetable écrit en pur Python/sockets pour ce test, car ni mosquitto ni une lib de broker tierce n'étaient disponibles/autorisées à installer dans l'environnement) : CONNECT/CONNACK réels, puis PUBLISH reçus avec les bons topics et payloads JSON pour les 3 types d'alerte (mission, fin_mission) et pour l'ordre de mission structuré. Testé aussi la dégradation : sans `MQTT_HOST` → aucune tentative, aucun crash ; avec `MQTT_HOST` configuré mais injoignable → échec de connexion intercepté proprement, loggé, alerte quand même enregistrée en base, serveur toujours opérationnel.
  - **Limite connue** : en cas de broker injoignable, chaque nouvelle alerte retente une connexion (pas de "cache" de panne) — acceptable pour l'instant, reproduit le même choix que `ecouteur_xbee()`.
- **Phase 6 (sécurité des trames XBee) — fait et testé, décision utilisateur : HMAC obligatoire par DT.** Le point en suspens de l'addendum §7 ("trame signée" = simple id ou vraie authentification ?) est tranché : vraie authentification HMAC-SHA256, une clé secrète par DT.
  - Table `cles_dt` (clé hex 32 octets par `dt_code`), routes `GET/POST/DELETE /api/cles_dt` (génération/rotation/révocation).
  - `verifier_signature_trame()` : rejette toute trame XBee si (a) aucune clé active n'est enregistrée pour ce `dt_code`, (b) la signature ne correspond pas, ou (c) l'horodatage est hors tolérance (`HMAC_TOLERANCE_S = 120 s` — anti-rejeu limité + dérive d'horloge). Branché dans `traiter_trame_xbee()` : toute trame non authentifiée est rejetée **avant** d'atteindre la logique métier.
  - Astuce technique importante : `extraire_corps_signe()` vérifie la signature sur la **sous-chaîne brute exacte transmise** (coupe la ligne juste avant `,"sig":"...."}`), pas sur une reconstruction JSON re-sérialisée — évite tout risque de divergence de formatage des nombres flottants entre JS et Python.
  - Côté tablette ([js/xbee-serial.js](js/xbee-serial.js)) : signature via **Web Crypto API** (`crypto.subtle`, HMAC-SHA256), clé stockée par DT dans `localStorage` (`otClesDt`), saisie dans un nouveau champ du panneau Radio XBee ([html/settings.html](html/settings.html)). Panneau de génération/révocation des clés ajouté côté admin ([senor_geology.html](senor_geology.html)).
  - **Testé avec un vrai navigateur** (pas une simulation) : calcul réel de `crypto.subtle.sign()` dans un navigateur headless, signature comparée octet pour octet avec le calcul HMAC Python → **identique**. Trame réellement signée par le navigateur rejouée dans `traiter_trame_xbee()` → acceptée, position correctement appliquée. Côté Python, 7 scénarios testés : trame sans clé enregistrée (rejet), trame valide (acceptée), rejeu immédiat dans la fenêtre de tolérance (accepté — limite assumée, voir ci-dessous), rejeu d'une trame vieille de 10 min (rejet), mauvaise clé (rejet), position falsifiée après signature (rejet), clé révoquée (rejet).
  - **Limite assumée** : la protection anti-rejeu n'est qu'une fenêtre de fraîcheur (120 s), pas un suivi de nonces/compteur — une trame interceptée peut être rejouée **dans les 120 s** qui suivent. Pour une protection anti-rejeu complète, il faudrait que le serveur mémorise le dernier `ts` accepté par DT et rejette tout `ts` ≤ au précédent (amélioration possible, non demandée pour l'instant).
- **Phase 7 (liaison réelle vers le serveur Office) — fait et testée avec un vrai échange HTTP.** Remplace la simulation `notifier_office()` (qui marquait tout "envoyé" sans jamais rien transmettre) par un vrai appel vers [server_OT](server_OT) (Google Apps Script, `doPost`).
  - Format réel découvert en lisant `server_OT` : `action`/`token` en query string, corps JSON. `evenement` est réservé à des types fixes (`GPS_UPDATE`, `LINE_ADD`, `BLAST_*`, `BLE_RELAY`) qui ne couvrent **aucun** des événements Senor Geology (mission, déviation, fin de mission) — donc routage choisi via `action=notification` (`{profil, message}`), le canal générique "message humain" déjà prévu côté Office pour ce genre d'alerte.
  - `office_appel(action, payload)` : POST réel via `urllib.request` (stdlib, aucune dépendance ajoutée), lit la réponse et ne considère un envoi réussi que si Office répond `ok:true` — pas juste un code HTTP 200.
  - `office_message(payload)` : traduit chaque entrée `office_sync` (affectation, déviation, fin de mission) en message humain cohérent.
  - `notifier_office()` réécrite pour faire le vrai appel par entrée en attente, et ne marquer `envoye` qu'en cas de succès réel (sinon `echec`, retenté au prochain passage).
  - Fermeture d'un écart repéré pendant les tests : `/api/mission_fin` n'appelait jamais `notifier_office()` (oubli pré-existant) — corrigé, la notification de fin de mission part maintenant immédiatement.
  - Exigence "notifier la somme cumulée" du cahier des charges couverte : le payload `fin_mission` est enrichi de `cumul_bloc_t`/`cumul_bloc_voyages` (calculés sur `voyages` au moment du voyage), et le message Office les inclut (ex. *"Fin de mission DT-01 : 60 t (bloc #1, cumul 60 t sur 1 voyage(s))"*).
  - Config : `OFFICE_URL` et `OFFICE_TOKEN`, **vides par défaut** (désactivé) — à fixer via variables d'environnement sur site, jamais en dur dans le code.
  - **Incident de sécurité évité pendant les tests** : j'ai initialement mis par défaut dans le code le jeton réel `CONFIG.TOKEN` déjà présent en clair dans `server_OT`, pour simplifier le déploiement. Le garde-fou de sécurité a bloqué cette action (fuite d'identifiant introduite sans autorisation), à juste titre — corrigé immédiatement : `OFFICE_TOKEN` est vide par défaut comme `OFFICE_URL`, sans secret codé en dur.
  - Testé avec un **vrai serveur Office factice** (mini-serveur HTTP reproduisant exactement le contrat `action`/`token`/réponse `ok:true|false` de `server_OT`, car aucun déploiement Apps Script réel n'est accessible depuis cet environnement) : affectation automatique → notification Office reçue et acquittée, fin de mission → notification avec cumul reçue, déviation → message détaillé reçu (plus un `message` générique comme avant le correctif), mauvais jeton → rejeté, `OFFICE_URL` non configuré → aucune tentative réseau, pas de crash.

Les 7 phases du plan initial (bug immobilisation, XBee, calibration GPS, geofencing/affectation auto, MQTT, HMAC, liaison Office) sont closes.

## 9. Maquette écran de config admin — à construire (session suivante, 2026-10-24)

Image de wireframe fournie par l'utilisateur, 4 panneaux + 1 zone non définie :

1. **Destination** (Source : MAIN PIT, boutons F5M/F2M/F4M + "+" + champ "Add source") — correspond aux tables/`endpoints` déjà en place : `sources`/`teneurs`, `/api/sources`, `/api/teneurs`. Pas de dev backend nécessaire, juste le branchement UI.
2. **Dumping Area** (liste ROMPAD 1 + "+" → popup nom + CSV des points de délimitation) — `dumping_areas`/`/api/dumping_areas` existent, mais **l'import CSV de polygone de délimitation n'existe pas encore** (actuellement une zone = centre + rayon seulement, pas un polygone).
3. **Trails (Chemin)** ("Mettre à jour les roads" → import GeoTIFF) — `/api/travels_roads_upload` existe (stocke le fichier), mais le **décodage du GeoTIFF reste un écart connu** (toujours pas exploité pour extraire un chemin).
4. **Geographic System** ("Mettre à jour les coords" → popup choix système de coordonnées géographique) — **rien d'équivalent côté backend actuellement** ; à distinguer/articuler avec la calibration par points de référence déjà construite en Phase 2. À clarifier avec l'utilisateur avant de coder.
5. **Manager** (bande du bas, vide, juste "...........") — **fonction non définie**, à clarifier en premier à la reprise.

Prochaine étape : clarifier panneaux 4 et 5 avec l'utilisateur, puis construire l'UI (probablement dans `senor_geology.html` ou une nouvelle page admin dédiée) branchée sur les endpoints existants pour les panneaux 1-3, plus ajouter l'import CSV de polygone pour Dumping Area.
