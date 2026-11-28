/* =====================================================================
 * digplan.js - page "Dig Plan" : gestion des blasts/oreblocs.
 *
 * Deux backends interchangeables (voir API()) :
 *   'sg' : serveur Senor Geology local (Python, :8101) - defaut
 *   'ot' : Serveur_OT (Apps Script en ligne), Sheets + Drive
 *
 * Bascule sans rebuild, depuis la console du navigateur :
 *   localStorage.dpBackend = 'ot';
 *   localStorage.dpOtUrl   = 'https://script.google.com/macros/s/XXX/exec';
 *   localStorage.dpOtToken = '<jeton>';
 *
 * La carte (JPG) n'est jamais stockee en base64 cote serveur : elle part
 * dans Drive (ou sur disque en local) et seule son URL est conservee -
 * une cellule Sheets plafonne a 50 000 caracteres.
 * ===================================================================== */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const BASE_KEY = 'sgApiBase';
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  /* ========================= Choix du backend ============================= */

  // Endpoints centralises dans ot-config.js (OTCFG) ; repli autonome si la
  // page ne l'a pas charge, pour que digplan.js reste utilisable seul.
  function backendActif() {
    if (global_OTCFG()) return global_OTCFG().backend();
    try {
      return String(localStorage.getItem('dpBackend') || 'sg').toLowerCase() === 'ot'
        ? 'ot' : 'sg';
    } catch (e) { return 'sg'; }
  }

  function global_OTCFG() {
    return (typeof window !== 'undefined' && window.OTCFG) ? window.OTCFG : null;
  }

  function base() {
    if (global_OTCFG()) return global_OTCFG().passerelle();
    const saved = localStorage.getItem(BASE_KEY);
    if (saved) return saved.replace(/\/+$/, '');
    return (location.port === '8100' ? location.protocol + '//' + (location.hostname || 'localhost') + ':8101' : location.origin + '/passerelle');
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

  /* ---- Serveur_OT (Apps Script) -----------------------------------------
   * Apps Script ne rend ni en-tetes ni codes HTTP : tout sort en 200 avec
   * un marqueur {ok:...}. Le jeton voyage en parametre de requete.
   * Content-Type text/plain volontaire : evite le preflight CORS, qu'Apps
   * Script ne sait pas traiter.                                          */
  function otSend(methode, action, params, body) {
    const cfg = global_OTCFG();
    if (!cfg) throw new Error('ot-config.js absent : backend Serveur_OT indisponible');
    return cfg.appelOt(methode, action, params, body);
  }

  /* ===================== Parsing des polygones (DXF / CSV) ================
   * Portage fidele de extraire_oreblocs_csv() / extraire_oreblocs_dxf()
   * (Server_Senor_Geology.py). Necessaire cote navigateur : Serveur_OT
   * stocke les polygones deja decoupes, Apps Script ne reparse rien.
   * ======================================================================= */

  function _num(v) {
    const n = parseFloat(String(v).replace(',', '.'));
    return isFinite(n) ? n : null;
  }

  /** CSV de polygones : [x,y] ou [nom,x,y], separateur , ou ;. */
  function parserPolygonesCsv(texte, prefixe) {
    const lignes = String(texte).replace(/;/g, ',').split(/\r\n|\r|\n/);
    const groupes = {};
    const ordre = [];
    let nomCourant = null;
    for (const brut of lignes) {
      const ligne = brut.trim();
      if (!ligne) continue;
      const cellules = ligne.split(',').map((c) => c.trim()).filter((c) => c !== '');
      if (!cellules.length) continue;
      const n0 = _num(cellules[0]);
      if (n0 !== null) {
        // suite de coordonnees, rattachee au polygone courant
        if (cellules.length < 2) continue;
        const y = _num(cellules[1]);
        if (y === null) continue;
        nomCourant = nomCourant || (prefixe + ' 1');
        if (!groupes[nomCourant]) { groupes[nomCourant] = []; ordre.push(nomCourant); }
        groupes[nomCourant].push([n0, y]);
      } else {
        const nom = cellules[0];
        const x = cellules.length > 1 ? _num(cellules[1]) : null;
        const y = cellules.length > 2 ? _num(cellules[2]) : null;
        if (x === null || y === null) { nomCourant = null; continue; } // ligne d'entete
        nomCourant = nom;
        if (!groupes[nomCourant]) { groupes[nomCourant] = []; ordre.push(nomCourant); }
        groupes[nomCourant].push([x, y]);
      }
    }
    return ordre
      .filter((n) => groupes[n].length >= 3)
      .map((n) => ({ nom: n, sommets: groupes[n] }));
  }

  /**
   * DXF minimal : entites LWPOLYLINE et POLYLINE/VERTEX, nom = calque (code 8).
   *
   * Piege de la specification DXF : une entite POLYLINE porte dans son propre
   * en-tete un point FICTIF (codes 10/20 valant toujours 0,0) ; les vrais
   * sommets n'arrivent qu'ensuite, dans les entites VERTEX. Sans distinction,
   * ce (0,0) entre dans le polygone, etire la boite englobante jusqu'a
   * l'origine et ecrase tout le dessin sur une diagonale.
   * On n'accepte donc les coordonnees d'une POLYLINE qu'apres un VERTEX ;
   * pour une LWPOLYLINE, les 10/20 sont bien les sommets.
   */
  function parserPolygonesDxf(texte, prefixe) {
    const lignes = String(texte).replace(/\r\n|\r/g, '\n').split('\n');
    const blocs = [];
    let autoN = 0, courant = null, nom = '', pendingX = null;
    let typeEntite = '';      // 'LW' | 'POLY'
    let dansVertex = false;   // seulement utile pour 'POLY'

    function cloturer() {
      if (courant && courant.length >= 3) {
        autoN += 1;
        blocs.push({ nom: nom || (prefixe + ' ' + autoN), sommets: courant });
      }
      courant = null; nom = ''; pendingX = null;
      typeEntite = ''; dansVertex = false;
    }

    let k = 0;
    while (k < lignes.length - 1) {
      const code = parseInt(lignes[k].trim(), 10);
      if (isNaN(code)) { k += 2; continue; }
      const val = lignes[k + 1].trim();
      k += 2;
      if (code === 0) {
        const mot = val.toUpperCase();
        if (mot === 'LWPOLYLINE') { cloturer(); courant = []; typeEntite = 'LW'; }
        else if (mot === 'POLYLINE') { cloturer(); courant = []; typeEntite = 'POLY'; }
        else if (mot === 'VERTEX') { dansVertex = true; pendingX = null; }
        else if (mot === 'SEQEND' || mot === 'ENDSEC' || mot === 'EOF') cloturer();
        else cloturer();
      } else if (courant !== null) {
        if (code === 8 && !nom) nom = val;
        // POLYLINE : ignorer les 10/20 de l'en-tete (point fictif 0,0)
        else if (typeEntite === 'POLY' && !dansVertex) { /* ignore */ }
        else if (code === 10) pendingX = _num(val);
        else if (code === 20 && pendingX !== null) {
          const y = _num(val);
          if (y !== null) courant.push([pendingX, y]);
          pendingX = null;
        }
      }
    }
    cloturer();
    return blocs;
  }

  function parserPolygones(nomFichier, contenu, prefixe) {
    return String(nomFichier).toLowerCase().endsWith('.dxf')
      ? parserPolygonesDxf(contenu, prefixe)
      : parserPolygonesCsv(contenu, prefixe);
  }

  function setStatus(msg, ok) {
    const el = $('#dpStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'sg2-status account-status' + (msg ? (ok ? ' ok' : ' err') : '');
    // Seuls les messages de succes s'effacent : une erreur decrit un etat
    // persistant (serveur injoignable, backend non configure) et doit rester
    // lisible, sinon il ne reste qu'un cadre rouge vide.
    if (msg && ok) setTimeout(() => {
      if (el.textContent === msg) { el.textContent = ''; el.className = 'sg2-status account-status'; }
    }, 4000);
  }

  function confirmerPopup(message) {
    return new Promise((resolve) => {
      const overlay = $('#dpConfirmOverlay');
      const msgEl = $('#dpConfirmMessage');
      const btnOk = $('#dpConfirmOk');
      const btnCancel = $('#dpConfirmCancel');
      if (!overlay || !msgEl || !btnOk || !btnCancel) { resolve(window.confirm(message)); return; }
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

  /* ======================== Adaptateur de backend =========================
   * Les deux implementations exposent le meme contrat, pour que le reste du
   * fichier ignore sur quel serveur il tourne :
   *   sources()            -> [{id, nom}]
   *   teneurs(source)      -> [{nom, valeur_min, valeur_max}]
   *   blasts()             -> [{id, numero, nom, ..., map_url, oreblocs:[...]}]
   *   enregistrer(p, neuf) -> cree ou met a jour, rend l'id
   *   supprimer(blast)
   *   engins()             -> [{code, type, statut, x, y}]
   * `id` est TOUJOURS une chaine : numerique cote SG, numero de blast cote OT.
   * ======================================================================= */

  const ApiSG = {
    nom: 'Senor Geology (local)',
    async sources() {
      const d = await jsend('/api/sources', 'GET');
      return (d.sources || []).map((s) => ({ id: String(s.id), nom: s.nom, _sgId: s.id }));
    },
    async teneurs(source) {
      if (!source) return [];
      const d = await jsend('/api/teneurs?source_id=' + source._sgId, 'GET');
      return d.teneurs || [];
    },
    async blasts() {
      const d = await jsend('/api/blasts', 'GET');
      return (d.blasts || []).map((b) => Object.assign({}, b, {
        id: String(b.id),
        oreblocs: (b.oreblocs || []).map((o) => Object.assign({}, o, {
          sommets: enTableau(o.sommets),
        })),
      }));
    },
    async enregistrer(p, neuf) {
      if (neuf) {
        const corpsReq = {
          numero: p.numero, nom: p.nom, name_base: p.name_base,
          name_dxf: p.name_dxf,
          name_geosum: p.name_geosum, name_survey: p.name_survey,
          name_map: p.name_map, map_contenu: p.map_contenu,
          source_nom: p.source_nom, destination: p.destination,
        };
        // Base de donnees (CSV des polygones) : sans elle, rien n'arrive dans
        // l'onglet `dig_bloc` que lisent les tablettes (oubli corrige 12/11).
        if (p.base_contenu) corpsReq.base_contenu = p.base_contenu;
        if (p.dxf_contenu) {
          corpsReq.source_fichier = p.name_dxf;
          corpsReq.contenu = p.dxf_contenu;
        }
        if (p.geosum_contenu) corpsReq.geosum_contenu = p.geosum_contenu;
        const d = await jsend('/api/blasts', 'POST', corpsReq);
        dernierGeosum = d.geosum || null;
        dernierBase = d.base || null;
        return String(d.id);
      }
      const majReq = {
        id: Number(p.id), nom: p.nom, name_base: p.name_base,
        name_dxf: p.name_dxf,
        name_geosum: p.name_geosum, name_survey: p.name_survey,
        name_map: p.name_map, map_contenu: p.map_contenu,
      };
      if (p.base_contenu) majReq.base_contenu = p.base_contenu;
      await jsend('/api/blasts', 'PUT', majReq);
      dernierBase = null;     // en modification la base passe AVANT l'import du DXF : son compte rendu porterait sur d'anciens blocs
      if (p.dxf_contenu) {
        await jsend('/api/blasts_import', 'POST', {
          blast_id: Number(p.id), source_fichier: p.name_dxf, contenu: p.dxf_contenu,
        });
      }
      // Apres l'import des polygones : le Geosum qualifie les blocs
      // existants, il doit donc passer en dernier.
      if (p.geosum_contenu) {
        dernierGeosum = await jsend('/api/blasts_geosum', 'POST', {
          blast_id: Number(p.id), contenu: p.geosum_contenu,
        });
      }
      return String(p.id);
    },
    async supprimer(blast) {
      await jsend('/api/blasts?id=' + encodeURIComponent(blast.id), 'DELETE');
    },
    // 18/11 : etat CLOS. La passerelle le transmet a Serveur_OT.
    async clore(blast) {
      return jsend('/api/blasts_clore', 'POST', { id: Number(blast.id) });
    },
    async engins() {
      const d = await jsend('/api/engins', 'GET');
      return d.engins || [];
    },
  };

  const ApiOT = {
    nom: 'Serveur_OT (Apps Script)',
    async sources() {
      const d = await otSend('GET', 'dig_referentiel');
      this._teneurs = d.teneurs || [];
      return (d.sources || []).map((s) => ({ id: s.nom, nom: s.nom }));
    },
    async teneurs(source) {
      if (!source) return [];
      // le referentiel entier est deja rendu par sources(), on filtre
      if (!this._teneurs) await this.sources();
      return (this._teneurs || []).filter((t) => t.source === source.nom);
    },
    async blasts() {
      const d = await otSend('GET', 'dig_plan');
      return (d.blasts || []).map((b) => Object.assign({}, b, {
        id: String(b.numero),
        oreblocs: (b.oreblocs || []).map((o) => Object.assign({}, o, {
          sommets: enTableau(o.sommets),
        })),
      }));
    },
    async enregistrer(p) {
      // Apps Script ne reparse pas les fichiers : on envoie les polygones
      // deja decoupes par le navigateur.
      const corpsReq = {
        numero: p.numero || p.nom, nom: p.nom,
        name_dxf: p.name_dxf, name_geosum: p.name_geosum,
        name_survey: p.name_survey, name_map: p.name_map,
        source_nom: p.source_nom, destination: p.destination,
      };
      if (p.map_contenu) corpsReq.map_contenu = p.map_contenu;
      if (p.dxf_contenu) {
        corpsReq.dxf_contenu = p.dxf_contenu;
        corpsReq.oreblocs = parserPolygones(
          p.name_dxf || 'import.csv', p.dxf_contenu, 'Orebloc ' + (p.nom || ''));
      }
      const d = await otSend('POST', 'blast_upload', null, corpsReq);
      return String(d.numero);
    },
    async supprimer(blast) {
      // Supprimer = clore puis retirer (18/11) : signal explicite aux tablettes.
      await otSend('POST', 'blast_clore', null, { numero: blast.numero || blast.id, origine: 'saas' });
      await otSend('GET', 'dig_plan_suppr', { numero: blast.numero || blast.id });
    },
    async clore(blast) {
      return otSend('POST', 'blast_clore', null, { numero: blast.numero || blast.id, origine: 'saas' });
    },
    async engins() {
      return []; // les positions vivent sur la passerelle, pas sur Serveur_OT
    },
  };

  /** `sommets` arrive en JSON (SG/Sheets) ou deja en tableau (OT). */
  function enTableau(v) {
    if (Array.isArray(v)) return v;
    try { return JSON.parse(v || '[]'); } catch (e) { return []; }
  }

  function API() { return backendActif() === 'ot' ? ApiOT : ApiSG; }

  /* ============================ Source / Destination ============================ */

  let sources = [];
  let teneurs = [];
  let sourceSelectionnee = null;
  let teneurSelectionnee = null;

  async function chargerSources() {
    try {
      sources = await API().sources();
      if (!sourceSelectionnee || !sources.some((s) => s.id === sourceSelectionnee)) {
        sourceSelectionnee = sources.length ? sources[0].id : null;
      }
      rendreSourceLabel();
      await chargerTeneurs();
    } catch (e) {
      setStatus(API().nom + ' injoignable (' + e.message + ')', false);
    }
  }

  function sourceCourante() {
    return sources.find((x) => x.id === sourceSelectionnee) || null;
  }

  function rendreSourceLabel() {
    const s = sources.find((x) => x.id === sourceSelectionnee);
    const label = $('#dpSourceLabel');
    if (label) label.textContent = s ? s.nom : '(aucune)';
    const codes = $('#dpSourceCodes');
    if (codes) codes.textContent = s && s.codes ? String(s.codes).trim() : '';
    const plusieurs = sources.length > 1;
    const prev = $('#dpSrcPrev');
    const next = $('#dpSrcNext');
    if (prev) prev.disabled = !plusieurs;
    if (next) next.disabled = !plusieurs;
  }

  // nom de la source courante : c'est lui qui relie un blast a sa source
  // (colonne blasts.source_nom), pas l'id.
  function nomSourceCourante() {
    const s = sources.find((x) => x.id === sourceSelectionnee);
    return s ? s.nom : '';
  }

  async function naviguerSource(delta) {
    if (sources.length < 2) return;
    const idx = sources.findIndex((s) => s.id === sourceSelectionnee);
    const suivant = ((idx === -1 ? 0 : idx) + delta + sources.length) % sources.length;
    sourceSelectionnee = sources[suivant].id;
    teneurSelectionnee = null;
    blastSelectionne = null; // le blast courant appartient a l'ancienne source
    mode = 'consultation';
    rendreSourceLabel();
    await chargerTeneurs();
    await chargerBlasts();   // la liste des blasts suit la source
  }

  async function chargerTeneurs() {
    const row = $('#dpTeneursRow');
    if (!row) return;
    if (!sourceSelectionnee) { row.innerHTML = ''; return; }
    try {
      teneurs = await API().teneurs(sourceCourante());
      if (teneurSelectionnee && !teneurs.some((t) => t.nom === teneurSelectionnee)) {
        teneurSelectionnee = null;
      }
      if (!teneurSelectionnee && teneurs.length) teneurSelectionnee = teneurs[0].nom;
      rendreChips();
    } catch (e) {
      setStatus('Teneurs injoignables (' + e.message + ')', false);
    }
  }

  function rendreChips() {
    const row = $('#dpTeneursRow');
    if (!row) return;
    row.innerHTML = teneurs.length
      ? teneurs.map((t) =>
          '<button type="button" class="dp-chip' + (t.nom === teneurSelectionnee ? ' sel' : '') +
          '" style="--fam:' + (window.OTCouleurs ? OTCouleurs.famille(t.nom) : '#8d887a') +
          '" data-teneur="' + esc(t.nom) + '">' + esc(t.nom) + '</button>').join('')
      : '<span class="dp-vide">Aucune teneur pour cette source (à configurer dans Senor).</span>';
  }

  /* ================================= Blasts ======================================= */

  let blasts = [];
  let blastSelectionne = null;
  // blast selectionne avant un "Nouveau Blast" : cible du retour si on annule
  let blastAvantCreation = null;

  // Mode du formulaire. Enregistrer/Annuler ne sont affiches que si l'on a
  // explicitement demande une creation ou une modification.
  //   'consultation' : on regarde un blast, champs verrouilles, aucun bouton
  //   'creation'     : via "Nouveau Blast"
  //   'edition'      : via "Modifier un blast"
  let mode = 'consultation';
  // Rognage de la carte en cours : { b, couche, barre, cadre, rect, x0, y0, actif }.
  let rognage = null;

  // champs fichier du formulaire : cle -> { nom, contenu? } (contenu seulement pour dxf, requis au parsing)
  // base : CSV des polygones (1 ligne par sommet) -> onglet Sheet "dig_bloc"
  const CHAMPS_FICHIER = ['base', 'dxf', 'geosum', 'survey', 'map'];
  let fichiers = { base: null, dxf: null, geosum: null, survey: null, map: null };

  // Convention de nommage : le libelle de chaque champ fichier est
  // "<Name>_<suffixe>", ou <Name> est la valeur saisie dans le champ Name.
  // Casse des suffixes reprise telle quelle de la maquette.
  const SUFFIXES_FICHIER = { base: '_base', dxf: '_dxf', geosum: '_Geosum', survey: '_servey', map: '_Map' };

  function blastCourant() {
    return blasts.find((b) => b.id === blastSelectionne) || null;
  }

  async function chargerBlasts(selectionnerId) {
    try {
      const tous = await API().blasts();
      // un blast appartient a une source : on n'affiche que ceux de la source
      // courante. Les blasts sans source renseignee restent visibles (legacy),
      // sinon ils deviendraient inaccessibles depuis l'interface.
      const src = nomSourceCourante();
      blasts = tous.filter((b) => {
        const bs = String(b.source_nom || '').trim();
        return !src || !bs || bs === src;
      });
      if (selectionnerId !== undefined) {
        blastSelectionne = selectionnerId === null ? null : String(selectionnerId);
      } else if (blastSelectionne && !blasts.some((b) => b.id === blastSelectionne)) {
        blastSelectionne = blasts.length ? blasts[0].id : null;
      } else if (!blastSelectionne && blasts.length) {
        blastSelectionne = blasts[0].id;
      }
      rendreForm();
      rendrePlan();
      rendreBoutons();
    } catch (e) {
      setStatus(API().nom + ' injoignable (' + e.message + ')', false);
    }
  }

  function estEnCreation() { return mode === 'creation'; }
  function estEnSaisie() { return mode !== 'consultation'; }

  function rendreBoutons() {
    const has = !!blastSelectionne;
    const saisie = estEnSaisie();

    // pendant une saisie, on doit d'abord Enregistrer ou Annuler
    const courant = has ? blastCourant() : null;
    const clos = !!(courant && courant.clos_le);
    $('#dpBtnEdit').disabled = !has || saisie || clos;   // un blast clos ne se modifie plus
    $('#dpBtnDelete').disabled = !has || saisie;
    const btnClore = $('#dpBtnClore');
    if (btnClore) {
      btnClore.disabled = !has || saisie || clos;
      btnClore.textContent = clos ? 'Blast clos' : 'Clore le blast';
    }

    // Enregistrer / Annuler : visibles uniquement quand on les a appeles
    const actions = $('#dpFormActions');
    if (actions) actions.hidden = !saisie;
    const btnCancel = $('#dpBtnCancel');
    if (btnCancel) {
      btnCancel.textContent = estEnCreation() ? 'Annuler nouveau blast' : 'Annuler modif';
    }

    // en consultation les champs sont verrouilles : sans bouton Enregistrer,
    // une saisie serait perdue sans que l'utilisateur comprenne pourquoi
    const nameInput = $('#dpName');
    if (nameInput) nameInput.readOnly = !saisie;
    CHAMPS_FICHIER.forEach((cle) => {
      const t = $('#dpName' + capitale(cle) + 'Trigger');
      if (t) t.disabled = !saisie;
    });
  }

  function rendreBlastList() {
    const list = $('#dpBlastList');
    if (!list) return;
    if (!blasts.length) {
      list.innerHTML = '<div class="dp-note">Aucun blast enregistre.</div>';
      return;
    }
    list.innerHTML = blasts.map((b) =>
      '<button type="button" class="dp-blast-item' + (b.id === blastSelectionne ? ' sel' : '') +
      '" data-blast="' + b.id + '">' + esc(b.nom || b.numero) +
      '<small>' + esc(b.numero) + ' - ' + (b.nb_blocs || 0) + ' bloc(s)' +
      (b.clos_le ? ' - CLOS le ' + esc(String(b.clos_le).slice(0, 10)) : '') +
      '</small></button>').join('');
  }


  // reinitialise les champs fichier a partir du blast courant (nom deja enregistre,
  // pas de contenu puisqu'on ne retelecharge pas le fichier depuis le serveur) ou vide.
  function rendreChampsFichier(b) {
    CHAMPS_FICHIER.forEach((cle) => {
      fichiers[cle] = null;
      const label = $('#dpName' + capitale(cle) + 'Label');
      const trigger = $('#dpName' + capitale(cle) + 'Trigger');
      const valeur = b ? (b['name_' + cle] || '') : '';
      if (label) label.textContent = valeur || '....';
      if (trigger) trigger.classList.toggle('has-file', !!valeur);
    });
  }

  function capitale(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  // Repercute le champ Name sur les libelles des champs fichier
  // ("LWP2_107" -> "LWP2_107_dxf", "LWP2_107_Geosum", ...).
  // Retombe sur "Name" tant que le champ est vide.
  function majLabelsFichier() {
    const base = (($('#dpName') || {}).value || '').trim() || 'Name';
    CHAMPS_FICHIER.forEach((cle) => {
      const el = $('#dpFieldLabel' + capitale(cle));
      if (el) el.textContent = base + SUFFIXES_FICHIER[cle];
    });
  }

  function rendreForm() {
    const b = blastCourant();
    $('#dpName').value = b ? (b.nom || b.numero || '') : '';
    rendreChampsFichier(b);
    majLabelsFichier();
  }

  // Dernier compte rendu Geosum rendu par le serveur. Un Geosum refuse ne
  // doit JAMAIS passer inapercu : sans ce retour, le blast s'enregistre, la
  // table reste a zero, et personne ne sait pourquoi.
  let dernierGeosum = null;
  let dernierBase = null;

  // Alerte d'appariement : les valeurs (volume, tonnes, Au, densite, destination) se rattachent au bloc PAR SON NOM.
  // Un DXF dont les calques portent d'autres noms que le CSV (cas typique : DXF exporte d'un modele precedent)
  // laisse tout a zero SANS erreur. Rend { grave, texte } ou null.
  function alerteAppariement() {
    const exemples = (l) => l.slice(0, 3).join(', ') + (l.length > 3 ? ' …' : '');
    const verifier = (rapport, fichier) => {
      if (!rapport || rapport.erreur) return null;
      if (rapport.illisible) return { grave: true, texte: fichier + ' illisible : colonne de noms de blocs introuvable.' };
      if (!rapport.blocs) return null;
      if (rapport.apparies === 0) {
        return { grave: true, texte: 'AUCUN bloc du DXF ne correspond au ' + fichier + ' : les noms diffèrent, les valeurs restent à 0. ' +
          'Noms du DXF : ' + exemples(rapport.sans_donnees || []) + ' — noms du ' + fichier + ' : ' + exemples(rapport.absents_du_dxf || []) + '.' };
      }
      // 26/11 : le Geosum ne liste que le MINERAI ; un bloc deja sterile par son nom (_WST_) y est absent par nature
      // (ses valeurs viennent du fichier de base) : pas d'avertissement pour lui
      const manquants = (rapport.sans_donnees || []).filter((n) => !(fichier === 'Geosum' && /_WST_/i.test(String(n))));
      if (manquants.length) {
        return { grave: false, texte: manquants.length + ' bloc(s) du DXF sans donnée dans le ' + fichier + ' (classés stérile) : ' +
          exemples(manquants) + '.' };
      }
      return null;
    };
    const liste = [verifier(dernierBase, 'fichier de base'), verifier(dernierGeosum, 'Geosum')].filter(Boolean);
    dernierBase = null;
    if (!liste.length) return null;
    return { grave: liste.some((a) => a.grave), texte: liste.map((a) => a.texte).join(' ') };
  }

  function resumeGeosum() {
    const g = dernierGeosum;
    dernierGeosum = null;
    if (!g) return '';
    if (g.erreur) return ' - GEOSUM REFUSE : ' + g.erreur;
    const n = (g.minerai || []).length;
    const w = (g.sterile || []).length;
    let t = ' - Geosum : ' + n + ' bloc(s) de minerai, ' + w + ' sterile';
    if ((g.absents_du_dxf || []).length) {
      t += ' - ' + g.absents_du_dxf.length + ' bloc(s) du Geosum absent(s) du DXF';
    }
    return t;
  }

  async function sauverBlast(e) {
    e.preventDefault();
    const nom = ($('#dpName').value || '').trim();
    if (!nom) { setStatus('Le champ Name est requis', false); return; }
    const b = blastCourant();
    const payload = { nom: nom };
    CHAMPS_FICHIER.forEach((cle) => {
      if (fichiers[cle]) {
        payload['name_' + cle] = fichiers[cle].nom;
      } else if (b) {
        payload['name_' + cle] = b['name_' + cle] || '';
      }
    });
    // La carte part en CONTENU transitoire (map_contenu) ; le serveur la
    // depose dans Drive (ou sur disque) et ne conserve que son URL. Jamais
    // de base64 stocke en base : plafond de 50 000 caracteres par cellule.
    if (fichiers.map && fichiers.map.dataUrl) payload.map_contenu = fichiers.map.dataUrl;
    if (fichiers.dxf && fichiers.dxf.contenu) payload.dxf_contenu = fichiers.dxf.contenu;
    if (fichiers.geosum && fichiers.geosum.contenu) {
      payload.geosum_contenu = fichiers.geosum.contenu;
    }
    // Base de donnees (CSV des polygones) : envoyee integralement, elle part
    // dans l'onglet Sheet "dig_bloc" (1 ligne par sommet) - la tablette
    // l'y lit pour avoir les blocs precis.
    if (fichiers.base && fichiers.base.contenu) payload.base_contenu = fichiers.base.contenu;

    // Splash bloquant : l'upload DXF/Geosum + extraction + push cloud peut
    // prendre plusieurs secondes — l'utilisateur doit voir chaque etape.
    const estCreation = !b;
    const sp = Splash.bloquer(
      estCreation ? 'Ajout du blast ' + nom + '...' : 'Mise a jour du blast...',
      'Envoi des fichiers...');
    try {
      let id;
      if (b) {
        payload.id = b.id;
        payload.numero = b.numero || b.id;
        Splash.etape('Enregistrement de la planche...');
        id = await API().enregistrer(payload, false);
        mode = 'consultation'; // enregistre : on ressort du mode saisie
        Splash.etape('Rechargement du blast...');
        await chargerBlasts(id);
        const alerte = alerteAppariement();
        if (alerte && alerte.grave) { Splash.erreur('Attention : valeurs non importées', alerte.texte); setStatus('Blast mis a jour - ' + alerte.texte, false); }
        else { Splash.succes('Blast mis a jour', nom); setStatus('Blast mis a jour' + (alerte ? ' - ' + alerte.texte : ''), !alerte); }
      } else {
        // un blast cree est rattache a la source affichee (sinon il n'apparait
        // dans aucune liste filtree) et a la teneur selectionnee
        payload.numero = nom;
        payload.source_nom = nomSourceCourante();
        payload.destination = teneurSelectionnee || '';
        Splash.etape('Extraction des blocs (DXF/CSV)...');
        id = await API().enregistrer(payload, true);
        blastAvantCreation = null; // creation menee a terme : plus rien a annuler
        mode = 'consultation';
        Splash.etape('Rechargement de la liste...');
        await chargerBlasts(id);
        const alerte = alerteAppariement();
        if (alerte && alerte.grave) { Splash.erreur('Attention : valeurs non importées', alerte.texte); setStatus('Blast cree - ' + alerte.texte, false); }
        else { Splash.succes('Blast cree : ' + nom); setStatus('Blast cree' + (alerte ? ' - ' + alerte.texte : ''), !alerte); }
      }
    } catch (err) {
      Splash.erreur(estCreation ? 'Creation impossible' : 'Mise a jour impossible', err.message);
      setStatus(err.message, false);
    }
  }

  function nouveauBlast() {
    // memorise la selection courante : sans ca, "Annuler" n'a aucun blast
    // vers lequel revenir une fois blastSelectionne passe a null.
    blastAvantCreation = blastSelectionne;
    blastSelectionne = null;
    mode = 'creation';
    rendreForm();
    rendrePlan();
    rendreBoutons();
    fermerSelectModal();
    $('#dpName').focus();
  }

  // Deux annulations distinctes selon le mode :
  //  - creation en cours -> abandonne et revient au blast d'avant
  //  - modification      -> recharge les valeurs enregistrees du blast courant
  function annulerForm() {
    if (estEnCreation()) {
      const cible = blasts.some((b) => b.id === blastAvantCreation)
        ? blastAvantCreation
        : (blasts.length ? blasts[0].id : null);
      blastAvantCreation = null;
      blastSelectionne = cible;
      mode = 'consultation';
      rendreForm();
      rendrePlan();
      rendreBoutons();
      const b = blastCourant();
      setStatus(b ? 'Nouveau blast annule, retour a "' + (b.nom || b.numero) + '"'
                  : 'Nouveau blast annule', true);
      return;
    }
    const b = blastCourant();
    mode = 'consultation';
    rendreForm();
    rendreBoutons();
    setStatus('Modifications annulees, "' + (b.nom || b.numero) + '" restaure', true);
  }

  // passe en mode edition : c'est ce qui fait apparaitre Enregistrer/Annuler.
  function modifierBlast() {
    const b = blastCourant();
    if (!b) { setStatus('Selectionnez d\'abord un blast', false); return; }
    mode = 'edition';
    rendreForm();
    rendreBoutons();
    $('#dpName').focus();
    $('#dpName').select();
    setStatus('Modifiez les champs de "' + (b.nom || b.numero) + '" puis cliquez Enregistrer', true);
  }

  /**
   * Clore un blast (18/11, demande client) : le blast est termine. Il reste
   * consultable ici, ne se modifie plus, et les tablettes purgent leurs
   * donnees locales des que leurs envois sont recus par le serveur.
   */
  async function cloreBlast() {
    const b = blastCourant();
    if (!b || b.clos_le) return;
    if (!await confirmerPopup('Clore le blast "' + (b.nom || b.numero) + '" ? ' +
        'Il ne sera plus modifiable et les tablettes effaceront ses donnees ' +
        'locales une fois leurs envois recus.')) return;
    const sp = Splash.bloquer('Cloture du blast...', (b.nom || b.numero));
    try {
      const d = await API().clore(b);
      await chargerBlasts();
      if (d && d.ot_sync === false) {
        Splash.succes('Blast clos (envoi au cloud en attente)', d.ot_erreur || '');
        setStatus('Blast clos - cloud injoignable, nouvel essai automatique', false);
      } else {
        Splash.succes('Blast clos', b.nom || b.numero);
        setStatus('Blast clos', true);
      }
      rendreBoutons();
    } catch (err) {
      Splash.erreur('Cloture impossible', err.message);
      setStatus(err.message, false);
    }
  }

  async function supprimerBlast() {
    const b = blastCourant();
    if (!b) return;
    if (!await confirmerPopup('Supprimer le blast "' + (b.nom || b.numero) + '" et tous ses blocs ?')) return;
    const sp = Splash.bloquer('Suppression du blast...', (b.nom || b.numero));
    try {
      Splash.etape('Suppression des blocs associes...');
      await API().supprimer(b);
      blastSelectionne = null;
      Splash.etape('Rechargement de la liste...');
      await chargerBlasts();
      Splash.succes('Blast supprime', b.nom || b.numero);
      setStatus('Blast supprime', true);
    } catch (err) {
      Splash.erreur('Suppression impossible', err.message);
      setStatus(err.message, false);
    }
  }

  function ouvrirSelectModal() {
    rendreBlastList();
    $('#dpSelectOverlay').hidden = false;
  }

  function fermerSelectModal() {
    $('#dpSelectOverlay').hidden = true;
  }

  // dxf    : contenu texte lu pour permettre le parsing reel (extraire_oreblocs_dxf/csv).
  // geosum : contenu texte lu aussi. Il porte le tonnage, la teneur mesuree
  //          (Au_ok), la densite, la litho et la famille de destination de
  //          chaque bloc de MINERAI. Un polygone du DXF absent de ce fichier
  //          est du sterile et ne doit pas partir sur les tablettes.
  // map    : image lue en data URL, affichee telle quelle sur l'onglet Map.
  // survey : simple reference (nom de fichier), pas de parsing.
  function choisirFichier(cle, file) {
    if (!file) return;
    const marquerChoisi = () => {
      const label = $('#dpName' + capitale(cle) + 'Label');
      const trigger = $('#dpName' + capitale(cle) + 'Trigger');
      if (label) label.textContent = file.name;
      if (trigger) trigger.classList.add('has-file');
    };
    if (cle === 'geosum') {
      // Lu en data URL, jamais en texte : le Geosum existe en CSV et en
      // .xlsx, et un .xlsx est une archive binaire que readAsText
      // corromprait silencieusement. Le serveur reconnait les deux.
      const reader = new FileReader();
      reader.onload = () => {
        fichiers.geosum = { nom: file.name, contenu: String(reader.result || '') };
        marquerChoisi();
        setStatus('Geosum "' + file.name + '" pret, cliquez Enregistrer', true);
      };
      reader.onerror = () => setStatus('Lecture du Geosum impossible', false);
      reader.readAsDataURL(file);
      return;
    }
    if (cle === 'base' || cle === 'dxf') {
      const reader = new FileReader();
      reader.onload = () => {
        fichiers[cle] = { nom: file.name, contenu: String(reader.result || '') };
        marquerChoisi();
        setStatus('Fichier "' + file.name + '" pret, cliquez Enregistrer', true);
      };
      reader.onerror = () => setStatus('Lecture du fichier impossible', false);
      reader.readAsText(file);
      return;
    }
    if (cle === 'map') {
      const reader = new FileReader();
      reader.onload = () => {
        fichiers.map = { nom: file.name, dataUrl: String(reader.result || '') };
        marquerChoisi();
        setStatus('Carte "' + file.name + '" prete, cliquez Enregistrer', true);
      };
      reader.onerror = () => setStatus('Lecture de l\'image impossible', false);
      reader.readAsDataURL(file);
      return;
    }
    fichiers[cle] = { nom: file.name };
    marquerChoisi();
  }

  /* ============================== Rendu du plan (Map) ============================= */

  const PALETTE_DEST = {
    '': 'rgba(148,163,184,.35)',
    'W': 'rgba(148,163,184,.35)',
  };
  const COULEURS_ORE = ['#ec4899', '#f59e0b', '#22c55e', '#38bdf8', '#a855f7'];
  let coursOreColor = 0;
  const coursOreColorMap = {};

  function couleurDestination(dest) {
    const key = (dest || '').toUpperCase();
    if (PALETTE_DEST[key]) return PALETTE_DEST[key];
    // couleur de la famille de teneur : meme source que toutes les pages (js/couleurs.js)
    if (window.OTCouleurs && OTCouleurs.estConnue(dest)) return OTCouleurs.famille(dest);
    if (!coursOreColorMap[key]) {
      coursOreColorMap[key] = COULEURS_ORE[coursOreColor % COULEURS_ORE.length];
      coursOreColor++;
    }
    return coursOreColorMap[key];
  }

  window.addEventListener('couleurs:pret', () => {
    try { rendreChips(); rendrePlan(); } catch (e) { /* plan pas encore pret */ }
  });

  function rendrePlan() {
    const b = blastCourant();
    const oreblocs = (b && b.oreblocs) || [];

    $('#dpPlanTitle').textContent = b ? (b.numero || b.nom || '-') : '-';
    const actif = $('#dpBlastActif');
    if (actif) actif.textContent = b ? (b.numero || b.nom || '—') : '—';
    rendreMeta(b);
    $('#dpAuthor').textContent = '';
    $('#dpPageLabel').textContent = b ? (b.numero || '') + (b.page_label ? ' ' + b.page_label : '') : '';

    rendreMapImage(b);
    rendreTable(oreblocs);
    rendreDiagramme(oreblocs);
    rendreProgress(oreblocs);
  }

  /**
   * Decode la nomenclature d'un nom de blast.
   *   230_OM_230_225_106          -> flitch 230, OM, mine de 230 a 225, blast 106
   *   232.5_OM_LWP2_235_230_106   -> idem avec le site LWP2
   * Les trois derniers segments sont toujours <debut>_<fin>_<pattern> ;
   * un eventuel site s'intercale entre l'activite et la plage de minage.
   */
  function decoderNomenclature(code) {
    const seg = String(code || '').split('_').filter((s) => s !== '');
    if (seg.length < 5) return null;
    const n = seg.length;
    const pattern = seg[n - 1];
    const profFin = seg[n - 2];
    const profDebut = seg[n - 3];
    // la plage de minage doit etre numerique, sinon ce n'est pas cette convention
    if (!/^[0-9.]+$/.test(profDebut) || !/^[0-9.]+$/.test(profFin)) return null;
    return {
      flitch: seg[0],
      activite: seg[1],
      site: n >= 6 ? seg.slice(2, n - 3).join('_') : '',
      profDebut: profDebut,
      profFin: profFin,
      pattern: pattern,
    };
  }

  /** Ligne de contexte du plan : seulement ce qui est reellement connu (flitch, activite, pattern, source...). */
  function rendreMeta(b) {
    const grille = $('#dpMetaGrid');
    if (!grille) return;
    if (!b) { grille.innerHTML = ''; return; }
    const code = b.numero || b.nom || '';
    const d = decoderNomenclature(code);
    const parts = [];
    if (d) {
      parts.push('Flitch ' + d.flitch);
      parts.push('Plage ' + d.activite);
      parts.push('Pattern ' + d.pattern);
      if (d.profDebut && d.profFin) parts.push('Minage ' + d.profDebut + ' → ' + d.profFin);
      if (d.site) parts.push(d.site);
    }
    if (b.source_nom) parts.push(b.source_nom);
    if (b.plan_date) parts.push(b.plan_date);
    if (b.scale) parts.push('Échelle ' + b.scale);
    const uniques = parts.filter((t, i) => parts.indexOf(t) === i);
    grille.innerHTML = uniques.map((t) => '<span>' + esc(t) + '</span>').join('<i>·</i>');
  }

  // onglet Map : affiche la carte importee (Name_Map) en plein cadre ; rien de genere.
  function rendreMapImage(b) {
    const wrap = $('#dpMapImageWrap');
    if (!wrap) return;
    // map_url : Drive (OT) ou fichier servi localement (SG). map_image est
    // l'ancien champ base64, conserve en repli pour les blasts deja crees.
    // `map_apercu` d'abord : copie locale servie par la passerelle. L'URL
    // Drive de `map_url` est refusee a l'integration par le navigateur
    // (Cross-Origin-Resource-Policy), alors qu'elle convient aux tablettes
    // qui telechargent le fichier au lieu de l'afficher en place.
    const src = b && (b.map_apercu || b.map_url || b.map_image);
    rognage = null;
    if (src) {
      // Rogner : seulement en consultation (le formulaire est verrouille).
      const boutons = mode === 'consultation'
        ? '<div class="dp-crop-barre">' +
          (lireRognage(b) ? '<button type="button" class="dp-crop-toggle" id="dpCropReset" title="Revenir a la carte entiere"><i class="fas fa-expand"></i> Plein cadre</button>' : '') +
          '<button type="button" class="dp-crop-toggle" id="dpCropBtn" title="Rogner la carte (affichage local a ce poste)"><i class="fas fa-crop-simple"></i> Rogner</button>' +
          '</div>'
        : '';
      wrap.innerHTML = '<div class="dp-map-box" id="dpMapBox"><img id="dpMapImg" src="' + esc(src) + '" alt="Carte du blast" /></div>' + boutons;
      const img = $('#dpMapImg');
      img.addEventListener('load', () => ajusterCarte(false));
      if (img.complete) ajusterCarte(false);
      const btn = $('#dpCropBtn');
      if (btn) btn.addEventListener('click', () => ouvrirRognage(b));
      const reset = $('#dpCropReset');
      if (reset) reset.addEventListener('click', () => { ecrireRognage(b, null); rendreMapImage(b); });
    } else {
      wrap.innerHTML = '<div class="dp-note">Aucune carte importee. Utilisez le champ Name_Map pour en ajouter une.</div>';
    }
  }

  /* ============================ Rogner la carte (LOCAL) ============================ */
  // Le rognage est un simple cadrage d'AFFICHAGE, propre a ce poste : le rectangle trace est memorise
  // en fractions de l'image (localStorage). Rien n'est envoye ni enregistre : ni Drive, ni passerelle,
  // ni fichier ; l'original et ce que voient les tablettes ne changent pas.

  const cleRognage = (b) => 'dp-rognage:' + (b.id || b.numero || '') + ':' + (b.name_map || '');

  function lireRognage(b) {
    try {
      const v = JSON.parse(localStorage.getItem(cleRognage(b)) || 'null');
      if (v && v.w > 0.02 && v.h > 0.02 && v.x >= 0 && v.y >= 0 && v.x + v.w <= 1.001 && v.y + v.h <= 1.001) return v;
    } catch (e) { /* stockage indisponible ou illisible : carte entiere */ }
    return null;
  }

  function ecrireRognage(b, frac) {
    try {
      if (frac) localStorage.setItem(cleRognage(b), JSON.stringify(frac));
      else localStorage.removeItem(cleRognage(b));
    } catch (e) { /* stockage indisponible : le cadrage ne sera pas memorise */ }
  }

  // Dimensionne la carte pour REMPLIR la zone disponible (zone cadree si un rognage existe, sinon image entiere).
  function ajusterCarte(complet) {
    const wrap = $('#dpMapImageWrap'), box = $('#dpMapBox'), img = $('#dpMapImg');
    if (!wrap || !box || !img || !img.naturalWidth) return;
    const b = blastCourant();
    const frac = (!complet && b && lireRognage(b)) || { x: 0, y: 0, w: 1, h: 1 };
    // largeur : celle de la zone ; hauteur maximale : ce que la fenetre permet (la zone s'ajuste ensuite a l'image)
    const W = wrap.clientWidth;
    const H = Math.max(420, Math.min(window.innerHeight - 330, 900));
    if (!W) return;
    const nw = img.naturalWidth, nh = img.naturalHeight;
    const cw = frac.w * nw, ch = frac.h * nh;
    const sc = Math.min(W / cw, H / ch);
    box.style.width = (cw * sc) + 'px';
    box.style.height = (ch * sc) + 'px';
    img.style.width = (nw * sc) + 'px';
    img.style.height = (nh * sc) + 'px';
    img.style.left = (-frac.x * nw * sc) + 'px';
    img.style.top = (-frac.y * nh * sc) + 'px';
    wrap.style.height = Math.max(ch * sc, 160) + 'px';   // pas de vide autour d'une carte basse ou large
  }

  let minuteurRedim = null;
  window.addEventListener('resize', () => {
    clearTimeout(minuteurRedim);
    minuteurRedim = setTimeout(() => { if (!rognage) ajusterCarte(false); }, 120);
  });

  function ouvrirRognage(b) {
    const img = $('#dpMapImg');
    const wrap = $('#dpMapImageWrap');
    if (!img || !wrap || !img.naturalWidth) {
      setStatus('Carte pas encore chargee, reessayez', false);
      return;
    }
    fermerRognage();
    ajusterCarte(true);                 // on trace toujours sur l'image ENTIERE
    const box = $('#dpMapBox');
    const zone = box.getBoundingClientRect();
    const base = wrap.getBoundingClientRect();
    const couche = document.createElement('div');
    couche.className = 'dp-crop-layer';
    couche.style.left = (zone.left - base.left) + 'px';
    couche.style.top = (zone.top - base.top) + 'px';
    couche.style.width = zone.width + 'px';
    couche.style.height = zone.height + 'px';
    couche.innerHTML = '<div class="dp-crop-hint">Tracez la zone a garder (affichage local)</div>' +
      '<div class="dp-crop-box" hidden></div>';
    const barre = document.createElement('div');
    barre.className = 'dp-crop-actions';
    barre.innerHTML =
      '<button type="button" class="dp-crop-ok" disabled><i class="fas fa-check"></i> Valider</button>' +
      '<button type="button" class="dp-crop-cancel"><i class="fas fa-xmark"></i> Annuler</button>';
    wrap.appendChild(couche);
    wrap.appendChild(barre);
    const tools = wrap.querySelector('.dp-crop-barre');
    if (tools) tools.hidden = true;
    rognage = { b: b, couche: couche, barre: barre, cadre: couche.querySelector('.dp-crop-box'),
                rect: null, x0: 0, y0: 0, actif: false };

    const point = (ev) => {
      const r = couche.getBoundingClientRect();
      return {
        x: Math.min(Math.max(ev.clientX - r.left, 0), r.width),
        y: Math.min(Math.max(ev.clientY - r.top, 0), r.height),
      };
    };
    couche.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      couche.setPointerCapture(ev.pointerId);
      const p = point(ev);
      rognage.actif = true;
      rognage.x0 = p.x;
      rognage.y0 = p.y;
      rognage.rect = { x: p.x, y: p.y, w: 0, h: 0 };
      dessinerCadre();
    });
    couche.addEventListener('pointermove', (ev) => {
      if (!rognage || !rognage.actif) return;
      const p = point(ev);
      rognage.rect = {
        x: Math.min(rognage.x0, p.x), y: Math.min(rognage.y0, p.y),
        w: Math.abs(p.x - rognage.x0), h: Math.abs(p.y - rognage.y0),
      };
      dessinerCadre();
    });
    const fin = () => {
      if (!rognage) return;
      rognage.actif = false;
      const r = rognage.rect;
      barre.querySelector('.dp-crop-ok').disabled = !(r && r.w >= 10 && r.h >= 10);
    };
    couche.addEventListener('pointerup', fin);
    couche.addEventListener('pointercancel', fin);
    barre.querySelector('.dp-crop-cancel').addEventListener('click', () => { fermerRognage(); ajusterCarte(false); });
    barre.querySelector('.dp-crop-ok').addEventListener('click', validerRognage);
    document.addEventListener('keydown', echapRognage);
  }

  function dessinerCadre() {
    const r = rognage && rognage.rect;
    if (!r) return;
    const c = rognage.cadre;
    c.hidden = false;
    c.style.left = r.x + 'px';
    c.style.top = r.y + 'px';
    c.style.width = r.w + 'px';
    c.style.height = r.h + 'px';
    const hint = rognage.couche.querySelector('.dp-crop-hint');
    if (hint) hint.hidden = true;
  }

  function echapRognage(ev) {
    if (ev.key === 'Escape') { fermerRognage(); ajusterCarte(false); }
  }

  function fermerRognage() {
    document.removeEventListener('keydown', echapRognage);
    if (!rognage) return;
    rognage.couche.remove();
    rognage.barre.remove();
    rognage = null;
    const tools = document.querySelector('#dpMapImageWrap .dp-crop-barre');
    if (tools) tools.hidden = false;
  }

  // Valide : memorise le cadrage LOCALEMENT (fractions de l'image), sans aucun envoi.
  function validerRognage() {
    if (!rognage || !rognage.rect) return;
    const b = rognage.b;
    const couche = rognage.couche.getBoundingClientRect();
    const r = rognage.rect;
    if (!couche.width || !couche.height) return;
    ecrireRognage(b, { x: r.x / couche.width, y: r.y / couche.height, w: r.w / couche.width, h: r.h / couche.height });
    fermerRognage();
    rendreMapImage(b);
    setStatus('Carte rognee pour ce poste (rien n\'est modifie sur le serveur ni le Drive)', true);
  }

  // point-in-polygon (ray casting), miroir de point_in_polygon() cote serveur
  // (Server_Senor_Geology.py) pour determiner quel bloc contient une excavatrice.
  function pointDansPolygone(px, py, polygon) {
    let dedans = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const [xi, yi] = polygon[i], [xj, yj] = polygon[j];
      const intersecte = ((yi > py) !== (yj > py)) &&
        (px < (xj - xi) * (py - yi) / (yj - yi) + xi);
      if (intersecte) dedans = !dedans;
    }
    return dedans;
  }

  // avancement par bloc (tonnes minees / theoriques), alimente par majDiagrammeOnline ; sert aux etiquettes du plan
  let progressionBlocs = {};
  let derniersEnCours = [];

  function rendreSvg(containerId, oreblocs, highlightIds) {
    const wrap = $('#' + containerId);
    if (!wrap) return;
    if (!oreblocs.length) {
      wrap.innerHTML = '<div class="dp-note">Aucun polygone pour ce blast. Importez un fichier CSV/DXF ou choisissez un autre blast.</div>';
      return;
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    oreblocs.forEach((b) => {
      let pts = [];
      pts = enTableau(b.sommets); // deja normalise par l'adaptateur
      pts.forEach(([x, y]) => {
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (y < minY) minY = y; if (y > maxY) maxY = y;
      });
    });
    if (!isFinite(minX)) { wrap.innerHTML = ''; return; }
    const pad = Math.max((maxX - minX), (maxY - minY)) * 0.1 || 10;
    minX -= pad; minY -= pad; maxX += pad; maxY += pad;
    const w = maxX - minX, h = maxY - minY;

    let num = 0;
    // epaisseur de trait exprimee dans les unites du viewBox : partagee avec
    // l'animation CSS du bloc en minage via --dp-stroke (sinon halo demesure).
    const traitW = w / 250;
    let svg = '<svg viewBox="0 0 ' + w.toFixed(1) + ' ' + h.toFixed(1) + '" class="dp-svg"' +
      ' style="--dp-stroke:' + traitW.toFixed(3) + '" preserveAspectRatio="xMidYMid meet">';
    oreblocs.forEach((b) => {
      let pts = [];
      pts = enTableau(b.sommets); // deja normalise par l'adaptateur
      if (pts.length < 3) return;
      const enCours = (highlightIds || []).includes(b.nom);
      const fill = couleurDestination(b.destination);
      const screenPts = pts.map(([x, y]) => [
        (x - minX).toFixed(1),
        (h - (y - minY)).toFixed(1), // flip vertical : y croissant vers le haut
      ]);
      svg += '<polygon points="' + screenPts.map((p) => p.join(',')).join(' ') +
        '" fill="' + fill + '" fill-opacity=".30" stroke="' + fill + '" stroke-width="' + (traitW * 1.6).toFixed(3) + '"' +
        (enCours ? ' class="dp-ore-mining"' : '') + ' />';
      screenPts.forEach(([sx, sy]) => {
        num++;
        svg += '<text x="' + sx + '" y="' + sy + '" class="dp-svg-num" font-size="' + (w / 70).toFixed(1) + '">' + num + '</text>';
      });
      // etiquette du bloc pres de son centroide
      const cx = screenPts.reduce((s, p) => s + Number(p[0]), 0) / screenPts.length;
      const cy = screenPts.reduce((s, p) => s + Number(p[1]), 0) / screenPts.length;
      const pr = progressionBlocs[b.nom];
      const fs = w / 45;
      svg += '<text x="' + cx.toFixed(1) + '" y="' + (cy - fs * 0.2).toFixed(1) + '" class="dp-svg-label" font-size="' + fs.toFixed(1) + '">' + esc(b.nom) + '</text>';
      if (pr && pr.theo) {
        const pct = Math.round(pr.mine / pr.theo * 100);
        const over = pr.mine > pr.theo;
        svg += '<text x="' + cx.toFixed(1) + '" y="' + (cy + fs * 0.85).toFixed(1) + '" class="dp-svg-sous' + (over ? ' over' : '') + '" font-size="' + (fs * 0.62).toFixed(1) + '">' +
          pct + ' %' + (over ? ' · OVERMINE' : (enCours ? ' · EN COURS' : '')) + '</text>';
        svg += '<text x="' + cx.toFixed(1) + '" y="' + (cy + fs * 1.5).toFixed(1) + '" class="dp-svg-sous2" font-size="' + (fs * 0.55).toFixed(1) + '">' +
          Math.round(pr.mine).toLocaleString('fr-FR') + ' / ' + Math.round(pr.theo).toLocaleString('fr-FR') + ' t</text>';
      }
    });
    svg += '</svg>';
    wrap.innerHTML = svg;
  }

  // Deux types seulement : bloc de minerai (icone) ou bloc sterile (« sterile »), d'apres le drapeau `minerai`
  // de la passerelle (0 = sterile, absent = minerai).
  const estSterile = (b) => b && (b.minerai === 0 || b.minerai === '0' || b.minerai === false);
  function pastilleMatiere(b) {
    return estSterile(b)
      ? '<span class="dp-matiere waste" title="Bloc stérile"><i class="fas fa-mountain"></i></span>'
      : '<span class="dp-matiere ore" title="Bloc de minerai"><i class="fas fa-gem"></i></span>';
  }

  function rendreTable(oreblocs) {
    const tbody = $('#dpTableBody');
    if (!tbody) return;
    if (!oreblocs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="dp-note">-</td></tr>';
      return;
    }
    tbody.innerHTML = oreblocs.map((b) => {
      const dest = (b.destination || '').toUpperCase();
      return '<tr' + (dest && dest !== 'W' ? ' class="dp-row-ore"' : '') + '>' +
        '<td>' + pastilleMatiere(b) + esc(b.nom) + '</td>' +
        '<td>' + esc(b.volume || 0) + '</td>' +
        '<td>' + esc(b.tonnes || 0) + '</td>' +
        '<td>' + esc(b.au_teneur || 0) + '</td>' +
        '<td>' + esc(b.density || 0) + '</td>' +
        '<td>' + esc(b.destination || '-') + '</td>' +
        '<td>' + esc(b.litho || '-') + '</td>' +
        '</tr>';
    }).join('');
  }

  /* ---- Graph ONLINE "Statut de blast" ----
   * Le theorique (tonnes Geosum) vient de la liste locale d'oreblocs ; le
   * mine (materiau effectivement deplace) vient de la passerelle
   * (/api/progression_blast) et est rafraichi toutes les 5 s. Une barre
   * double par bloc : mine (sombre, sur la piste) vs theorique (clair). */
  let diagTimer = null;

  function rendreDiagramme(oreblocs) {
    const el = $('#dpDiagram');
    if (!el) return;
    if (!oreblocs.length) {
      el.innerHTML = '';
      if (diagTimer) { clearInterval(diagTimer); diagTimer = null; }
      progressionBlocs = {};
      const p0 = $('#dpAvancementPct'), t0 = $('#dpAvancementTonnes'), b0 = $('#dpAvancementBarre');
      if (p0) p0.textContent = '--'; if (t0) t0.textContent = ''; if (b0) b0.style.width = '0';
      rendreOvermine([]);
      return;
    }
    el.innerHTML = '<div class="dp-diag-legende">' +
      '<span><span class="dp-leg-mine"></span> Mine (reel)</span>' +
      '<span><span class="dp-leg-theo"></span> Theorique (plan)</span>' +
      '</div>' +
      '<div class="dp-diag-barres" id="dpDiagBarres"></div>';
    // premier rendu + polling passeur
    majDiagrammeOnline();
    if (diagTimer) clearInterval(diagTimer);
    diagTimer = setInterval(() => {
      if (!document.hidden) majDiagrammeOnline();
    }, 5000);
  }

  async function majDiagrammeOnline() {
    const box = $('#dpDiagBarres');
    if (!box) return;
    const b = blastCourant();
    if (!b) { box.innerHTML = '<div class="dp-note">Selectionnez un blast.</div>'; return; }
    const oreblocs = (b.oreblocs) || [];
    if (!oreblocs.length) { box.innerHTML = ''; return; }

    // theorique par nom de bloc (liste locale, source Geosum)
    const theo = {};
    oreblocs.forEach((ob) => { theo[ob.nom] = Number(ob.tonnes) || 0; });

    let mine = {};
    try {
      const d = await jgetPasserelle('/api/progression_blast?blast_id=' + b.id);
      (d.progression || []).forEach((r) => { mine[r.bloc] = Number(r.tonnes) || 0; });
    } catch (e) {
      // passerelle injoignable : on garde l'affichage theorique seul
    }

    const noms = Object.keys(theo);
    // avancement du blast : total mine / total theorique (le mine d'un bloc est plafonne a son theorique
    // pour le pourcentage global : un overmine ne doit pas masquer un bloc encore a miner)
    progressionBlocs = {};
    noms.forEach((n) => { progressionBlocs[n] = { mine: mine[n] || 0, theo: theo[n] }; });
    const totTheo = noms.reduce((s, n) => s + theo[n], 0);
    const totMine = noms.reduce((s, n) => s + Math.min(mine[n] || 0, theo[n] || (mine[n] || 0)), 0);
    const pctGlobal = totTheo ? Math.round(totMine / totTheo * 100) : 0;
    const elPct = $('#dpAvancementPct'), elT = $('#dpAvancementTonnes'), elB = $('#dpAvancementBarre');
    if (elPct) elPct.textContent = totTheo ? pctGlobal + ' %' : '--';
    if (elT) elT.textContent = totTheo
      ? Math.round(totMine).toLocaleString('fr-FR') + ' / ' + Math.round(totTheo).toLocaleString('fr-FR') + ' t minées' : '';
    if (elB) elB.style.width = Math.min(100, pctGlobal) + '%';
    const vue = $('#dpProgressView');
    if (vue && !vue.hidden) rendreSvg('dpSvgWrapProgress', oreblocs, derniersEnCours);
    const maxT = Math.max.apply(null, noms.map((n) => Math.max(theo[n], mine[n] || 0))) || 1;
    box.innerHTML = noms.map((nom) => {
      const t = theo[nom];
      const m = mine[nom] || 0;
      const pctT = Math.max(2, Math.round((t / maxT) * 100));
      const pctM = Math.max(0, Math.min(100, Math.round((m / maxT) * 100)));
      const pct = t ? Math.min(100, Math.round((m / t) * 100)) : 0;
      const ok = t ? (m >= t * 0.98) : false; // atteint si >= 98% du plan
      const ob = oreblocs.find((o) => o.nom === nom) || {};
      const couleur = window.OTCouleurs ? OTCouleurs.famille(ob.destination) : '#8d887a';
      return '<div class="dp-bar-row" style="--fam:' + couleur + '">' +
        '<span class="dp-bar-label" title="' + esc(nom) + '">' + pastilleMatiere(ob) + esc(nom) + '</span>' +
        '<div class="dp-bar-track">' +
          '<div class="dp-bar-fill theo" style="width:' + pctT + '%"></div>'
          + '<div class="dp-bar-fill mine" style="width:' + pctM + '%"></div>' +
        '</div>' +
        '<span class="dp-bar-value">' + Math.round(m).toLocaleString('fr-FR') + ' / ' +
          Math.round(t).toLocaleString('fr-FR') + ' t' +
          (t ? ' <em class="dp-pct' + (ok ? ' ok' : '') + '">' + pct + '%</em>' : '') +
        '</span>' +
      '</div>';
    }).join('');

    // --- Overmine : blocs ou le mine depasse le theorique ---
    const over = noms
      .filter((n) => theo[n] > 0 && (mine[n] || 0) > theo[n])
      .map((n) => ({ nom: n, mine: mine[n] || 0, theo: theo[n],
                    exces: (mine[n] || 0) - theo[n] }))
      .sort((a, b2) => b2.exces - a.exces);
    rendreOvermine(over);
  }

  function rendreOvermine(liste) {
    const card = $('#dpOvermineCard');
    const box = $('#dpOvermineListe');
    if (!box) return;
    if (card) card.hidden = !liste || !liste.length;
    const titre = $('#dpOvermineTitre');
    if (titre && liste && liste.length) titre.textContent = 'Overmine · ' + liste.length + (liste.length > 1 ? ' blocs' : ' bloc');
    box.innerHTML = (liste || []).map((o) => {
      const pct = o.theo ? Math.round(o.exces / o.theo * 100) : 0;
      return '<div class="dp-overmine-item">' +
        '<b title="' + esc(o.nom) + '">' + esc(o.nom) + ' : ' + Math.round(o.mine).toLocaleString('fr-FR') + ' t minées / ' +
          Math.round(o.theo).toLocaleString('fr-FR') + ' t théoriques</b>' +
        '<span>+' + Math.round(o.exces).toLocaleString('fr-FR') + ' t · dépassement de ' + pct + ' %</span>' +
      '</div>';
    }).join('');
  }

  function jgetPasserelle(path) {
    return fetch(base() + path).then((r) => {     // 28/11 : meme adresse que le reste (lien en ligne compris)
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function rendreProgress(oreblocs) {
    const el = $('#dpProgressSummary');
    if (!el) return;
    if (!oreblocs.length) {
      el.innerHTML = '<div class="dp-note">Aucun bloc pour ce blast.</div>';
      rendreSvg('dpSvgWrapProgress', [], []);
      return;
    }
    const somme = (l, k) => l.reduce((s, b) => s + (Number(b[k]) || 0), 0);
    const fmtN = (v, d) => v.toLocaleString('fr-FR', { maximumFractionDigits: d });
    const ore = oreblocs.filter((b) => !estSterile(b));
    const ster = oreblocs.filter(estSterile);
    const carte = (valeur, libelle, detail) =>
      '<div class="dp-kpi"><b>' + valeur + '</b><span>' + libelle + '</span>' + (detail ? '<small>' + detail + '</small>' : '') + '</div>';
    el.innerHTML = '<div class="dp-progress-kpis">' +
      carte(oreblocs.length, 'Blocs', ore.length + ' minerai · ' + ster.length + ' stérile' + (ster.length > 1 ? 's' : '')) +
      carte(fmtN(somme(oreblocs, 'tonnes'), 0) + ' t', 'Tonnes totales', fmtN(somme(ore, 'tonnes'), 0) + ' t minerai · ' + fmtN(somme(ster, 'tonnes'), 0) + ' t stérile') +
      carte(fmtN(somme(oreblocs, 'volume'), 0) + ' m³', 'Volume total', fmtN(somme(ore, 'volume'), 0) + ' m³ minerai · ' + fmtN(somme(ster, 'volume'), 0) + ' m³ stérile') +
      carte(somme(ore, 'tonnes') ? fmtN(somme(ore.map((b) => ({ x: (Number(b.tonnes) || 0) * (Number(b.au_teneur) || 0) })), 'x') / somme(ore, 'tonnes'), 2) : '--',
        'Teneur moyenne', 'Au_ok · minerai') +
      '</div>';
    rafraichirEngins();
  }

  /* ================ Minage en temps reel (onglet Progress) ================
   * Deux canaux combines (decision metier) :
   *   A - declaration du spotter (minage_actuel, fraiche < 30 min)
   *   D - preuve d'activite : dernier voyage du bloc < 15 min
   * Toute info plus vieille est MORTE : pas de clignotement fantome
   * (lecon du 07/11 : des positions OMEX de 95 min faisaient clignoter
   * un bloc sans aucune pelle qui emet). Les trames GNSS de plus de
   * 10 min ne participent plus (canal C, quand le materiel existera). */
  const TIMEOUT_TRAME_MIN = 10;       // trames GNSS : mortes apres 10 min
  const TIMEOUT_DECLARATION_MIN = 30; // declaration spotter : morte apres 30 min
  const TIMEOUT_VOYAGE_MIN = 15;      // preuve d'activite : voyage < 15 min

  let enginsPollTimer = null;

  function ageMinutes(horodatage, maintenant) {
    const t = new Date(String(horodatage || '').replace(' ', 'T') + 'Z').getTime();
    if (isNaN(t)) return Infinity;
    const ref = maintenant ? maintenant.getTime() : Date.now();
    return (ref - t) / 60000;
  }

  async function rafraichirEngins() {
    const b = blastCourant();
    const oreblocs = (b && b.oreblocs) || [];
    if (!oreblocs.length) return;
    const enCoursIds = [];
    try {
      const r = await fetch(base() + '/api/minage_actuel');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      const maintenant = d.maintenant ? new Date(String(d.maintenant).replace(' ', 'T') + 'Z') : null;

      // Canal A : declarations du spotter fraiches uniquement
      (d.declarations || []).forEach((dec) => {
        if (ageMinutes(dec.declared_at, maintenant) <= TIMEOUT_DECLARATION_MIN) {
          const ob = oreblocs.find((o) => Number(o.id) === Number(dec.orebloc_id) || o.nom === dec.bloc);
          if (ob && enCoursIds.indexOf(ob.nom) < 0) enCoursIds.push(ob.nom);
        }
      });

      // Canal D : preuve d'activite (voyage recent sur le bloc)
      (d.activite || []).forEach((act) => {
        if (ageMinutes(act.dernier_voyage, maintenant) <= TIMEOUT_VOYAGE_MIN) {
          const ob = oreblocs.find((o) => Number(o.id) === Number(act.orebloc_id) || o.nom === act.bloc);
          if (ob && enCoursIds.indexOf(ob.nom) < 0) enCoursIds.push(ob.nom);
        }
      });

      // Canal C (futur) : positions GNSS fraiches seulement (< 10 min).
      // Une pelle OMEX silencieuse depuis plus longtemps ne clignote plus.
      try {
        const engins = await API().engins();
        const excas = engins.filter((e) => e.type === 'EXCA' && e.statut === 'OMEX'
          && ageMinutes(e.derniere_maj, maintenant) <= TIMEOUT_TRAME_MIN);
        excas.forEach((exca) => {
          oreblocs.forEach((ob) => {
            const pts = enTableau(ob.sommets);
            if (pts.length < 3) return;
            if (pointDansPolygone(exca.x, exca.y, pts) && enCoursIds.indexOf(ob.nom) < 0) {
              enCoursIds.push(ob.nom);
            }
          });
        });
      } catch (e2) { /* engins indisponibles : les canaux A+D suffisent */ }

      derniersEnCours = enCoursIds;
      rendreSvg('dpSvgWrapProgress', oreblocs, enCoursIds);
    } catch (e) {
      derniersEnCours = [];
      rendreSvg('dpSvgWrapProgress', oreblocs, []);
    }
  }

  function demarrerPollEngins() {
    arreterPollEngins();
    rafraichirEngins();
    enginsPollTimer = setInterval(rafraichirEngins, 4000);
  }

  function arreterPollEngins() {
    if (enginsPollTimer) { clearInterval(enginsPollTimer); enginsPollTimer = null; }
  }

  /* ================================== Onglets ====================================== */

  function activerTab(nom) {
    const isMap = nom === 'map';
    $('#dpTabMap').classList.toggle('active', isMap);
    $('#dpTabProgress').classList.toggle('active', !isMap);
    $('#dpMapView').hidden = !isMap;
    $('#dpProgressView').hidden = isMap;
    if (isMap) arreterPollEngins();
    else demarrerPollEngins();
  }

  /* =================================== Init ========================================= */

  function brancherEvenements() {
    $('#dpSrcPrev').addEventListener('click', () => naviguerSource(-1));
    $('#dpSrcNext').addEventListener('click', () => naviguerSource(1));
    $('#dpTeneursRow').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-teneur]');
      if (!btn) return;
      teneurSelectionnee = btn.getAttribute('data-teneur');
      chargerTeneurs();
    });

    $('#dpBtnSelect').addEventListener('click', ouvrirSelectModal);
    $('#dpSelectCancel').addEventListener('click', fermerSelectModal);
    $('#dpSelectOverlay').addEventListener('click', (e) => {
      if (e.target.id === 'dpSelectOverlay') fermerSelectModal();
    });
    $('#dpBlastList').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-blast]');
      if (!btn) return;
      // id garde en chaine : numerique cote SG, numero de blast cote OT
      blastSelectionne = btn.getAttribute('data-blast');
      blastAvantCreation = null; // choix explicite : la creation eventuelle est abandonnee
      mode = 'consultation';     // on visualise le blast choisi, pas de saisie
      rendreForm();
      rendrePlan();
      rendreBoutons();
      fermerSelectModal();
    });
    $('#dpBtnNew').addEventListener('click', nouveauBlast);
    $('#dpBtnEdit').addEventListener('click', modifierBlast);
    $('#dpBtnDelete').addEventListener('click', supprimerBlast);
    const btnClore = $('#dpBtnClore');
    if (btnClore) btnClore.addEventListener('click', cloreBlast);
    $('#dpForm').addEventListener('submit', sauverBlast);
    $('#dpBtnCancel').addEventListener('click', annulerForm);
    $('#dpName').addEventListener('input', majLabelsFichier);

    CHAMPS_FICHIER.forEach((cle) => {
      const trigger = $('#dpName' + capitale(cle) + 'Trigger');
      const input = $('#dpName' + capitale(cle) + 'Input');
      if (!trigger || !input) return;
      trigger.addEventListener('click', () => input.click());
      input.addEventListener('change', (e) => choisirFichier(cle, e.target.files && e.target.files[0]));
    });

    $('#dpTabMap').addEventListener('click', () => activerTab('map'));
    $('#dpTabProgress').addEventListener('click', () => activerTab('progress'));
  }

  /* ---- Equipe de service (jour / nuit) : memes parametres equipe_jour / equipe_nuit que Senor et Home ---- */
  async function chargerService() {
    const sj = $('#dpEquipeJour'), sn = $('#dpEquipeNuit');
    const g = window.API;     // `API` est ici la fonction du backend Dig Plan : on lit l'objet global
    if (!sj || !sn || !g) return;
    const st = $('#dpServiceStatus');
    try {
      const [cfg, ident] = await Promise.all([g.getConfig(), g.getRows('sync_identifiants')]);
      const p = (cfg && (cfg.params || cfg)) || {};
      const codes = Array.from(new Set(((ident && ident.rows) || [])
        .filter((r) => r.profil === 'SPOT' && r.actif === 1).map((r) => r.equipe).filter(Boolean))).sort();
      servicePersonnel = ((ident && ident.rows) || []).filter((r) => r.profil === 'SPOT' && r.actif === 1);
      [[sj, p.equipe_jour || ''], [sn, p.equipe_nuit || '']].forEach(([sel, val]) => {
        const liste = val && codes.indexOf(val) < 0 ? codes.concat(val) : codes;
        sel.innerHTML = '<option value="">Non définie</option>' +
          liste.map((c) => '<option value="' + esc(c) + '"' + (c === val ? ' selected' : '') + '>' + esc(c) + '</option>').join('');
      });
      if (st) st.textContent = '';
      rendreService();
    } catch (e) {
      if (st) st.textContent = 'Connexion requise';
    }
  }

  // Carte « Equipe de service » : le shift en cours est mis en avant, avec les membres de son equipe.
  let servicePersonnel = [];
  function rendreService() {
    const sj = $('#dpEquipeJour'), sn = $('#dpEquipeNuit');
    if (!sj || !sn) return;
    const h = new Date().getHours();
    const jour = h >= 6 && h < 18;
    const rj = $('#dpSvcJour'), rn = $('#dpSvcNuit');
    if (rj) rj.classList.toggle('actif', jour);
    if (rn) rn.classList.toggle('actif', !jour);
    const code = (jour ? sj : sn).value;
    const shift = $('#dpServiceShift');
    if (shift) shift.textContent = (jour ? 'Shift de jour' : 'Shift de nuit') + ' · ' + (code || 'non définie');
    const box = $('#dpServiceMembres');
    if (!box) return;
    const membres = servicePersonnel.filter((r) => r.equipe === code)
      .sort((a, b) => (String(b.role).toUpperCase() === 'CHEF_EQUIPE') - (String(a.role).toUpperCase() === 'CHEF_EQUIPE'));
    box.innerHTML = code && membres.length
      ? membres.map((m) => '<span class="dp-svc-membre' + (String(m.role).toUpperCase() === 'CHEF_EQUIPE' ? ' chef' : '') + '">' +
          esc(m.nom || m.identifiant) + (String(m.role).toUpperCase() === 'CHEF_EQUIPE' ? ' · chef' : '') + '</span>').join('')
      : '';
  }

  async function enregistrerService(cle, valeur) {
    const st = $('#dpServiceStatus');
    try {
      await window.API.saveConfig({ [cle]: valeur });
      if (st) st.textContent = 'Équipe de service enregistrée.';
    } catch (e) {
      if (st) st.textContent = 'Enregistrement impossible : ' + ((e && e.message) || 'serveur indisponible');
    }
  }

  async function init() {
    if (!$('#page-digplan')) return;
    brancherEvenements();
    chargerService();
    const sj = $('#dpEquipeJour'), sn = $('#dpEquipeNuit');
    if (sj) sj.addEventListener('change', () => { rendreService(); enregistrerService('equipe_jour', sj.value); });
    if (sn) sn.addEventListener('change', () => { rendreService(); enregistrerService('equipe_nuit', sn.value); });
    // sequentiel : chargerBlasts filtre sur la source courante, qui doit
    // donc etre connue avant (en parallele la liste partait non filtree).
    await chargerSources();
    await chargerBlasts();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
