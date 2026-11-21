/* =====================================================================
 * tablettes.js — Settings › Tablettes : detection automatique, affectation et installation.
 *
 * Une installation = une famille de camion = un id DT.
 *   1. La passerelle detecte les tablettes (adb : USB ou Wi-Fi) ;
 *   2. on affecte un DT a chaque tablette (sa famille suit : sync_flotte) ;
 *   3. « Installer » pousse l'APK du DT, ecrit le provisionnement (DT + famille) sur la tablette
 *      et lance l'app, qui s'ouvre directement sur ce DT.
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
  let flotte = [];                 // [{ code: 'DT-04', famille: '1500' }]
  const jobs = {};                 // serie -> { job, etapes, etat, erreur }
  let minuteur = null;
  let enCours = false;

  async function chargerFlotte() {
    try {
      const d = await API.getRows('sync_flotte');
      flotte = ((d && d.rows) || [])
        .filter((r) => r.actif !== 0 && /^DT-/i.test(String(r.code || '')))
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
    const apkBox = $('#tabApk');
    if (apkBox) {
      const retenu = (apk.fichiers || []).find((f) => f.nom === apk.retenu);
      apkBox.innerHTML = retenu
        ? '<b>' + esc(retenu.nom) + '</b><small>Production · ' + esc(retenu.date) + ' · ' + retenu.taille_mo + ' Mo</small>'
        : '<b>Aucun APK de production</b><small>' + esc(apk.motif || '') + '</small>';
    }
    if (!etat.tablettes.length) {
      box.innerHTML = '<div class="pm-vide">Aucune tablette détectée. Branchez une tablette en USB (débogage USB activé) ou connectez-la par son adresse IP.</div>';
      return;
    }
    box.innerHTML = etat.tablettes.map((t) => {
      const job = jobs[t.serie];
      const options = '<option value="">Choisir un DT…</option>' +
        flotte.map((d) => '<option value="' + esc(d.code) + '"' + (d.code === t.dt_code ? ' selected' : '') + '>' + esc(d.code) + ' · ' + esc(d.famille) + '</option>').join('') +
        (t.dt_code && !flotte.some((d) => d.code === t.dt_code) ? '<option value="' + esc(t.dt_code) + '" selected>' + esc(t.dt_code) + ' · ' + esc(t.famille) + '</option>' : '');
      const transports = t.transports.map((x) => '<span class="pm-tab-puce">' + (x === 'wifi' ? 'Wi-Fi' : 'USB') + '</span>').join('');
      const appTxt = t.app_installee
        ? 'App DT installée' + (t.app_version ? ' · v' + esc(t.app_version) : '')
        : 'App DT non installée';
      const etatTxt = t.etat === 'device' ? '' : (t.etat === 'unauthorized' ? 'Autorisez le débogage USB sur la tablette' : (t.etat === 'absente' ? 'Absente' : esc(t.etat)));
      const affecte = t.dt_code && t.famille;
      const etapes = job ? '<div class="pm-tab-etapes">' + job.etapes.map((e) => '<span class="' + (e.ok ? '' : 'ko') + '">' + (e.ok ? '✓ ' : '✕ ') + esc(e.texte) + '</span>').join('') +
        (job.etat === 'en_cours' ? '<span class="en-cours">Installation en cours…</span>' : '') +
        (job.etat === 'erreur' && /AUTRE cle/.test(job.erreur) ? '<button type="button" class="pm-lien danger" data-tab-reinstaller="' + esc(t.serie) + '">Réinstaller (efface les données de l\'app)</button>' : '') + '</div>' : '';
      return '<div class="pm-tab" data-serie="' + esc(t.serie) + '">' +
        '<div class="pm-ligne pm-ligne-tab">' +
          '<span class="pm-tab-id"><b>' + esc(t.modele || 'Tablette') + '</b><small>' + esc(t.serie) +
            (t.batterie != null ? ' · ' + t.batterie + ' %' : '') + (t.android ? ' · Android ' + esc(t.android) : '') + '</small>' +
            '<span class="pm-tab-transports">' + transports + (t.ip ? '<span class="pm-tab-ip">' + esc(t.ip) + '</span>' : '') + (etatTxt ? '<span class="pm-tab-alerte">' + etatTxt + '</span>' : '') + '</span></span>' +
          '<span class="pm-tab-app' + (t.app_installee ? ' ok' : '') + '">' + appTxt + (t.installee_le ? '<small>Installée le ' + esc(t.installee_le.slice(0, 16)) + '</small>' : '') + '</span>' +
          '<span class="pm-tab-dt"><select data-tab-dt="' + esc(t.serie) + '"' + (t.connectee ? '' : ' disabled') + '>' + options + '</select>' +
            '<small>' + (affecte ? 'Famille ' + esc(t.famille) : 'Un DT = une famille') + '</small></span>' +
          '<span class="pm-actions pm-tab-actions">' +
            (t.connectee && t.transports.indexOf('usb') >= 0 && t.transports.indexOf('wifi') < 0 ? '<button type="button" class="pm-lien" data-tab-wifi="' + esc(t.serie) + '" title="Ouvre le port adb Wi-Fi (5555) : la tablette peut ensuite être débranchée">Ouvrir le Wi-Fi</button>' : '') +
            (t.connectee ? '<button type="button" class="pm-btn-sombre pm-tab-installer" data-tab-installer="' + esc(t.serie) + '"' + (affecte ? '' : ' disabled title="Affectez d\'abord un DT"') + (job && job.etat === 'en_cours' ? ' disabled' : '') + '>' + (t.app_installee ? 'Mettre à jour' : 'Installer') + '</button>' : '') +
            (t.connectee && t.app_installee ? '<button type="button" class="pm-lien danger" data-tab-reinit="' + esc(t.serie) + '" title="Supprime l\'affectation sur la tablette : l\'écran de connexion revient">Réinitialiser</button>' : '') +
          '</span>' +
        '</div>' + etapes + '</div>';
    }).join('');
  }

  async function rafraichir(silencieux) {
    if (enCours) return;
    enCours = true;
    try {
      etat = await appel('/api/tablettes');
      if (!silencieux) statut('', true);
    } catch (e) {
      etat = { tablettes: [], apk: {}, adb: { erreur: 'passerelle injoignable (' + e.message + ')' } };
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
    pane.addEventListener('change', async (e) => {
      const sel = e.target.closest('[data-tab-dt]');
      if (!sel || !sel.value) return;
      const d = flotte.find((x) => x.code === sel.value);
      try {
        await appel('/api/tablettes/affecter', 'POST', { serie: sel.dataset.tabDt, dt: sel.value, famille: d ? d.famille : '' });
        statut(sel.value + ' affecté à la tablette (famille ' + (d ? d.famille : '?') + ').', true);
      } catch (err) { statut(err.message, false); }
      rafraichir(true);
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
