/* =====================================================================
 * gnss.js — Settings › GNSS : identification, configuration, affectation et liste des recepteurs.
 *
 *   1. « Detecter » : le recepteur branche en USB sur CE poste est reconnu par sa puce (UBX-SEC-UNIQID,
 *      Web Serial). Une puce nouvelle recoit l'ID GNSS-NN suivant ; une puce connue retrouve le sien.
 *   2. « Configurer » : le profil GNSS du site (vitesse, frequence, trames NMEA) est ecrit dans le
 *      recepteur (UBX-CFG-VALSET, RAM + sauvegarde + flash) puis RELU (UBX-CFG-VALGET) ; seule une
 *      configuration verifiee est enregistree comme faite.
 *   3. Chaque recepteur est affecte a UNE tablette, donc a un engin (DT, excavatrice) ou a la mesure (Link).
 * L'inventaire, le profil et les affectations vivent sur la passerelle ; le navigateur ne fait que parler
 * au recepteur.
 * ===================================================================== */
(function (racine) {
  'use strict';

  /* ----------------------------------------------------------- UBX (pur, testable sans navigateur) */
  const CLES = {
    RATE_MEAS: 0x30210001, RATE_NAV: 0x30210002, UART1_BAUD: 0x40520001,
    UART1_NMEA: 0x10740002, USB_NMEA: 0x10780002,
  };
  // CFG-MSGOUT-NMEA_ID_xxx_UART1 / _USB (u-blox generation 9 et 10)
  const MSGOUT = {
    GGA: [0x209100bb, 0x209100bd], RMC: [0x209100ac, 0x209100ae], VTG: [0x209100b1, 0x209100b3],
    GSA: [0x209100c0, 0x209100c2], GSV: [0x209100c5, 0x209100c7], GLL: [0x209100ca, 0x209100cc],
  };

  function somme(octets) {
    let a = 0, b = 0;
    for (let i = 0; i < octets.length; i++) { a = (a + octets[i]) & 0xff; b = (b + a) & 0xff; }
    return [a, b];
  }

  /** Trame UBX complete : B5 62 classe id longueur charge somme. */
  function trame(cls, id, charge) {
    const c = charge || [];
    const corps = [cls, id, c.length & 0xff, (c.length >> 8) & 0xff].concat(Array.prototype.slice.call(c));
    return Uint8Array.from([0xb5, 0x62].concat(corps, somme(corps)));
  }

  /** Taille en octets de la valeur d'une cle de configuration (bits 28-30 de la cle). */
  function tailleCle(cle) { return [0, 1, 1, 2, 4, 8][(cle >>> 28) & 7] || 1; }

  function octetsValeur(cle, valeur) {
    const n = tailleCle(cle), o = [];
    let v = Math.floor(Number(valeur));
    for (let i = 0; i < n; i++) { o.push(v % 256); v = Math.floor(v / 256); }
    return o;
  }

  function octetsCle(cle) { return [cle & 0xff, (cle >>> 8) & 0xff, (cle >>> 16) & 0xff, (cle >>> 24) & 0xff]; }

  /** UBX-CFG-VALSET : paires [cle, valeur] ; couches 0x07 = RAM + sauvegarde (BBR) + flash. */
  function valset(paires, couches) {
    let charge = [0x00, couches == null ? 0x07 : couches, 0, 0];
    paires.forEach(([cle, valeur]) => { charge = charge.concat(octetsCle(cle), octetsValeur(cle, valeur)); });
    return trame(0x06, 0x8a, charge);
  }

  /** UBX-CFG-VALGET (couche 0 = RAM) : relit les cles demandees. */
  function valget(cles) {
    let charge = [0x00, 0x00, 0x00, 0x00];
    cles.forEach((cle) => { charge = charge.concat(octetsCle(cle)); });
    return trame(0x06, 0x8b, charge);
  }

  /** Reponse VALGET -> { cle: valeur }. */
  function lireValget(charge) {
    const res = {};
    let i = 4;
    while (i + 4 <= charge.length) {
      const cle = (charge[i] | (charge[i + 1] << 8) | (charge[i + 2] << 16) | (charge[i + 3] << 24)) >>> 0;
      const n = tailleCle(cle);
      if (i + 4 + n > charge.length) break;
      let v = 0;
      for (let k = n - 1; k >= 0; k--) v = v * 256 + charge[i + 4 + k];
      res[cle] = v;
      i += 4 + n;
    }
    return res;
  }

  /** Trames UBX valides (somme verifiee) trouvees dans un tampon. */
  function lireTrames(tampon) {
    const sortie = [];
    for (let i = 0; i + 8 <= tampon.length; i++) {
      if (tampon[i] !== 0xb5 || tampon[i + 1] !== 0x62) continue;
      const len = tampon[i + 4] | (tampon[i + 5] << 8);
      if (i + 8 + len > tampon.length) break;
      const corps = Array.prototype.slice.call(tampon, i + 2, i + 6 + len);
      const [a, b] = somme(corps);
      if (a === tampon[i + 6 + len] && b === tampon[i + 7 + len]) {
        sortie.push({ cls: tampon[i + 2], id: tampon[i + 3], charge: Array.prototype.slice.call(tampon, i + 6, i + 6 + len) });
        i += 7 + len;
      }
    }
    return sortie;
  }

  /** Paires [cle, valeur] du profil du site : cadence, protocoles NMEA, trames emises. */
  function pairesProfil(profil) {
    const paires = [
      [CLES.RATE_MEAS, Math.round(1000 / profil.hz)], [CLES.RATE_NAV, 1],
      [CLES.UART1_NMEA, 1], [CLES.USB_NMEA, 1],
    ];
    Object.keys(MSGOUT).forEach((nom) => {
      const actif = profil.trames.indexOf(nom) >= 0 ? 1 : 0;
      MSGOUT[nom].forEach((cle) => paires.push([cle, actif]));
    });
    return paires;
  }

  /** Puce (hex) lue dans une reponse UBX-SEC-UNIQID : version, 3 reserves, puis 5 ou 6 octets. */
  function lirePuce(trameUniq) {
    const c = trameUniq.charge;
    const id = c.slice(4, 4 + Math.min(6, c.length - 4));
    if (id.length < 5) return '';
    return id.map((x) => ('0' + x.toString(16)).slice(-2)).join('').toUpperCase();
  }

  /** Modele lu dans UBX-MON-VER : extension « MOD=ZED-F9P ». */
  function lireModele(trameVer) {
    const texte = String.fromCharCode.apply(null, trameVer.charge.slice(40).filter((x) => x > 0));
    const m = /MOD=([A-Za-z0-9\-]+)/.exec(texte);
    return m ? m[1] : '';
  }

  const Ubx = { CLES, MSGOUT, somme, trame, valset, valget, lireValget, lireTrames, pairesProfil, lirePuce, lireModele,
                INTERROGE_PUCE: trame(0x27, 0x03, []), INTERROGE_VERSION: trame(0x0a, 0x04, []) };
  racine.OreGnssUbx = Ubx;
  if (typeof module !== 'undefined' && module.exports) { module.exports = Ubx; return; }
  if (typeof document === 'undefined') return;

  /* ----------------------------------------------------------- liaison serie (Web Serial) */
  class Liaison {
    constructor(port) { this.port = port; this.tampon = []; this.ouvert = false; this.baud = 0; }

    async ouvrir(baud) {
      await this.port.open({ baudRate: baud, dataBits: 8, stopBits: 1, parity: 'none', flowControl: 'none' });
      this.baud = baud; this.ouvert = true; this.tampon = [];
      this.lecteur = this.port.readable.getReader();
      this.boucle = (async () => {
        try {
          for (;;) {
            const { value, done } = await this.lecteur.read();
            if (done) break;
            if (value && value.length) {
              for (let i = 0; i < value.length; i++) this.tampon.push(value[i]);
              if (this.tampon.length > 65536) this.tampon.splice(0, this.tampon.length - 32768);
            }
          }
        } catch (e) { /* port ferme ou debranche */ }
      })();
    }

    async fermer() {
      if (!this.ouvert) return;
      this.ouvert = false;
      try { await this.lecteur.cancel(); } catch (e) { /* deja ferme */ }
      try { await this.boucle; } catch (e) { /* idem */ }
      try { this.lecteur.releaseLock(); } catch (e) { /* idem */ }
      try { await this.port.close(); } catch (e) { /* idem */ }
    }

    async ecrire(octets) {
      const w = this.port.writable.getWriter();
      try { await w.write(octets); } finally { w.releaseLock(); }
    }

    /** Envoie une trame et attend une reponse UBX qui satisfait `accepte` (delai en ms). */
    async echanger(octets, accepte, delai) {
      this.tampon = [];
      await this.ecrire(octets);
      const fin = Date.now() + (delai || 1500);
      while (Date.now() < fin) {
        const t = Ubx.lireTrames(this.tampon).find(accepte);
        if (t) return t;
        await new Promise((r) => setTimeout(r, 25));
      }
      return null;
    }
  }

  /* ----------------------------------------------------------- page */
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = (s) => document.querySelector(s);
  const base = () => (window.OTCFG && OTCFG.passerelle) ? OTCFG.passerelle() : location.protocol + '//' + (location.hostname || 'localhost') + ':8101';

  async function appel(chemin, methode, corps) {
    const r = await fetch(base() + chemin, {
      method: methode || 'GET', cache: 'no-store',
      headers: corps ? { 'Content-Type': 'application/json' } : undefined,
      body: corps ? JSON.stringify(corps) : undefined,
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || ('HTTP ' + r.status)), { status: r.status });
    return d;
  }

  let etat = { gnss: [], tablettes: [], profil: { baud: 115200, hz: 1, trames: [] }, bauds: [], trames: [] };
  let liaison = null;                 // recepteur branche sur ce poste
  let recepteur = null;               // { chip, id, modele, nouveau }
  let minuteur = null;
  let occupe = false;

  function statut(texte, ok) {
    const el = $('#gnssStatus');
    if (!el) return;
    el.textContent = texte || '';
    el.className = 'account-status' + (texte ? (ok === false ? ' err' : ' ok') : '');
  }

  function visible() {
    const pane = document.querySelector('#page-settings .onglet-pane[data-onglet="gnss"]');
    return pane && !pane.hidden;
  }

  function rendreProfil() {
    const p = etat.profil;
    const vitesse = $('#gnssBaud'), hz = $('#gnssHz');
    if (!vitesse || document.activeElement === vitesse || document.activeElement === hz) return;
    vitesse.innerHTML = (etat.bauds || []).map((b) => '<option value="' + b + '"' + (b === p.baud ? ' selected' : '') + '>' + b + ' bauds</option>').join('');
    hz.innerHTML = [1, 2, 5, 10].map((h) => '<option value="' + h + '"' + (h === p.hz ? ' selected' : '') + '>' + h + ' Hz</option>').join('');
    const box = $('#gnssTrames');
    if (box && !box.contains(document.activeElement)) {
      box.innerHTML = (etat.trames || []).map((t) => '<label class="pm-gnss-trame' + (t === 'GGA' ? ' fixe' : '') + '"><input type="checkbox" data-trame="' + t + '"' +
        (p.trames.indexOf(t) >= 0 ? ' checked' : '') + (t === 'GGA' ? ' disabled' : '') + ' /> ' + t + '</label>').join('');
    }
  }

  function rendreRecepteur() {
    const box = $('#gnssRecepteur');
    if (!box) return;
    const ok = !!(liaison && liaison.ouvert && recepteur);
    box.innerHTML = ok
      ? '<b>' + esc(recepteur.id) + '</b><small>Puce ' + esc(recepteur.chip) + (recepteur.modele ? ' · ' + esc(recepteur.modele) : '') +
        ' · ' + liaison.baud + ' bauds' + (recepteur.nouveau ? ' · ID attribué' : '') + '</small>'
      : '<em>Aucun récepteur branché sur ce poste</em>';
    $('#gnssConfigurer').disabled = !ok || occupe;
    $('#gnssFermer').hidden = !ok;
    $('#gnssDetecter').disabled = occupe;
  }

  function rendreListe() {
    const box = $('#gnssListe');
    if (!box) return;
    const n = etat.gnss.length;
    const cpt = $('#gnssCompteur');
    if (cpt) cpt.textContent = n + (n > 1 ? ' récepteurs enregistrés' : ' récepteur enregistré') + ' · ' + etat.gnss.filter((g) => g.tablette).length + ' affecté' + (etat.gnss.filter((g) => g.tablette).length > 1 ? 's' : '');
    const actif = document.activeElement;
    if (actif && box.contains(actif) && actif.tagName === 'SELECT') return;
    if (!n) { box.innerHTML = '<div class="pm-vide">Aucun récepteur. Branchez-en un en USB sur ce poste, puis « Détecter ».</div>'; return; }
    box.innerHTML = etat.gnss.map((g) => {
      const libres = etat.tablettes.filter((t) => t.libre || t.serial === g.tablette);
      const opts = '<option value="">Aucune tablette</option>' + libres.map((t) => '<option value="' + esc(t.serial) + '"' + (t.serial === g.tablette ? ' selected' : '') + '>' +
        esc(t.nom || t.serial) + (t.engin ? ' · ' + esc(t.engin) : (t.profil === 'link' ? ' · Link' : '')) + '</option>').join('');
      const conf = g.a_jour ? '<span class="pm-gnss-etat ok">Configuré</span><small>' + esc(g.configure_le.slice(0, 16)) + '</small>'
        : (g.configure_le ? '<span class="pm-gnss-etat attente">Profil modifié : à reconfigurer</span><small>Dernière : ' + esc(g.configure_le.slice(0, 16)) + '</small>'
          : '<span class="pm-gnss-etat ko">Non configuré</span>');
      const engin = g.engin ? '<b>' + esc(g.engin) + '</b>' + (g.famille ? '<small>Famille ' + esc(g.famille) + '</small>' : '') : '<small>—</small>';
      return '<div class="pm-tab pm-gnss-ligne" data-chip="' + esc(g.chip) + '"><div class="pm-ligne pm-ligne-gnss">' +
        '<span class="pm-tab-id"><b>' + esc(g.id) + '</b><small>Puce ' + esc(g.chip) + (g.modele ? ' · ' + esc(g.modele) : '') + '</small></span>' +
        '<span class="pm-tab-app">' + conf + '</span>' +
        '<span class="pm-tab-dt"><select data-gnss-tablette="' + esc(g.chip) + '">' + opts + '</select></span>' +
        '<span class="pm-tab-app">' + engin + '</span>' +
        '<span class="pm-actions pm-tab-actions"><button type="button" class="pm-lien danger" data-gnss-supprimer="' + esc(g.chip) + '">Supprimer</button></span>' +
        '</div></div>';
    }).join('');
  }

  let radio = null;
  async function chargerRadio() {
    try { radio = await appel('/api/radio'); } catch (e) { radio = null; }
    rendreRadio();
  }

  function rendreRadio() {
    const box = $('#radioEtat');
    const sp = $('#radioPort'), sb = $('#radioBaud');
    if (!box || !sp) return;
    if (!radio) { box.innerHTML = '<em>Passerelle injoignable (ou à redémarrer)</em>'; return; }
    box.innerHTML = !radio.pyserial
      ? '<b>Radio désactivée</b><small>pyserial n\'est pas installé sur ce poste</small>'
      : (radio.connecte
        ? '<b>Connectée</b><small>' + esc(radio.port) + ' · ' + radio.baud + ' bauds · ' + radio.valides + ' position(s) de DT valide(s) · ' + radio.rejetees + ' rejetée(s)' + (radio.derniere_valide ? ' · dernière ' + esc(radio.derniere_valide.slice(11, 19)) : '') + '</small>' +
          '<small>Excavatrices entendues : ' + (radio.pelles_ok || 0) + ' signée(s) valide(s) · ' + (radio.pelles_non_signee || 0) + ' non signée(s) · ' + (radio.pelles_invalide || 0) + ' invalide(s)' +
          (radio.derniere_pelle ? ' · dernière ' + esc(radio.derniere_pelle.id) + ' (' + ({ ok: 'signature valide', non_signee: 'non signée', invalide: 'signature invalide' }[radio.derniere_pelle.resultat] || radio.derniere_pelle.resultat) + ') à ' + esc(radio.derniere_pelle.le.slice(11, 19)) : '') + '</small>'
        : '<b>' + (radio.configure_port ? 'Non connectée' : 'Aucun port choisi') + '</b><small>' + esc(radio.erreur || (radio.configure_port ? '' : 'Branchez la radio, puis choisissez son port')) + '</small>');
    if (document.activeElement === sp || document.activeElement === sb) return;
    const ex = $('#radioExiger');
    if (ex && document.activeElement !== ex) ex.checked = !!radio.exiger_signature;
    const ports = radio.ports || [];
    sp.innerHTML = '<option value="">Aucun</option>' + ports.map((p) => '<option value="' + esc(p.device) + '"' + (p.device === radio.configure_port ? ' selected' : '') + '>' + esc(p.device) + ' · ' + esc(p.description) + '</option>').join('') +
      (radio.configure_port && !ports.some((p) => p.device === radio.configure_port) ? '<option value="' + esc(radio.configure_port) + '" selected>' + esc(radio.configure_port) + ' (absent)</option>' : '');
    sb.innerHTML = (radio.bauds || []).map((b) => '<option value="' + b + '"' + (b === radio.configure_baud ? ' selected' : '') + '>' + b + ' bauds</option>').join('');
  }

  function rendre() { rendreProfil(); rendreRecepteur(); rendreListe(); chargerRadio(); }

  async function rafraichir(silencieux) {
    try {
      etat = await appel('/api/gnss');
      if (!silencieux) statut('', true);
    } catch (e) {
      const ancienne = e.status === 404 || /endpoint inconnu/i.test(e.message);
      statut(ancienne ? 'La passerelle en cours ne gère pas encore les GNSS : redémarrez-la (lanceur.py).' : 'Passerelle injoignable : ' + e.message, false);
    }
    rendre();
  }

  /* ----------------------------------------------------------- detection */
  async function detecter() {
    if (!navigator.serial) { statut('Web Serial indisponible : ouvrez la console dans Chrome ou Edge.', false); return; }
    occupe = true; rendreRecepteur();
    try {
      if (liaison) { await liaison.fermer(); liaison = null; recepteur = null; }
      statut('Choisissez le port du récepteur…', true);
      const port = await navigator.serial.requestPort();
      const l = new Liaison(port);
      const essais = [etat.profil.baud, 115200, 38400, 9600, 57600, 230400, 460800, 921600]
        .filter((b, i, a) => a.indexOf(b) === i);
      let puce = null;
      for (const b of essais) {
        statut('Recherche du récepteur à ' + b + ' bauds…', true);
        try { await l.ouvrir(b); } catch (e) { statut('Port impossible à ouvrir : ' + e.message, false); return; }
        puce = await l.echanger(Ubx.INTERROGE_PUCE, (t) => t.cls === 0x27 && t.id === 0x03, 1200);
        if (puce) break;
        await l.fermer();
      }
      if (!puce) { statut('Aucun récepteur u-blox ne répond sur ce port (puce non lue).', false); return; }
      const chip = Ubx.lirePuce(puce);
      if (!chip) { statut('Réponse du récepteur illisible.', false); return; }
      const ver = await l.echanger(Ubx.INTERROGE_VERSION, (t) => t.cls === 0x0a && t.id === 0x04, 1500);
      const modele = ver ? Ubx.lireModele(ver) : '';
      const rep = await appel('/api/gnss/enregistrer', 'POST', { chip: chip, modele: modele });
      liaison = l;
      recepteur = { chip: chip, id: rep.id, modele: modele, nouveau: rep.nouveau };
      statut(rep.nouveau ? 'Nouveau récepteur : ID ' + rep.id + ' attribué.' : 'Récepteur reconnu : ' + rep.id + '.', true);
      await rafraichir(true);
    } catch (e) {
      statut(e && e.name === 'NotFoundError' ? 'Aucun port choisi.' : (e.message || 'Détection impossible'), false);
    } finally {
      occupe = false; rendreRecepteur();
    }
  }

  /* ----------------------------------------------------------- configuration */
  const estAccuse = (t) => t.cls === 0x05 && (t.id === 0x01 || t.id === 0x00) && t.charge[0] === 0x06 && t.charge[1] === 0x8a;

  async function verifier(l, paires) {
    const rep = await l.echanger(Ubx.valget(paires.map((p) => p[0])), (t) => t.cls === 0x06 && t.id === 0x8b, 2000);
    if (!rep) return 'le récepteur ne répond pas à la relecture';
    const lu = Ubx.lireValget(rep.charge);
    const faux = paires.filter(([cle, v]) => lu[cle] !== v);
    return faux.length ? faux.length + ' réglage(s) non retenu(s) par le récepteur' : '';
  }

  async function configurer() {
    if (!liaison || !liaison.ouvert || !recepteur) return;
    const p = etat.profil;
    if (window.Splash && Splash.confirmer) {
      const ok = await Splash.confirmer('Configurer ' + recepteur.id + ' ?', {
        titre: 'Configuration du récepteur', danger: false, libelleOk: 'Configurer',
        details: p.baud + ' bauds · ' + p.hz + ' Hz · trames ' + p.trames.join(', ') + '. Écrit dans le récepteur (mémoire flash comprise).' });
      if (!ok) return;
    }
    occupe = true; rendreRecepteur();
    try {
      const paires = Ubx.pairesProfil(p);
      statut('Écriture de la configuration…', true);
      const acc = await liaison.echanger(Ubx.valset(paires), estAccuse, 2500);
      if (!acc) { statut('Le récepteur n’a pas répondu à la configuration.', false); return; }
      if (acc.id === 0x00) { statut('Configuration refusée : récepteur non pris en charge (u-blox série 9 ou 10 requis).', false); return; }
      statut('Vérification par relecture…', true);
      let echec = await verifier(liaison, paires);
      if (echec) { statut('Configuration non vérifiée : ' + echec + '.', false); return; }
      if (liaison.baud !== p.baud) {
        statut('Passage à ' + p.baud + ' bauds…', true);
        await liaison.echanger(Ubx.valset([[Ubx.CLES.UART1_BAUD, p.baud]]), estAccuse, 2000);
        const port = liaison.port;
        await liaison.fermer();
        liaison = new Liaison(port);
        await liaison.ouvrir(p.baud);
        const puce = await liaison.echanger(Ubx.INTERROGE_PUCE, (t) => t.cls === 0x27 && t.id === 0x03, 2000);
        if (!puce || Ubx.lirePuce(puce) !== recepteur.chip) { statut('Le récepteur ne répond pas à ' + p.baud + ' bauds après le changement.', false); return; }
        echec = await verifier(liaison, [[Ubx.CLES.UART1_BAUD, p.baud]]);
        if (echec) { statut('Vitesse non vérifiée : ' + echec + '.', false); return; }
      }
      await appel('/api/gnss/configure', 'POST', { chip: recepteur.chip });
      statut(recepteur.id + ' configuré et vérifié.', true);
      await rafraichir(true);
    } catch (e) {
      statut(e.message || 'Configuration impossible', false);
    } finally {
      occupe = false; rendreRecepteur();
    }
  }

  /* ----------------------------------------------------------- evenements */
  function brancher() {
    const pane = document.querySelector('#page-settings .onglet-pane[data-onglet="gnss"]');
    if (!pane) return;
    document.querySelector('#settingsOnglets').addEventListener('click', (e) => {
      const b = e.target.closest('.onglet');
      if (b && b.dataset.onglet === 'gnss') demarrer();
    });
    $('#radioForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await appel('/api/radio', 'POST', { port: $('#radioPort').value, baud: Number($('#radioBaud').value) });
        statut('Radio enregistrée : la passerelle la rouvre d\'elle-même (quelques secondes).', true);
      } catch (err) { statut(err.message, false); }
      setTimeout(chargerRadio, 6500);
    });
    $('#radioExiger').addEventListener('change', async (e) => {
      const veut = e.target.checked;
      if (veut && window.Splash && Splash.confirmer && !await Splash.confirmer('Exiger la signature des excavatrices ?', {
        titre: 'Signature des excavatrices', danger: true, libelleOk: 'Exiger',
        details: 'Les DT ignoreront toute trame de pelle non signée. Vérifiez d\'abord que chaque excavatrice et chaque DT a reçu ses clés (« Envoyer les paramètres »), sinon une pelle légitime ne sera plus entendue.' })) { e.target.checked = false; return; }
      try {
        await appel('/api/radio', 'POST', { exiger_signature: veut });
        statut(veut ? 'Signature exigée : à partir de la prochaine lecture des paramètres par chaque DT.' : 'Signature non exigée : toutes les trames passent.', true);
      } catch (err) { statut(err.message, false); e.target.checked = !veut; }
    });
    $('#radioTestEnvoyer').addEventListener('click', async () => {
      try {
        const r = await appel('/api/radio/emettre', 'POST', { pelle: $('#radioTestPelle').value.trim(), dt: $('#radioTestDt').value.trim(), mode: $('#radioTestMode').value, type: $('#radioTestType').value });
        statut('Trame envoyée par la radio du PC (' + $('#radioTestMode').selectedOptions[0].textContent + ').', true);
      } catch (err) { statut(err.message, false); }
    });
    $('#gnssDetecter').addEventListener('click', detecter);
    $('#gnssConfigurer').addEventListener('click', configurer);
    $('#gnssFermer').addEventListener('click', async () => {
      if (liaison) { await liaison.fermer(); liaison = null; recepteur = null; }
      statut('Récepteur déconnecté de ce poste.', true); rendreRecepteur();
    });
    $('#gnssProfilForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const trames = Array.prototype.map.call(pane.querySelectorAll('[data-trame]:checked'), (x) => x.dataset.trame);
      try {
        await appel('/api/gnss/profil', 'POST', { baud: Number($('#gnssBaud').value), hz: Number($('#gnssHz').value), trames: trames.concat(trames.indexOf('GGA') < 0 ? ['GGA'] : []) });
        statut('Profil enregistré : les récepteurs déjà configurés passent à « à reconfigurer » si le profil a changé.', true);
      } catch (err) { statut(err.message, false); }
      rafraichir(true);
    });
    pane.addEventListener('change', async (e) => {
      const s = e.target.closest('[data-gnss-tablette]');
      if (!s) return;
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      try {
        await appel('/api/gnss/affecter', 'POST', { chip: s.dataset.gnssTablette, tablette: s.value });
        statut(s.value ? 'Récepteur affecté à la tablette.' : 'Récepteur dissocié.', true);
      } catch (err) { statut(err.message, false); }
      rafraichir(true);
    });
    pane.addEventListener('click', async (e) => {
      const d = e.target.closest('[data-gnss-supprimer]');
      if (!d) return;
      const g = etat.gnss.find((x) => x.chip === d.dataset.gnssSupprimer);
      if (window.Splash && Splash.confirmer && !await Splash.confirmer('Supprimer ' + (g ? g.id : 'ce récepteur') + ' ?',
        { titre: 'Suppression du récepteur', danger: true, libelleOk: 'Supprimer', details: 'Il sera de nouveau reconnu (et reprendra un ID) s’il est rebranché.' })) return;
      try { await appel('/api/gnss/supprimer', 'POST', { chip: d.dataset.gnssSupprimer }); statut('Récepteur supprimé.', true); }
      catch (err) { statut(err.message, false); }
      rafraichir(true);
    });
  }

  function demarrer() {
    rafraichir(false);
    if (minuteur) return;
    minuteur = setInterval(() => { if (visible() && !document.hidden) rafraichir(true); }, 8000);
  }

  function init() {
    if (!document.getElementById('page-settings') || !$('#gnssListe')) return;
    brancher();
    if (visible()) demarrer();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})(typeof window !== 'undefined' ? window : globalThis);
