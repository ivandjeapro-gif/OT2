# Broker MQTT OreTracking — mise en place sur Oracle Cloud (offre gratuite)

Ce broker relaie en temps réel les positions des DT : tablettes (4G) → broker → passerelle → Live / dashboard.

- **Logiciel :** Mosquitto, open source.
- **Contrat des sujets :** `ELEMENTS CONJOINTS/19 Novembre/CDC_MQTT_APP_DT_2026-11-19.md`.

| Qui | Quoi |
|---|---|
| **Vous** (exploitation) | Étapes 1 à 4 : compte Oracle, machine, ouverture des ports, nom de domaine. Elles demandent votre identité et une carte bancaire. |
| **Kit** (ce dossier) | Étapes 5 à 7 : installation, comptes, vérification. Il suffit de lancer des commandes. |

> ⚠ **Aucun mot de passe ne doit circuler en clair** (chat, courriel, note). Les mots de passe générés à l'étape 6 vont
> directement dans votre gestionnaire de mots de passe, puis dans les paramètres des tablettes et de la passerelle.

---

## Pourquoi Oracle Cloud (et pas Google Cloud)

| | Oracle Cloud « Always Free » | Google Cloud « Free Tier » |
|---|---|---|
| Machine gratuite | oui, sans limite de durée | oui (e2-micro) |
| Régions de la machine gratuite | votre région d'origine (choisissez **Marseille** ou **Paris**, proches de la Côte d'Ivoire) | **États-Unis seulement** |
| Trafic sortant gratuit | **10 To / mois** | **1 Go / mois** |

Le trafic du suivi (positions relayées vers la passerelle) représentera plusieurs Go par mois : il dépasserait l'offre
gratuite de Google. Les conditions des offres changent : vérifiez-les au moment de l'inscription
([Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm),
[Google Cloud Free](https://cloud.google.com/free/docs/compute-getting-started)).

**Point d'attention Oracle :** sur un compte resté en offre gratuite, Oracle peut **récupérer une machine jugée inactive**
(processeur très peu utilisé pendant 7 jours). C'est le cas d'un broker Mosquitto, très économe. Pour l'éviter, passez le
compte en **« Pay As You Go »** (étape 1.4). Les ressources « Always Free » y restent gratuites ; seul un dépassement serait
facturé. Faites-vous confirmer ce point par les conditions Oracle en vigueur.

---

## Étape 1 — Créer le compte Oracle Cloud (vous)

1. Allez sur https://www.oracle.com/cloud/free/ → **Start for free**.
2. **Région d'origine (« Home Region ») : France South (Marseille) ou France Central (Paris).** Ce choix est **définitif**,
   et la machine gratuite ne peut être créée que dans cette région.
3. La carte bancaire sert à vérifier l'identité ; elle n'est pas débitée sur l'offre gratuite.
4. Conseillé : **Billing → Upgrade to Pay As You Go** (voir le point d'attention ci-dessus).

## Étape 2 — Créer la machine (vous)

Console → **Compute → Instances → Create instance**

| Réglage | Valeur |
|---|---|
| Nom | `ot-broker-mqtt` |
| Image | **Canonical Ubuntu 24.04** |
| Shape | **VM.Standard.E2.1.Micro** (« Always Free-eligible »). Si disponible, **VM.Standard.A1.Flex** 1 OCPU / 6 Go convient aussi. |
| Réseau | VCN créé par défaut, sous-réseau **public**, **« Assign a public IPv4 address »** coché |
| Clé SSH | **Generate a key pair** → téléchargez la clé privée et gardez-la en lieu sûr |

Notez l'**adresse IP publique** de la machine.

Une petite machine suffit largement : quelques dizaines de DT à une position par seconde, c'est très peu pour Mosquitto.

## Étape 3 — Ouvrir les ports côté Oracle (vous)

Console → **Networking → Virtual Cloud Networks** → votre VCN → **Security Lists** → *Default Security List* →
**Add Ingress Rules** (une règle par port) :

| Source CIDR | Protocole | Port | Usage |
|---|---|---|---|
| `0.0.0.0/0` | TCP | **80** | obtention et renouvellement du certificat TLS |
| `0.0.0.0/0` | TCP | **443** | MQTT sur WebSocket chiffré (repli si l'opérateur 4G bloque le 8883) |
| `0.0.0.0/0` | TCP | **8883** | MQTT chiffré (tablettes, passerelle) |

Le pare-feu interne de la machine est ouvert par le script (étape 5).

## Étape 4 — Donner un nom à la machine (vous)

Le certificat TLS exige un **nom de domaine** qui pointe vers l'IP publique. Deux possibilités :

- **Gratuit :** https://www.duckdns.org → connexion → créez un sous-domaine (ex. `ot-lafigue`) → collez l'IP publique.
  Nom obtenu : `ot-lafigue.duckdns.org`.
- **Domaine de l'entreprise :** enregistrement DNS de type **A**, ex. `mqtt.votre-domaine.com` → IP publique.

Vérifiez depuis un poste Windows : `nslookup ot-lafigue.duckdns.org` doit renvoyer l'IP de la machine.

## Étape 5 — Installer le broker (kit)

Depuis le poste du bureau, dans le dossier `deploiement\mqtt` du projet (PowerShell) :

```powershell
scp -i C:\chemin\cle-privee.key oretracking.conf acl installer_broker.sh ajouter_comptes.sh verifier_broker.sh ubuntu@IP_PUBLIQUE:~/
```

```powershell
ssh -i C:\chemin\cle-privee.key ubuntu@IP_PUBLIQUE
```

Puis, **sur la machine** :

```bash
sudo bash installer_broker.sh ot-lafigue.duckdns.org email-exploitation@exemple.com
```

Ce que fait le script :
- il installe Mosquitto et certbot ;
- il ouvre le pare-feu interne ;
- il obtient le certificat Let's Encrypt, renouvelé ensuite automatiquement ;
- il installe la configuration et les droits ;
- il crée les comptes `passerelle` et `controle`, et **affiche leurs mots de passe une seule fois**.

L'adresse email ne sert qu'aux avis d'expiration du certificat ; en lançant le script, vous acceptez les conditions de
Let's Encrypt. Le script peut être relancé sans risque.

## Étape 6 — Créer un compte par DT (kit)

```bash
sudo ot-ajouter-comptes DT-01 DT-02 DT-03 DT-04 DT-TEST
```

- **L'identifiant est le code du DT, exactement celui des remontées (`dt.id`).** Les droits limitent chaque compte à ses
  propres sujets `camions/<code>/...`.
- Chaque mot de passe s'affiche **une seule fois** : notez-le dans le gestionnaire de mots de passe, puis saisissez-le dans
  les paramètres MQTT de la tablette correspondante (cahier des charges, § 4).
- Relancer la commande pour un compte existant lui donne un nouveau mot de passe (perte, tablette volée…).

## Étape 7 — Vérifier (kit)

```bash
bash verifier_broker.sh ot-lafigue.duckdns.org
```

Le script vérifie :
- qu'une connexion sans compte est refusée ;
- qu'un DT publie sa position et que le compte `controle` la reçoit ;
- **qu'un DT ne peut pas publier pour un autre DT** ;
- que la dernière position est conservée.

Attendu : `BROKER CONFORME`.

## Étape 8 — Prévenir Zone 1

Transmettez **seulement le nom de domaine** (ex. `ot-lafigue.duckdns.org`), jamais les mots de passe. Ensuite :

- **mot de passe du compte `passerelle` :** saisissez-le vous-même dans les Paramètres d'OreTracking (bloc MQTT, ajouté à
  l'étape suivante du projet) ;
- **mots de passe des DT :** ils vont aux tablettes, par le canal sécurisé convenu avec Zone 2.

---

## Exploitation courante

| Sujet | Commande / remarque |
|---|---|
| État du broker | `systemctl status mosquitto` |
| Journal (connexions, refus) | `journalctl -u mosquitto -f` |
| Certificat | renouvelé automatiquement (`systemctl list-timers \| grep certbot`) ; test : `sudo certbot renew --dry-run` |
| Mises à jour de sécurité | automatiques sur Ubuntu (`unattended-upgrades`) |
| Sauvegarde | `/etc/mosquitto/passwd` (comptes) : copie chiffrée à conserver |
| Retirer un DT | `sudo mosquitto_passwd -D /etc/mosquitto/passwd DT-xx && sudo systemctl reload mosquitto` |
