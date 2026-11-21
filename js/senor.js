/* =====================================================================
 * senor.js - page "Senor" : plans de minage, DA v2 (cartes sg2-*).
 * Carte 1 Destination : source miniere (Main Pit, Second Pit...) et ses
 * teneurs (familles F5M, F2M...).
 * Carte 2 Dumping Area : zones persistees (nom/x/y/couleur) + import CSV.
 * Carte 3 Trails (Chemin) : import du fichier GeoTiff/GeoJSON du trafic
 * road, liste et suppression.
 * Carte 4 Geographic System : non cablee (en attente de specification).
 * ===================================================================== */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const BASE_KEY = 'sgApiBase';
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  function base() {
    const saved = localStorage.getItem(BASE_KEY);
    if (saved) return saved.replace(/\/+$/, '');
    const host = window.location.hostname || 'localhost';
    return window.location.protocol + '//' + host + ':8101';
  }

  async function jsend(path, method, body) {
    const r = await fetch(base() + path, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
    return j;
  }

  function setStatus(id, msg, ok) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' ok' : ' err') : '');
    if (msg) setTimeout(() => { el.textContent = ''; }, 4000);
  }

  function coord(n) {
    return String(Math.round(Number(n) || 0));
  }

  function toggle(el) {
    if (!el) return;
    el.style.display = el.style.display === 'none' ? '' : 'none';
  }

  // Popup de confirmation (remplace window.confirm) : resout true/false.
  // `opts` adapte titre et bouton ; par defaut la popup reste celle d'une
  // suppression, forme attendue par la majorite des appels. Sans ce
  // parametrage, confirmer une AFFECTATION affichait « Confirmer la
  // suppression » et un bouton « Supprimer » rouge.
  //   opts = { titre, libelleOk, danger }
  function confirmerPopup(message, opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      const overlay = $('#sgConfirmOverlay');
      const msgEl = $('#sgConfirmMessage');
      const btnOk = $('#sgConfirmOk');
      const btnCancel = $('#sgConfirmCancel');
      const titreEl = overlay ? overlay.querySelector('h3') : null;
      if (!overlay || !msgEl || !btnOk || !btnCancel) { resolve(window.confirm(message)); return; }
      const danger = o.danger !== false;
      if (titreEl) titreEl.textContent = o.titre || 'Confirmer la suppression';
      btnOk.textContent = o.libelleOk || 'Supprimer';
      btnOk.className = 'sg2-outline ' + (danger ? 'sg2-danger' : 'sg2-ok');
      msgEl.textContent = message;
      overlay.hidden = false;
      function nettoyer(reponse) {
        overlay.hidden = true;
        btnOk.removeEventListener('click', onOk);
        btnCancel.removeEventListener('click', onCancel);
        overlay.removeEventListener('click', onBackdrop);
        resolve(reponse);
      }
      function onOk() { nettoyer(true); }
      function onCancel() { nettoyer(false); }
      function onBackdrop(e) { if (e.target === overlay) nettoyer(false); }
      btnOk.addEventListener('click', onOk);
      btnCancel.addEventListener('click', onCancel);
      overlay.addEventListener('click', onBackdrop);
    });
  }

  /* =========================== CARTE 1 : DESTINATION =================== */

  let sources = [];
  let teneurs = [];
  let sourceSelectionnee = null;
  let teneurSelectionnee = null;

  /* ---- Listes des onglets (meme charte que « Consignes ») ---- */
  let toutesTeneurs = [];            // toutes sources confondues : compteurs par source, familles par aire
  const COULEUR_DA = { blue: '#1d6fd1', green: '#2f9e44', yellow: '#e0b413', orange: '#e8641c' };
  const couleurFamille = (t) => (window.OTCouleurs ? OTCouleurs.famille(t.nom) : '#8d887a');
  const pastilleFamille = (t) => '<span class="sg-fam-pastille" style="--fam:' + couleurFamille(t) + '">' + esc(t.nom) + '</span>';
  const deuxN = (n) => String(n).padStart(2, '0');

  function aireM2(z) {                // surface reelle du polygone (formule du lacet)
    let pts = [];
    try { pts = JSON.parse(z.sommets || '[]'); } catch (e) { pts = []; }
    if (pts.length < 3) return 0;
    let a = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x1, y1] = pts[i];
      const [x2, y2] = pts[(i + 1) % pts.length];
      a += x1 * y2 - x2 * y1;
    }
    return Math.abs(a) / 2;
  }

  function rendreListeSources() {
    const box = $('#sgSourcesList');
    if (!box) return;
    box.innerHTML = sources.length ? sources.map((src) => {
      const n = toutesTeneurs.filter((t) => t.source_id === src.id).length;
      const actif = src.id === sourceSelectionnee;
      return '<div class="sg-ligne' + (actif ? ' actif' : '') + '">' +
        '<i class="fas fa-layer-group sg-ligne-ico"></i>' +
        '<span class="sg-ligne-nom"><b>' + esc(src.nom) + '</b><small>' + (src.codes ? 'Codes admis : ' + esc(src.codes) : 'Aucun code admis') + '</small></span>' +
        '<span class="sg-ligne-meta">' + deuxN(n) + (n > 1 ? ' familles' : ' famille') + '</span>' +
        (actif ? '<span class="sg-badge">Active</span>'
          : '<button type="button" class="sg-btn" data-src-select="' + src.id + '">Activer</button>') +
        '</div>';
    }).join('') : '<div class="sg-vide">Aucune source.</div>';
    const nom = $('#sgFamillesSource');
    const cur = sources.find((x) => x.id === sourceSelectionnee);
    if (nom) nom.textContent = cur ? '· ' + cur.nom : '';
  }

  function rendreListeAires() {
    const box = $('#sgDaRow');
    if (!box) return;
    box.innerHTML = dumpingAreas.length ? dumpingAreas.map((z) => {
      let nbSommets = 0;
      try { nbSommets = JSON.parse(z.sommets || '[]').length; } catch (e) { /* sommets illisibles */ }
      const m2 = Math.round(aireM2(z));
      const fam = toutesTeneurs.filter((t) => t.dumping_area_id === z.id);
      return '<div class="sg-ligne">' +
        '<i class="far fa-square-full sg-ligne-ico" style="color:' + (COULEUR_DA[z.couleur] || COULEUR_DA.blue) + '"></i>' +
        '<span class="sg-ligne-nom"><b>' + esc(z.nom) + '</b><small>' + nbSommets + ' sommets · centre ' + esc(coord(z.x)) + ' / ' + esc(coord(z.y)) + '</small></span>' +
        '<span class="sg-ligne-meta">' + m2.toLocaleString('fr-FR') + ' m²</span>' +
        '<span class="sg-ligne-fams">' + (fam.length ? fam.map(pastilleFamille).join('') : '<em>Aucune famille</em>') + '</span>' +
        '<button type="button" class="sg-ico-btn" data-da-edit="' + z.id + '" title="Modifier"><i class="fas fa-pen"></i></button>' +
        '</div>';
    }).join('') : '<div class="sg-vide">Aucune aire de déchargement.</div>';
  }

  /* ---- Trajectoires : source -> dumping area, avec la liste ORDONNEE des checkpoints ----
   * Ce qui est defini ici fait foi : la passerelle l'applique aux missions et le pousse vers
   * Serveur_OT puis Serveur_SG (synchroniserSG), qui se mettent a jour d'apres cette liste. */
  let trajectoires = [];
  let tousCheckpoints = [];
  let trajEdition = null;            // { id, ids: [checkpoint ids dans l'ordre] }

  async function synchroniserSG() {
    if (!window.OTCFG) return;
    setStatus('#sgTrajStatus', 'Synchronisation avec Serveur_SG…', true);
    try {
      await OTCFG.pousserReferentiel();
      const r = await fetch(base().replace(/\/$/, '') + '/api/sg/sync_ref', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + r.status));
      setStatus('#sgTrajStatus', 'Serveur_SG mis à jour avec ces trajectoires', true);
    } catch (e) {
      setStatus('#sgTrajStatus', 'Enregistré ici, mais Serveur_SG non synchronisé : ' + e.message, false);
    }
  }

  function rendreTrajectoires() {
    const box = $('#sgTrajList');
    if (!box) return;
    const cpt = $('#sgTrajCompte');
    if (cpt) cpt.textContent = trajectoires.length ? '· ' + deuxN(trajectoires.length) : '';
    if (!trajectoires.length) {
      box.innerHTML = '<div class="sg-vide">Aucune trajectoire. Sans trajectoire, les checkpoints viennent de Serveur_SG.</div>';
      return;
    }
    box.innerHTML = trajectoires.map((t) => {
      const ouvert = trajEdition && trajEdition.id === t.id;
      const liste = ouvert ? trajEdition.ids.map((id) => tousCheckpoints.find((c) => c.id === id)).filter(Boolean) : t.checkpoints;
      const puces = liste.length
        ? liste.map((c, i) => '<span class="sg-cp-puce"><b>' + (i + 1) + '</b>' + esc(c.nom) + '</span>').join('<i class="fas fa-chevron-right sg-cp-sep"></i>')
        : '<em>Aucun checkpoint</em>';
      let editeur = '';
      if (ouvert) {
        const libres = tousCheckpoints.filter((c) => trajEdition.ids.indexOf(c.id) < 0);
        editeur = '<div class="sg-traj-edit">' +
          (liste.length ? liste.map((c, i) =>
            '<div class="sg-traj-cp"><span class="sg-traj-rang">' + (i + 1) + '</span><b>' + esc(c.nom) + '</b>' +
            '<button type="button" class="sg-ico-btn" data-traj-haut="' + i + '" title="Monter"' + (i === 0 ? ' disabled' : '') + '><i class="fas fa-arrow-up"></i></button>' +
            '<button type="button" class="sg-ico-btn" data-traj-bas="' + i + '" title="Descendre"' + (i === liste.length - 1 ? ' disabled' : '') + '><i class="fas fa-arrow-down"></i></button>' +
            '<button type="button" class="sg-ico-btn danger" data-traj-retirer="' + i + '" title="Retirer"><i class="fas fa-xmark"></i></button></div>').join('')
            : '<div class="sg-vide">Ajoutez les checkpoints dans l\'ordre de passage.</div>') +
          '<div class="sg-traj-ajout">' +
            '<select id="sgTrajAjoutSel" class="sg-input"><option value="">Ajouter un checkpoint…</option>' +
              libres.map((c) => '<option value="' + c.id + '">' + esc(c.nom) + '</option>').join('') + '</select>' +
            '<button type="button" class="sg-btn" data-traj-ajouter="1">Ajouter</button>' +
            '<button type="button" class="sg-btn sg-btn-ok" data-traj-enregistrer="' + t.id + '">Enregistrer</button>' +
            '<button type="button" class="sg-btn" data-traj-annuler="1">Annuler</button>' +
          '</div></div>';
      }
      return '<div class="sg-traj' + (ouvert ? ' ouvert' : '') + '">' +
        '<div class="sg-ligne">' +
          '<i class="fas fa-route sg-ligne-ico"></i>' +
          '<span class="sg-ligne-nom"><b>' + esc(t.source) + ' <i class="fas fa-arrow-right sg-traj-fleche"></i> ' + esc(t.dumping_area) + '</b>' +
            '<span class="sg-cp-suite">' + puces + '</span></span>' +
          '<button type="button" class="sg-ico-btn" data-traj-edit="' + t.id + '" title="Modifier l\'ordre des checkpoints"><i class="fas fa-pen"></i></button>' +
          '<button type="button" class="sg-ico-btn danger" data-traj-del="' + t.id + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
        '</div>' + editeur + '</div>';
    }).join('');
  }

  async function chargerTrajectoires() {
    if (!$('#sgTrajList')) return;
    try {
      trajectoires = (await jsend('/api/trajectoires', 'GET')).trajectoires || [];
      tousCheckpoints = (await jsend('/api/checkpoints', 'GET')).checkpoints || [];
      rendreTrajectoires();
    } catch (e) {
      setStatus('#sgTrajStatus', 'Trajectoires injoignables (' + e.message + ')', false);
    }
  }

  function remplirFormTrajectoire() {
    const ss = $('#sgTrajSource'), sd = $('#sgTrajDa');
    if (ss) ss.innerHTML = sources.map((x) => '<option value="' + x.id + '">' + esc(x.nom) + '</option>').join('');
    if (sd) sd.innerHTML = dumpingAreas.map((z) => '<option value="' + z.id + '">' + esc(z.nom) + '</option>').join('');
  }

  async function creerTrajectoire(e) {
    e.preventDefault();
    try {
      const d = await jsend('/api/trajectoires', 'POST', {
        source_id: Number($('#sgTrajSource').value), dumping_area_id: Number($('#sgTrajDa').value) });
      $('#sgTrajForm').style.display = 'none';
      await chargerTrajectoires();
      trajEdition = { id: d.id, ids: [] };      // ouvre l'editeur : on ajoute aussitot les checkpoints
      rendreTrajectoires();
    } catch (err) { setStatus('#sgTrajStatus', err.message, false); }
  }

  async function enregistrerTrajectoire(id) {
    try {
      await jsend('/api/trajectoire_checkpoints', 'POST', { id: id, checkpoints: trajEdition.ids });
      trajEdition = null;
      await chargerTrajectoires();
      await synchroniserSG();
    } catch (err) { setStatus('#sgTrajStatus', err.message, false); }
  }

  async function chargerSources() {
    try {
      const d = await jsend('/api/sources', 'GET');
      sources = d.sources || [];
      if (!sourceSelectionnee || !sources.some((s) => s.id === sourceSelectionnee)) {
        sourceSelectionnee = sources.length ? sources[0].id : null;
        teneurSelectionnee = null;
      }
      rendreSourceLabel();
      await chargerTeneurs();
    } catch (e) {
      setStatus('#sgDestStatus', 'Serveur SG injoignable (' + e.message + ')', false);
    }
  }

  function rendreSourceLabel() {
    const s = sources.find((x) => x.id === sourceSelectionnee);
    const texte = s ? s.nom : '(aucune)';
    const plusieurs = sources.length > 1;
    [['#sgSourceLabel', '#sgSrcPrev', '#sgSrcNext'],
     ['#sgDiSourceLabel', '#sgDiSrcPrev', '#sgDiSrcNext']].forEach((ids) => {
      const label = $(ids[0]);
      if (label) label.textContent = texte;
      const prev = $(ids[1]);
      const next = $(ids[2]);
      if (prev) prev.disabled = !plusieurs;
      if (next) next.disabled = !plusieurs;
    });
    rendreEntete();
    rendreListeSources();
    // codes de source admis (garde-fou blasts) : pre-remplir le champ
    const codesEl = $('#sgSrcCodes');
    if (codesEl) codesEl.value = s ? (s.codes || '') : '';
  }

  function naviguerSource(delta) {
    if (sources.length < 2) return;
    const idx = sources.findIndex((s) => s.id === sourceSelectionnee);
    const suivant = ((idx === -1 ? 0 : idx) + delta + sources.length) % sources.length;
    sourceSelectionnee = sources[suivant].id;
    teneurSelectionnee = null;
    rendreSourceLabel();
    chargerTeneurs();
  }

  /**
   * Pastilles du panneau Dumping Instruction.
   *
   * Chacune porte la consigne en clair (« F5M -> ROMPAD 1 ») : sans cela il
   * fallait ouvrir la popup pour savoir ce qui etait affecte, et rien
   * n'indiquait qu'un second clic permet de MODIFIER la consigne.
   * Appelee aussi depuis chargerZones() : au demarrage les teneurs peuvent
   * arriver avant la liste des Dumping Areas, et le nom manquerait.
   */
  // Couleur d'une famille : js/couleurs.js (grade du 1er code de bloc, ou couleur propre de la famille).
  window.addEventListener('couleurs:pret', () => { rendreChipsInstruction(); rendreListeAires(); rendreListeSources(); chargerTeneurs(); });
  const premierCode = (t) => String(t.codes || '').split(/[,\s;]+/).filter(Boolean)[0] || '';

  /**
   * Liste « Consignes de déchargement » (onglet Consignes) : une ligne par famille de teneur
   * (code du nom de bloc, plage d'admission, aire de déchargement). Un clic ouvre la popup
   * d'affectation. Appelée aussi depuis chargerZones() : au démarrage les teneurs peuvent
   * arriver avant la liste des dumping areas.
   */
  function rendreChipsInstruction() {
    const box = $('#sgConsignes');
    if (!box) return;
    const fmt = (v) => (Number(v) || 0).toFixed(2).replace('.', ',');
    box.innerHTML = teneurs.length ? teneurs.map((t) => {
      const da = dumpingAreas.find((z) => z.id === t.dumping_area_id);
      const code = premierCode(t);
      const bloc = '<div class="sg-cons-fam"><span class="sg-cons-code">' + esc(code || '?') + '</span>' +
        '<div><b>' + esc(t.nom) + '</b><small>' + (t.codes ? 'Code de bloc : ' + esc(String(t.codes).trim()) : 'Aucun code de bloc') + '</small></div></div>';
      const plage = '<span class="sg-cons-plage">' + fmt(t.valeur_min) + ' – ' + fmt(t.valeur_max) + ' %</span>';
      const aire = da
        ? '<button type="button" class="sg-cons-aire" data-teneur-assign="' + t.id + '" title="Cliquer pour modifier">' +
          '<i class="far fa-square-full"></i><span><b>' + esc(da.nom) + '</b><small>Aire polygonale affectée</small></span><i class="fas fa-chevron-down"></i></button>'
        : '<button type="button" class="sg-cons-aire manquante" data-teneur-assign="' + t.id + '" title="Cliquer pour affecter">' +
          '<i class="fas fa-triangle-exclamation"></i><span><b>Affecter une aire</b><small>Obligatoire pour cette famille</small></span><i class="fas fa-arrow-right"></i></button>';
      return '<div class="sg-cons-row' + (da ? '' : ' manquante') + '" style="--fam:' + (window.OTCouleurs ? OTCouleurs.famille(t.nom) : '#8d887a') + '">' + bloc + plage + '<i class="fas fa-arrow-right sg-cons-fleche"></i>' + aire + '</div>';
    }).join('') : '<div class="sg2-di-empty">Aucune famille de teneur pour cette source.</div>';

    const manquantes = teneurs.filter((t) => !t.dumping_area_id).length;
    const prog = $('#sgDiProgress');
    if (prog) prog.textContent = teneurs.length
      ? (teneurs.length - manquantes) + ' famille' + (teneurs.length - manquantes > 1 ? 's' : '') + ' affectée' + (teneurs.length - manquantes > 1 ? 's' : '') + ' sur ' + teneurs.length
      : '';
    const fill = $('#sgProgressFill');
    if (fill) fill.style.width = teneurs.length ? Math.round((teneurs.length - manquantes) / teneurs.length * 100) + '%' : '0';
    const statusDi = $('#sgDiStatus');
    if (statusDi) {
      statusDi.textContent = manquantes
        ? manquantes + ' teneur' + (manquantes > 1 ? 's' : '') + ' sans Dumping Area' : '';
      statusDi.className = 'sg2-status account-status' + (manquantes ? ' err' : '');
    }
    rendreEntete();
  }

  // En-tete : rang de la source, compteurs, pastille du systeme geographique, onglets (tout calcule)
  let nbPistes = null;
  function rendreEntete() {
    const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
    const deux = (n) => String(n).padStart(2, '0');
    const idx = sources.findIndex((x) => x.id === sourceSelectionnee);
    set('#sgSourceRang', idx >= 0 ? deux(idx + 1) : '--');
    set('#sgStatFamilles', deux(teneurs.length));
    set('#sgStatAires', deux(dumpingAreas.length));
    set('#sgStatPistes', nbPistes === null ? '--' : deux(nbPistes));
    set('#sgTabConsignes', deux(teneurs.length));
    set('#sgTabSources', deux(sources.length));
    set('#sgTabAires', deux(dumpingAreas.length));
    set('#sgTabTrails', nbPistes === null ? '--' : deux(nbPistes));
    const s = sources.find((x) => x.id === sourceSelectionnee);
    const codes = s && s.codes ? String(s.codes).trim() : '';
    set('#sgInfoCodes', codes
      ? 'Le code ' + codes + ' doit appartenir à ' + s.nom + ' avant toute affectation.'
      : 'Aucun code admis pour cette source : le contrôle du nom de bloc est désactivé.');
    const n = new Date();
    set('#sgMaj', 'Données lues à ' + deux(n.getHours()) + ':' + deux(n.getMinutes()));
  }

  async function chargerTeneurs() {
    const row = $('#sgTeneursRow');
    const rowDi = $('#sgDiTeneursRow');
    if (!row && !rowDi) return;
    if (!sourceSelectionnee) {
      if (row) row.innerHTML = '';
      if (rowDi) rowDi.innerHTML = '';
      rendreDetailTeneur();
      return;
    }
    try {
      const d = await jsend('/api/teneurs?source_id=' + sourceSelectionnee, 'GET');
      teneurs = d.teneurs || [];
      if (teneurSelectionnee && !teneurs.some((t) => t.id === teneurSelectionnee)) teneurSelectionnee = null;
      if (row) {
        const fmt = (v) => (Number(v) || 0).toFixed(2).replace('.', ',');
        row.innerHTML = teneurs.length ? teneurs.map((t) =>
          '<div class="sg-ligne">' +
            '<span class="sg-fam-puce" style="--fam:' + couleurFamille(t) + '"></span>' +
            '<span class="sg-ligne-nom"><b>' + esc(t.nom) + '</b><small>' + (t.codes ? 'Codes de bloc : ' + esc(String(t.codes).trim()) : 'Aucun code de bloc') + '</small></span>' +
            '<span class="sg-ligne-meta">' + fmt(t.valeur_min) + ' – ' + fmt(t.valeur_max) + ' %</span>' +
            '<button type="button" class="sg-ico-btn" data-teneur-edit="' + t.id + '" title="Modifier"><i class="fas fa-pen"></i></button>' +
            '<button type="button" class="sg-ico-btn danger" data-teneur-del="' + t.id + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
          '</div>').join('') : '<div class="sg-vide">Aucune famille de teneur pour cette source.</div>';
      }
      rendreChipsInstruction();
      rendreDetailTeneur();
      await chargerResumeDI();
    } catch (e) {
      setStatus('#sgDestStatus', 'Teneurs injoignables (' + e.message + ')', false);
    }
  }

  function rendreDetailTeneur() {
    const det = $('#sgDestDetail');
    if (!det) return;
    const t = teneurs.find((x) => x.id === teneurSelectionnee);
    if (!t) { det.classList.remove('show'); det.innerHTML = ''; return; }
    det.classList.add('show');
    // Les codes viennent du nom des blocs DXF (HG, MG, LG, WST...). Sans eux
    // la teneur d'un bloc importe reste vide, donc aucune consigne de
    // dumping area ne peut s'appliquer : on les met bien en evidence.
    const codes = String(t.codes || '').trim();
    det.innerHTML = '<span><b>' + esc(t.nom) + '</b> - ' + esc(t.valeur_min) + ' % a ' + esc(t.valeur_max) + ' %</span>' +
      '<span class="sg2-codes' + (codes ? '' : ' vide') + '">' +
      (codes ? 'codes : ' + esc(codes) : 'aucun code de bloc') + '</span>' +
      '<button type="button" class="sg2-x" id="sgTeneurEdit" title="Modifier la teneur"><i class="fas fa-pen"></i></button>' +
      '<button type="button" class="sg2-x" id="sgTeneurDel" title="Supprimer la teneur"><i class="fas fa-trash"></i></button>';
  }

  async function ajouterSource(e) {
    e.preventDefault();
    const nom = ($('#sgNewSource').value || '').trim();
    if (!nom) return;
    const sp = Splash.bloquer('Ajout de la source...', nom);
    try {
      const d = await jsend('/api/sources', 'POST', { nom: nom });
      $('#sgNewSource').value = '';
      $('#sgSrcAddForm').style.display = 'none';
      sourceSelectionnee = d.id; // bascule tout de suite sur la source creee
      teneurSelectionnee = null;
      Splash.etape('Rechargement des sources...');
      await chargerSources();
      Splash.succes('Source ajoutee', nom);
      setStatus('#sgDestStatus', 'Source ajoutee', true);
    } catch (err) {
      Splash.erreur('Ajout impossible', err.message);
      setStatus('#sgDestStatus', err.message, false);
    }
  }

  async function supprimerSource() {
    if (!sourceSelectionnee) return;
    const s = sources.find((x) => x.id === sourceSelectionnee);
    if (!await confirmerPopup('Supprimer la source "' + (s ? s.nom : '') + '" et toutes ses teneurs ?')) return;
    const sp = Splash.bloquer('Suppression de la source...', s ? s.nom : '');
    try {
      Splash.etape('Desactivation des teneurs...');
      await jsend('/api/sources?id=' + sourceSelectionnee, 'DELETE');
      sourceSelectionnee = null;
      Splash.etape('Rechargement...');
      await chargerSources();
      Splash.succes('Source supprimee', s ? s.nom : '');
      setStatus('#sgDestStatus', 'Source supprimee', true);
    } catch (err) {
      Splash.erreur('Suppression impossible', err.message);
      setStatus('#sgDestStatus', err.message, false);
    }
  }

  /** Ouvre le formulaire de teneur, vierge (ajout) ou pre-rempli (modif). */
  function ouvrirFormTeneur(t) {
    const form = $('#sgTeneurForm');
    if (!form) return;
    form.dataset.edit = t ? String(t.id) : '';
    $('#sgNewTeneur').value = t ? t.nom : '';
    $('#sgNewTeneurMin').value = t ? t.valeur_min : '';
    $('#sgNewTeneurMax').value = t ? t.valeur_max : '';
    $('#sgNewTeneurCodes').value = t ? (t.codes || '') : '';
    const propre = t && /^#[0-9a-f]{6}$/i.test(t.couleur || '');
    $('#sgNewTeneurAuto').checked = !propre;
    $('#sgNewTeneurCouleur').value = propre ? t.couleur : (t && window.OTCouleurs ? OTCouleurs.famille(t.nom) : '#8d887a');
    const bouton = $('#sgTeneurSubmit');
    if (bouton) bouton.textContent = t ? 'Modifier' : 'Enregistrer';
    form.style.display = '';
    $('#sgNewTeneur').focus();
  }

  function fermerFormTeneur() {
    const form = $('#sgTeneurForm');
    if (!form) return;
    form.style.display = 'none';
    form.dataset.edit = '';
    $('#sgNewTeneur').value = '';
    $('#sgNewTeneurMin').value = '';
    $('#sgNewTeneurMax').value = '';
    $('#sgNewTeneurCodes').value = '';
    $('#sgNewTeneurAuto').checked = true;
    const bouton = $('#sgTeneurSubmit');
    if (bouton) bouton.textContent = 'Enregistrer';
  }

  async function ajouterTeneur(e) {
    e.preventDefault();
    if (!sourceSelectionnee) return;
    const form = $('#sgTeneurForm');
    const idEdit = form ? (form.dataset.edit || '') : '';
    const nom = ($('#sgNewTeneur').value || '').trim();
    let valeurMin = parseFloat(String($('#sgNewTeneurMin').value || '').replace(',', '.')) || 0;
    let valeurMax = parseFloat(String($('#sgNewTeneurMax').value || '').replace(',', '.')) || 0;
    const codes = ($('#sgNewTeneurCodes').value || '').trim();
    // couleur propre a la famille ; vide = couleur de son grade (voir js/couleurs.js)
    const couleur = $('#sgNewTeneurAuto').checked ? '' : ($('#sgNewTeneurCouleur').value || '');
    if (!nom) return;
    if (valeurMin > valeurMax) { const t = valeurMin; valeurMin = valeurMax; valeurMax = t; }
    try {
      if (idEdit) {
        await jsend('/api/teneur_maj', 'POST', {
          id: Number(idEdit), nom: nom, valeur_min: valeurMin,
          valeur_max: valeurMax, codes: codes, couleur: couleur,
        });
      } else {
        await jsend('/api/teneurs', 'POST', {
          source_id: sourceSelectionnee, nom: nom, valeur_min: valeurMin,
          valeur_max: valeurMax, codes: codes, couleur: couleur,
        });
      }
      if (window.OTCouleurs) await OTCouleurs.recharger();
      fermerFormTeneur();
      await chargerTeneurs();
      setStatus('#sgDestStatus', idEdit ? 'Teneur modifiee' : 'Teneur ajoutee', true);
    } catch (err) {
      setStatus('#sgDestStatus', err.message, false);
    }
  }

  async function supprimerTeneur() {
    if (!teneurSelectionnee) return;
    const t = teneurs.find((x) => x.id === teneurSelectionnee);
    if (!await confirmerPopup('Supprimer la teneur "' + (t ? t.nom : '') + '" ?')) return;
    try {
      await jsend('/api/teneurs?id=' + teneurSelectionnee, 'DELETE');
      teneurSelectionnee = null;
      await chargerTeneurs();
      setStatus('#sgDestStatus', 'Teneur supprimee', true);
    } catch (err) {
      setStatus('#sgDestStatus', err.message, false);
    }
  }

  /* ============ DUMPING INSTRUCTION : affectation teneur -> DA ============= */

  async function ouvrirAssignation(teneurId) {
    const t = teneurs.find((x) => x.id === teneurId);
    if (!t) return;
    const overlay = $('#sgAssignOverlay');
    const msg = $('#sgAssignMessage');
    const sel = $('#sgAssignDaSelect');
    if (!overlay || !msg || !sel) return;
    msg.textContent = 'Teneur "' + t.nom + '" : choisir la Dumping Area de destination.';
    try {
      const d = await jsend('/api/dumping_areas', 'GET');
      const zones = d.dumping_areas || [];
      // Valeur 0 = retirer la consigne. Sans elle, une teneur affectee par
      // erreur ne pouvait plus revenir a « non affectee ».
      sel.innerHTML =
        '<option value="0"' + (t.dumping_area_id ? '' : ' selected') +
        '>— Aucune (retirer la consigne) —</option>' +
        zones.map((z) =>
          '<option value="' + z.id + '" ' + (z.id === t.dumping_area_id ? 'selected' : '') + '>' +
          esc(z.nom) + '</option>').join('');
    } catch (e) {
      sel.innerHTML = '';
    }
    overlay.dataset.teneurId = teneurId;
    overlay.hidden = false;
  }

  function fermerAssignation() {
    const overlay = $('#sgAssignOverlay');
    if (overlay) { overlay.hidden = true; overlay.dataset.teneurId = ''; }
  }

  async function confirmerAssignation() {
    const overlay = $('#sgAssignOverlay');
    const sel = $('#sgAssignDaSelect');
    const teneurId = overlay ? Number(overlay.dataset.teneurId) : 0;
    // daId = 0 : l'utilisateur a choisi « Aucune » -> retrait de la consigne.
    const daId = sel ? Number(sel.value) : 0;
    if (!teneurId) { fermerAssignation(); return; }
    const t = teneurs.find((x) => x.id === teneurId);
    const da = dumpingAreas.find((x) => x.id === daId);
    const nomTeneur = t ? t.nom : '';
    const nomDa = da ? da.nom : '';
    fermerAssignation();
    const dejaAffectee = t && t.dumping_area_id;
    const retrait = !daId;

    // Rien a faire si la teneur n'avait deja aucune consigne.
    if (retrait && !dejaAffectee) {
      setStatus('#sgDiStatus', 'Cette teneur n\'a deja aucune Dumping Area', true);
      return;
    }

    let message, titre, libelleOk, danger;
    if (retrait) {
      message = 'Retirer la consigne de la teneur "' + nomTeneur +
        '" ? Les DT ne recevront plus de destination pour cette teneur ' +
        'tant qu\'une nouvelle Dumping Area ne sera pas affectee.';
      titre = 'Retirer la consigne';
      libelleOk = 'Retirer';
      danger = true;   // on retire une instruction operationnelle
    } else {
      message = (dejaAffectee
        ? 'Changer la consigne de la teneur "' + nomTeneur + '" vers la Dumping Area "' + nomDa + '" ?'
        : 'Affecter la teneur "' + nomTeneur + '" a la Dumping Area "' + nomDa + '" ?') +
        ' Cette consigne sera poussee vers le serveur en ligne pour instruire les DT.';
      titre = dejaAffectee ? 'Modifier la consigne' : 'Confirmer l\'affectation';
      libelleOk = dejaAffectee ? 'Modifier' : 'Affecter';
      danger = false;
    }
    if (!await confirmerPopup(message, {
      titre: titre, libelleOk: libelleOk, danger: danger,
    })) return;
    try {
      await jsend('/api/teneur_dumping_area', 'POST', { teneur_id: teneurId, dumping_area_id: daId });
      await chargerTeneurs(); // reaffiche le resume persistant (X teneurs sans DA)
    } catch (err) {
      setStatus('#sgDiStatus', err.message, false);
    }
  }

  // Resume diagramme (toutes sources confondues) : Source -> Teneur -> Dumping
  // Area, pour voir d'un coup d'oeil l'ensemble des consignes deja donnees.
  async function chargerResumeDI() {
    const box = $('#sgDiSummary');
    const compteur = $('#sgDiSummaryCount');
    if (!box) return;
    try {
      const d = await jsend('/api/teneurs', 'GET');
      const tous = d.teneurs || [];
      toutesTeneurs = tous;
      rendreListeSources();
      rendreListeAires();
      if (!tous.length) {
        box.innerHTML = '<div class="sg2-di-empty">Aucune teneur enregistree</div>';
        if (compteur) compteur.textContent = '';
        return;
      }
      const parSource = {};
      tous.forEach((t) => { (parSource[t.source_id] = parSource[t.source_id] || []).push(t); });
      const nomSource = (id) => { const s = sources.find((x) => x.id === id); return s ? s.nom : 'Source #' + id; };
      const nomDa = (id) => { const z = dumpingAreas.find((x) => x.id === id); return z ? z.nom : null; };
      box.innerHTML = Object.keys(parSource).map((sid) => {
        const lignes = parSource[sid].map((t) => {
          const da = nomDa(t.dumping_area_id);
          return '<div class="sg2-di-row' + (da ? '' : ' sg2-di-missing') + '">' +
            '<span class="sg2-di-teneur">' + esc(t.nom) + '</span>' +
            '<span class="sg2-di-arrow">&#8594;</span>' +
            '<span class="sg2-di-da">' + (da ? esc(da) : 'non affectee') + '</span>' +
            '</div>';
        }).join('');
        return '<div class="sg2-di-source-group"><div class="sg2-di-source-name">' +
          esc(nomSource(Number(sid))) + '</div>' + lignes + '</div>';
      }).join('');
      const affectees = tous.filter((t) => t.dumping_area_id).length;
      if (compteur) compteur.textContent = affectees + '/' + tous.length + ' affectees';
    } catch (e) {
      box.innerHTML = '<div class="sg2-di-empty">Resume injoignable</div>';
    }
  }

  /* ==================== CARTE 2 : DUMPING AREA (zones) ======================= */

  const COULEURS = ['blue', 'green', 'yellow', 'orange'];

  let dumpingAreas = [];
  let daSelectionnee = null;

  async function chargerZones() {
    const row = $('#sgDaRow');
    if (!row) return;
    try {
      const d = await jsend('/api/dumping_areas', 'GET');
      dumpingAreas = d.dumping_areas || [];
      if (daSelectionnee && !dumpingAreas.some((z) => z.id === daSelectionnee)) daSelectionnee = null;
      rendreListeAires();
      rendreDetailDA();
      // Les pastilles de Dumping Instruction affichent le nom de la DA ; elles
      // ont pu etre dessinees avant que cette liste arrive (chargements
      // paralleles au demarrage). On les redessine une fois les noms connus.
      if (teneurs.length) rendreChipsInstruction();
      rendreEntete();
      await chargerResumeDI();
    } catch (e) {
      setStatus('#sgDaStatus', 'Serveur SG injoignable (' + e.message + ')', false);
    }
  }

  function rendreDetailDA() {
    const det = $('#sgDaDetail');
    if (!det) return;
    const z = dumpingAreas.find((x) => x.id === daSelectionnee);
    if (!z) { det.classList.remove('show'); det.innerHTML = ''; return; }
    det.classList.add('show');
    let sommets = [];
    try { sommets = JSON.parse(z.sommets || '[]'); } catch (e) {}
    const aire = Math.round((Number(z.rayon_m) || 0) * (Number(z.rayon_m) || 0));
    det.innerHTML = '<span><b>' + esc(z.nom) + '</b> - ' + sommets.length + ' sommets, ~' + aire +
      ' m2, centre x=' + esc(coord(z.x)) + ' y=' + esc(coord(z.y)) + ' (' + esc(z.couleur || 'blue') + ')</span>' +
      '<button type="button" class="sg2-x" id="sgDaEdit" title="Modifier"><i class="fas fa-pen"></i></button>';
  }

  function ouvrirFormDA(z) {
    const form = $('#sgDaForm');
    if (!form) return;
    form.dataset.id = z ? z.id : '';
    $('#sgDaNom').value = z ? z.nom : '';
    $('#sgDaX').value = z ? coord(z.x) : '';
    $('#sgDaY').value = z ? coord(z.y) : '';
    $('#sgDaColor').value = z ? (z.couleur || 'blue') : 'blue';
    $('#sgDaDelete').style.display = z ? '' : 'none';
    form.style.display = '';
    $('#sgDaNom').focus();
  }

  function fermerFormDA() {
    const form = $('#sgDaForm');
    if (form) { form.style.display = 'none'; form.dataset.id = ''; }
  }

  async function enregistrerDA(e) {
    e.preventDefault();
    const form = $('#sgDaForm');
    const id = form.dataset.id;
    const payload = {
      nom: ($('#sgDaNom').value || 'Dumping Area').trim(),
      x: parseFloat(String($('#sgDaX').value).replace(',', '.')) || 0,
      y: parseFloat(String($('#sgDaY').value).replace(',', '.')) || 0,
      couleur: $('#sgDaColor').value,
    };
    try {
      if (id) {
        await jsend('/api/dumping_area_maj', 'POST', Object.assign({ id: Number(id) }, payload));
        setStatus('#sgDaStatus', 'Dumping area modifiee', true);
      } else {
        await jsend('/api/dumping_areas', 'POST', payload);
        setStatus('#sgDaStatus', 'Dumping area ajoutee', true);
      }
      fermerFormDA();
      await chargerZones();
    } catch (err) {
      setStatus('#sgDaStatus', err.message, false);
    }
  }

  async function supprimerDA() {
    const form = $('#sgDaForm');
    const id = form.dataset.id;
    if (!id) return;
    const nom = ($('#sgDaNom').value || '').trim();
    if (!await confirmerPopup('Supprimer la dumping area "' + nom + '" ?')) return;
    try {
      await jsend('/api/dumping_areas?id=' + id, 'DELETE');
      daSelectionnee = null;
      fermerFormDA();
      await chargerZones();
      setStatus('#sgDaStatus', 'Dumping area supprimee', true);
    } catch (err) {
      setStatus('#sgDaStatus', err.message, false);
    }
  }

  // Une Dumping Area est une AIRE (m2), pas un point : le CSV regroupe les
  // sommets par nom ("nom,x,y" repete pour chaque sommet du meme polygone,
  // au moins 3 lignes par zone), meme convention que le CSV de blasts.
  // Le regroupement et la validation se font cote serveur (meme parseur
  // que les oreblocs) pour rester coherents avec le geofencing d'arrivee.
  function importZonesCSV(file) {
    const reader = new FileReader();
    reader.onload = async (e) => {
      try {
        const contenu = String(e.target.result).replace(/^/, '');
        const couleurChamp = $('#sgDaColor');
        const couleur = couleurChamp ? couleurChamp.value : 'blue';
        // Nom de la zone, par ordre de priorite :
        //   1. une colonne nom dans le CSV (seule facon d'avoir plusieurs
        //      zones dans un meme fichier) -- geree cote serveur ;
        //   2. le champ "Nom" du formulaire : la saisie explicite ;
        //   3. le nom du fichier ("RomPad 111.csv" -> "RomPad 111").
        // Sans ce nom, tout CSV sans colonne nom retombait sur un meme
        // "Dumping Area 1" et seul le premier fichier passait.
        const champNom = $('#sgDaNom');
        const nomSaisi = champNom ? String(champNom.value || '').trim() : '';
        const nomFichier = String(file.name || '').replace(/\.[^.]+$/, '').trim();
        const d = await jsend('/api/dumping_areas_import', 'POST', {
          contenu: contenu, couleur: couleur, nom: nomSaisi || nomFichier,
        });
        fermerFormDA();
        await chargerZones();
        const noms = (d.dumping_areas || []).map(function (z) { return z.nom; });
        let msg = d.nb
          ? d.nb + ' dumping area' + (d.nb > 1 ? 's' : '') + ' importee' +
            (d.nb > 1 ? 's' : '') + ' : ' + noms.join(', ')
          : 'aucune zone importee';
        if (d.doublons && d.doublons.length) {
          msg += ' - deja existante(s), ignoree(s) : ' + d.doublons.join(', ');
        }
        setStatus('#sgDaStatus', msg, d.nb > 0);
      } catch (err) {
        setStatus('#sgDaStatus', 'Erreur CSV : ' + err.message, false);
      }
    };
    reader.readAsText(file);
  }

  /* ======================= CARTE 3 : TRAILS (GeoTiff) ==================== */

  function renderRoads(rows) {
    nbPistes = (rows || []).length;
    rendreEntete();
    const b = $('#sgRoadsList');
    if (!b) return;
    if (!rows || !rows.length) { b.innerHTML = '<div class="sg-vide">Aucune piste importée.</div>'; return; }
    b.innerHTML = rows.map((r) => {
      const meta = (r.type === 'geotiff' ? 'GeoTiff' : 'GeoJSON') + ' · ' + esc((r.created_at || '').slice(0, 16));
      return '<div class="sg-ligne">' +
        '<i class="fas fa-route sg-ligne-ico"></i>' +
        '<span class="sg-ligne-nom"><b>' + esc(r.nom) + '</b></span>' +
        '<span class="sg-ligne-meta">' + meta + '</span>' +
        '<button type="button" class="sg-ico-btn danger" data-road-del="' + r.id + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
      '</div>';
    }).join('');
  }

  async function chargerRoads() {
    try {
      const d = await jsend('/api/travels_roads', 'GET');
      renderRoads(d.travels_roads || []);
    } catch (e) { /* silencieux, recharge au prochain cycle */ }
  }

  function appelerGeotiff(file) {
    const okType = /\.(tif|tiff|json|geojson)$/i.test(file.name);
    if (!okType) {
      setStatus('#sgRoadStatus', 'Format attendu : .tif / .tiff (GeoTiff) ou .geojson', false);
      return;
    }
    const btn = $('#sgBtnRoadUpload');
    if (btn) btn.disabled = true;
    const reader = new FileReader();
    reader.onload = async (e) => {
      try {
        const buf = new Uint8Array(e.target.result);
        if (/\.(json|geojson)$/i.test(file.name)) {
          const texte = String.fromCharCode.apply(null, buf);
          const geojson = JSON.parse(texte);
          await jsend('/api/travels_roads', 'POST', { nom: file.name.replace(/\.(json|geojson)$/i, ''), geojson: geojson });
          setStatus('#sgRoadStatus', 'Chemins importes (' + file.name + ')', true);
        } else {
          const r = await fetch(base() + '/api/travels_roads_upload?nom=' +
            encodeURIComponent(file.name), { method: 'POST', body: buf });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
          setStatus('#sgRoadStatus', 'GeoTiff enregistre (' + j.taille + ' octets) - extraction des polylines a venir', true);
        }
        await chargerRoads();
      } catch (err) {
        setStatus('#sgRoadStatus', 'Echec : ' + (err.message || 'erreur'), false);
      } finally {
        if (btn) btn.disabled = false;
      }
    };
    reader.readAsArrayBuffer(file);
  }

  async function supprimerRoad(id) {
    if (!await confirmerPopup('Supprimer ce chemin ?')) return;
    try {
      await jsend('/api/travels_roads?id=' + encodeURIComponent(id), 'DELETE');
      setStatus('#sgRoadStatus', 'Chemin supprime (#' + id + ')', true);
      await chargerRoads();
    } catch (e) {
      setStatus('#sgRoadStatus', 'Suppression impossible : ' + (e.message || 'erreur'), false);
    }
  }

  /* ======================= CARTE : CHECKPOINTS ========================== */
  // Une ligne de passage = deux extremites. Le CSV (Y, X, id, Nom ; deux
  // lignes par Nom) est lu et converti en UTM par la passerelle
  // (/api/checkpoints_import), meme regle que les dumping areas.

  function renderCheckpoints(rows) {
    const b = $('#sgChkList');
    if (!b) return;
    const cpt = $('#sgChkCompte');
    if (cpt) cpt.textContent = rows && rows.length ? '· ' + deuxN(rows.length) : '';
    if (!rows || !rows.length) {
      b.innerHTML = '<div class="sg-vide">Aucun checkpoint.</div>';
      return;
    }
    b.innerHTML = rows.map((r) =>
      '<div class="sg-ligne">' +
        '<i class="fas fa-flag-checkered sg-ligne-ico"></i>' +
        '<span class="sg-ligne-nom"><b title="' + esc(r.nom) + '">' + esc(r.nom) + '</b></span>' +
        '<span class="sg-ligne-meta" title="(' + coord(r.x1) + ', ' + coord(r.y1) + ') - (' +
          coord(r.x2) + ', ' + coord(r.y2) + ')">' + esc(String(r.longueur_m)) + ' m</span>' +
        '<button type="button" class="sg-ico-btn danger" data-chk-del="' + r.id + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
      '</div>').join('');
  }

  async function chargerCheckpoints() {
    try {
      const d = await jsend('/api/checkpoints', 'GET');
      renderCheckpoints(d.checkpoints || []);
      chargerTrajectoires();
    } catch (e) {
      const b = $('#sgChkList');
      if (b) b.innerHTML = '<div class="sg-vide">Passerelle injoignable.</div>';
    }
  }

  // Le detail (rejets) reste affiche : setStatus s'efface en 4 s, trop court
  // pour lire la liste des checkpoints refuses.
  function detailCheckpoints(texte, ok) {
    const el = $('#sgChkDetail');
    if (!el) return;
    el.textContent = texte || '';
    el.className = 'sg2-detail' + (texte ? (ok ? ' ok' : ' err') : '');
  }

  function importCheckpointsCSV(file) {
    const reader = new FileReader();
    reader.onload = async (e) => {
      detailCheckpoints('', true);
      try {
        const r = await fetch(base() + '/api/checkpoints_import', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ contenu: String(e.target.result) }),
        });
        const d = await r.json().catch(() => ({}));
        const rejets = (d.rejets || []).map((x) => x.nom + ' (' + x.motif + ')');
        if (!r.ok) {
          setStatus('#sgChkStatus', 'Import refuse : ' + (d.error || ('HTTP ' + r.status)), false);
          if (rejets.length) detailCheckpoints('Refuses : ' + rejets.join(', '), false);
          return;
        }
        const n = (d.crees || []).length, m = (d.mis_a_jour || []).length;
        setStatus('#sgChkStatus', n + ' checkpoint(s) ajoute(s), ' + m + ' mis a jour' +
          (d.convertis_utm ? ' (degres convertis en UTM)' : ''), true);
        detailCheckpoints(rejets.length ? 'Refuses : ' + rejets.join(', ') : '', !rejets.length);
        await chargerCheckpoints();
      } catch (err) {
        setStatus('#sgChkStatus', 'Erreur CSV : ' + err.message, false);
      }
    };
    reader.readAsText(file);
  }

  async function supprimerCheckpoint(id) {
    if (!await confirmerPopup('Supprimer ce checkpoint ?')) return;
    try {
      await jsend('/api/checkpoints?id=' + encodeURIComponent(id), 'DELETE');
      setStatus('#sgChkStatus', 'Checkpoint supprime', true);
      await chargerCheckpoints();
    } catch (e) {
      setStatus('#sgChkStatus', 'Suppression impossible : ' + (e.message || 'erreur'), false);
    }
  }

  async function supprimerTousCheckpoints() {
    if (!await confirmerPopup('Supprimer TOUS les checkpoints ?')) return;
    try {
      const d = await jsend('/api/checkpoints?tous=1', 'DELETE');
      setStatus('#sgChkStatus', d.supprimes + ' checkpoint(s) supprime(s)', true);
      detailCheckpoints('', true);
      await chargerCheckpoints();
    } catch (e) {
      setStatus('#sgChkStatus', 'Suppression impossible : ' + (e.message || 'erreur'), false);
    }
  }

  /* =================== CARTE 4 : GEOGRAPHIC SYSTEM ==================== */

  let geoActuel = null; // { systeme, zone, hemisphere, datum }

  function rendreDetailGeo() {
    const det = $('#sgGeoDetail');
    if (!det) return;
    if (!geoActuel) { det.classList.remove('show'); det.innerHTML = ''; return; }
    let libelle;
    if (geoActuel.systeme === 'UTM') {
      libelle = 'UTM fuseau ' + esc(geoActuel.zone) + esc(geoActuel.hemisphere || 'N') +
        ' · ' + esc(geoActuel.datum || 'WGS 84');
    } else {
      libelle = 'WGS84 (latitude / longitude)';
    }
    det.classList.add('show');
    det.innerHTML = '<span><b>' + libelle + '</b></span>';
    const pill = $('#sgGeoPill');
    if (pill) pill.textContent = geoActuel.systeme === 'UTM'
      ? 'WGS84 · UTM ' + geoActuel.zone + (geoActuel.hemisphere || 'N') : 'WGS84';
  }

  async function chargerGeo() {
    try {
      const d = await jsend('/api/geographic_system', 'GET');
      geoActuel = d;
      rendreDetailGeo();
    } catch (e) {
      setStatus('#sgGeoStatus', 'Serveur SG injoignable (' + e.message + ')', false);
    }
  }

  function majVisibiliteUtm() {
    const choisi = document.querySelector('input[name="sgGeoSys"]:checked');
    const utmRow = $('#sgGeoUtmRow');
    if (utmRow) utmRow.style.display = (choisi && choisi.value === 'UTM') ? '' : 'none';
  }

  function ouvrirFormGeo() {
    const overlay = $('#sgGeoOverlay');
    if (!overlay) return;
    const systeme = (geoActuel && geoActuel.systeme) || 'WGS84';
    const radio = document.querySelector('input[name="sgGeoSys"][value="' + systeme + '"]');
    if (radio) radio.checked = true;
    $('#sgGeoZone').value = geoActuel && geoActuel.zone ? geoActuel.zone : '';
    $('#sgGeoHemi').value = geoActuel && geoActuel.hemisphere ? geoActuel.hemisphere : 'N';
    $('#sgGeoDatum').value = geoActuel && geoActuel.datum ? geoActuel.datum : 'WGS 84';
    setStatus('#sgGeoFormStatus', '');
    majVisibiliteUtm();
    overlay.hidden = false;
  }

  function fermerFormGeo() {
    const overlay = $('#sgGeoOverlay');
    if (overlay) overlay.hidden = true;
  }

  async function enregistrerGeo(e) {
    e.preventDefault();
    const choisi = document.querySelector('input[name="sgGeoSys"]:checked');
    if (!choisi) { setStatus('#sgGeoFormStatus', 'Choisis WGS84 ou UTM', false); return; }
    const payload = { systeme: choisi.value };
    if (choisi.value === 'UTM') {
      payload.zone = parseInt(String($('#sgGeoZone').value).trim(), 10);
      if (!payload.zone || payload.zone < 1 || payload.zone > 60) {
        setStatus('#sgGeoFormStatus', 'Fuseau UTM valide requis (1 a 60)', false);
        return;
      }
      payload.hemisphere = $('#sgGeoHemi').value;
      payload.datum = $('#sgGeoDatum').value;
    }
    try {
      geoActuel = await jsend('/api/geographic_system', 'POST', payload);
      rendreDetailGeo();
      fermerFormGeo();
      setStatus('#sgGeoStatus', 'Systeme enregistre', true);
    } catch (err) {
      setStatus('#sgGeoFormStatus', err.message, false);
    }
  }

  /* ============================ init ===================================== */

  function init() {
    if (!$('#sgSourceLabel')) return; // pas sur cette page

    document.addEventListener('click', async (e) => {
      // trajectoires : edition de l'ordre des checkpoints
      const tEd = e.target.closest('[data-traj-edit]');
      if (tEd) {
        const t = trajectoires.find((x) => x.id === Number(tEd.dataset.trajEdit));
        trajEdition = t ? { id: t.id, ids: t.checkpoints.map((c) => c.id) } : null;
        rendreTrajectoires();
        return;
      }
      if (trajEdition) {
        const haut = e.target.closest('[data-traj-haut]');
        const bas = e.target.closest('[data-traj-bas]');
        const ret = e.target.closest('[data-traj-retirer]');
        const ids = trajEdition.ids;
        if (haut) { const i = Number(haut.dataset.trajHaut); [ids[i - 1], ids[i]] = [ids[i], ids[i - 1]]; rendreTrajectoires(); return; }
        if (bas) { const i = Number(bas.dataset.trajBas); [ids[i + 1], ids[i]] = [ids[i], ids[i + 1]]; rendreTrajectoires(); return; }
        if (ret) { ids.splice(Number(ret.dataset.trajRetirer), 1); rendreTrajectoires(); return; }
        if (e.target.closest('[data-traj-ajouter]')) {
          const v = Number(($('#sgTrajAjoutSel') || {}).value);
          if (v) { ids.push(v); rendreTrajectoires(); }
          return;
        }
        const enr = e.target.closest('[data-traj-enregistrer]');
        if (enr) { enregistrerTrajectoire(Number(enr.dataset.trajEnregistrer)); return; }
        if (e.target.closest('[data-traj-annuler]')) { trajEdition = null; rendreTrajectoires(); return; }
      }
      const tSup = e.target.closest('[data-traj-del]');
      if (tSup) {
        const t = trajectoires.find((x) => x.id === Number(tSup.dataset.trajDel));
        if (t && await confirmerPopup('Supprimer la trajectoire ' + t.source + ' → ' + t.dumping_area + ' ?')) {
          try {
            await jsend('/api/trajectoires?id=' + t.id, 'DELETE');
            if (trajEdition && trajEdition.id === t.id) trajEdition = null;
            await chargerTrajectoires();
            await synchroniserSG();
          } catch (err) { setStatus('#sgTrajStatus', err.message, false); }
        }
        return;
      }
      // source : activer
      const srcSel = e.target.closest('[data-src-select]');
      if (srcSel) {
        sourceSelectionnee = Number(srcSel.dataset.srcSelect);
        teneurSelectionnee = null;
        rendreSourceLabel();
        chargerTeneurs();
        return;
      }
      // famille : modifier / supprimer depuis la ligne
      const tEdit = e.target.closest('[data-teneur-edit]');
      if (tEdit) { ouvrirFormTeneur(teneurs.find((x) => x.id === Number(tEdit.dataset.teneurEdit)) || null); return; }
      const tDel = e.target.closest('[data-teneur-del]');
      if (tDel) { teneurSelectionnee = Number(tDel.dataset.teneurDel); supprimerTeneur(); return; }
      // aire : modifier depuis la ligne
      const daEdit = e.target.closest('[data-da-edit]');
      if (daEdit) {
        const z = dumpingAreas.find((x) => x.id === Number(daEdit.dataset.daEdit));
        if (z) { daSelectionnee = z.id; ouvrirFormDA(z); }
        return;
      }
      // teneur : selection
      const chipT = e.target.closest('[data-teneur]');
      if (chipT) {
        const id = Number(chipT.dataset.teneur);
        teneurSelectionnee = teneurSelectionnee === id ? null : id;
        chargerTeneurs();
        return;
      }
      // teneur : ouvrir le formulaire d'ajout
      if (e.target.closest('#sgBtnTeneurAdd')) {
        const ouvert = $('#sgTeneurForm').style.display !== 'none';
        if (ouvert) fermerFormTeneur(); else ouvrirFormTeneur(null);
        return;
      }
      // teneur : modification depuis le detail (bornes et codes de bloc)
      if (e.target.closest('#sgTeneurEdit')) {
        ouvrirFormTeneur(teneurs.find((x) => x.id === teneurSelectionnee) || null);
        return;
      }
      // teneur : suppression depuis le detail
      if (e.target.closest('#sgTeneurDel')) { supprimerTeneur(); return; }

      // Dumping Instruction : teneur cliquee -> popup d'affectation de la DA
      const chipAssign = e.target.closest('[data-teneur-assign]');
      if (chipAssign) { ouvrirAssignation(Number(chipAssign.dataset.teneurAssign)); return; }

      // dumping area : selection -> ouvre le formulaire en edition
      const chipDa = e.target.closest('[data-da]');
      if (chipDa) {
        const id = Number(chipDa.dataset.da);
        daSelectionnee = daSelectionnee === id ? null : id;
        rendreDetailDA();
        return;
      }
      // dumping area : "+" -> formulaire vide (ajout)
      if (e.target.closest('#sgBtnDaAdd')) { ouvrirFormDA(null); return; }
      // dumping area : "e" (modifier) depuis le detail
      if (e.target.closest('#sgDaEdit')) {
        const z = dumpingAreas.find((x) => x.id === daSelectionnee);
        if (z) ouvrirFormDA(z);
        return;
      }

      // chemin (road) : suppression
      const roadDel = e.target.closest('[data-road-del]');
      if (roadDel) { supprimerRoad(roadDel.dataset.roadDel); return; }
    });

    // source
    const srcPrev = $('#sgSrcPrev');
    if (srcPrev) srcPrev.addEventListener('click', () => naviguerSource(-1));
    const srcNext = $('#sgSrcNext');
    if (srcNext) srcNext.addEventListener('click', () => naviguerSource(1));
    const diPrev = $('#sgDiSrcPrev');
    if (diPrev) diPrev.addEventListener('click', () => naviguerSource(-1));
    const diNext = $('#sgDiSrcNext');
    if (diNext) diNext.addEventListener('click', () => naviguerSource(1));
    const btnSrcAdd = $('#sgBtnSrcAdd');
    if (btnSrcAdd) btnSrcAdd.addEventListener('click', () => {
      toggle($('#sgSrcAddForm'));
      if ($('#sgSrcAddForm').style.display !== 'none') $('#sgNewSource').focus();
    });
    const formSrc = $('#sgSrcAddForm');
    if (formSrc) formSrc.addEventListener('submit', ajouterSource);
    const cancelSrc = $('#sgSrcAddCancel');
    if (cancelSrc) cancelSrc.addEventListener('click', () => { $('#sgSrcAddForm').style.display = 'none'; });
    const delSrc = $('#sgSrcDel');
    if (delSrc) delSrc.addEventListener('click', supprimerSource);

    // teneur
    const formTeneur = $('#sgTeneurForm');
    if (formTeneur) formTeneur.addEventListener('submit', ajouterTeneur);
    const cancelTeneur = $('#sgTeneurCancel');
    // Annuler doit aussi vider le formulaire : sinon une modification
    // abandonnee laisse l'id d'edition en place et le prochain ajout
    // ecraserait la teneur precedemment ouverte.
    if (cancelTeneur) cancelTeneur.addEventListener('click', fermerFormTeneur);

    // Dumping Instruction : popup d'affectation
    const assignCancel = $('#sgAssignCancel');
    if (assignCancel) assignCancel.addEventListener('click', fermerAssignation);
    const assignOk = $('#sgAssignOk');
    if (assignOk) assignOk.addEventListener('click', confirmerAssignation);
    const assignOverlay = $('#sgAssignOverlay');
    if (assignOverlay) assignOverlay.addEventListener('click', (e) => {
      if (e.target === assignOverlay) fermerAssignation();
    });

    // dumping area
    const formDa = $('#sgDaForm');
    if (formDa) formDa.addEventListener('submit', enregistrerDA);
    const delDa = $('#sgDaDelete');
    if (delDa) delDa.addEventListener('click', supprimerDA);
    const cancelDa = $('#sgDaCancel');
    if (cancelDa) cancelDa.addEventListener('click', fermerFormDA);
    const btnImp = $('#btnSgImportZones');
    const fileInput = $('#sgZoneFileInput');
    if (btnImp && fileInput) {
      btnImp.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', () => {
        if (fileInput.files[0]) importZonesCSV(fileInput.files[0]);
        fileInput.value = '';
      });
    }

    // checkpoints
    const btnChk = $('#btnSgImportChk');
    const chkInput = $('#sgChkFileInput');
    if (btnChk && chkInput) {
      btnChk.addEventListener('click', () => chkInput.click());
      chkInput.addEventListener('change', () => {
        if (chkInput.files[0]) importCheckpointsCSV(chkInput.files[0]);
        chkInput.value = '';
      });
    }
    const chkTout = $('#btnSgChkToutSuppr');
    if (chkTout) chkTout.addEventListener('click', supprimerTousCheckpoints);
    const chkList = $('#sgChkList');
    if (chkList) chkList.addEventListener('click', (e) => {
      const b = e.target.closest('[data-chk-del]');
      if (b) supprimerCheckpoint(b.getAttribute('data-chk-del'));
    });

    // trails / geotiff
    const btnRoad = $('#sgBtnRoadUpload');
    const roadInput = $('#sgRoadInput');
    if (btnRoad && roadInput) {
      btnRoad.addEventListener('click', () => roadInput.click());
      roadInput.addEventListener('change', () => {
        if (roadInput.files[0]) appelerGeotiff(roadInput.files[0]);
        roadInput.value = '';
      });
    }

    // geographic system (WGS84 / UTM)
    const btnGeo = $('#sgBtnGeoSys');
    if (btnGeo) btnGeo.addEventListener('click', ouvrirFormGeo);
    const geoCancel = $('#sgGeoCancel');
    if (geoCancel) geoCancel.addEventListener('click', fermerFormGeo);
    const geoOverlay = $('#sgGeoOverlay');
    if (geoOverlay) geoOverlay.addEventListener('click', (e) => {
      if (e.target === geoOverlay) fermerFormGeo();
    });
    const geoForm = $('#sgGeoForm');
    if (geoForm) geoForm.addEventListener('submit', enregistrerGeo);
    document.querySelectorAll('input[name="sgGeoSys"]').forEach((r) => {
      r.addEventListener('change', majVisibiliteUtm);
    });

    // choisir une couleur = ne plus etre en « Auto » (sinon la couleur etait ignoree a l'enregistrement)
    const selCouleur = $('#sgNewTeneurCouleur');
    if (selCouleur) selCouleur.addEventListener('input', () => { $('#sgNewTeneurAuto').checked = false; });

    // trajectoires : formulaire d'ajout
    const btnTraj = $('#sgBtnTrajAdd');
    if (btnTraj) btnTraj.addEventListener('click', () => {
      remplirFormTrajectoire();
      const f = $('#sgTrajForm');
      f.style.display = f.style.display === 'none' ? '' : 'none';
    });
    const formTraj = $('#sgTrajForm');
    if (formTraj) formTraj.addEventListener('submit', creerTrajectoire);
    const annulTraj = $('#sgTrajCancel');
    if (annulTraj) annulTraj.addEventListener('click', () => { $('#sgTrajForm').style.display = 'none'; });

    // onglets
    const tabs = document.querySelectorAll('.sg-tab');
    const ouvrirOnglet = (nom) => {
      tabs.forEach((t) => t.classList.toggle('is-active', t.dataset.sgtab === nom));
      document.querySelectorAll('.sg-pane').forEach((p) => {
        const actif = p.dataset.sgpane === nom;
        p.hidden = !actif;
        p.classList.toggle('is-active', actif);
      });
    };
    tabs.forEach((t) => t.addEventListener('click', () => ouvrirOnglet(t.dataset.sgtab)));
    // « Nouvelle source » : onglet Sources & teneurs + ouverture du formulaire d'ajout
    const btnNouvelle = $('#sgBtnNouvelleSource');
    if (btnNouvelle) btnNouvelle.addEventListener('click', () => {
      ouvrirOnglet('sources');
      const form = $('#sgSrcAddForm');
      if (form && form.style.display === 'none') $('#sgBtnSrcAdd').click();
      else if ($('#sgNewSource')) $('#sgNewSource').focus();
    });
    // « Voir le resume » : resume des consignes de toutes les sources
    const btnResume = $('#sgVoirResume');
    if (btnResume) btnResume.addEventListener('click', () => {
      const r = $('#sgResume');
      if (r) r.hidden = !r.hidden;
    });

    chargerSources();
    chargerZones();
    chargerCheckpoints();
    chargerTrajectoires();
    chargerRoads();
    chargerGeo();
    demarrerSuivi();

    // codes de source admis (garde-fou blasts) : enregistrer via PUT
    const codesForm = $('#sgSrcCodesForm');
    if (codesForm) codesForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!sourceSelectionnee) return;
      const codes = ($('#sgSrcCodes') || {}).value || '';
      try {
        await jsend('/api/sources', 'PUT', { id: sourceSelectionnee, codes: codes });
        setStatus('#sgSrcCodesStatus', 'Codes enregistres', true);
        await chargerSources();
      } catch (err) {
        setStatus('#sgSrcCodesStatus', err.message, false);
      }
    });
    setInterval(() => {
      if (document.hidden) return;
      chargerZones();
      chargerRoads();
      chargerSources();
    }, 10000);
  }

  /* ---- Suivi des missions (donnees passerelle via missions-ui.js) ----
   * Lecture seule : cartes par DT en mission, chrono, dernieres alertes
   * et cumuls du jour. Chaque DT est colore par la couleur de son equipe
   * (champ couleur pose a la creation de l'equipe). */
  function demarrerSuivi() {
    if (!window.MSUI) return;
    MSUI.onTick(renderSuivi);
    MSUI.demarrer(5000);
    initMinageUI();
  }

  /* ---- Canal A : declaration du minage par le spotter ----
   * Le spotter sait quelles pelles chargent : deux listes (pelle / bloc)
   * et un bouton. La declaration est fraiche 30 min (timeout serveur). */
  /* ---- Minages en cours : TOUS, une ligne par pelle, avec la source de l'information ----
   * DT (automatique, la plus fiable) > spotter (secours, quand son app est dans le reseau) > bureau
   * (formulaire de secours). Seuls les minages declares a la main peuvent etre arretes ici. */
  const ORIGINE_LIBELLE = { dt: 'DT · auto', spotter: 'Spotter', bureau: 'Bureau' };

  async function chargerMinages() {
    const box = $('#sgMinagesListe');
    if (!box) return;
    try {
      const r = await fetch(base().replace(/\/$/, '') + '/api/minages', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const liste = (await r.json()).minages || [];
      box.innerHTML = liste.length ? liste.map((m) =>
        '<div class="sg-minage">' +
          '<span class="sg-minage-pelle">' + esc(m.exca_code) + '</span>' +
          '<span class="sg-minage-bloc">' + esc(m.bloc || ('#' + m.orebloc_id)) + (m.matiere === 'waste' ? ' <em class="sg-minage-waste">stérile</em>' : '') + '</span>' +
          '<span class="sg-minage-origine o-' + esc(m.origine) + '">' + esc(ORIGINE_LIBELLE[m.origine] || m.origine) + '</span>' +
          (m.origine === 'dt' ? '' :
            '<button type="button" class="sg-minage-stop" data-minage-stop="' + esc(m.exca_code) + '" title="Arrêter ce minage"><i class="fas fa-stop"></i></button>') +
        '</div>').join('') : '<div class="member-empty">Aucun minage en cours.</div>';
    } catch (e) {
      box.innerHTML = '<div class="member-empty">Minages indisponibles (' + esc(e.message) + ')</div>';
    }
  }

  async function arreterMinage(exca) {
    try {
      const r = await fetch(base().replace(/\/$/, '') + '/api/minage_declare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ exca_code: exca, stop: true }),
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      Splash.succes('Minage arrêté', exca);
    } catch (e) { Splash.erreur('Arrêt impossible', e.message); }
    chargerMinages();
  }

  async function initMinageUI() {
    chargerMinages();
    setInterval(() => { if (!document.hidden) chargerMinages(); }, 5000);
    const listeMin = $('#sgMinagesListe');
    if (listeMin) listeMin.addEventListener('click', (e) => {
      const b = e.target.closest('[data-minage-stop]');
      if (b) arreterMinage(b.dataset.minageStop);
    });
    const selExca = $('#sgMinageExca');
    const selBloc = $('#sgMinageBloc');
    if (!selExca || !selBloc) return;

    // pelles : engins EXCA de la passerelle
    try {
      const r = await fetch(base().replace(/\/$/, '') + '/api/engins');
      const d = await r.json();
      selExca.innerHTML = '<option value="">Choisir une pelle...</option>' +
        (d.engins || []).filter((e) => e.type === 'EXCA')
          .map((e) => '<option value="' + esc(e.code) + '">' + esc(e.code) + '</option>').join('');
    } catch (e) { selExca.innerHTML = '<option value="">Pelles indisponibles</option>'; }

    // blocs : oreblocs de la passerelle (tous blasts)
    try {
      const r = await fetch(base().replace(/\/$/, '') + '/api/oreblocs');
      const d = await r.json();
      selBloc.innerHTML = '<option value="">Choisir un bloc...</option>' +
        (d.oreblocs || []).filter((o) => o.actif)
          .map((o) => '<option value="' + o.id + '">' + esc(o.nom) + '</option>').join('');
    } catch (e) { selBloc.innerHTML = '<option value="">Blocs indisponibles</option>'; }

    const btn = $('#sgMinageValider');
    if (btn) btn.addEventListener('click', async () => {
      const exca = selExca.value;
      const bloc = selBloc.value;
      const etat = $('#sgMinageEtat');
      if (!exca || !bloc) {
        if (etat) { etat.textContent = 'Choisissez la pelle et le bloc'; etat.className = 'suivi-minage-etat ko'; }
        return;
      }
      try {
        const r = await fetch(base().replace(/\/$/, '') + '/api/minage_declare', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ exca_code: exca, orebloc_id: Number(bloc), origine: 'bureau' }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status);
        Splash.succes('Minage declare', exca + ' sur bloc #' + bloc);
        chargerMinages();
        if (etat) { etat.textContent = 'Minage declare : ' + exca + ' sur bloc #' + bloc; etat.className = 'suivi-minage-etat ok'; }
      } catch (err) {
        Splash.erreur('Declaration refusee', err.message);
        if (etat) { etat.textContent = 'Echec : ' + err.message; etat.className = 'suivi-minage-etat ko'; }
      }
    });
  }

  function renderSuivi(etat) {
    const fraicheur = $('#sgSuiviFraicheur');
    if (fraicheur) {
      fraicheur.textContent = etat.passerelleDown
        ? 'Passerelle injoignable' + (etat.dernierReussi
            ? ' — donnees de ' + etat.dernierReussi.toLocaleTimeString() : '')
        : 'En direct — passerelle ' + base().replace(/^https?:\/\//, '');
      fraicheur.classList.toggle('suivi-off', etat.passerelleDown);
    }

    const box = $('#sgSuiviMissions');
    if (box) {
      const enCours = etat.missions.filter((m) => m.statut === 'en_cours');
      if (!enCours.length) {
        box.innerHTML = '<div class="member-empty">Aucune mission en cours.</div>';
      } else {
        box.innerHTML = enCours.map((m) => {
          const devie = Number(m.devie) === 1;
          const cls = devie ? 'suivi-carte devie' : 'suivi-carte';
          return '<div class="' + cls + '">' +
            '<div class="suivi-dt"><i class="fas fa-truck"></i> ' + esc(m.dt_code) + '</div>' +
            '<div class="suivi-info">' +
              '<div>Bloc #' + esc(m.orebloc_id) + ' &rarr; DA #' + esc(m.dumping_area_id) + '</div>' +
              '<div class="suivi-chrono"><i class="fas fa-stopwatch"></i> ' + MSUI.duree(m.debut) +
              ' &middot; ' + Math.round(m.distance_parcourue || 0) + ' m</div>' +
            '</div>' +
            '<span class="' + (MSUI.STATUT_MISSION[m.statut] || {}).cls + '">' +
              (MSUI.STATUT_MISSION[m.statut] || {}).lbl + '</span>' +
          '</div>';
        }).join('');
      }
    }

    const enCoursN = etat.missions.filter((m) => m.statut === 'en_cours').length;
    const sm = $('#sgStatMissions');
    if (sm) sm.textContent = String(enCoursN).padStart(2, '0');
    const aujourdhuiT = new Date().toISOString().slice(0, 10);
    const tonnesJour = etat.voyages
      .filter((v) => String(v.created_at || '').slice(0, 10) === aujourdhuiT)
      .reduce((t, v) => t + (v.volume_t || 0), 0);
    const st = $('#sgStatTonnes');
    if (st) st.textContent = Math.round(tonnesJour).toLocaleString('fr-FR');
    const derniere = etat.alertes.find((a) => !MSUI.estInfo(a)) || etat.alertes[0];
    const boxD = $('#sgDerniereAlerte');
    if (boxD) {
      boxD.hidden = !derniere;
      if (derniere) $('#sgDerniereAlerteTxt').textContent = MSUI.heure(derniere.created_at) + ' · ' + String(derniere.message || '');
    }

    const boxA = $('#sgSuiviAlertes');
    if (boxA) {
      const recentes = etat.alertes.slice(0, 5);
      boxA.innerHTML = recentes.length
        ? recentes.map((a) => '<div class="suivi-alerte' + (a.acquittee ? ' vue' : '') + '">' +
            '<span class="suivi-h">' + MSUI.heure(a.created_at) + '</span>' +
            '<b>' + esc(a.dt_code) + '</b> ' + esc(a.message) + '</div>').join('')
        : '<div class="member-empty">Aucune alerte recue.</div>';
    }

    const boxC = $('#sgSuiviCumuls');
    if (boxC) {
      const aujourdhui = new Date().toISOString().slice(0, 10);
      const voy = etat.voyages.filter((v) => String(v.created_at || '').slice(0, 10) === aujourdhui);
      const tonnes = voy.reduce((s, v) => s + (v.volume_t || 0), 0);
      const nonConf = voy.filter((v) => v.conformite === 'non_conforme').length;
      boxC.innerHTML = '<span class="suivi-kpi"><b>' + voy.length + '</b> voyages</span>' +
        '<span class="suivi-kpi"><b>' + Math.round(tonnes) + ' t</b></span>' +
        '<span class="suivi-kpi' + (nonConf ? ' ko' : '') + '"><b>' + nonConf + '</b> non conformes</span>';
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
