/* OreTracking V2 - Liaison UI <-> Serveur
 * Charge apres api.js. Branches sur :
 *   - page Settings : URL API, test connexion, sauvegarde des parametres
 *   - page Sync : etat serveur + bouton "Synchroniser maintenant"
 *   - ouverture du dashboard
 */
(function () {
  'use strict';

  function $(sel) { return document.querySelector(sel); }
  function setText(el, t) { if (el) el.textContent = t; }

  function pad(n) { return String(n).padStart(2, '0'); }
  function now() {
    const d = new Date();
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  // ---- choix du backend (App Script / Azure) ----
  function getBackend() {
    const el = $('#cfgBackend');
    return el ? el.value : 'appscript';
  }

  function toggleBackend() {
    const mode = getBackend();
    const gas = $('#backendAppScript');
    const azure = $('#backendAzure');
    if (gas) gas.hidden = (mode !== 'appscript');
    if (azure) azure.hidden = (mode !== 'azure');
  }

  async function testConnection() {
    const status = $('#cfgStatus');
    const btn = $('#btnTestDb');
    if (btn) {
      btn.textContent = 'Test en cours...';
      btn.disabled = true;
    }
    if (status) status.textContent = '';
    // appliquer l'URL saisie avant le test
    const urlInput = $('#cfgApiUrl');
    if (urlInput && urlInput.value) API.setApiBase(urlInput.value);

    // backend azure : pas encore implemente
    if (getBackend() === 'azure') {
      if (btn) {
        btn.textContent = 'Bientot';
        btn.style.background = '#f97316';
      }
      if (status) {
        status.textContent = 'Azure PostgreSQL sera disponible prochainement.';
        status.style.color = '#f97316';
      }
      setTimeout(() => {
        if (btn) { btn.textContent = 'Tester'; btn.style.background = ''; btn.disabled = false; }
      }, 2000);
      return false;
    }

    // backend app script : sante du serveur local + ping reel de l'URL GAS
    try {
      const h = await API.health();
      const gasUrl = $('#gasUrl') ? $('#gasUrl').value.trim() : '';

      if (!gasUrl) {
        if (btn) { btn.textContent = 'Serveur OK'; btn.style.background = '#22c55e'; }
        if (status) {
          status.textContent = 'Serveur ' + (h.server || '') + ' OK - URL App Script manquante';
          status.style.color = '#f97316';
        }
        return true;
      }

      // ping reel de l'App Script
      if (btn) { btn.textContent = 'Test App Script...'; }
      try {
        const gas = await API.testGas(gasUrl);
        const hs = gas.http_status || 0;
        if (hs >= 200 && hs < 400) {
          if (btn) { btn.textContent = 'Connecte !'; btn.style.background = '#22c55e'; }
          if (status) {
            status.textContent = 'App Script joignable (HTTP ' + hs + ', ' + gas.latency_ms + ' ms) - ' + (h.server || 'serveur local') + ' OK';
            status.style.color = '#22c55e';
          }
          return true;
        }
        // URL repond mais deploiement invalide / non autorise
        if (btn) { btn.textContent = 'HTTP ' + hs; btn.style.background = '#f97316'; }
        if (status) {
          status.textContent = 'App Script repond (HTTP ' + hs + ') mais le deploiement semble invalide ou non autorise - ' + (h.server || 'serveur local') + ' OK';
          status.style.color = '#f97316';
        }
        return false;
      } catch (ge) {
        if (btn) { btn.textContent = 'GAS KO'; btn.style.background = '#ef4444'; }
        if (status) {
          status.textContent = 'Serveur local OK mais App Script inaccessible: ' + ge.message;
          status.style.color = '#ef4444';
        }
        return false;
      }
    } catch (e) {
      if (btn) {
        btn.textContent = 'Echec';
        btn.style.background = '#ef4444';
      }
      if (status) {
        status.textContent = 'Echec: ' + e.message;
        status.style.color = '#ef4444';
      }
      return false;
    } finally {
      if (btn) setTimeout(() => {
        btn.textContent = 'Tester';
        btn.style.background = '';
        btn.disabled = false;
      }, 2500);
    }
  }

  async function loadConfigIntoForm() {
    // pre-remplir l'URL API depuis localStorage (prioritaire)
    const urlInput = $('#cfgApiUrl');
    if (urlInput && !urlInput.value) urlInput.value = API.getApiBase();
    try {
      const { params } = await API.getConfig();
      // URL API enregistree sur le serveur (si pas deja definie localement)
      if (urlInput && params.api_url && !localStorage.getItem('oreTrackingApiBase')) {
        urlInput.value = params.api_url;
        API.setApiBase(params.api_url);
      }
      // backend + URL App Script
      const be = $('#cfgBackend');
      if (be && params.backend_mode) be.value = params.backend_mode;
      const gasUrl = $('#gasUrl');
      if (gasUrl && params.gas_url) gasUrl.value = params.gas_url;
      // le token n'est jamais re-affiche : placeholder si deja enregistre
      const gasTok = $('#gasToken');
      if (gasTok && params.gas_token_set === 'true') {
        gasTok.placeholder = 'Token deja enregistre (laisser vide pour garder)';
      }
      // seuil alerte rouge (param passerelle, replique dans params SaaS)
      const seuil = $('#cfgAlerteRouge');
      if (seuil && params.alerte_rouge_min) seuil.value = params.alerte_rouge_min;
      // charge moyenne par voyage (calibrage tonnage)
      const charge = $('#cfgChargementMoyen');
      if (charge && params.charge_moyenne_t) charge.value = params.charge_moyenne_t;
      // couches de synchronisation (booleens stockes en params)
      const couches = {
        syncCouchePull: params.sync_couche_pull,
        syncCoucheLan: params.sync_couche_lan,
        syncCoucheOffice: params.sync_couche_office,
        syncBandeauVisible: params.sync_bandeau_visible,
      };
      Object.entries(couches).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el && val !== undefined && val !== null) el.checked = val === '1';
      });
    } catch (e) { /* serveur hors ligne : garder les valeurs par defaut */ }
    toggleBackend();
  }

  async function saveConfig() {
    const status = $('#cfgStatus');
    const btn = $('#btnSaveConfig');
    const urlInput = $('#cfgApiUrl');
    const apiUrl = urlInput ? urlInput.value.trim() : '';
    if (apiUrl) API.setApiBase(apiUrl);
    const gasTokEl = $('#gasToken');
    const params = {
      api_url: apiUrl,
      backend_mode: getBackend(),
      gas_url: $('#gasUrl') ? $('#gasUrl').value.trim() : '',
    };
    const seuilEl = $('#cfgAlerteRouge');
    if (seuilEl) {
      const v = parseInt(seuilEl.value, 10);
      if (v >= 1 && v <= 120) params.alerte_rouge_min = String(v);
    }
    const chargeEl = $('#cfgChargementMoyen');
    if (chargeEl) {
      const c = parseFloat(chargeEl.value);
      if (c >= 1 && c <= 500) params.charge_moyenne_t = String(c);
    }
    // couches de synchronisation
    params.sync_couche_pull = $('#syncCouchePull') && $('#syncCouchePull').checked ? '1' : '0';
    params.sync_couche_lan = $('#syncCoucheLan') && $('#syncCoucheLan').checked ? '1' : '0';
    params.sync_couche_office = $('#syncCoucheOffice') ? ($('#syncCoucheOffice').checked ? '1' : '0') : '1';
    params.sync_bandeau_visible = $('#syncBandeauVisible') && $('#syncBandeauVisible').checked ? '1' : '0';
    // token seulement s'il est saisi (sinon on garde celui deja enregistre)
    if (gasTokEl && gasTokEl.value.trim()) params.gas_token = gasTokEl.value.trim();
    if (btn) { btn.disabled = true; btn.textContent = 'Enregistrement...'; }
    try {
      const savedTok = !!(gasTokEl && params.gas_token);
      await API.saveConfig(params);
      // etat reel cote serveur : un token peut deja etre enregistre
      let tokenSet = savedTok;
      try {
        const { params: fresh } = await API.getConfig();
        tokenSet = fresh.gas_token_set === 'true';
      } catch (e) { /* garder l'hypothese locale */ }
      if (status) {
        status.textContent = savedTok
          ? 'Parametres enregistres. Token sauvegarde sur le serveur.'
          : 'Parametres enregistres sur le serveur.';
        status.style.color = '#22c55e';
      }
      // jamais de token en clair dans le DOM : placeholder reflete l'etat serveur
      if (gasTokEl) {
        gasTokEl.value = '';
        gasTokEl.placeholder = tokenSet
          ? 'Token deja enregistre (laisser vide pour garder)'
          : 'Optionnel - colle ton token si ton script GAS en verifie un';
      }
    } catch (e) {
      if (status) {
        status.textContent = 'Echec sauvegarde: ' + e.message;
        status.style.color = '#ef4444';
      }
    } finally {
      if (btn) setTimeout(() => { btn.disabled = false; btn.textContent = 'Enregistrer les parametres'; }, 1500);
    }
  }

  async function refreshSyncStatus() {
    const t = $('#syncTime'), s = $('#syncServer'), db = $('#syncDb');
    try {
      const h = await API.health();
      const sync = await API.sync();
      setText(t, 'Derniere sync: ' + (sync.synced_at || now()));
      setText(s, 'Serveur: Connecte (' + API.getApiBase() + ')');
      setText(db, 'Base de donnees: OK');
      if (t) t.style.color = ''; if (s) s.style.color = ''; if (db) db.style.color = '';
    } catch (e) {
      setText(t, 'Derniere sync: -');
      setText(s, 'Serveur: Hors ligne');
      setText(db, 'Base de donnees: inaccessible');
      if (s) s.style.color = '#ef4444';
      if (db) db.style.color = '#ef4444';
    }
  }

  async function doSync() {
    const btn = $('#btnSyncNow');
    if (btn) { btn.disabled = true; btn.textContent = 'Synchronisation...'; }
    await refreshSyncStatus();
    try { await API.stats(); } catch (e) { /* offline tolerated */ }
    if (btn) {
      btn.textContent = 'Synchronise !';
      setTimeout(() => { btn.textContent = 'Synchroniser maintenant'; btn.disabled = false; }, 1500);
    }
  }

  function openDashboard() {
    // dashboard servi par le meme serveur ; fallback fichier local
    const base = API.getApiBase();
    window.open(base + '/html/dashboard.html', '_blank');
  }

  // ---- preferences (panneau Settings) ----
  function initPrefsUI() {
    if (typeof PREFS === 'undefined') return;

    // map id -> cle PREFS
    var map = {
      cfgTheme: 'theme',
      cfgAnimations: 'animations',
      cfgFontSize: 'fontSize',
      cfgHighContrast: 'highContrast',
      cfgReducedMotion: 'reducedMotion',
      cfgLang: 'lang',
      cfgUnits: 'units',
      cfgDateFormat: 'dateFormat',
      cfgNumberFormat: 'numberFormat',
      cfgTimezone: 'timezone',
      cfgSound: 'sound',
      cfgHomePage: 'homePage',
      cfgModeSim: 'modeSim',
      cfgRefresh: 'refresh'
    };

    // pre-remplir depuis PREFS
    Object.keys(map).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var val = PREFS.get(map[id]);
      if (el.type === 'checkbox') el.checked = !!val;
      else el.value = val;
    });

    // ecouter les changements
    Object.keys(map).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var key = map[id];
      var evt = (el.type === 'checkbox' || el.tagName === 'SELECT') ? 'change' : 'input';
      el.addEventListener(evt, function () {
        var v = (el.type === 'checkbox') ? el.checked : el.value;
        if (key === 'refresh' || key === 'fontSize') v = parseInt(v, 10) || (key === 'fontSize' ? 100 : 5);
        PREFS.set(key, v);
      });
    });
  }

  // ---- equipements ----
  function eqEscape(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---- table engins : source unique des DT (Settings > Driving Truck) ----
  // ancien referentiel equipements (id numerique) -> code DT, pour traduire
  // les affectations GNSS enregistrees avant la migration
  const legacyTruckLabels = {};

  // familles de camions (codes arretes avec l'utilisateur) :
  // 1500/785/ADT => prefixe DT, tout le reste prend les 4 premieres lettres
  const FAMILLES_DT = ['1500', '785', 'ADT'];

  // libelle affiche : "DT-01" / "EX-001" tels quels, sinon prefixe + cible
  function enginLabel(type, target) {
    const prefix = type === 'truck' ? 'DT' : 'EXC';
    let t = String(target == null ? '' : target);
    if (type === 'truck' && legacyTruckLabels[t]) t = legacyTruckLabels[t];
    if (/^[A-Za-z]{2,6}-?\d/.test(t) || t.indexOf(prefix) === 0) return t;
    return prefix + ' ' + t;
  }
  // types simples (liste standard)

  const EQUIP_TYPES = {

    truck: {
      container: 'truckContainer',
      form: 'truckAddForm',
      statusId: 'truckStatus',
      icon: 'fa-truck',
      // flotte miroir du Google Sheet, familles DT uniquement
      familles: FAMILLES_DT.concat(['AUTRE']),
    },
    excavator: {
      container: 'excavatorContainer',
      form: 'excavatorAddForm',
      statusId: 'excavatorStatus',
      icon: 'fa-digging',
      familles: ['EXCAVATOR', 'EX', 'CAT', 'HITACHI', 'HYUNDAI', 'HYUN', 'KOMATSU',
                 'KOM', 'LIEBHERR', 'LIEB', 'VOLVO', 'VOLV', 'DOOSAN', 'PC',
                 'AUTRE'],
    },
  };
  function equipStatus(elId, msg, ok) {
    const el = $('#' + elId);
    if (!el) return;
    el.textContent = msg;
    el.className = 'account-status ' + (ok ? 'ok' : 'err');
    // erreurs plus visibles / plus longues que les succes
    if (msg) setTimeout(() => { el.textContent = ''; el.className = 'account-status'; }, ok ? 4000 : 8000);
  }

  // Affiche/masque les formulaires d'ajout selon l'etat d'authentification.
  // Sans session, un 401 silencieux passait inapercu : mieux vaut cacher le +.
  function setEquipAuthUi(authenticated) {
    const forms = ['#teamAddForm', '#truckAddForm', '#excavatorAddForm']
      .map((s) => document.querySelector(s))
      .filter(Boolean);
    forms.forEach((f) => {
      f.style.display = authenticated ? '' : 'none';
    });
    const btnF = document.getElementById('btnTruckFactors');
    if (btnF) btnF.style.display = authenticated ? '' : 'none';
  }

  // ============ TEAMS (Spotter) ============
  // Source de verite : onglet `identifiants` du Google Sheet (miroir local
  // sync_identifiants). Un membre = une ligne (profil, identifiant, nom,
  // role, equipe, actif). L'equipe est un attribut du membre : la carte
  // d'equipe regroupe les lignes a la volée. Une team est creee AVEC son
  // chef d'equipe (premiere ligne) — jamais vide.

  const ROLE_LIBELLE = {
    'SPOTER': 'Spotter',
    'SPOTTER': 'Spotter',
    'CHEF_EQUIPE': 'Chef d\'equipe',
  };

  function loadMembresSync() {
    return API.getRows('sync_identifiants').then((d) => d.rows || []);
  }

  function coderMembre(id, nom) {
    // identifiant lisible : nom sans espaces doubles/tirets etranges
    return String(id || nom || '').trim();
  }

  function coderEngin(famille, numero) {
    const f = String(famille || '').trim().toUpperCase();
    const prefixe = (f === '1500' || f === '785' || f === 'ADT') ? 'DT' : f.substring(0, 4);
    return prefixe + '-' + String(parseInt(numero, 10)).padStart(2, '0');
  }

  // couleur stable deduite du nom d'equipe (palette padoniquement claire,
  // visible sur fond sombre). Deux equipes differentes ne partagent la meme
  // couleur que si leur nom hache pareil.
  const TEAM_COLORS = ['#3b82f6', '#06b6d4', '#22c55e', '#eab308', '#f97316', '#a855f7', '#ec4899', '#14b8a6'];
  function couleurEquipe(nom) {
    let h = 0;
    const s = String(nom || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return TEAM_COLORS[h % TEAM_COLORS.length];
  }

  function renderTeams(rows) {
    const box = $('#teamContainer');
    if (!box) return;
    const membres = (rows || []).filter((r) => r.profil === 'SPOT');
    if (!membres.length) {
      box.innerHTML = '<div class="empty-state"><i class="fas fa-users"></i><span>Aucune equipe creee. Utilisez "Nouvelle equipe" ci-dessous.</span></div>';
      return;
    }
    // regroupement par code d'equipe (champ `equipe` du membre)
    const equipes = {};
    membres.forEach((r) => {
      const cle = r.equipe || '(sans equipe)';
      (equipes[cle] = equipes[cle] || []).push(r);
    });
    const cles = Object.keys(equipes).sort();
    box.innerHTML = '<div class="team-grid">' + cles.map((cle) => {
      const liste = equipes[cle];
      const active = liste.some((r) => r.actif === 1);
      const chef = liste.find((r) => (r.role || '').toUpperCase() === 'CHEF_EQUIPE');
      const couleur = (liste.find((r) => r.couleur) || {}).couleur || couleurEquipe(cle);
      const nomChef = chef ? (chef.nom || chef.identifiant || '') : '';

      let html = '<div class="team-card' + (active ? '' : ' desactive') + '" data-equipe="' + eqEscape(cle) + '" style="--team-color:' + couleur + '">' +
        '<div class="team-head">' +
          '<span class="team-badge">' + eqEscape(cle.split('-')[0] || cle) + '</span>' +
          '<div class="team-head-info">' +
            '<div class="team-title">' +
              '<span class="team-nom" title="' + eqEscape(cle) + '">' + eqEscape(cle) + '</span>' +
              '<span class="team-state">' + (active ? 'Active' : 'Inactive') + '</span>' +
            '</div>' +
            '<div class="team-meta">' +
              '<i class="fas fa-star"></i>' +
              '<span class="chef-ligne">' + (nomChef
                ? 'Chef : <span class="chef-nom">' + eqEscape(nomChef) + '</span>'
                : '<em>Aucun chef d\'equipe nomme</em>') + '</span>' +
              ' &middot; <i class="fas fa-user-friends"></i> ' + liste.length + (liste.length > 1 ? ' membres' : ' membre') +
            '</div>' +
          '</div>' +
          '<span class="team-actions">' +
            '<button class="team-btn" data-action="team-toggle" data-equipe="' + eqEscape(cle) + '" data-active="' + active + '">' +
              '<i class="fas fa-' + (active ? 'pause' : 'play') + '"></i> ' + (active ? 'Desactiver' : 'Activer') + '</button>' +
            '<button class="team-btn danger" data-action="team-delete" data-equipe="' + eqEscape(cle) + '">' +
              '<i class="fas fa-trash"></i> Supprimer</button>' +
          '</span>' +
        '</div>';
      html += '<div class="team-body">';
      html += '<div class="team-members">';
      if (!liste.length) {
        html += '<div class="member-empty">Equipe vide</div>';
      } else {
        html += liste.map((r) => {
          const estChef = (r.role || '').toUpperCase() === 'CHEF_EQUIPE';
          const roleLb = ROLE_LIBELLE[String(r.role || '').toUpperCase()] || (r.role || 'Membre');
          return '<div class="team-member"' + (r.actif === 1 ? '' : ' style="opacity:0.5"') + '>' +
            '<i class="fas fa-' + (estChef ? 'user-tie' : 'user') + '"></i>' +
            '<span class="member-name">' + eqEscape(r.nom || r.identifiant) + '</span>' +
            '<span class="member-role' + (estChef ? ' chef' : '') + '">' + eqEscape(roleLb) + '</span>' +
            '<button class="account-btn" data-action="member-remove" data-id="' + r.id + '" title="Retirer cette personne de l\'equipe">' +
              '<i class="fas fa-times"></i></button>' +
          '</div>';
        }).join('');
      }
      html += '</div>';
      html += '<form class="member-add-form" data-equipe="' + eqEscape(cle) + '">' +
        '<input type="text" class="form-input-sm" placeholder="Ajouter une personne : son nom" name="mname" required />' +
        '<select class="form-select-sm" name="mrole">' +
          '<option value="SPOTER">Spotter</option>' +
          '<option value="CHEF_EQUIPE">Chef d\'equipe</option>' +
          '<option value="OPERATEUR">Operateur</option>' +
        '</select>' +
        '<button type="submit" class="btn-add-xs"><i class="fas fa-plus"></i> Ajouter</button>' +
      '</form>';
      html += '</div></div>';
      return html;
    }).join('') + '</div>';
  }

  function equipListError(e) {
    if (e && (e.status === 401 || /authentification/i.test(e.message || ''))) {
      return 'Connectez-vous pour gerer les equipements';
    }
    return 'Serveur indisponible';
  }

  async function loadTeams() {
    try {
      const rows = await loadMembresSync();
      renderTeams(rows);
      setEquipAuthUi(true);
    } catch (e) {
      const box = $('#teamContainer');
      if (box) box.innerHTML = '<div class="account-item muted">' + equipListError(e) + '</div>';
      if (e && e.status === 401) setEquipAuthUi(false);
    }
  }

  async function addTeam(e) {
    e.preventDefault();
    const equipe = $('#newTeamName').value.trim();
    const chefNom = $('#newTeamChef').value.trim();
    const couleur = ($('#newTeamColor') ? $('#newTeamColor').value : '') || '';
    if (!equipe) { equipStatus('teamStatus', 'Nom de team requis', false); return; }
    if (!chefNom) { equipStatus('teamStatus', 'Chef d\'equipe requis', false); return; }
    try {
      // une team nait avec son chef (regle de gestion) : premier membre.
      // La couleur choisie est posee sur chaque ligne de l'equipe.
      const sp = Splash.bloquer('Creation de l\'equipe...', equipe);
      Splash.etape('Enregistrement du chef d\'equipe...');
      await API.insertRow('sync_identifiants', {
        profil: 'SPOT', identifiant: coderMembre('', chefNom), nom: chefNom,
        role: 'CHEF_EQUIPE', equipe: equipe, actif: 1, couleur: couleur,
      });
      Splash.etape('Publication vers le cloud...');
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch (e2) {}
      Splash.succes('Equipe creee', equipe + ' - chef : ' + chefNom);
      equipStatus('teamStatus', 'Team creee (chef: ' + chefNom + ')', true);
      e.target.reset();
      if ($('#newTeamColor')) $('#newTeamColor').value = '#3b82f6';
      await loadTeams();
    } catch (err) { Splash.erreur('Creation impossible', err.message); equipStatus('teamStatus', 'Erreur: ' + err.message, false); }
  }

  async function addMember(equipe, e) {
    e.preventDefault();
    const form = e.target;
    const nom = form.mname.value.trim();
    const role = form.mrole.value;
    if (!nom) return;
    try {
      await API.insertRow('sync_identifiants', {
        profil: 'SPOT', identifiant: coderMembre('', nom), nom: nom,
        role: role, equipe: equipe, actif: 1,
      });
      equipStatus('teamStatus', 'Membre ajoute: ' + nom, true);
      await loadTeams();
    } catch (err) { equipStatus('teamStatus', 'Erreur: ' + err.message, false); }
  }

  async function removeMember(id) {
    const sp = Splash.bloquer('Retrait du membre...', '');
    try {
      await API.deleteRow('sync_identifiants', id);
      sp.fermer();
      Splash.succes('Membre retire');
      equipStatus('teamStatus', 'Membre retire', true);
      await loadTeams();
    } catch (err) {
      sp.fermer();
      Splash.erreur('Retrait impossible', err.message);
      equipStatus('teamStatus', 'Erreur: ' + err.message, false);
    }
  }

  async function toggleTeam(equipe, active) {
    const sp = Splash.bloquer(active ? 'Desactivation de l\'equipe...' : 'Activation de l\'equipe...', equipe);
    try {
      const rows = await loadMembresSync();
      const liste = rows.filter((r) => r.profil === 'SPOT' && r.equipe === equipe);
      Splash.etape(liste.length + ' membre(s) a mettre a jour...');
      for (const r of liste) {
        await API.updateRow('sync_identifiants', r.id, { actif: active ? 0 : 1 });
      }
      sp.fermer();
      Splash.succes(active ? 'Equipe desactivee' : 'Equipe activee', equipe);
      equipStatus('teamStatus', active ? 'Team desactivee' : 'Team activee', true);
      await loadTeams();
    } catch (e) {
      sp.fermer();
      Splash.erreur('Operation impossible', e.message);
      equipStatus('teamStatus', 'Erreur: ' + e.message, false);
    }
  }

  async function deleteTeam(equipe) {
    if (!await Splash.confirmer(
        'Supprimer l\'equipe "' + equipe + '" et TOUS ses membres ?',
        { titre: 'Suppression d\'equipe', danger: true,
          details: 'Cette action est irreversible.', libelleOk: 'Supprimer' })) return;
    const sp = Splash.bloquer('Suppression de l\'equipe...', equipe);
    try {
      const rows = await loadMembresSync();
      const liste = rows.filter((r) => r.profil === 'SPOT' && r.equipe === equipe);
      Splash.etape(liste.length + ' membre(s) a supprimer...');
      for (const r of liste) {
        await API.deleteRow('sync_identifiants', r.id);
      }
      Splash.etape('Publication vers le cloud...');
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch (e2) {}
      sp.fermer();
      Splash.succes('Equipe supprimee', equipe);
      equipStatus('teamStatus', 'Team supprimee', true);
      await loadTeams();
    } catch (e) {
      sp.fermer();
      Splash.erreur('Suppression impossible', e.message);
      equipStatus('teamStatus', 'Erreur: ' + e.message, false);
    }
  }

  function onTeamAction(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    if (action === 'team-toggle') toggleTeam(btn.dataset.equipe, btn.dataset.active === 'true');
    else if (action === 'team-delete') deleteTeam(btn.dataset.equipe);
    else if (action === 'member-remove') removeMember(btn.dataset.id);
  }

  // ============ MACHINES (Truck / Excavator) ============
  // Source de verite : onglet `flotte` du Google Sheet (miroir sync_flotte).
  // Profil DT -> familles 1500/785/ADT ; profil EXCAVATOR -> famille
  // EXCAVATOR. Une machine = une ligne (code derive famille+numero).
  // Les reglages locaux (reservoir, benne...) restent sur engins : le
  // referentiel machine vit en ligne.

  function loadFlotteSync() {
    return API.getRows('sync_flotte').then((d) => d.rows || []);
  }

  // Rendu « Parametres › Flotte » (maquette du 19/11) : une ligne par engin,
  // 6 visibles puis « consulter le reste ». Les DT ont une colonne « Temps
  // reel » (compte MQTT tenu par la passerelle, voir etatMqtt ci-dessous).
  const VISIBLES_INITIAL = 6;
  const flotteDepliee = { truck: false, excavator: false };
  const dernieresLignes = { truck: [], excavator: [] };

  function renderEquipList(type, rows) {
    const cfg = EQUIP_TYPES[type];
    const box = $('#' + cfg.container);
    if (!box) return;
    const machines = (rows || []).filter((r) => cfg.familles.indexOf(String(r.famille || '').toUpperCase()) >= 0
      || (type === 'truck' && /^DT-/i.test(String(r.code || ''))));
    machines.sort((a, b) => String(a.code || '').localeCompare(String(b.code || ''), undefined, { numeric: true }));
    dernieresLignes[type] = machines;
    const cpt = $(type === 'truck' ? '#cptTrucks' : '#cptExcavators');
    if (cpt) cpt.textContent = String(machines.length).padStart(2, '0') + ' engin' + (machines.length > 1 ? 's' : '') + ' enregistré' + (machines.length > 1 ? 's' : '');
    if (!machines.length) {
      box.innerHTML = '<div class="pm-vide">Aucun engin</div>';
      majVoirPlus(type, 0);
      return;
    }
    const visibles = flotteDepliee[type] ? machines : machines.slice(0, VISIBLES_INITIAL);
    box.innerHTML = visibles.map((r) => {
      const active = Number(r.actif) === 1;
      const code = eqEscape(r.code || ('?-' + r.numero));
      let html = '<div class="pm-ligne' + (active ? '' : ' desactive') + '" data-id="' + r.id + '" data-code="' + code + '">' +
        '<span class="pm-code">' + code + '</span>' +
        '<span class="pm-famille">' + eqEscape(r.famille || '') + (active ? '' : ' <em>· désactivé</em>') + '</span>';
      if (type === 'truck') html += '<span class="pm-tr" data-tr="' + code + '">' + celluleTempsReel(r.code, active) + '</span>';
      html += '<span class="pm-actions">' +
        '<button type="button" class="pm-menu-btn" data-eq="' + type + '" data-action="menu" aria-haspopup="true" title="Actions">…</button>' +
        '<span class="pm-menu" hidden>' +
          '<button type="button" data-eq="' + type + '" data-action="toggle" data-id="' + r.id + '" data-active="' + active + '">' + (active ? 'Désactiver' : 'Activer') + '</button>' +
          (type === 'truck' && active ? '<button type="button" data-eq="truck" data-action="renouveler" data-code="' + code + '">Renouveler l\'accès temps réel</button>' : '') +
          '<button type="button" class="danger" data-eq="' + type + '" data-action="delete" data-id="' + r.id + '">Supprimer</button>' +
        '</span></span></div>';
      return html;
    }).join('');
    majVoirPlus(type, machines.length);
  }

  function majVoirPlus(type, total) {
    const b = $(type === 'truck' ? '#truckVoirPlus' : '#excavatorVoirPlus');
    if (!b) return;
    b.hidden = total <= VISIBLES_INITIAL;
    b.textContent = flotteDepliee[type]
      ? 'Réduire l\'affichage ↑'
      : 'Affichage de ' + VISIBLES_INITIAL + ' engins · consulter le reste de la flotte (' + total + ') →';
  }

  // ---- temps reel (MQTT) : etat des comptes tenu par la passerelle ----
  // GET /api/mqtt/comptes (jamais de mot de passe) ; apres un changement de
  // flotte, POST /api/mqtt/rapprocher pour un effet immediat (sinon la
  // passerelle rattrape d'elle-meme toutes les 30 s).
  const passerelleBase = () => (window.OTCFG && OTCFG.passerelle)
    ? OTCFG.passerelle() : 'http://' + (location.hostname || 'localhost') + ':8101';
  let etatMqtt = null;            // derniere reponse /api/mqtt/comptes

  function celluleTempsReel(code, actif) {
    if (!etatMqtt) return '<span class="pm-tr-etat attente">…</span>';
    if (!etatMqtt.configure) return '<span class="pm-tr-etat absent" title="Broker non installé">—</span>';
    const c = (etatMqtt.comptes || []).find((x) => x.code === code);
    if (!actif) return '<span class="pm-tr-etat inactif" title="DT désactivé : accès coupé">Désactivé</span>';
    if (!c || !c.applique_broker) return '<span class="pm-tr-etat attente" title="Compte en cours de création">En création</span>';
    if (c.en_ligne) {
      const age = c.derniere_position_s;
      return '<span class="pm-tr-etat ok" title="Tablette connectée">En ligne' + (age != null ? ' · ' + (age < 60 ? age + ' s' : Math.round(age / 60) + ' min') : '') + '</span>';
    }
    return '<span class="pm-tr-etat hors" title="Compte prêt, tablette non connectée">Hors ligne</span>';
  }

  function majCellulesTempsReel() {
    dernieresLignes.truck.forEach((r) => {
      const el = document.querySelector('[data-tr="' + CSS.escape(String(r.code || '')) + '"]');
      if (el) el.innerHTML = celluleTempsReel(r.code, Number(r.actif) === 1);
    });
    const bandeau = $('#mqttEtatFlotte');
    if (!bandeau || !etatMqtt) return;
    bandeau.hidden = false;
    const t = bandeau.querySelector('.pm-mqtt-texte');
    let cls = 'ok', msg;
    if (!etatMqtt.configure) {
      cls = 'ko'; msg = 'Temps réel : broker MQTT non installé sur ce poste.';
    } else if (etatMqtt.erreur) {
      cls = 'ko'; msg = 'Temps réel : ' + etatMqtt.erreur;
    } else {
      const enLigne = (etatMqtt.comptes || []).filter((c) => c.en_ligne).length;
      msg = 'Temps réel : ' + (etatMqtt.comptes || []).length + ' accès DT tenus à jour, ' + enLigne + ' tablette(s) en ligne';
      if (etatMqtt.ot_erreur) { cls = 'warn'; msg += ' — envoi à Serveur_OT : ' + etatMqtt.ot_erreur; }
      else if (etatMqtt.ot_envoye_le) msg += ' — accès envoyés à Serveur_OT le ' + etatMqtt.ot_envoye_le;
    }
    bandeau.className = 'pm-mqtt ' + cls;
    t.textContent = msg;
  }

  async function lireEtatMqtt() {
    try {
      const r = await fetch(passerelleBase() + '/api/mqtt/comptes', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      etatMqtt = await r.json();
    } catch (e) {
      etatMqtt = { configure: true, erreur: 'passerelle :8101 injoignable (' + e.message + ')', comptes: [] };
    }
    majCellulesTempsReel();
  }

  async function rapprocherMqtt() {
    try {
      await fetch(passerelleBase() + '/api/mqtt/rapprocher', { method: 'POST', cache: 'no-store' });
    } catch (e) { /* la passerelle rattrape toute seule toutes les 30 s */ }
    lireEtatMqtt();
  }

  async function renouvelerMqtt(code) {
    if (!await Splash.confirmer('Renouveler l\'accès temps réel de ' + code + ' ?\nL\'ancien mot de passe est refusé immédiatement ; la tablette se reconnecte seule sous 2 min.',
        { titre: 'Accès temps réel', libelleOk: 'Renouveler' })) return;
    try {
      const r = await fetch(passerelleBase() + '/api/mqtt/renouveler', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
      const d = await r.json();
      equipStatus('truckStatus', d.ok ? 'Accès de ' + code + ' renouvelé' : 'Renouvellement impossible : ' + (d.erreur || d.broker || '?'), !!d.ok);
    } catch (e) {
      equipStatus('truckStatus', 'Passerelle injoignable : ' + e.message, false);
    }
    lireEtatMqtt();
  }

  function initTempsReel() {
    if (!$('#mqttEtatFlotte')) return;
    lireEtatMqtt();
    setInterval(() => {
      const onglet = document.querySelector('#page-settings .onglet-pane[data-onglet="flotte"]');
      if (onglet && !onglet.hidden) lireEtatMqtt();
    }, 10000);
    ['truck', 'excavator'].forEach((type) => {
      const b = $(type === 'truck' ? '#truckVoirPlus' : '#excavatorVoirPlus');
      if (b) b.addEventListener('click', () => {
        flotteDepliee[type] = !flotteDepliee[type];
        renderEquipList(type, dernieresLignes[type].length ? dernieresLignes[type].concat([]) : []);
      });
    });
    // un menu « … » ouvert se referme au clic ailleurs
    document.addEventListener('click', (e) => {
      if (e.target.closest('.pm-actions')) return;
      document.querySelectorAll('.pm-menu').forEach((m) => { m.hidden = true; });
    });
  }

  // ---- truck factor par famille de DT (17/11) ----
  // Stocke dans params.truck_factor_familles (JSON) ; envoye avec la flotte
  // (colonne truck_factor_t de l'onglet flotte de Serveur_OT).
  let facteursFamilles = {};
  function lireFacteur(v) {
    const n = parseFloat(String(v == null ? '' : v).replace(',', '.'));
    return isFinite(n) && n > 0 ? n : null;
  }
  async function renderTruckFactors(rows) {
    const box = $('#truckFactorList');
    if (!box) return;
    try {
      const { params } = await API.getConfig();
      facteursFamilles = JSON.parse((params && params.truck_factor_familles) || '{}') || {};
    } catch (e) { /* hors ligne : on garde ce qu'on a */ }
    const presentes = new Set(FAMILLES_DT);
    (rows || []).forEach((r) => {
      const f = String(r.famille || '').toUpperCase();
      if (FAMILLES_DT.indexOf(f) >= 0) presentes.add(f);
    });
    box.innerHTML = Array.from(presentes).map((f) => {
      const nb = (rows || []).filter((r) => String(r.famille || '').toUpperCase() === f).length;
      const v = facteursFamilles[f] != null ? facteursFamilles[f] : '';
      return '<div class="account-item truck-factor-row">' +
        '<span class="truck-factor-fam">' + eqEscape(f) + ' <small>(' + nb + ' DT)</small></span>' +
        '<input type="number" class="form-input-sm" min="0.5" max="500" step="0.5" placeholder="global"' +
        ' data-famille="' + eqEscape(f) + '" value="' + eqEscape(String(v)) + '" /> <span>t</span></div>';
    }).join('');
  }
  async function saveTruckFactors() {
    const btn = $('#btnTruckFactors');
    const nouveaux = {};
    let invalide = '';
    document.querySelectorAll('#truckFactorList input[data-famille]').forEach((inp) => {
      if (String(inp.value).trim() === '') return;
      const n = lireFacteur(inp.value);
      if (n === null || n > 500) invalide = invalide || inp.dataset.famille;
      else nouveaux[inp.dataset.famille] = n;
    });
    if (invalide) { equipStatus('truckFactorStatus', 'Valeur invalide pour ' + invalide + ' (0.5 a 500 t)', false); return; }
    if (btn) btn.disabled = true;
    try {
      await API.saveConfig({ truck_factor_familles: JSON.stringify(nouveaux) });
      facteursFamilles = nouveaux;
      let msg = 'Enregistre';
      try {
        const rep = await API.pushGas(['flotte']);
        const r = rep && rep.resultats && rep.resultats.flotte;
        msg = (rep && rep.ok && r && !r.erreur)
          ? 'Enregistre et envoye a Serveur_OT (' + r.ecrites + ' engins)'
          : 'Enregistre ; envoi a Serveur_OT en echec : ' + ((r && r.erreur) || (rep && (rep.errors || []).join(', ')) || '?');
        equipStatus('truckFactorStatus', msg, !!(rep && rep.ok));
      } catch (eP) {
        equipStatus('truckFactorStatus', 'Enregistre ; envoi a Serveur_OT impossible : ' + (eP.message || eP), false);
      }
    } catch (e) {
      equipStatus('truckFactorStatus', 'Echec : ' + (e.message || e), false);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadEquipList(type) {
    const cfg = EQUIP_TYPES[type];
    try {
      const lignes = await loadFlotteSync();
      renderEquipList(type, lignes);
      if (type === 'truck') renderTruckFactors(lignes);
      setEquipAuthUi(true);
    } catch (e) {
      const box = $('#' + cfg.container);
      if (box) box.innerHTML = '<div class="account-item muted">' + equipListError(e) + '</div>';
      const boxF = type === 'truck' ? $('#truckFactorList') : null;
      if (boxF) boxF.innerHTML = '<div class="account-item muted">' + equipListError(e) + '</div>';
      if (e && e.status === 401) setEquipAuthUi(false);
    }
  }

  async function addEquip(type, e) {
    e.preventDefault();
    const cfg = EQUIP_TYPES[type];
    const familleSelect = document.getElementById(type === 'excavator' ? 'newExcavatorFamille' : 'newTruckFamille');
    const familleAutre = document.getElementById(type === 'excavator' ? 'newExcavatorFamilleAutre' : 'newTruckFamilleAutre');
    let famille = (type === 'excavator' ? (familleSelect ? familleSelect.value : 'EXCAVATOR') : (familleSelect ? familleSelect.value : '1500'));
    if (famille === 'AUTRE') {
      const v = (familleAutre && familleAutre.value ? familleAutre.value.trim() : '').toUpperCase();
      if (!v) { equipStatus(cfg.statusId, 'Précisez la famille', false); return; }
      famille = v;
    }
    const numField = e.target.querySelector('input[type="number"]');
    const numero = numField ? parseInt(numField.value, 10) : NaN;
    if (!numero || numero < 1) { equipStatus(cfg.statusId, 'Numero requis', false); return; }
    const code = coderEngin(famille, numero);
    try {
      const rows = await loadFlotteSync();
      if (rows.some((r) => String(r.famille).toUpperCase() === famille.toUpperCase()
          && parseInt(r.numero, 10) === numero)) {
        equipStatus(cfg.statusId, 'Deja en flotte: ' + code, false);
        return;
      }
      const sp = Splash.bloquer('Ajout de la machine...', code);
      await API.insertRow('sync_flotte', { code: code, famille: famille, numero: numero, actif: 1 });
      Splash.etape('Publication vers le cloud...');
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch (eSync) { }
      Splash.succes('Machine ajoutee', code);
      equipStatus(cfg.statusId, 'Ajouté : ' + code + (type === 'truck' ? ' — accès temps réel en cours de création' : ''), true);
      if (type === 'truck') rapprocherMqtt();
      e.target.reset();
      if (familleAutre) { familleAutre.value = ''; familleAutre.style.display = 'none'; }
      if (familleSelect) familleSelect.value = type === 'excavator' ? 'EXCAVATOR' : '1500';
      await loadEquipList(type);
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch(err) { /* sync best effort */ }
    } catch (err) { equipStatus(cfg.statusId, 'Erreur: ' + err.message, false); }
  }

  async function toggleEquip(type, id, active) {
    try {
      await API.updateRow('sync_flotte', id, { actif: active ? 0 : 1 });
      equipStatus(EQUIP_TYPES[type].statusId, active ? 'Désactivé' + (type === 'truck' ? ' — accès temps réel coupé' : '') : 'Activé', true);
      await loadEquipList(type);
      if (type === 'truck') rapprocherMqtt();
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch(err) { /* sync best effort */ }
    } catch (e) { equipStatus(EQUIP_TYPES[type].statusId, 'Erreur: ' + e.message, false); }
  }

  async function deleteEquip(type, id) {
    if (!await Splash.confirmer('Supprimer cet engin de la flotte ?',
        { titre: 'Suppression de machine', danger: true, libelleOk: 'Supprimer' })) return;
    try {
      await API.deleteRow('sync_flotte', id);
      equipStatus(EQUIP_TYPES[type].statusId, 'Supprimé' + (type === 'truck' ? ' — accès temps réel supprimé' : ''), true);
      await loadEquipList(type);
      if (type === 'truck') rapprocherMqtt();
      try { if (window.pushGasSiCouche) await window.pushGasSiCouche(); } catch(err) { /* sync best effort */ }
    } catch (e) { equipStatus(EQUIP_TYPES[type].statusId, 'Erreur: ' + e.message, false); }
  }

  function onEquipAction(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn || !btn.dataset.eq) return;
    const type = btn.dataset.eq;
    if (btn.dataset.action === 'menu') {
      const menu = btn.parentElement.querySelector('.pm-menu');
      const ouvrir = menu.hidden;
      document.querySelectorAll('.pm-menu').forEach((m) => { m.hidden = true; });
      menu.hidden = !ouvrir;
      return;
    }
    const menu = btn.closest('.pm-menu');
    if (menu) menu.hidden = true;
    if (btn.dataset.action === 'renouveler') { renouvelerMqtt(btn.dataset.code); return; }
    if (btn.dataset.action === 'toggle') toggleEquip(type, btn.dataset.id, btn.dataset.active === 'true');
    else if (btn.dataset.action === 'delete') deleteEquip(type, btn.dataset.id);
  }

  // ============ TABLETTES + AFFECTATION ============

  const TABLET_KEY = (id) => 'tab-' + id;

  function loadTablets() {
    try { return JSON.parse(localStorage.getItem('otTablets')) || []; }
    catch (e) { return []; }
  }
  function saveTablets(list) {
    try { localStorage.setItem('otTablets', JSON.stringify(list)); } catch (e) {}
  }

  function loadAssignments() {
    try { return JSON.parse(localStorage.getItem('otAssignments')) || { gnss: {}, tabTeam: {} }; }
    catch (e) { return { gnss: {}, tabTeam: {} }; }
  }
  function saveAssignments(a) {
    try { localStorage.setItem('otAssignments', JSON.stringify(a)); } catch (e) {}
  }

  async function allFlotteRows() {
    try { return await loadFlotteSync(); } catch (e) { return []; }
  }

  function loadGnssRegistry() {
    try { return JSON.parse(localStorage.getItem('otGnssRegistry')) || []; }
    catch (e) { return []; }
  }

  // Inventaire GNSS : le catalogue (otGnssCatalog, alimente par le formulaire
  // "Enregistrer un GNSS" et par le pairing) retrace 1 ligne par GNSS, avec
  // sa tablette liee et son engin affecte.
  function renderGnssRegistry() {
    const box = $('#gnssRegistryContainer');
    if (!box) return;
    const reg = loadGnssRegistry();
    const a = loadAssignments();
    const cat = (window.OreTrackingGnss && window.OreTrackingGnss.loadGnssCatalog)
      ? window.OreTrackingGnss.loadGnssCatalog() : [];
    // un GNSSÐ¸Ñ€ÑƒÐµÑ‚ÑÑ pairé hors catalogue reste visible
    cat.forEach((c) => { if (!reg.some((r) => r.id === c.id)) reg.push({ id: c.id, terminal: '' }); });

    if (!reg.length) {
      box.innerHTML = '<div class="empty-state"><i class="fas fa-satellite-dish"></i><span>Aucun GNSS enregistre</span></div>';
      return;
    }
    box.innerHTML = reg.map((r) => {
      const lie = !!r.terminal;
      // engin affecte a ce GNSS (via assignments.gnss[id])
      const ent = a.gnss[r.id];
      const engin = ent && ent.type !== 'gnss'
        ? enginLabel(ent.type, ent.target) : null;
      let html = '<div class="account-item' + (lie ? ' gnss-bound' : '') + '" data-gnss="' + eqEscape(r.id) + '">' +
        '<i class="fas fa-satellite-dish"></i>' +
        '<span class="account-nom">' + eqEscape(r.id) + '</span>' +
        '<span class="account-email" style="flex:1">' +
          (lie ? '<i class="fas fa-tablet-alt"></i> ' + eqEscape(r.terminal)
               : '<span style="opacity:.4">Non lie</span>');
      if (engin) {
        html += ' &nbsp;·&nbsp; <i class="fas fa-digging"></i> ' + eqEscape(engin);
      }
      html += '</span>';
      // actions : dissocier (si lie) + supprimer, sur chaque ligne
      html += '<span class="account-actions">' +
        (lie
          ? '<button class="account-btn" data-action="gnss-unbind" data-gnss="' + eqEscape(r.id) + '" title="Dissocier"><i class="fas fa-times"></i></button>'
          : '') +
        '<button class="account-btn" data-action="gnss-del" data-gnss="' + eqEscape(r.id) + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
      '</span>';
      html += '</div>';
      return html;
    }).join('');
  }

  async function unbindGnss(gnssId) {
    if (!await Splash.confirmer('Dissocier ' + gnssId + ' de sa tablette ?',
        { titre: 'Dissociation GNSS', danger: true, libelleOk: 'Dissocier' })) return;
    // 1. supprimer le lien du registre GNSS (le GNSS reste au catalogue)
    const reg = loadGnssRegistry().filter((r) => r.id !== gnssId);
    try { localStorage.setItem('otGnssRegistry', JSON.stringify(reg)); } catch (err) {}
    // 2. supprimer son affectation (lien GNSS + engin eventuel)
    const a = loadAssignments();
    delete a.gnss[gnssId];
    saveAssignments(a);
    equipStatus('tabEquipStatus', 'GNSS dissocie : ' + gnssId, true);
    refreshAssignments();
  }

  // suppression definitive d'un GNSS : catalogue + lien + affectation
  async function deleteGnssEntry(gnssId) {
    if (!await Splash.confirmer('Supprimer definitivement ' + gnssId + ' ?',
        { titre: 'Suppression GNSS', danger: true,
          details: 'Le catalogue, le lien et l\'affectation seront supprimes.',
          libelleOk: 'Supprimer' })) return;
    try {
      localStorage.setItem('otGnssRegistry',
        JSON.stringify(loadGnssRegistry().filter((r) => r.id !== gnssId)));
    } catch (err) {}
    const g = window.OreTrackingGnss;
    if (g && g.loadGnssCatalog && g.saveGnssCatalog) {
      g.saveGnssCatalog(g.loadGnssCatalog().filter((c) => c.id !== gnssId));
    }
    const a = loadAssignments();
    delete a.gnss[gnssId];
    saveAssignments(a);
    equipStatus('gnssAddStatus', 'GNSS supprime : ' + gnssId, true);
    refreshAssignments();
  }

  function refreshAssignments() {
    renderFleet();
    renderGnssRegistry();
    renderTabEquip();
  }

  async function syncTablettesToServer() {
    const list = loadTablets();
    if (!list.length) return;
    let n = 0;
    for (const t of list) {
      try {
        const resp = await fetch('/api/tablettes/detect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ nom: t.id, device_id: t.id, type: t.type || 'terrain', modele: t.modele || '' })
        });
        if (resp.ok) n++;
      } catch (err) { /* ignore individuellement */ }
    }
    equipStatus('tabletStatus', n + '/' + list.length + ' tablette(s) synchronisees avec le serveur', true);
  }

  async function addTablet(e) {
    e.preventDefault();
    const id = $('#newTabletId').value.trim();
    if (!id) { equipStatus('tabletStatus', 'ID requis', false); return; }
    const list = loadTablets();
    if (list.find((t) => t.id === id)) { equipStatus('tabletStatus', 'Tablette deja existante', false); return; }
    list.push({ id, team: '', actif: true });
    saveTablets(list);
    equipStatus('tabletStatus', 'Ajoutée: ' + id, true);
    e.target.reset();
    renderFleet();
    // Synchronisation avec le serveur (API publique, pas d'auth requise)
    try {
      const resp = await fetch('/api/tablettes/detect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ nom: id, device_id: id, type: 'terrain' })
      });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      equipStatus('tabletStatus', 'Ajoutée: ' + id + ' (serveur OK)', true);
    } catch (err) {
      equipStatus('tabletStatus', 'Ajoutée localement (serveur indisponible)', false);
    }
  }

  // ---- auto-detection de la tablette courante ----
  function getDeviceModel() {
    const ua = navigator.userAgent || '';
    let m;
    // Android : "SM-T870 Build/..." (Galaxy Tab), "SM-X200", etc.
    if ((m = /\b(SM-[A-Z0-9]{4,})\b/.exec(ua))) return m[1];
    if ((m = /\b(Android)\b.*?\b(\d+(?:\.\d+)?)\b/.exec(ua))) return 'Android ' + m[2];
    // iPad : "iPad; CPU OS 17_0"
    if (/iPad/.test(ua)) {
      if ((m = /iPad; CPU OS (\d+(?:[._]\d+)*)/.exec(ua))) return 'iPadOS ' + m[1].replace(/_/g, '.');
      return 'iPad';
    }
    if (/iPhone/.test(ua)) return 'iPhone';
    if (/Windows/.test(ua)) { if ((m = /Windows NT (\d+\.\d+)/.exec(ua))) return 'Windows ' + m[1]; return 'Windows'; }
    if (/Macintosh/.test(ua)) return 'Mac';
    if (/Linux/.test(ua)) return 'Linux';
    return ua.slice(0, 24) || 'Inconnu';
  }

  function getDeviceId() {
    try { return localStorage.getItem('otThisDeviceId') || ''; } catch (e) { return ''; }
  }
  function setDeviceId(id) {
    try { localStorage.setItem('otThisDeviceId', id); } catch (e) {}
  }
  function getIgnoredDevices() {
    try { return JSON.parse(localStorage.getItem('otIgnoredDevices') || '[]'); } catch (e) { return []; }
  }
  function addIgnoredDevice(id) {
    try {
      var list = getIgnoredDevices();
      if (!list.includes(id)) { list.push(id); localStorage.setItem('otIgnoredDevices', JSON.stringify(list)); }
    } catch (e) {}
  }
  function removeIgnoredDevice(id) {
    try {
      var list = getIgnoredDevices().filter(function (x) { return x !== id; });
      localStorage.setItem('otIgnoredDevices', JSON.stringify(list));
    } catch (e) {}
  }
  function getDeviceDetectedAt() {
    try { return localStorage.getItem('otThisDeviceDetectedAt') || ''; } catch (e) { return ''; }
  }
  function setDeviceDetectedAt(ts) {
    try { localStorage.setItem('otThisDeviceDetectedAt', String(ts)); } catch (e) {}
  }

  // Une tablette est consideree comme "connectee" seulement si elle a ete
  // redetectee recemment (sinon l'ID en localStorage peut dater de plusieurs
  // jours alors que l'appareil n'est plus branche).
  const DEVICE_STALE_MS = 24 * 60 * 60 * 1000;

  function detectThisDevice() {
    const model = getDeviceModel();
    const list = loadTablets();
    // la tablette existe deja si l'ID enregistre pointe sur une entree presente
    const saved = getDeviceId();
    const found = saved ? list.find((t) => t.id === saved) : null;
    if (found) return { model, id: found.id, isNew: false, tablet: found };

    // sinon on propose un ID a partir du modele
    let base = 'TAB-' + model.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').toUpperCase();
    if (base.length > 24) base = base.slice(0, 24);
    // l'utilisateur a supprime cette tablette (ex. Windows 10) : on ne la
    // redetecte pas automatiquement depuis l'User Agent.
    var ignored = getIgnoredDevices();
    if (!saved && ignored.includes(base)) {
      return { model, id: base, ignored: true, tablet: null };
    }
    let id = base;
    let n = 2;
    while (list.some((t) => t.id === id)) { id = base + '-' + n; n++; }
    return { model, id, isNew: true, tablet: null };
  }

  // ---- Fiche complete de l'appareil local ----
  // ADB/USB-dbg n'est pas accessible au navigateur (interface reservee a l'OS).
  // App ouvert SUR la tablette : Chrome expose le modele exact, la version
  // Android, l'archi CPU... via les User-Agent Client Hints haute entropie.
  async function readDeviceDetails() {
    const ua = navigator.userAgent;
    const d = {
      model: getDeviceModel(),
      platform: (navigator.userAgentData || {}).platform || 'Inconnu',
      platformVersion: '',
      arch: '',
      cores: navigator.hardwareConcurrency || null,
      ram: navigator.deviceMemory || null,
      screen: screen.width + 'x' + screen.height + (screen.devicePixelRatio > 1 ? ' @' + screen.devicePixelRatio + 'x' : ''),
      browser: ua.slice(0, 90),
    };
    const uad = navigator.userAgentData;
    if (uad && uad.getHighEntropyValues) {
      try {
        const h = await uad.getHighEntropyValues(
          ['platformVersion', 'model', 'architecture', 'bitness', 'fullVersionList']);
        if (h.model) d.model = h.model;
        if (h.platformVersion) d.platformVersion = h.platformVersion;
        d.arch = [h.architecture, h.bitness].filter(Boolean).join('/');
        const full = (h.fullVersionList || []).map((b) => b.brand + ' ' + b.version)
          .filter((b) => b.indexOf('Not') !== 0).join(' ; ');
        if (full) d.browser = full;
      } catch (e) { /* hints refuses : fallback UA */ }
    }
    if (!d.platformVersion && (ua.match(/Android (\d+[\d.]*)/) || [])[1]) {
      d.platformVersion = ua.match(/Android (\d+[\d.]*)/)[1];
    }
    if (!d.arch && /WOW64|x64|Win64/.test(ua)) d.arch = 'x86_64';
    return d;
  }

  function renderDeviceInfo(d) {
    const box = $('#deviceInfoBox');
    if (!box) return;
    const row = (icon, k, v) => v
      ? '<div class="device-info-row"><i class="fas ' + icon + '"></i><span class="account-email">' + k + '</span><span class="account-nom">' + eqEscape(String(v)) + '</span></div>'
      : '';
    box.innerHTML =
      '<div class="account-item device-info">' +
        row('fa-mobile-alt', 'Modele', d.model) +
        row('fa-robot', 'OS', d.platform + (d.platformVersion ? ' ' + d.platformVersion : '')) +
        row('fa-microchip', 'CPU', d.arch || null) +
        row('fa-tachometer-alt', 'RAM', d.ram ? d.ram + ' Go' : null) +
        row('fa-microchip', 'Coeurs', d.cores || null) +
        row('fa-desktop', 'Ecran', d.screen) +
        row('fa-globe', 'Navigateur', d.browser) +
      '</div>';
  }

  function registerThisDevice(id) {
    const list = loadTablets();
    if (!list.find((t) => t.id === id)) {
      list.push({ id, team: '', actif: true, auto: true });
      saveTablets(list);
    }
    setDeviceId(id);
    setDeviceDetectedAt(Date.now());
    renderFleet();
  }

  function renderThisDeviceBanner() {
    const banner = $('#thisDeviceBanner');
    if (!banner) return;
    const info = detectThisDevice();
    const label = eqEscape(info.model);
    const detectedAt = getDeviceDetectedAt();
    const stale = !detectedAt || (Date.now() - parseInt(detectedAt, 10)) > DEVICE_STALE_MS;
    if (info.isNew) {
      banner.innerHTML =
        '<div class="account-item gnss-bound">' +
          '<i class="fas fa-tablet-alt"></i>' +
          '<span class="account-nom" title="' + label + '">' + label + '</span>' +
          '<span class="account-email" data-i18n>Cette tablette</span>' +
          '<span class="account-actions">' +
            '<button class="account-btn" id="btnRegisterDevice" title="Enregistrer cette tablette">' +
              '<i class="fas fa-plus"></i></button>' +
          '</span>' +
        '</div>';
      const btn = $('#btnRegisterDevice');
      if (btn) btn.addEventListener('click', () => {
        registerThisDevice(info.id);
        equipStatus('tabletStatus', 'Tablette enregistrée: ' + info.id, true);
        renderThisDeviceBanner();
      });
    } else if (info.ignored) {
      // L'utilisateur a ignore cette tablette (suppression depuis l'User Agent).
      banner.innerHTML =
        '<div class="account-item">' +
          '<i class="fas fa-tablet-alt"></i>' +
          '<span class="account-nom" title="' + label + '">' + eqEscape(info.id) + '</span>' +
          '<span class="account-email" style="color:#6b7280;">Tablette ignoree (ne sera plus proposee)</span>' +
          '<span class="account-actions">' +
            '<button class="account-btn" id="btnRestoreDevice" title="Restaurer cette tablette">' +
              '<i class="fas fa-undo"></i></button>' +
          '</span>' +
        '</div>';
      const res = $('#btnRestoreDevice');
      if (res) res.addEventListener('click', () => {
        removeIgnoredDevice(info.id);
        renderThisDeviceBanner();
        equipStatus('tabletStatus', 'Tablette restauree : ' + info.id, true);
      });
    } else if (stale) {
      // L'ID est enregistre mais l'appareil n'a pas ete redetecte depuis 24h :
      // on n'affiche pas "Detectee" comme si c'etait actuel.
      const date = detectedAt ? new Date(parseInt(detectedAt, 10)).toLocaleString() : 'jamais';
      banner.innerHTML =
        '<div class="account-item">' +
          '<i class="fas fa-tablet-alt"></i>' +
          '<span class="account-nom" title="' + label + '">' + eqEscape(info.id) + '</span>' +
          '<span class="account-email" style="color:#ef4444;">Tablette non connectee (derniere detection le ' + date + ')</span>' +
          '<span class="account-actions">' +
            '<button class="account-btn" id="btnRedetectDevice" title="Re-detecter cette tablette">' +
              '<i class="fas fa-redo"></i></button>' +
            '<button class="account-btn" id="btnIgnoreDevice" title="Ne plus proposer cette tablette">' +
              '<i class="fas fa-ban"></i></button>' +
          '</span>' +
        '</div>';
      const btn = $('#btnRedetectDevice');
      if (btn) btn.addEventListener('click', () => {
        readDeviceDetails().then((d) => {
          renderDeviceInfo(d);
          setDeviceDetectedAt(Date.now());
          removeIgnoredDevice(info.id);
          renderThisDeviceBanner();
          equipStatus('tabletStatus', 'Re-detectee : ' + info.model + ' -> ' + info.id, true);
        }, () => {
          equipStatus('tabletStatus', 'Re-detection echouee.', false);
        });
      });
      const ban = $('#btnIgnoreDevice');
      if (ban) ban.addEventListener('click', () => {
        addIgnoredDevice(info.id);
        setDeviceId('');
        setDeviceDetectedAt('');
        renderThisDeviceBanner();
        equipStatus('tabletStatus', 'Tablette ignoree : ' + info.id, true);
      });
    } else {
      banner.innerHTML =
        '<div class="account-item">' +
          '<i class="fas fa-tablet-alt"></i>' +
          '<span class="account-nom" title="' + label + '">' + eqEscape(info.id) + '</span>' +
          '<span class="account-email">' + label + '</span>' +
          '<span class="account-actions">' +
            '<button class="account-btn" id="btnDeleteDevice" title="Supprimer cette tablette">' +
              '<i class="fas fa-trash"></i></button>' +
          '</span>' +
          '<span class="account-flag"><i class="fas fa-check"></i></span>' +
        '</div>';
      const del = $('#btnDeleteDevice');
      if (del) del.addEventListener('click', async () => {
        if (!await Splash.confirmer('Supprimer cette tablette (' + info.id + ') ?',
            { titre: 'Suppression de tablette', danger: true, libelleOk: 'Supprimer' })) return;
        saveTablets(loadTablets().filter((t) => t.id !== info.id));
        addIgnoredDevice(info.id); // ne plus la redetecter depuis l'User Agent
        setDeviceId('');
        setDeviceDetectedAt('');
        renderThisDeviceBanner();
        renderFleet();
        renderTabEquip();
        equipStatus('tabletStatus', 'Tablette supprimée : ' + info.id, true);
      });
    }
  }

  // ---- Flotte : vue unifiee tablette + GNSS + engin ----
  // Modele : 1 tablette = 1 GNSS = 1 DT/EXC (Spotter exclu, pas besoin de GNSS)
  function renderFleet() {
    const box = $('#fleetContainer');
    if (!box) return;
    const tablets = loadTablets();
    const reg = loadGnssRegistry();
    const a = loadAssignments();

    if (!tablets.length) {
      box.innerHTML = '<div class="empty-state"><i class="fas fa-tablet-alt"></i><span>Aucune tablette</span></div>';
      return;
    }

    box.innerHTML = tablets.map((t) => {
      const active = t.actif !== false;
      // GNSS lie a cette tablette (via registre)
      const gnssEntry = reg.find((r) => r.terminal === t.id);
      const gnssId = gnssEntry ? gnssEntry.id : null;
      // engin affecte (via assignments.gnss[gnssId])
      const equip = gnssId && a.gnss[gnssId] && a.gnss[gnssId].type !== 'gnss'
        ? a.gnss[gnssId] : null;
      const equipLabel = equip ? enginLabel(equip.type, equip.target) : null;

      const rowCls = 'account-item' + (active ? '' : ' desactive');
      return '<div class="' + rowCls + '" data-tab-id="' + eqEscape(t.id) + '">' +
        '<i class="fas fa-tablet-alt"></i>' +
        '<span class="account-nom">' + eqEscape(t.id) + '</span>' +
        '<span class="account-email" style="flex:1">' +
          (gnssId ? '<i class="fas fa-satellite-dish"></i> ' + eqEscape(gnssId) : '<span style="opacity:.4">Pas de GNSS</span>') +
          ' &nbsp;·&nbsp; ' +
          (equipLabel ? '<i class="fas fa-digging"></i> ' + eqEscape(equipLabel) : '<span style="opacity:.4">Pas d\'engin</span>') +
        '</span>' +
        (active ? '' : '<span class="account-flag">desactive</span>') +
        '<span class="account-actions">' +
          '<button class="account-btn" data-action="tab-toggle" data-id="' + eqEscape(t.id) + '" data-active="' + active + '" title="' + (active ? 'Desactiver' : 'Activer') + '">' +
            '<i class="fas fa-' + (active ? 'ban' : 'check') + '"></i></button>' +
          '<button class="account-btn" data-action="tab-delete" data-id="' + eqEscape(t.id) + '" title="Supprimer"><i class="fas fa-trash"></i></button>' +
        '</span></div>';
    }).join('');
  }

  async function onFleetAction(e) {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    const id = btn.dataset.id;
    if (action === 'tab-toggle') {
      const list = loadTablets();
      const t = list.find((x) => x.id === id);
      if (t) { t.actif = btn.dataset.active !== 'true'; saveTablets(list); renderFleet(); }
    } else if (action === 'tab-delete') {
      if (!await Splash.confirmer('Supprimer cette tablette (' + id + ') et son affectation ?',
          { titre: 'Suppression de tablette', danger: true, libelleOk: 'Supprimer' })) return;
      const list = loadTablets().filter((t) => t.id !== id);
      saveTablets(list);
      // nettoie aussi le GNSS associe et l'affectation
      const reg = loadGnssRegistry().filter((r) => r.terminal !== id);
      try { localStorage.setItem('otGnssRegistry', JSON.stringify(reg)); } catch (err) {}
      const a = loadAssignments();
      reg.forEach((r) => { delete a.gnss[r.id]; });
      saveAssignments(a);
      renderFleet();
      renderGnssRegistry();
      renderTabEquip();
    }
  }

  // ---- Etape 3 : affecter un engin (DT/EXC uniquement, Spotter exclu) ----
  async function renderTabEquip() {
    const box = $('#tabEquipContainer');
    if (!box) return;
    // le formulaire n'a de sens qu'apres le pairing du GNSS (Etape 2)
    const form = $('#tabEquipAddForm');
    if (form) form.hidden = false;
    const reg = loadGnssRegistry();
    const a = loadAssignments();
    const thisId = getDeviceId();

    // le GNSS de cette tablette
    const gnssEntry = reg.find((r) => r.terminal === thisId);
    if (!gnssEntry) {
      box.innerHTML = '<div class="empty-state"><i class="fas fa-satellite-dish"></i><span>Pairer un GNSS d\'abord (section Tablette et GNSS)</span></div>';
      if (form) form.hidden = true;
      populateEquipSelect([]);
      return;
    }
    const gnssId = gnssEntry.id;
    const current = a.gnss[gnssId] && a.gnss[gnssId].type !== 'gnss' ? a.gnss[gnssId] : null;

    if (!current) {
      box.innerHTML = '<div class="empty-state"><i class="fas fa-digging"></i><span>Aucun engin affecte</span></div>';
    } else {
      box.innerHTML =
        '<div class="account-item" data-gnss="' + eqEscape(gnssId) + '">' +
          '<i class="fas fa-digging"></i>' +
          '<span class="account-nom">' + eqEscape(enginLabel(current.type, current.target)) + '</span>' +
          '<span class="account-email">via ' + eqEscape(gnssId) + '</span>' +
          '<span class="account-actions">' +
            '<button class="account-btn" data-action="tab-equip-del" title="Retirer"><i class="fas fa-times"></i></button>' +
          '</span></div>';
    }
    // peuple le select avec les engins disponibles
    populateEquipSelect([gnssId]);
  }

  async function populateEquipSelect(excludeGnssIds) {
    const sel = $('#tabEquipSelect');
    if (!sel) return;
    const a = loadAssignments();
    // engins deja affectes a un autre GNSS
    const assigned = new Set();
    Object.keys(a.gnss).forEach((g) => {
      const ent = a.gnss[g];
      if (ent.type === 'gnss' || excludeGnssIds.includes(g)) return;
      let target = ent.target;
      // ancien format DT : id numerique de la table equipements -> code
      if (ent.type === 'truck' && legacyTruckLabels[String(target)]) target = legacyTruckLabels[String(target)];
      assigned.add(ent.type + ':' + target);
    });

    // machines : miroir sync_flotte. Camions = familles 1500/785/ADT ;
    // excavatrices = toutes les autres familles (Hitachi EX, Komatsu PC,
    // HYUNDAI, VOLVO, EXCAVATOR...) — meme regle que la page Sync.
    const flotte = await allFlotteRows();
    const trucks = flotte.filter((r) => FAMILLES_DT.indexOf(String(r.famille).toUpperCase()) >= 0)
      .map((r) => ({ equip_id: r.code }));
    const excs = flotte.filter((r) => FAMILLES_DT.indexOf(String(r.famille).toUpperCase()) < 0)
      .map((r) => ({ code: r.code }));
    // fallback si API indisponible : engins deja connus dans assignments
    if (!trucks.length && !excs.length) {
      sel.innerHTML = '<option value="">Choisir un engin...</option>' +
        '<option value="truck:__manual">— Saisir un ID DT —</option>' +
        '<option value="excavator:__manual">— Saisir un ID EXC —</option>';
      return;
    }

    let opts = '<option value="">Choisir un engin...</option>';
    trucks.forEach((r) => {
      const key = 'truck:' + r.equip_id;
      if (assigned.has(key)) return;
      opts += '<option value="' + eqEscape(key) + '">' + eqEscape(enginLabel('truck', r.equip_id)) + '</option>';
    });
    excs.forEach((r) => {
      const key = 'excavator:' + r.code;
      if (assigned.has(key)) return;
      opts += '<option value="' + eqEscape(key) + '">' + eqEscape(r.code || '?') + '</option>';
    });
    sel.innerHTML = opts;
  }

  async function addTabEquip(e) {
    e.preventDefault();
    const sel = $('#tabEquipSelect');
    const val = sel ? sel.value : '';
    if (!val) { equipStatus('tabEquipStatus', 'Choisissez un engin', false); return; }

    const reg = loadGnssRegistry();
    const thisId = getDeviceId();
    const gnssEntry = reg.find((r) => r.terminal === thisId);
    if (!gnssEntry) { equipStatus('tabEquipStatus', 'Pas de GNSS associe', false); return; }
    const gnssId = gnssEntry.id;

    const [type, target] = val.split(':');
    const a = loadAssignments();
    a.gnss[gnssId] = { type, target };
    saveAssignments(a);
    equipStatus('tabEquipStatus', 'Affecté: ' + enginLabel(type, target), true);
    await renderTabEquip();
    renderFleet();
  }

  async function delTabEquip(e) {
    const reg = loadGnssRegistry();
    const thisId = getDeviceId();
    const gnssEntry = reg.find((r) => r.terminal === thisId);
    if (!gnssEntry) return;
    const a = loadAssignments();
    delete a.gnss[gnssEntry.id];
    saveAssignments(a);
    equipStatus('tabEquipStatus', 'Retiré', true);
    await renderTabEquip();
    renderFleet();
  }

  function initTabletUi() {
    const tForm = $('#tabletAddForm');
    if (tForm) tForm.addEventListener('submit', addTablet);
    const fleetBox = $('#fleetContainer');
    if (fleetBox) fleetBox.addEventListener('click', onFleetAction);

    // detection locale explicite de cette tablette (symetrique au bouton GNSS)
    const dForm = $('#deviceDetectForm');
    if (dForm) dForm.addEventListener('submit', (e) => {
      e.preventDefault();
      readDeviceDetails().then((d) => {
        renderDeviceInfo(d);
        setDeviceDetectedAt(Date.now());
        const det = detectThisDevice();
        const os = (d.platform + (d.platformVersion ? ' ' + d.platformVersion : '')).trim();
        if (det.isNew) {
          equipStatus('tabletStatus', 'Détectée : ' + det.model
            + (os && os !== det.model && d.platform !== 'Inconnu' ? ' (' + os + ')' : '')
            + ' -> ' + det.id + ' (cliquer + pour enregistrer)', true);
        } else {
          equipStatus('tabletStatus', 'Détectée : ' + det.model
            + (os && os !== det.model && d.platform !== 'Inconnu' ? ' (' + os + ')' : '')
            + ' -> ' + det.id + ' (deja enregistrée)', true);
        }
      }, () => {
        const det = detectThisDevice();
        renderThisDeviceBanner();
        equipStatus('tabletStatus', 'Detection base effacee pour cette tablette.', false);
      });
    });

    const teForm = $('#tabEquipAddForm');
    if (teForm) teForm.addEventListener('submit', addTabEquip);
    const teBox = $('#tabEquipContainer');
    if (teBox) teBox.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="tab-equip-del"]')) delTabEquip(e);
    });

    const regBox = $('#gnssRegistryContainer');
    if (regBox) regBox.addEventListener('click', (e) => {
      if (e.target.closest('[data-action="gnss-unbind"]')) {
        const btn = e.target.closest('[data-gnss]');
        if (btn) unbindGnss(btn.dataset.gnss);
      }
      if (e.target.closest('[data-action="gnss-del"]')) {
        const btn = e.target.closest('[data-gnss]');
        if (btn) deleteGnssEntry(btn.dataset.gnss);
      }
    });

    // Enregistrer un GNSS au catalogue (avant pairing)
    const gnssForm = $('#gnssAddForm');
    if (gnssForm) gnssForm.addEventListener('submit', (e) => { e.preventDefault(); addGnss(e); });

    renderFleet();
    renderGnssRegistry();
    renderThisDeviceBanner();
    renderTabEquip();
    // pousse les tablettes locales vers le serveur (synchronisation initiale)
    syncTablettesToServer();
  }

  function addGnss(e) {
    const id = ($('#newGnssId') ? $('#newGnssId').value.trim() : '').toUpperCase();
    if (!id) { equipStatus('gnssAddStatus', 'ID requis', false); return; }
    if (!/^GNSS-\d{2,3}$/.test(id) && !/^[A-Z0-9-]{3,20}$/.test(id)) {
      equipStatus('gnssAddStatus', 'Format attendu : GNSS-04 (ou specifique)', false);
      return;
    }
    const cat = (window.OreTrackingGnss || {}).loadGnssCatalog
      ? window.OreTrackingGnss.loadGnssCatalog() : [];
    if (cat.some((r) => r.id === id)) {
      equipStatus('gnssAddStatus', 'GNSS deja enregistre : ' + id, false);
      return;
    }
    cat.push({ id, addedAt: Date.now() });
    if (window.OreTrackingGnss) window.OreTrackingGnss.saveGnssCatalog(cat);
    equipStatus('gnssAddStatus', 'GNSS enregistre : ' + id, true);
    e.target.reset();
    renderGnssRegistry();
  }

  // ============ INIT ============

  function initEquipUI() {
    // formulaires d'ajout masques tant qu'aucune lecture n'a reussi
    // (evite un clic + qui part en 401 "authentification requise")
    setEquipAuthUi(false);

    // afficher/masquer le champ "Autre" pour les familles
    const truckFamille = document.getElementById('newTruckFamille');
    const truckFamilleAutre = document.getElementById('newTruckFamilleAutre');
    if (truckFamille) {
      truckFamille.addEventListener('change', () => {
        if (truckFamilleAutre) truckFamilleAutre.style.display = truckFamille.value === 'AUTRE' ? '' : 'none';
      });
    }
    const excavFamille = document.getElementById('newExcavatorFamille');
    const excavFamilleAutre = document.getElementById('newExcavatorFamilleAutre');
    if (excavFamille) {
      excavFamille.addEventListener('change', () => {
        if (excavFamilleAutre) excavFamilleAutre.style.display = excavFamille.value === 'AUTRE' ? '' : 'none';
      });
    }

    // teams (spotter)
    const teamForm = $('#teamAddForm');
    if (teamForm) teamForm.addEventListener('submit', addTeam);
    const teamBox = $('#teamContainer');
    if (teamBox) {
      teamBox.addEventListener('click', onTeamAction);
      teamBox.addEventListener('submit', (e) => {
        const form = e.target.closest('.member-add-form');
        if (form) addMember(form.dataset.equipe, e);
      });
    }

    // equipements simples
    Object.keys(EQUIP_TYPES).forEach((type) => {
      const cfg = EQUIP_TYPES[type];
      const form = $('#' + cfg.form);
      if (form) form.addEventListener('submit', (e) => addEquip(type, e));
      const container = $('#' + cfg.container);
      if (container) container.addEventListener('click', onEquipAction);
    });

    // premiere lecture : ouvre les formulaires si la session est valide
    // (Firebase ou token localStorage non expire)
    loadTeams();
    Object.keys(EQUIP_TYPES).forEach((t) => loadEquipList(t));
    initTempsReel();

    // session Firebase etablie -> recharge (loadTeams re-affiche les +)
    const MSG_AUTH = 'Connectez-vous pour gerer les equipements';
    window.addEventListener('app:auth-ready', () => {
      loadTeams();
      Object.keys(EQUIP_TYPES).forEach((t) => loadEquipList(t));
      refreshSyncStatus();
    });
    window.addEventListener('app:auth-lost', () => {
      setEquipAuthUi(false);
      const box = $('#teamContainer');
      if (box) box.innerHTML = '<div class="account-item muted">' + MSG_AUTH + '</div>';
      Object.keys(EQUIP_TYPES).forEach((t) => {
        const b = $('#' + EQUIP_TYPES[t].container);
        if (b) b.innerHTML = '<div class="account-item muted">' + MSG_AUTH + '</div>';
      });
    });
  }

  async function pullFromGas() {
    const btn = $('#btnPullGas');
    const status = $('#pullGasStatus');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Recuperation...'; }
    if (status) { status.textContent = ''; status.className = 'account-status'; }
    try {
      const res = await API.pullGas();
      const c = res.counts || {};
      const LIBELLES = {
         profils: 'profils', identifiants: 'Spotter', flotte: 'camions DT',
        positions: 'positions', affectation: 'affectation',
        blasts: 'blasts', notifications: 'notifications'
      };
      const parts = Object.keys(c)
        .filter(k => c[k])
        .map(k => c[k] + ' ' + (LIBELLES[k] || k));
      const msg = 'Recupere: ' + (parts.join(', ') || 'rien');
      if (status) {
        status.textContent = msg + (res.errors && res.errors.length ? ' — ' + res.errors.length + ' erreur(s)' : '');
        status.className = 'account-status ' + (res.errors && res.errors.length ? 'err' : 'ok');
      }
      if (res.errors && res.errors.length) console.warn('[GAS Pull] erreurs:', res.errors);
      // recharger les listes d'equipements apres pull
      if (window.OreTrackingServerUi && window.OreTrackingServerUi.loadEquipLists) {
        window.OreTrackingServerUi.loadEquipLists();
      }
    } catch (e) {
      if (status) { status.textContent = 'Echec: ' + e.message; status.className = 'account-status err'; }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-cloud-download-alt"></i> Recuperer depuis Google Sheets'; }
    }
  }

  /**
   * Initialiser / Migrer le Sheet : cree les onglets manquants et corrige la
   * structure (identifiants <= equipements, flotte <= dts). C'est le bouton
   * a utiliser quand la synchronisation annonce "referentiels absents ou
   * incoherents" : la correction se fait cote Sheet, pas a la main.
   */
  async function migrateFromGas() {
    const btn = $('#btnMigrateGas');
    const status = $('#migrateGasStatus');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Migration...'; }
    if (status) { status.textContent = ''; status.className = 'account-status'; }
    try {
      const res = await API.migrateGas();
      const rapport = res.rapport || 'aucun changement';
      if (status) {
        status.textContent = 'Migration: ' + rapport;
        status.className = 'account-status ' + (res.ok ? 'ok' : 'err');
      }
      await refreshSyncStatus();
    } catch (e) {
      if (status) { status.textContent = 'Echec: ' + e.message; status.className = 'account-status err'; }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-screwdriver-wrench"></i> Initialiser / Migrer le Sheet'; }
    }
  }

  /**
   * Diagnostic des referentiels : dit precisement pourquoi la synchronisation
   * est refusee (onglet vide, doublon, profil sans machine, profil sans
   * membre actif) au lieu du message generique.
   */
  async function diagnosticFromGas() {
    const btn = $('#btnDiagGas');
    const status = $('#diagGasStatus');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Analyse...'; }
    if (status) { status.textContent = ''; status.className = 'cfg-status'; }
    try {
      const res = await API.diagnosticGas();
      const a = res.anomalies || {};
      const lignes = [];
      if (a.profils_vides) lignes.push('onglet profils vide');
      if (a.doublons_profils && a.doublons_profils.length) lignes.push('profils en double : ' + a.doublons_profils.join(', '));
      if (a.doublons_flotte && a.doublons_flotte.length) lignes.push('engins en double : ' + a.doublons_flotte.join(', '));
      if (a.profils_sans_machine && a.profils_sans_machine.length) lignes.push('profil sans engin actif : ' + a.profils_sans_machine.join(', '));
      if (a.profils_sans_membre && a.profils_sans_membre.length) lignes.push('profil sans membre actif : ' + a.profils_sans_membre.join(', '));
      const onglets = res.onglets || {};
      const msg = 'profils ' + (res.profils || []).length
        + ' | Spotter ' + (res.identifiants || []).length
        + ' | engins ' + (res.flotte || []).length
        + ' | ' + (lignes.length ? lignes.join(' — ') : 'aucune anomalie detectee');
      if (status) {
        status.textContent = msg;
        status.style.color = lignes.length ? '#f97316' : '#22c55e';
      }
      console.info('[GAS Diagnostic]', { onglets: onglets, anomalies: a, referentiels_ok: res.referentiels_ok });
    } catch (e) {
      if (status) { status.textContent = 'Echec: ' + e.message; status.style.color = '#ef4444'; }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-stethoscope"></i> Diagnostiquer les referentiels'; }
    }
  }

  async function pushFromGas() {
    const btn = $('#btnPushGas');
    const status = $('#pushGasStatus');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Envoi...'; }
    if (status) { status.textContent = ''; status.className = 'account-status'; }
    try {
      const res = await API.pushGas();
      const r = res.resultats || {};
      const parts = Object.keys(r).filter(k => r[k] && r[k].ecrites != null).map(k => k + ': ' + r[k].ecrites);
      const errs = res.errors || [];
      if (status) {
        if (errs.length) {
          status.textContent = 'Pousse: ' + (parts.join(', ') || 'rien') + ' — ' + errs[0];
          status.className = 'account-status err';
        } else {
          status.textContent = 'Pousse vers Google Sheets: ' + (parts.join(', ') || 'rien');
          status.className = 'account-status ok';
        }
      }
      if (errs.length) console.warn('[GAS Push] erreurs:', errs);
      await refreshSyncStatus();
    } catch (e) {
      if (status) { status.textContent = 'Echec: ' + e.message; status.className = 'account-status err'; }
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-cloud-upload-alt"></i> Pousser vers Google Sheets'; }
    }
  }

  const ETAT_SYNC = {
    identique: { txt: 'Identique', icone: 'fa-check-circle', cls: 'ok' },
    different: { txt: 'Differences', icone: 'fa-exclamation-triangle', cls: 'warn' },
    erreur: { txt: 'Erreur', icone: 'fa-times-circle', cls: 'err' },
    local: { txt: 'A modifier en ligne', icone: 'fa-arrow-up', cls: 'warn' },
    'hors_ligne': { txt: 'Hors ligne', icone: 'fa-plug', cls: 'err' },
    inconnu: { txt: 'A verifier', icone: 'fa-question-circle', cls: 'muted' }
  };

  const LIBELLE_DATASET = {
    profils: 'Profils', identifiants: 'Personnes & equipes', flotte: 'Machines',
    positions: 'Positions', affectation: 'Affectations', blasts: 'Blasts',
    notifications: 'Notifications',
  };

  function renderSyncDatasets(datasets) {
    const body = $('#syncDatasets tbody');
    if (!body) return;
    if (!datasets || !datasets.length) {
      body.innerHTML = '<tr><td colspan="6" class="muted">Aucun etat disponible</td></tr>';
      majSante(null);
      return;
    }
    const fdate = (s) => (s ? String(s).replace('T', ' ') : '-');
    body.innerHTML = datasets.map((d) => {
      const etat = ETAT_SYNC[d.etat] || ETAT_SYNC.inconnu;
      let details = d.message || '';
      // traduire l'ecart en langage clair + ce qui se passe automatiquement
      const loc = d.local_count || 0, rem = d.remote_count || 0;
      if (d.dirty) details = details || 'modifie ici — envoi automatique en cours (< 10 s)';
      else if (loc > rem) details = details || 'des elements locaux ne sont pas encore en ligne : envoi automatique';
      else if (rem > loc) details = details || 'des elements en ligne ne sont pas encore recuperes : arrive a la prochaine synchro';
      return '<tr>'
        + '<td class="strong">' + eqEscape(LIBELLE_DATASET[d.dataset] || d.dataset) + (d.dirty ? ' <span class="badge-mod">modifie</span>' : '') + '</td>'
        + '<td>' + loc + '</td>'
        + '<td>' + rem + '</td>'
        + '<td><i class="fas ' + etat.icone + ' sync-etat-' + etat.cls + '"></i> ' + etat.txt + '</td>'
        + '<td>' + fdate(d.dernier_pull || d.dernier_push) + '</td>'
        + '<td class="muted">' + eqEscape(details) + '</td>'
        + '</tr>';
    }).join('');
    majSante(datasets);
  }

  /** Badge de sante global : tout est aligne ? des envois en cours ? un blocage ? */
  function majSante(datasets) {
    const box = document.getElementById('syncSante');
    const txt = document.getElementById('syncSanteTxt');
    if (!box || !txt) return;
    const dot = box.querySelector('.sync-sante-dot');
    if (!datasets || !datasets.length) {
      if (dot) dot.className = 'sync-sante-dot ko';
      txt.textContent = 'Etat de synchronisation indisponible';
      box.className = 'sync-sante ko';
      return;
    }
    const dirty = datasets.filter((d) => d.dirty);
    const ecarts = datasets.filter((d) => !d.dirty && (d.local_count || 0) !== (d.remote_count || 0));
    if (dirty.length) {
      if (dot) dot.className = 'sync-sante-dot warn';
      txt.textContent = dirty.length + ' modification(s) locale(s) — envoi automatique en cours';
      box.className = 'sync-sante warn';
    } else if (ecarts.length) {
      if (dot) dot.className = 'sync-sante-dot warn';
      txt.textContent = ecarts.length + " entite(s) en attente d'alignement — synchro automatique toutes les 5 s";
      box.className = 'sync-sante warn';
    } else {
      if (dot) dot.className = 'sync-sante-dot ok';
      txt.textContent = 'Toutes les donnees sont alignees avec le cloud';
      box.className = 'sync-sante ok';
    }
  }

  async function refreshSyncStatus() {
    const t = $('#syncTime'), s = $('#syncServer'), db = $('#syncDb');
    try {
      const h = await API.health();
      const sync = await API.sync();
      setText(t, 'Derniere sync: ' + (sync.synced_at || now()));
      setText(s, 'Serveur: Connecte (' + API.getApiBase() + ')');
      setText(db, 'Base de donnees: OK');
      if (t) t.style.color = ''; if (s) s.style.color = ''; if (db) db.style.color = '';
    } catch (e) {
      setText(t, 'Derniere sync: -');
      setText(s, 'Serveur: Hors ligne');
      setText(db, 'Base de donnees: inaccessible');
      if (s) s.style.color = '#ef4444';
      if (db) db.style.color = '#ef4444';
    }
    // etat par entite (comparaison locale / Game Protocol via App Script)
    try {
      const st = await API.gasStatus();
      renderSyncDatasets(st.datasets || []);
    } catch (e) {
      renderSyncDatasets(null);
    }
  }

  /** Push vers le Sheet, respectant la couche 2 (Parametres > Connection).
   * Resout sans appel reseau si la couche est coupee. */
  function pushGasSiCouche() {
    // la couche 2 (LAN) n'affecte pas le push cloud : le push GAS reste
    // necessaire pour les postes hors site tant que la couche 2 n'est
    // pas l'echangeur direct entre postes.
    return API.pushGas().catch((err) => console.warn('[Sync] push echec:', err.message));
  }
  window.pushGasSiCouche = pushGasSiCouche;

  async function doSync() {
    const btn = $('#btnSyncNow');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Synchronisation...'; }
    try {
      await API.pullGas();
    } catch (e) { console.warn('[Sync] pull echec:', e.message); }
    await refreshSyncStatus();
    if (btn) {
      btn.innerHTML = '<i class="fas fa-check"></i> Synchronise !';
      setTimeout(() => { btn.textContent = 'Synchroniser maintenant'; btn.disabled = false; }, 1500);
    }
  }

  /* ---- B1 : synchronisation periodique rapprochee ----
   * Toutes les 5 s quand la page est visible : le pull GAS rapproche les
   * references (machines, teams) du poste avec le Sheet. Les couches sont
   * pilotables dans Parametres > Connection : pull coupe = boucle arretee,
   * push coupe = on n'envoie pas ce poste vers le Sheet.
   * Branche sur TOUTES les pages. Pause onglet cache (quota GAS). */
  let syncTimer = null;
  let syncCouches = { pull: true, lan: true, office: true, bandeau: true };

  function lireCouches() {
    if (typeof API === 'undefined' || !API.getConfig) return;
    API.getConfig().then((d) => {
      const p = (d && d.params) || {};
      if (p.sync_couche_pull !== undefined) syncCouches.pull = p.sync_couche_pull === '1';
      if (p.sync_couche_lan !== undefined) syncCouches.lan = p.sync_couche_lan === '1';
      if (p.sync_couche_office !== undefined) syncCouches.office = p.sync_couche_office === '1';
      if (p.sync_bandeau_visible !== undefined) syncCouches.bandeau = p.sync_bandeau_visible === '1';
      window.syncCouches = syncCouches;
      document.dispatchEvent(new CustomEvent('sync:couches', { detail: syncCouches }));
    }).catch(() => {});
  }

  function demarrerSyncPeriodique() {
    if (!API || !API.pullGas) return;   // toutes les pages de l'app
    lireCouches();
    setInterval(lireCouches, 15000);
    const boucle = () => {
      if (document.hidden) return;
      if (syncCouches.pull) {
        API.pullGas().catch((e) => console.warn('[Sync] pull echec:', e.message));
      }
      if (document.getElementById('page-settings')) refreshSyncStatus();
    };
    boucle();
    syncTimer = setInterval(boucle, 5000);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearInterval(syncTimer); syncTimer = null; }
      else { boucle(); syncTimer = setInterval(boucle, 5000); }
    });
  }

  function initServerUi() {
    // Settings
    initOngletsSettings();
    demarrerSyncPeriodique();
    // page Sync : auto-refresh du tableau de bord sante
    if (document.getElementById('syncDatasets')) {
      refreshSyncStatus();
      setInterval(() => { if (!document.hidden) refreshSyncStatus(); }, 5000);
    }
    const btnTest = $('#btnTestDb');
    if (btnTest) btnTest.addEventListener('click', testConnection);
    const btnSave = $('#btnSaveConfig');
    if (btnSave) btnSave.addEventListener('click', saveConfig);
    const btnFacteurs = $('#btnTruckFactors');
    if (btnFacteurs) btnFacteurs.addEventListener('click', saveTruckFactors);
    const selBackend = $('#cfgBackend');
    if (selBackend) selBackend.addEventListener('change', toggleBackend);
    const btnPull = $('#btnPullGas');
    if (btnPull) btnPull.addEventListener('click', pullFromGas);
    const btnMigrate = $('#btnMigrateGas');
    if (btnMigrate) btnMigrate.addEventListener('click', migrateFromGas);
    const btnDiag = $('#btnDiagGas');
    if (btnDiag) btnDiag.addEventListener('click', diagnosticFromGas);

    // Sync
    const btnSync = $('#btnSyncNow');
    if (btnSync) btnSync.addEventListener('click', doSync);
    const btnPush = $('#btnPushGas');
    if (btnPush) btnPush.addEventListener('click', pushFromGas);

    // Bouton dashboard (menu / sidebar / header)
    document.querySelectorAll('[data-action="open-dashboard"]').forEach(el => {
      el.addEventListener('click', openDashboard);
    });

    // pre-remplir le formulaire de connexion depuis le serveur
    loadConfigIntoForm();

    // ---- preferences (champs du panneau) ----
    initPrefsUI();

    // ---- equipements (Spotter / Truck / Excavator) ----
    initEquipUI();

    // ---- tablettes + affectations ----
    initTabletUi();

    // rafraichir l'etat de connexion quand on arrive sur Settings/Sync.
    // Les pages sont des fichiers distincts : on observe celles qui sont
    // presentes dans le document courant.
    const observePages = () => {
      const cibles = [$('#page-settings'), $('#page-sync')].filter(Boolean);
      if (!cibles.length) return;
      const check = () => {
        const visible = cibles.some(el =>
          !el.hidden && (getComputedStyle(el).display !== 'none'));
        if (visible) refreshSyncStatus();
      };
      const obs = new MutationObserver(check);
      cibles.forEach(el => obs.observe(el, { attributes: true, attributeFilter: ['class', 'style'] }));
      check();
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', observePages);
    } else {
      observePages();
    }
  }

  /* ============ SETTINGS : onglets + bandeau de synchronisation ============
   * La page est decoupee en 4 onglets (Connexion / Flotte / Equipes /
   * Terrain). Le bandeau affiche l'etat du lien avec le serveur de
   * references (SaaS) : dernier echange reussi, ou hors ligne.
   * Revenir a la section FLotte met les compteurs de machines a jour. */
  function initOngletsSettings() {
    const barre = document.getElementById('settingsOnglets');
    if (!barre) return; // pas sur la page Settings

    // navigation par onglet
    barre.addEventListener('click', (e) => {
      const b = e.target.closest('.onglet');
      if (!b) return;
      barre.querySelectorAll('.onglet').forEach((x) => x.classList.toggle('actif', x === b));
      const cible = b.dataset.onglet;
      document.querySelectorAll('#page-settings .onglet-pane').forEach((p) => {
        const actif = p.dataset.onglet === cible;
        p.classList.toggle('actif', actif);
        p.hidden = !actif;
      });
    });

    // bandeau de sync : ping leger du SaaS toutes les 10 s
    const dot = document.getElementById('syncDot');
    const texte = document.getElementById('syncTexte');
    let dernierOk = null;
    const ping = () => {
      API.getConfig().then(() => {
        dernierOk = new Date();
        if (dot) dot.className = 'sync-dot ok';
        if (texte) {
          texte.textContent = 'Synchronise — dernier echange ' +
            dernierOk.toLocaleTimeString();
        }
      }).catch(() => {
        if (dot) dot.className = 'sync-dot ko';
        if (texte) {
          texte.textContent = 'Hors ligne' + (dernierOk
            ? ' — dernier echange ' + dernierOk.toLocaleTimeString()
            : ' — aucune connexion au serveur');
        }
      });
    };
    ping();
    setInterval(ping, 10000);

    const btn = document.getElementById('btnSyncMaintenant');
    if (btn) btn.addEventListener('click', () => {
      if (texte) texte.textContent = 'Synchronisation en cours...';
      ping();
      if (typeof chargerToutEquipements === 'function') chargerToutEquipements();
    });

    // compteurs de machines : ecrits par renderEquipList (19/11)
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initServerUi);
  } else {
    initServerUi();
  }

  // expose pour usage externe (app.js, gnss-serial.js, console)
  window.OreTrackingServerUi = {
    testConnection, refreshSyncStatus, doSync, openDashboard, toggleBackend,
    pullFromGas, pushFromGas, migrateFromGas, diagnosticFromGas,
    loadEquipList, loadEquipLists: async () => {
      for (const t of Object.keys(EQUIP_TYPES)) await loadEquipList(t);
    },
    refreshAssignments, renderGnssRegistry, loadGnssRegistry, renderFleet, renderTruckFactors,
  };
})();