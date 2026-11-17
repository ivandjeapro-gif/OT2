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
  function rendreChipsInstruction() {
    const rowDi = $('#sgDiTeneursRow');
    if (!rowDi) return;
    rowDi.innerHTML = teneurs.map((t) => {
      const da = dumpingAreas.find((z) => z.id === t.dumping_area_id);
      const libelle = da ? esc(t.nom) + ' → ' + esc(da.nom) : esc(t.nom);
      const infobulle = t.dumping_area_id
        ? (da ? 'Consigne : ' + da.nom + ' - cliquer pour modifier'
              : 'Dumping Area affectee - cliquer pour modifier')
        : 'Aucune Dumping Area - cliquer pour affecter';
      return '<button type="button" class="sg2-outline sg2-chip' +
        (t.dumping_area_id ? '' : ' sg2-warn') +
        '" data-teneur-assign="' + t.id + '" title="' + esc(infobulle) + '">' +
        libelle + '</button>';
    }).join('');
    const statusDi = $('#sgDiStatus');
    if (statusDi) {
      const manquantes = teneurs.filter((t) => !t.dumping_area_id).length;
      statusDi.textContent = manquantes
        ? manquantes + ' teneur' + (manquantes > 1 ? 's' : '') + ' sans Dumping Area' : '';
      statusDi.className = 'sg2-status account-status' + (manquantes ? ' err' : '');
    }
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
        row.innerHTML = teneurs.map((t) =>
          '<button type="button" class="sg2-outline sg2-chip' + (t.id === teneurSelectionnee ? ' sel' : '') +
          '" data-teneur="' + t.id + '">' + esc(t.nom) + '</button>').join('') +
          '<button type="button" class="sg2-outline" id="sgBtnTeneurAdd" title="Ajouter une teneur">+</button>';
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
    if (!nom) return;
    if (valeurMin > valeurMax) { const t = valeurMin; valeurMin = valeurMax; valeurMax = t; }
    try {
      if (idEdit) {
        await jsend('/api/teneur_maj', 'POST', {
          id: Number(idEdit), nom: nom, valeur_min: valeurMin,
          valeur_max: valeurMax, codes: codes,
        });
      } else {
        await jsend('/api/teneurs', 'POST', {
          source_id: sourceSelectionnee, nom: nom, valeur_min: valeurMin,
          valeur_max: valeurMax, codes: codes,
        });
      }
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
      row.innerHTML = dumpingAreas.map((z) =>
        '<button type="button" class="sg2-outline sg2-chip' + (z.id === daSelectionnee ? ' sel' : '') +
        '" data-da="' + z.id + '">' + esc(z.nom) + '</button>').join('') +
        '<button type="button" class="btn-add-account" id="sgBtnDaAdd" title="Ajouter une dumping area">+</button>';
      rendreDetailDA();
      // Les pastilles de Dumping Instruction affichent le nom de la DA ; elles
      // ont pu etre dessinees avant que cette liste arrive (chargements
      // paralleles au demarrage). On les redessine une fois les noms connus.
      if (teneurs.length) rendreChipsInstruction();
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
    const b = $('#sgRoadsList');
    if (!b) return;
    if (!rows || !rows.length) { b.innerHTML = ''; return; }
    b.innerHTML = rows.map((r) => {
      const meta = r.type === 'geotiff'
        ? 'GeoTiff - le ' + esc((r.created_at || '').slice(0, 16))
        : 'GeoJSON - le ' + esc((r.created_at || '').slice(0, 16));
      return '<div class="sg2-road-item">' +
        '<i class="fas fa-route"></i>' +
        '<span class="sg2-road-nom">' + esc(r.nom) + '</span>' +
        '<span class="sg2-road-meta">' + meta + '</span>' +
        '<button type="button" class="sg2-x" data-road-del="' + r.id + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
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

    document.addEventListener('click', (e) => {
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

    chargerSources();
    chargerZones();
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
  async function initMinageUI() {
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
          body: JSON.stringify({ exca_code: exca, orebloc_id: Number(bloc) }),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status);
        Splash.succes('Minage declare', exca + ' sur bloc #' + bloc);
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
