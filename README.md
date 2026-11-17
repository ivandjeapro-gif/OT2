# OreTracking V2 - DT Travel (Software)

Logiciel de gestion des activites de minage : application web + serveur + base
de donnees + dashboard live.

## Lancement

    python server.py [port]      # port par defaut : 8100

Puis ouvrir :

- Application    : http://localhost:8100/
- Dashboard live : http://localhost:8100/html/dashboard.html
- API            : http://localhost:8100/api/health

Base de donnees : data/oretracking.db (SQLite, creee et pre-remplie au
premier lancement).

## API REST

| Methode | Route                  | Description                                  |
|---------|------------------------|----------------------------------------------|
| GET     | /api/health            | etat serveur + base                          |
| GET     | /api/tables            | tables et nombre de lignes                   |
| GET     | /api/rows?table=X      | lire toutes les lignes                       |
| POST    | /api/rows              | {table, row:{...}}   inserer                 |
| PUT     | /api/rows              | {table, id, row:{...}} modifier              |
| DELETE  | /api/rows?table=X&id=N | supprimer une ligne                          |
| GET     | /api/query?sql=...     | SELECT seul (lecture)                        |
| POST    | /api/query             | {sql} toute requete                          |
| GET/POST| /api/config            | lire / enregistrer les parametres (Settings) |
| GET     | /api/alerts            | alertes recentes                             |
| POST    | /api/alerts            | {text, type, cat, engin}                     |
| DELETE  | /api/alerts?id=N       | supprimer une alerte (ou toutes si sans id)  |
| GET     | /api/stats             | KPI aggregees (dashboard)                    |
| GET     | /api/sync              | dernier horodatage de synchronisation        |

Tables : zones, trails, engins, voyages, alertes, params, comptes, meta.

## Fichiers

- `server.py`        serveur HTTP + API REST + SQLite (aucune dependance)
- `api.js`           client API (fetch) pour l'application et le dashboard
- `server-ui.js`     liaison UI : bouton Tester, Enregistrer parametres,
                     statut Sync, ouverture du dashboard
- `index.html`       application principale (Home, Live, Alerts, Inbox,
                     Settings, Sync)
- `app.js`           logique de la carte / couches / zones / trails
- `dashboard.html/.js/.css`  dashboard live (KPI, grades, voyants, carte)
- `scripts/serve_tiles.py`   serveur de tuiles MBTiles (carte)

## Cotes UI

- Settings > Connection : champ "URL API" (ex. http://192.168.x.x:8100),
  bouton Tester (verifie /api/health), bouton "Enregistrer les parametres"
  (ecrit dans la table params du serveur).
- Sidebar > Dashboard : ouvre le dashboard servi par le serveur.
- Sync : bouton "Synchroniser maintenant" met a jour l'horodatage serveur.