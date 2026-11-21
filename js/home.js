/* =====================================================================
 * home.js — page Home : le voyant du site.
 * Tout est calcule depuis les donnees passerelle (missions-ui.js) :
 *   - voyant vert / orange / rouge (regle : alerte non vue > seuil
 *     reglable, DT charge sans mission, passerelle muette)
 *   - 4 compteurs : engins, alertes a traiter, missions, tonnage
 *   - la "file du matin" : les choses a traiter, cliquables vers
 *     la page qui les traite.
 * ===================================================================== */
(function () {
  'use strict';

  // Les raisons renvoyees par MSUI sont techniques (« offroad non vue
  // (DT-02) »). On les reformule pour l'affichage, sans changer la regle
  // de calcul du voyant (niveau rouge/jaune/vert reste identique).
  function raisonLisible(v, etat) {
    const brut = String(v.raison || '');
    const code = (brut.match(/\((DT-?\d+)\)/i) || [])[1];
    const aTraiter = (etat.alertes || [])
      .filter(MSUI.aTraiter).length;

    if (/offroad/i.test(brut)) {
      return code
        ? 'Alerte de sortie de route non lue sur ' + code.toUpperCase()
        : 'Alertes de sortie de route non lues';
    }
    if (/mission/i.test(brut) && /dev/i.test(brut)) {
      return code
        ? 'Mission déviée non traitée sur ' + code.toUpperCase()
        : 'Missions déviées non traitées';
    }
    if (/passerelle|muette|down/i.test(brut)) {
      return 'La passerelle ne répond plus';
    }
    if (aTraiter) {
      return aTraiter + (aTraiter > 1 ? ' alertes restent à traiter' : ' alerte reste à traiter');
    }
    return brut;
  }

  function rendreVoyant(etat) {
    const box = document.getElementById('homeVoyant');
    if (!box) return;
    const v = MSUI.voyant();
    const pastille = box.querySelector('.voyant-pastille');
    const texte = box.querySelector('.voyant-texte');
    if (pastille) pastille.className = 'voyant-pastille ' + v.niveau;
    if (texte) {
      texte.textContent = raisonLisible(v, etat);
      if (etat.passerelleDown && etat.dernierReussi) {
        texte.textContent += ' — données de ' + etat.dernierReussi.toLocaleTimeString('fr-FR');
      }
    }

    // Titre du bandeau : traduction du niveau en une phrase lisible,
    // affichee dans la maquette au-dessus du detail du voyant.
    const titre = document.getElementById('homeVoyantTxt');
    if (titre) {
      const aTraiter = (etat.alertes || [])
        .filter(MSUI.aTraiter).length;
      if (etat.passerelleDown) {
        titre.textContent = 'Passerelle injoignable';
      } else if (v.niveau === 'rouge') {
        titre.textContent = aTraiter
          ? aTraiter + (aTraiter > 1 ? ' alertes demandent ' : ' alerte demande ') + 'votre attention'
          : 'Le site demande votre attention';
      } else if (v.niveau === 'jaune') {
        titre.textContent = 'Le site demande une verification';
      } else {
        titre.textContent = 'Le site fonctionne normalement';
      }
    }
  }

  // ------------------------------------------------------------ compteurs : sources REELLES
  //  - engins suivis : DT dont la position est fraiche (/api/suivi_dt : pas « signal perdu », 2 min) / parc DT (SaaS)
  //  - alertes a traiter : alertes non vues et non informatives (MSUI)
  //  - missions en cours : missions au statut en_cours (MSUI)
  //  - tonnes du jour : production reelle de la journee 06 h - 06 h lue chez Serveur_SG (/api/kpi), minerai + waste
  // Aucune valeur par defaut, aucune estimation : une source muette affiche « — », pas un chiffre invente.
  const COMPTEURS = { suivi: null, kpi: null };

  function rendreStats(etat) {
    const alertes = (etat.alertes || []).filter(MSUI.aTraiter);
    const missions = (etat.missions || []).filter((m) => m.statut === 'en_cours');

    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    // Parc total : referentiel SaaS (sync_flotte, camions seulement : les excavatrices ne roulent pas de missions).
    const parc = (typeof window.PARC_ENGINS === 'number' && window.PARC_ENGINS > 0) ? window.PARC_ENGINS : null;
    if (COMPTEURS.suivi) {
      const enDirect = COMPTEURS.suivi.filter((e) => e.type === 'DT' && e.etat !== 'signal_perdu').length;
      set('statEngins', parc ? (enDirect + ' / ' + parc) : String(enDirect));
    } else {
      set('statEngins', '—');
    }
    set('statAlertes', String(alertes.length).padStart(2, '0'));
    set('statMissions', String(missions.length).padStart(2, '0'));
    const k = COMPTEURS.kpi;
    set('statTonnage', k ? formaterTonnage(Math.round((Number(k.minerais_t) || 0) + (Number(k.waste_t) || 0))) : '—');
  }

  function lireCompteurs() {
    const base = basePasserelleHome();
    fetch(base + '/api/suivi_dt', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
      .then((d) => { COMPTEURS.suivi = Array.isArray(d) ? d : (d && (d.engins || null)); if (TERRAIN.etat) rendreStats(TERRAIN.etat); })
      .catch(() => { COMPTEURS.suivi = null; });
    fetch(base + '/api/kpi', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
      .then((d) => { COMPTEURS.kpi = d && typeof d.minerais_t !== 'undefined' ? d : null; if (TERRAIN.etat) rendreStats(TERRAIN.etat); })
      .catch(() => { COMPTEURS.kpi = null; });
  }

  // Tonnage lisible : « 53 500 t » avec séparateur milliers.
  function formaterTonnage(t) {
    return String(t).replace(/\B(?=(\d{3})+(?!\d))/g, '\u202f') + ' t';
  }

    // Un code engin absent ne doit rien afficher (pas de tiret orphelin).
    const codePropre = (v) => {
      const t = String(v == null ? '' : v).trim();
      return (t === '—' || t === '-' || t === '–' || t === 'null' || t === 'undefined') ? '' : t;
    };

  // Mise en forme d'un message d'alerte : la base stocke les libelles en
  // majuscules (« SORTIE DE ROUTE : ... »). On les ramene a une casse
  // lisible pour l'affichage, sans abimer les codes engin (DT-02,
  // DT-019) qui doivent rester en capitales. Aucune donnee modifiee.
  function casseLisible(txt) {
    const s = String(txt || '').trim();
    if (!s) return '';

    // On ne touche qu'au PREMIER mot quand il est entierement en majuscules
    // (« SORTIE DE ROUTE : ... » -> « Sortie de route : ... »).
    // Les codes techniques (BLAST, TEST_BASE, 242.5_OM_LWP2) gardent
    // leur casse d'origine, et les codes engin restent en capitales.
    const m = s.match(/^([A-Z][A-Z\s]{2,}?)(?=\s*[:—·]|\s+[a-z0-9])/);
    let out = s;
    if (m) {
      const premier = m[1].trim();
      const bas = premier.toLowerCase();
      out = bas.charAt(0).toUpperCase() + bas.slice(1) + s.slice(premier.length);
    }
    return out.replace(/\bdt-(\d+)\b/gi, function (t, n) { return 'DT-' + n; });
  }

  // Duree ecoulee, format court et lisible (« 12 min », « 7 h », « 2 j »).
  // Meme donnee que MSUI.ilYA, mise en forme pour la colonne « Delai ».
  function delaiCourt(createdAt) {
    if (!createdAt) return '';
    const d = new Date(String(createdAt).replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return '';
    const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    const h = Math.floor(s / 3600);
    if (h < 24) return h + ' h';
    const j = Math.floor(h / 24);
    return j + ' j ' + (h % 24) + ' h';
  }

  // Heure de l'evenement, affichee dans la colonne « Heure / Engin »
  // comme sur la maquette. Simple mise en forme d'une donnee deja recue.
  function heureDe(createdAt) {
    const d = new Date(String(createdAt || '').replace(' ', 'T') + 'Z');
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  }

  // File du matin : regles simples, chaque item mene a la page qui traite.
  function rendreFile(etat) {
    const box = document.getElementById('homeFile');
    if (!box) return;
    const items = [];
    const seuilMs = (etat.seuilRougeMin || 10) * 60000;

    for (const a of (etat.alertes || [])) {
      if (!MSUI.aTraiter(a)) continue;
      const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
      const vieux = Date.now() - t > seuilMs;
      // maquette : titre + sous-titre, decoupe sur « : » ou « — »
      const brut = casseLisible(a.message);
      const sep = brut.search(/[:\u2014]/);
      const titre = sep > 0 ? brut.slice(0, sep).trim() : brut;
      const detail = sep > 0 ? brut.slice(sep + 1).trim() : '';
      items.push({
        urgence: vieux ? 2 : 1,
        id: a.id,
        titre: titre,
        detail: detail,
        code: codePropre(a.dt_code),
        heure: heureDe(a.created_at),
        age: delaiCourt(a.created_at),
        lien: 'alerts.html',
      });
    }
    // DT charge sans mission
    const enMission = new Set(
      (etat.missions || []).filter((m) => m.statut === 'en_cours').map((m) => m.dt_code));
    for (const e of (etat.engins || [])) {
      if (e.type === 'DT' && e.statut === 'ODT' && !enMission.has(e.code)) {
        items.push({
          urgence: 2,
          titre: 'Charge sans mission',
          detail: 'est chargé (ODT) mais n\'a pas de mission',
          code: codePropre(e.code),
          heure: new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' }),
          lien: 'senor.html',
        });
      }
    }
    // missions deviees
    for (const m of (etat.missions || [])) {
      if (m.statut === 'en_cours' && Number(m.devie) === 1 || m.statut === 'devoie') {
        items.push({
          urgence: 1,
          titre: 'Mission déviée',
          detail: Math.round(m.ecart_m || 0) + ' m du trajet, bloc #' + m.orebloc_id,
          code: codePropre(m.dt_code),
          heure: new Date().toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' }),
          lien: 'senor.html',
        });
      }
    }

    if (!items.length) {
      box.innerHTML = '<div class="member-empty">Rien a signaler. Bon shift !</div>';
      return;
    }
    items.sort((a, b) => b.urgence - a.urgence);
    // Rendu de la file comme la maquette : heure au-dessus du code,
    // titre + sous-titre, delai, et un bouton « Marquer vu » par ligne.
    // Le bouton appelle la vraie route d'acquittement (POST /api/alertes).
    box.innerHTML = items.slice(0, 8).map((it) =>
      '<div class="file-item u' + it.urgence + '">' +
        '<div class="file-cell">' +
          '<div class="file-slot">' +
            '<span class="file-quand">' +
              (it.heure ? '<em class="file-heure">' + it.heure + '</em>' : '') +
              (it.code ? '<b class="file-code">' + MSUI.esc(it.code) + '</b>' : '') +
            '</span>' +
            '<span class="file-quoi">' +
              '<span class="file-titre">' + MSUI.esc(it.titre) + '</span>' +
              (it.detail ? '<span class="file-detail">' + MSUI.esc(it.detail) + '</span>' : '') +
            '</span>' +
            '<span class="file-age">' + (it.age || '') + '</span>' +
            '<button type="button" class="file-marquer" data-alerte="' + (it.id || '') + '">' +
              'Marquer vu</button>' +
          '</div>' +
        '</div>' +
      '</div>').join('');
  }

  // « Marquer vu » : branche sur la route d'acquittement existante.
  // On delegue a alerts-ui si present, sinon appel direct a la passerelle.
  function brancherMarquerVu() {
    const box = document.getElementById('homeFile');
    if (!box) return;
    box.addEventListener('click', (e) => {
      const btn = e.target.closest('.file-marquer');
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      const id = btn.getAttribute('data-alerte');
      if (!id) return;
      btn.disabled = true;
      btn.textContent = 'Enregistrement...';

      const faire = (window.AlertsUI && typeof AlertsUI.acquitter === 'function')
        ? AlertsUI.acquitter(id)
        : fetch(basePasserelleHome() + '/api/alertes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'acquit', id: id }),
          }).then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); });

      Promise.resolve(faire)
        .catch(() => {
          btn.disabled = false;
          btn.textContent = 'Marquer vu';
        });
    });
  }

  // base passerelle : meme logique que missions-ui (OTCFG, sinon :8101)
  function basePasserelleHome() {
    if (window.OTCFG && typeof OTCFG.passerelle === 'function') return OTCFG.passerelle();
    return 'http://' + location.hostname + ':8101';
  }

  // Total du parc (referentiel administratif, SaaS :8100, table sync_flotte).
  // Différent du nb d'engins suivis en direct par la passerelle : le parc
  // compte tout le materiel, la passerelle ne voit que celui qui emet.
  function lireParc() {
    if (typeof API === 'undefined' || !API.getRows) return;
    API.getRows('sync_flotte').then((d) => {
      // parc de camions uniquement : les 8 excavatrices du meme referentiel
      // ne roulent pas de missions, les compter gonflerait le denominateur
      window.PARC_ENGINS = ((d && d.rows) || [])
        .filter((r) => r.actif !== 0 && String(r.code || '').startsWith('DT-')).length;
    }).catch(() => { window.PARC_ENGINS = window.PARC_ENGINS || null; });
  }

  // Panneau SHIFT : horaires et progression CALCULÉS d'après l'heure
  // réelle (shift de jour 06:00–18:00, de nuit 18:00–06:00). Rien en dur.
  function rendreShift() {
    const h = document.querySelector('.shift-hours');
    const el = document.querySelector('.shift-elapsed');
    const bar = document.querySelector('.shift-progress span');
    if (!h || !el || !bar) return;

    const now = new Date();
    const min = now.getHours() * 60 + now.getMinutes();
    const jour = min >= 360 && min < 1080; // 06:00 → 18:00
    const debut = jour ? 360 : 1080;
    const duree = 720; // 12 h
    const ecoule = ((min - debut) + 1440) % 1440;
    const clamp = Math.max(0, Math.min(duree, ecoule));

    const fmt = (m) =>
      String(Math.floor(m / 60) % 24).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');

    h.innerHTML = fmt(debut) + ' <span class="shift-dash">—</span> ' + fmt(debut + duree);
    el.textContent =
      Math.floor(clamp / 60) + ' h ' + (clamp % 60) + ' écoulées sur 12 heures';
    bar.style.width = Math.round((clamp / duree) * 100) + '%';
  }

  // Le titre du panneau suit le shift réel (jour/nuit).
  function titreShift() {
    const t = document.querySelector('.panel-side-title');
    if (!t) return;
    const min = new Date().getHours() * 60 + new Date().getMinutes();
    t.textContent = (min >= 360 && min < 1080) ? 'Shift de jour' : 'Shift de nuit';
  }

  // ---------------------------------------------------------------- Le terrain
  // Trois lignes du panneau, toutes calculees : jamais de texte en dur.
  const TERRAIN = { etat: null, blasts: [], equipes: null, relais: null, cloud: null };

  function ecrireItem(id, titre, detail) {
    const el = document.getElementById(id);
    if (!el) return;
    el.querySelector('.side-item-titre').textContent = titre;
    el.querySelector('.side-item-detail').textContent = detail || '';
  }

  function dansPolygone(x, y, poly) {
    let dedans = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) dedans = !dedans;
    }
    return dedans;
  }

  // Excavatrices en minage (statut OMEX : dans un orebloc ET position fraiche) : code, pit, blast.
  function rendreMinage() {
    const etat = TERRAIN.etat;
    if (!etat) return;
    const exca = (etat.engins || []).filter((e) => e.type === 'EXCA' && e.statut === 'OMEX' && e.actif !== 0);
    if (!exca.length) {
      ecrireItem('terrainMinage', 'Aucune excavatrice en minage', 'Elles apparaissent ici dès qu\'une pelle travaille dans un bloc.');
      return;
    }
    const blastDe = (e) => {
      for (const b of TERRAIN.blasts) {
        for (const o of (b.oreblocs || [])) {
          let pts = o.sommets;
          if (typeof pts === 'string') { try { pts = JSON.parse(pts); } catch (x) { pts = null; } }
          if (Array.isArray(pts) && pts.length >= 3 && dansPolygone(e.x, e.y, pts)) return b;
        }
      }
      return null;
    };
    const pitDe = (b) => {
      const n = String((b && (b.numero || b.nom)) || '').toUpperCase();
      return /LMP/.test(n) ? 'Main Pit' : (/LWP/.test(n) ? 'West Pit' : '');
    };
    const premiere = exca[0];
    const b = blastDe(premiere);
    const pit = pitDe(b);
    const codes = exca.map((e) => e.code).join(', ');
    ecrireItem('terrainMinage', (pit ? pit + ' · ' : '') + codes,
      'Minage en cours' + (b ? ' · ' + (b.numero || b.nom) : '') + (exca.length > 1 ? ' · ' + exca.length + ' excavatrices' : ''));
  }

  // Equipes spotter actives (referentiel SaaS, table sync_identifiants, profil SPOT) : « Alpha · K. Traoré / ... ».
  function rendreEquipes() {
    const rows = TERRAIN.equipes;
    if (!rows) { ecrireItem('terrainEquipes', 'Équipes spotter', 'Connectez-vous pour les afficher.'); return; }
    const parEquipe = {};
    rows.filter((r) => r.profil === 'SPOT').forEach((r) => {
      const cle = r.equipe || '(sans équipe)';
      (parEquipe[cle] = parEquipe[cle] || []).push(r);
    });
    const actives = Object.keys(parEquipe).filter((c) => parEquipe[c].some((r) => r.actif === 1)).sort();
    if (!actives.length) { ecrireItem('terrainEquipes', 'Aucune équipe spotter active', ''); return; }
    const detail = actives.map((c) => {
      const chef = parEquipe[c].find((r) => String(r.role || '').toUpperCase() === 'CHEF_EQUIPE');
      return c + (chef ? ' · ' + (chef.nom || chef.identifiant || '') : '');
    }).join(' / ');
    ecrireItem('terrainEquipes', actives.length + (actives.length > 1 ? ' équipes spotter actives' : ' équipe spotter active'), detail);
  }

  // Liaisons : Cloud (SaaS joignable), LAN temps reel (passerelle joignable), Office (relais configure).
  function rendreLiaisons() {
    const etat = TERRAIN.etat;
    const lan = !!etat && !etat.passerelleDown;
    const cloud = TERRAIN.cloud === true;
    const office = !!(TERRAIN.relais && TERRAIN.relais.office_actif);
    const liste = [['Cloud', cloud], ['LAN temps réel', lan], ['Office', office]];
    const ok = liste.filter((l) => l[1]);
    const mots = ['Aucune', 'Une', 'Deux', 'Trois'];
    const titre = ok.length === 0 ? 'Aucune liaison connectée'
      : mots[ok.length] + (ok.length > 1 ? ' liaisons connectées' : ' liaison connectée');
    const hors = liste.filter((l) => !l[1]).map((l) => l[0]);
    ecrireItem('terrainLiaisons', titre, ok.map((l) => l[0]).join(' · ') + (hors.length ? ' — hors ligne : ' + hors.join(', ') : ''));
  }

  function rendreTerrain() { rendreMinage(); rendreEquipes(); rendreLiaisons(); }

  function lireTerrain() {
    const base = basePasserelleHome();
    fetch(base + '/api/blasts', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d && d.blasts) TERRAIN.blasts = d.blasts; rendreTerrain(); }).catch(() => {});
    fetch(base + '/api/relais', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null))
      .then((d) => { TERRAIN.relais = d; rendreLiaisons(); }).catch(() => { TERRAIN.relais = null; rendreLiaisons(); });
    if (typeof API !== 'undefined' && API.getConfig) {
      API.getConfig().then(() => { TERRAIN.cloud = true; rendreLiaisons(); }).catch(() => { TERRAIN.cloud = false; rendreLiaisons(); });
    }
    if (typeof API !== 'undefined' && API.getRows) {
      API.getRows('sync_identifiants').then((d) => { TERRAIN.equipes = (d && d.rows) || []; rendreEquipes(); })
        .catch(() => { TERRAIN.equipes = null; rendreEquipes(); });
    }
  }

  function init() {
    if (!document.getElementById('homeVoyant')) return;
    if (!window.MSUI) return;
    lireParc();
    setInterval(lireParc, 60000);
    MSUI.onTick((etat) => { TERRAIN.etat = etat; rendreVoyant(etat); rendreStats(etat); rendreFile(etat); rendreMinage(); rendreLiaisons(); });
    lireTerrain();
    setInterval(lireTerrain, 60000);
    lireCompteurs();
    setInterval(lireCompteurs, 10000);
    MSUI.demarrer(5000);
    brancherMarquerVu();
    rendreShift();
    titreShift();
    setInterval(rendreShift, 60000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
