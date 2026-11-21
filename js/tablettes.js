/* =====================================================================
 * tablettes.js — Settings › Tablettes : detection automatique, affectation et installation.
 *
 * Un profil = une application : DT, Excavatrice, Spotter ou Link (mesure).
 *   1. La passerelle detecte les tablettes (adb : USB ou Wi-Fi) ;
 *   2. on choisit le profil de chaque tablette et son identite :
 *      DT = id + famille (sync_flotte) ; Excavatrice = id + famille ; Spotter et Link = aucune (le spotter se connecte lui-meme) ;
 *   3. « Installer » pousse l'APK du profil, ecrit le provisionnement sur la tablette et lance l'app.
 * « Ouvrir le Wi-Fi » = adb tcpip 5555 (tablette branchee en USB) puis adb connect : on peut ensuite
 * debrancher le cable.
 * ===================================================================== */
(function () {
  'use strict';

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = (s) => document.querySelector(s);

  function base() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return location.protocol + '//' + (location.hostname || 'localhost') + ':8101';
  }

  async function appel(chemin, methode, corps) {
    const r = await fetch(base() + chemin, {
      method: methode || 'GET', cache: 'no-store',
      headers: corps ? { 'Content-Type': 'application/json' } : undefined,
      body: corps ? JSON.stringify(corps) : undefined,
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(d.error || ('HTTP ' + r.status)), { status: r.status, donnees: d });
    return d;
  }

  let etat = { tablettes: [], apk: {}, adb: {} };
  let clesRadio = [];                           // cles radio des DT (empreintes seulement)
  let jetons = { jetons: [], actifs: false };   // jetons par tablette (generation, renouvellement, revocation)
  let flotte = [];                 // [{ code: 'DT-04', famille: '1500' }]
  const PROFILS = [['dt', 'DT'], ['exc', 'Excavatrice'], ['spotter', 'Spotter'], ['link', 'Link']];
  const jobs = {};                 // serie -> { job, etapes, etat, erreur }
  let minuteur = null;
  let enCours = false;

  async function chargerFlotte() {
    try {
      const d = await API.getRows('sync_flotte');
      flotte = ((d && d.rows) || [])
        .filter((r) => r.actif !== 0 && r.code)
        .map((r) => ({ code: String(r.code).toUpperCase(), famille: String(r.famille || '') }))
        .sort((a, b) => a.code.localeCompare(b.code));
    } catch (e) { /* non connecte : saisie manuelle de secours */ }
  }

  function statut(texte, ok) {
    const el = $('#tabStatus');
    if (!el) return;
    el.textContent = texte || '';
    el.className = 'account-status' + (texte ? (ok === false ? ' err' : ' ok') : '');
  }

  function rendre() {
    const box = $('#tabListe');
    if (!box) return;
    const apk = etat.apk || {};
    const entete = $('#tabAdbEtat');
    if (entete) {
      const n = etat.tablettes.filter((t) => t.connectee).length;
      entete.textContent = etat.adb.erreur
        ? 'adb indisponible : ' + etat.adb.erreur
        : n + (n > 1 ? ' tablettes connectées' : ' tablette connectée') + ' · ' + etat.tablettes.length + ' enregistrée' + (etat.tablettes.length > 1 ? 's' : '');
    }
    const cx = $('#tabConnexionEtat');
    if (cx) {
      const c = etat.connexion || {};
      cx.textContent = c.envoyee_le && !c.erreur ? 'Connexion des tablettes distribuée par Serveur_OT · ' + c.envoyee_le.slice(0, 16)
        : (c.erreur ? 'Distribution de la connexion : ' + c.erreur : (c.envoyee_le ? 'Connexion des tablettes distribuée par Serveur_OT · ' + c.envoyee_le.slice(0, 16) : ''));
    }
    const apkBox = $('#tabApk');
    if (apkBox) {
      const pr = apk.profils || {};
      apkBox.innerHTML = PROFILS.map(([k, nom]) => {
        const d = pr[k] || {};
        const f = (d.fichiers || [])[0];
        const etatApk = !f ? ['absent', 'Aucun APK'] : (f.test ? ['test', 'Test'] : ['prod', 'Production']);
        return '<div class="pm-apk-carte ' + etatApk[0] + '">' +
          '<div class="pm-apk-tete"><span class="pm-apk-profil">' + nom + '</span><span class="pm-apk-etat">' + etatApk[1] + '</span></div>' +
          (f ? '<b class="pm-apk-nom">' + esc(f.nom) + '</b><span class="pm-apk-meta">' + esc(f.date) + ' · ' + f.taille_mo + ' Mo' +
               ((d.fichiers || []).length > 1 ? ' · ' + (d.fichiers.length) + ' versions' : '') + '</span>'
             : '<span class="pm-apk-meta">Aucun fichier dans le dossier apk/</span>') +
          '</div>';
      }).join('');
    }
    if (!etat.tablettes.length) {
      box.innerHTML = '<div class="pm-vide">Aucune tablette détectée. Branchez une tablette en USB (débogage USB activé) ou connectez-la par son adresse IP.</div>';
      return;
    }
    box.innerHTML = etat.tablettes.map((t) => {
      const job = jobs[t.serie];
      const profilOpts = PROFILS.map(([k, nom]) => '<option value="' + k + '"' + (k === t.profil ? ' selected' : '') + '>' + nom + '</option>').join('');
      const transports = t.transports.map((x) => '<span class="pm-tab-puce">' + (x === 'wifi' ? 'Wi-Fi' : 'USB') + '</span>').join('');
      const nomProfil = (PROFILS.find((x) => x[0] === t.profil) || [])[1] || '';
      const appTxt = t.app_installee
        ? 'App ' + nomProfil + ' installée' + (t.app_version ? ' · v' + esc(t.app_version) : '')
        : 'App ' + nomProfil + ' non installée';
      const autres = PROFILS.filter(([k]) => k !== t.profil && t.apps && t.apps[k] && t.apps[k].installee).map((x) => x[1]);
      const etatTxt = t.etat === 'device' ? '' : (t.etat === 'unauthorized' ? 'Autorisez le débogage USB sur la tablette' : (t.etat === 'absente' ? 'Absente' : esc(t.etat)));
      const manque = incomplete(t);
      const etapes = job ? '<div class="pm-tab-etapes">' + job.etapes.map((e) => '<span class="' + (e.ok ? '' : 'ko') + '">' + (e.ok ? '✓ ' : '✕ ') + esc(e.texte) + '</span>').join('') +
        (job.etat === 'en_cours' ? '<span class="en-cours">Installation en cours…</span>' : '') +
        (job.etat === 'erreur' && /AUTRE cle/.test(job.erreur) ? '<button type="button" class="pm-lien danger" data-tab-reinstaller="' + esc(t.serie) + '">Réinstaller (efface les données de l\'app)</button>' : '') + '</div>' : '';
      const dis = t.connectee ? '' : ' disabled';
      return '<div class="pm-tab" data-serie="' + esc(t.serie) + '">' +
        '<div class="pm-ligne pm-ligne-tab">' +
          '<span class="pm-tab-id"><b>' + esc(t.modele || 'Tablette') + '</b><small>' + esc(t.serie) +
            (t.batterie != null ? ' · ' + t.batterie + ' %' : '') + (t.android ? ' · Android ' + esc(t.android) : '') + '</small>' +
            '<span class="pm-tab-transports">' + transports + (t.ip ? '<span class="pm-tab-ip">' + esc(t.ip) + '</span>' : '') + (etatTxt ? '<span class="pm-tab-alerte">' + etatTxt + '</span>' : '') + '</span></span>' +
          '<span class="pm-tab-app' + (t.app_installee ? ' ok' : '') + '"><select data-tab-profil="' + esc(t.serie) + '"' + dis + '>' + profilOpts + '</select>' +
            '<small>' + appTxt + (t.installee_le ? ' · le ' + esc(t.installee_le.slice(0, 16)) : '') + (autres.length ? ' · aussi : ' + esc(autres.join(', ')) : '') + '</small></span>' +
          '<span class="pm-tab-dt">' + identite(t, dis) + jetonTexte(t) + '</span>' +
          '<span class="pm-actions pm-tab-actions">' +
            (t.connectee && t.transports.indexOf('usb') >= 0 && t.transports.indexOf('wifi') < 0 ? '<button type="button" class="pm-lien" data-tab-wifi="' + esc(t.serie) + '" title="Ouvre le port adb Wi-Fi (5555) : la tablette peut ensuite être débranchée">Ouvrir le Wi-Fi</button>' : '') +
            (t.connectee ? '<button type="button" class="pm-btn-sombre pm-tab-installer" data-tab-installer="' + esc(t.serie) + '"' + (manque ? ' disabled title="' + esc(manque) + '"' : '') + (job && job.etat === 'en_cours' ? ' disabled' : '') + '>' + (t.app_installee ? 'Mettre à jour' : 'Installer') + '</button>' : '') +
            ((t.profil === 'dt' || t.profil === 'exc') && t.ident ? '<button type="button" class="pm-lien" data-tab-jeton="renouveler" data-code="' + esc(t.ident) + '" title="Nouveau jeton : l\'ancien reste valable 7 jours, le temps que la tablette reçoive le nouveau">Renouveler le jeton</button>' +
              '<button type="button" class="pm-lien danger" data-tab-jeton="revoquer" data-code="' + esc(t.ident) + '" title="Coupe l\'accès de cette tablette immédiatement">Révoquer</button>' : '') +
            (t.profil === 'dt' && t.ident ? '<button type="button" class="pm-lien" data-tab-cle-radio="' + esc(t.ident) + '" title="Nouvelle clé de signature des positions envoyées par radio ; l\'ancienne reste valable 7 jours">Renouveler la clé radio</button>' : '') +
            (t.connectee && t.app_installee && (t.profil === 'dt' || t.profil === 'exc') && !manque ? '<button type="button" class="pm-lien" data-tab-parametres="' + esc(t.serie) + '" title="Renvoie le DT et la connexion (serveurs, MQTT) à la tablette, sans réinstaller">Envoyer les paramètres</button>' : '') +
            (t.connectee && t.app_installee && t.profil !== 'link' && t.profil !== 'spotter' ? '<button type="button" class="pm-lien danger" data-tab-reinit="' + esc(t.serie) + '" title="Supprime l\'identité écrite sur la tablette">Réinitialiser</button>' : '') +
          '</span>' +
        '</div>' + etapes + '</div>';
    }).join('');
  }

  function jetonTexte(t) {
    if (!(t.profil === 'dt' || t.profil === 'exc') || !t.ident) return '';
    const j = (jetons.jetons || []).find((x) => x.code === t.ident);
    if (!j) return '';
    const etatJ = j.action === 'revoque' ? 'jeton révoqué : à reprovisionner' : (j.action === 'renouvele' ? 'jeton renouvelé' + (j.tolerance ? ' (ancien valable 7 jours)' : '') : 'jeton n°' + j.gen);
    const cle = clesRadio.find((x) => x.dt_code === t.ident);
    const etatCle = t.profil !== 'dt' ? '' : (cle ? (cle.actif ? 'clé radio ' + cle.empreinte + (cle.tolerance ? ' (ancienne valable 7 jours)' : '') : 'clé radio révoquée') : 'clé radio : créée à l\'envoi des paramètres');
    return '<small>' + esc(jetons.actifs ? etatJ : 'jeton maître (jetons par tablette en attente des serveurs)') + '</small>' + (etatCle ? '<small>' + esc(etatCle) + '</small>' : '');
  }

  function incomplete(t) {
    if (t.profil === 'dt' || t.profil === 'exc') return t.ident && t.famille ? '' : (t.profil === 'dt' ? 'Choisissez le DT' : 'Renseignez l\'id et la famille');
    return '';
  }

  function identite(t, dis) {
    const s = esc(t.serie);
    if (t.profil === 'link' || t.profil === 'spotter') return '<small>Aucune identité</small>';
    const dt = t.profil === 'dt';
    const liste = flotte.filter((d) => (/^DT-/i.test(d.code)) === dt);
    if (!liste.length && !dt) {
      return '<input type="text" data-tab-champ="ident" data-serie="' + s + '" value="' + esc(t.ident) + '" placeholder="Id"' + dis + ' />' +
             '<input type="text" data-tab-champ="famille" data-serie="' + s + '" value="' + esc(t.famille) + '" placeholder="Famille"' + dis + ' />';
    }
    const opts = '<option value="">Choisir…</option>' + liste.map((d) => '<option value="' + esc(d.code) + '"' + (d.code === t.ident ? ' selected' : '') + '>' + esc(d.code) + ' · ' + esc(d.famille) + '</option>').join('') +
      (t.ident && !liste.some((d) => d.code === t.ident) ? '<option value="' + esc(t.ident) + '" selected>' + esc(t.ident) + ' · ' + esc(t.famille) + '</option>' : '');
    return '<select data-tab-flotte="' + s + '"' + dis + '>' + opts + '</select><small>' + (t.famille ? 'Famille ' + esc(t.famille) : 'Id + famille') + '</small>';
  }

  async function rafraichir(silencieux) {
    if (enCours) return;
    enCours = true;
    try {
      etat = await appel('/api/tablettes');
      try { jetons = await appel('/api/jetons'); } catch (e) { jetons = { jetons: [], actifs: false }; }
      try { clesRadio = (await appel('/api/cles_dt')).cles_dt || []; } catch (e) { clesRadio = []; }
      if (!silencieux) statut('', true);
    } catch (e) {
      const ancienne = e.status === 404 || /endpoint inconnu/i.test(e.message);
      etat = { tablettes: [], apk: {}, adb: { erreur: ancienne
        ? 'la passerelle en cours ne gère pas encore les tablettes : redémarrez-la (lanceur.py)'
        : 'passerelle injoignable (' + e.message + ')' } };
    } finally {
      enCours = false;
    }
    rendre();
  }

  function visible() {
    const pane = document.querySelector('#page-settings .onglet-pane[data-onglet="tablettes"]');
    return pane && !pane.hidden;
  }

  function demarrer() {
    if (minuteur) return;
    rafraichir(false);
    minuteur = setInterval(() => { if (visible() && !document.hidden) rafraichir(true); }, 6000);
  }

  async function suivreJob(serie, job) {
    jobs[serie] = { job: job, etapes: [], etat: 'en_cours', erreur: '' };
    for (let i = 0; i < 200; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      try {
        const d = await appel('/api/tablettes/installation?job=' + encodeURIComponent(job));
        jobs[serie] = d;
        rendre();
        if (d.etat !== 'en_cours') break;
      } catch (e) { break; }
    }
    await rafraichir(true);
  }

  async function installer(serie, reinstaller) {
    const t = etat.tablettes.find((x) => x.serie === serie);
    if (!t) return;
    const test = !!($('#tabAutoriserTest') || {}).checked;
    try {
      const d = await appel('/api/tablettes/installer', 'POST', {
        serie: serie, adresse_adb: t.adresse_adb, autoriser_test: test, reinstaller: !!reinstaller });
      statut('', true);
      suivreJob(serie, d.job);
      jobs[serie] = { job: d.job, etapes: [], etat: 'en_cours', erreur: '' };
      rendre();
    } catch (e) { statut(e.message, false); }
  }

  function brancher() {
    const pane = document.querySelector('#page-settings .onglet-pane[data-onglet="tablettes"]');
    if (!pane) return;
    document.querySelector('#settingsOnglets').addEventListener('click', (e) => {
      const b = e.target.closest('.onglet');
      if (b && b.dataset.onglet === 'tablettes') { demarrer(); rafraichir(false); }
    });
    pane.addEventListener('click', async (e) => {
      const w = e.target.closest('[data-tab-wifi]');
      if (w) {
        const t = etat.tablettes.find((x) => x.serie === w.dataset.tabWifi);
        statut('Ouverture du port Wi-Fi…', true);
        try {
          const d = await appel('/api/tablettes/wifi', 'POST', { serie: w.dataset.tabWifi, adresse_adb: t && t.adresse_adb });
          statut(d.ok ? 'Port ' + d.port + ' ouvert : connectée en Wi-Fi sur ' + d.ip + '. Le câble peut être retiré.' : (d.erreur || d.detail || 'Échec'), !!d.ok);
        } catch (err) { statut(err.message, false); }
        rafraichir(true);
        return;
      }
      const i = e.target.closest('[data-tab-installer]');
      if (i) { installer(i.dataset.tabInstaller, false); return; }
      const r = e.target.closest('[data-tab-reinstaller]');
      if (r) { installer(r.dataset.tabReinstaller, true); return; }
      const kr = e.target.closest('[data-tab-cle-radio]');
      if (kr) {
        const code = kr.dataset.tabCleRadio;
        const t = etat.tablettes.find((x) => x.ident === code);
        try {
          await appel('/api/cles_dt', 'POST', { dt_code: code });
          if (t && t.connectee) {
            await appel('/api/tablettes/parametres', 'POST', { serie: t.serie, adresse_adb: t.adresse_adb });
            statut('Clé radio de ' + code + ' renouvelée et envoyée à la tablette.', true);
          } else {
            statut('Clé radio de ' + code + ' renouvelée : à envoyer à la tablette (« Envoyer les paramètres »), l\'ancienne reste valable 7 jours.', true);
          }
        } catch (err) { statut(err.message, false); }
        rafraichir(true);
        return;
      }
      const jt = e.target.closest('[data-tab-jeton]');
      if (jt) {
        const action = jt.dataset.tabJeton;
        const code = jt.dataset.code;
        const revoque = action === 'revoquer';
        if (window.Splash && Splash.confirmer && !await Splash.confirmer((revoque ? 'Révoquer ' : 'Renouveler le jeton de ') + code + ' ?', {
          titre: revoque ? 'Révocation du jeton' : 'Renouvellement du jeton', danger: revoque, libelleOk: revoque ? 'Révoquer' : 'Renouveler',
          details: revoque ? 'La tablette perd l\'accès aux serveurs immédiatement. Il faudra la reprovisionner depuis la console (« Envoyer les paramètres »).'
            : 'Un nouveau jeton est distribué par Serveur_OT ; l\'ancien reste valable 7 jours.' })) return;
        try {
          await appel('/api/jetons/' + action, 'POST', { code: code });
          statut(revoque ? 'Jeton de ' + code + ' révoqué.' : 'Jeton de ' + code + ' renouvelé : distribué à la tablette par Serveur_OT.', true);
        } catch (err) { statut(err.message, false); }
        rafraichir(true);
        return;
      }
      const pa = e.target.closest('[data-tab-parametres]');
      if (pa) {
        const t = etat.tablettes.find((x) => x.serie === pa.dataset.tabParametres);
        statut('Envoi des paramètres…', true);
        try {
          const d = await appel('/api/tablettes/parametres', 'POST', { serie: pa.dataset.tabParametres, adresse_adb: t && t.adresse_adb });
          statut('Paramètres envoyés' + (d.connexion && d.connexion.length ? ' (' + d.connexion.map((x) => ({ serveur1: 'serveur n°1', serveur2: 'serveur n°2', mqtt: 'MQTT' }[x] || x)).join(', ') + ')' : '') + ' : l\'app les reprend d\'elle-même.', true);
        } catch (err) { statut(err.message, false); }
        return;
      }
      const ri = e.target.closest('[data-tab-reinit]');
      if (ri) {
        const t = etat.tablettes.find((x) => x.serie === ri.dataset.tabReinit);
        try {
          const d = await appel('/api/tablettes/reinitialiser', 'POST', { serie: ri.dataset.tabReinit, adresse_adb: t && t.adresse_adb });
          statut(d.ok ? 'Affectation retirée de la tablette : l\'écran de connexion revient.' : (d.erreur || 'Échec'), !!d.ok);
        } catch (err) { statut(err.message, false); }
        rafraichir(true);
      }
    });
    async function enregistrer(serie, champs) {
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      const t = etat.tablettes.find((x) => x.serie === serie) || {};
      const corps = Object.assign({ serie: serie, profil: t.profil, ident: t.ident, famille: t.famille, equipe: t.equipe }, champs);
      try {
        await appel('/api/tablettes/affecter', 'POST', corps);
        statut('Enregistré.', true);
      } catch (err) { statut(err.message, false); }
      await rafraichir(true);
    }
    pane.addEventListener('change', (e) => {
      const pr = e.target.closest('[data-tab-profil]');
      if (pr) { enregistrer(pr.dataset.tabProfil, { profil: pr.value, ident: '', famille: '', equipe: '' }); return; }
      const sel = e.target.closest('[data-tab-flotte]');
      if (sel) {
        const d = flotte.find((x) => x.code === sel.value);
        enregistrer(sel.dataset.tabFlotte, { ident: sel.value, famille: d ? d.famille : '' });
        return;
      }
      const ch = e.target.closest('[data-tab-champ]');
      if (ch) { const c = {}; c[ch.dataset.tabChamp] = ch.value; enregistrer(ch.dataset.serie, c); }
    });
    const form = $('#tabConnecterForm');
    if (form) form.addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const d = await appel('/api/tablettes/connecter', 'POST', { adresse: $('#tabAdresse').value });
        statut(d.ok ? 'Connectée : ' + d.adresse : (d.erreur || d.detail || 'Connexion impossible'), !!d.ok);
      } catch (err) { statut(err.message, false); }
      rafraichir(true);
    });
    const bDet = $('#tabDetecter');
    if (bDet) bDet.addEventListener('click', () => { statut('Détection…', true); rafraichir(false); });
  }

  function init() {
    if (!document.getElementById('page-settings')) return;
    brancher();
    chargerFlotte().then(rendre);
    window.addEventListener('app:auth-ready', () => chargerFlotte().then(rendre));
    // l'onglet peut etre ouvert par un lien direct : on demarre aussi s'il est deja visible
    if (visible()) demarrer();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
