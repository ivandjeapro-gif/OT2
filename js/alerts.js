/* =====================================================================
 * alerts.js — page Alerts : flux d'alertes reel (passerelle via
 * missions-ui.js), filtres et acquittement.
 * Types reels : deviation, retour_trajet, offroad, retour_piste,
 * non_conforme, fin_mission, arrivee, mission ; 24/11 : mauvaise_direction, demi_tour, retour_direction
 * (alerte unique du chauffeur). Statuts : Off-route / Wrong Dumping (colonne et filtre).
 * Les types de la maquette sans donnees (carburant, meteo, grade)
 * ne sont pas pretendus : ils reviendront quand la source existera.
 * ===================================================================== */
(function () {
  'use strict';

  const TYPE_LIBELLE = {
    deviation: 'Deviation',
    retour_trajet: 'Retour sur trajet',
    offroad: 'Sortie de route',
    retour_piste: 'Retour sur piste',
    non_conforme: 'Dumping non conforme',
    fin_mission: 'Fin de mission',
    blast_rejete: 'Blast refuse',
    checkpoint: 'Checkpoint saute',
    checkpoint_interdit: 'Checkpoint interdit',
    zone_approche: 'Zone interdite proche',
    zone_interdite: 'Entrée en zone interdite',
    dumping_hors_zone: 'Dumping hors zone',
    arrivee: 'Arrivee DA',
    mission: 'Nouvelle mission',
    // 24/11 : alerte unique du chauffeur
    mauvaise_direction: 'Mauvaise direction',
    demi_tour: 'Demi-tour en cours',
    retour_direction: 'Retour bonne direction',
    // 27/11 : panne / mecanique avec materiau a bord (bureau seulement)
    arret_charge: 'DT chargé à l\'arrêt',
    reprise_arret: 'DT chargé reparti',
  };

  // 24/11 (decision utilisateur) : statuts de la page Alertes. Meme classement que la passerelle (STATUT_ALERTE).
  const STATUT = {
    offroad: 'off_route', retour_piste: 'off_route', deviation: 'off_route', retour_trajet: 'off_route', checkpoint: 'off_route',
    mauvaise_direction: 'wrong_dumping', demi_tour: 'wrong_dumping', retour_direction: 'wrong_dumping',
    checkpoint_interdit: 'wrong_dumping', zone_approche: 'wrong_dumping', zone_interdite: 'wrong_dumping',
    dumping_hors_zone: 'wrong_dumping', non_conforme: 'wrong_dumping',
  };
  const STATUT_LIBELLE = { off_route: 'Off-route', wrong_dumping: 'Wrong Dumping' };
  const statutDe = (a) => a.statut || STATUT[a.type] || '';

  // alertes d'information : rien a traiter
  // 25/11 (calibrage) : meme regle que toutes les pages (champ `info` de la passerelle, via MSUI)
  const TYPES_INFO = { reprise_arret: 1, fin_mission: 1, arrivee: 1, mission: 1, retour_piste: 1, retour_trajet: 1, retour_direction: 1,
    demi_tour: 1, checkpoint_valide: 1, zone_approche: 1, blast_rejete: 1 };
  const estInfo = (a) => (window.MSUI && MSUI.estInfo) ? MSUI.estInfo(a) : !!TYPES_INFO[a.type];
  const debutShift = () => (window.MSUI && MSUI.debutShiftMs) ? MSUI.debutShiftMs() : debutJourMs();

  let filtreDt = '';
  let filtreType = '';
  let filtreStatut = '';
  let seulementNonVues = true;
  let filtrePeriode = '';
  let filtreDate = '';

  function passerelle() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return (location.port === '8100' ? location.protocol + '//' + (location.hostname || 'localhost') + ':8101' : location.origin + '/passerelle');
  }

  function acquitter(id) {
    return fetch(passerelle() + '/api/alertes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(id === 'tous' ? { tous: true } : { id: id }),
    }).catch(() => { });
  }

  const deux = (n) => String(n).padStart(2, '0');
  const MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
    'septembre', 'octobre', 'novembre', 'décembre'];
  const ms = (a) => new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();

  // Journee d'exploitation : 06 h - 06 h (comme le tonnage de Home)
  function debutJourMs() {
    const d = new Date();
    if (d.getHours() < 6) d.setDate(d.getDate() - 1);
    d.setHours(6, 0, 0, 0);
    return d.getTime();
  }

  // Compteurs de la rangee, pied de tableau et date : calcules sur les vraies alertes
  function rendreCompteurs(etat, affichees) {
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    const seuilMs = (etat.seuilRougeMin || 10) * 60000;
    const debut = debutJourMs();
    const jour = (etat.alertes || []).filter((a) => ms(a) >= debut);
    // non vues / en retard : shift en cours, comme la pastille de la barre et Home
    const nonVues = jour.filter((a) => ms(a) >= debutShift() && !Number(a.acquittee) && !estInfo(a));
    const enRetard = nonVues.filter((a) => Date.now() - ms(a) > seuilMs);
    set('rCountTotal', deux(jour.length));
    set('rCountUnseen', deux(nonVues.length));
    set('rCountLate', deux(enRetard.length));
    set('rCountLateLabel', 'au-delà de ' + (etat.seuilRougeMin || 10) + ' min');
    const n = new Date();
    set('rCounterDate', n.getDate() + ' ' + MOIS[n.getMonth()] + ' ' + n.getFullYear() + ' · ' + deux(n.getHours()) + ':' + deux(n.getMinutes()));
    const aVoir = affichees.filter((a) => !Number(a.acquittee) && !estInfo(a)).length;
    set('alertsFootLeft', affichees.length + (affichees.length > 1 ? ' événements · ' : ' événement · ') +
      aVoir + ' non vus · ' + (affichees.length - aVoir) + ' vus ou informatifs');
    set('alertsFootSeuil', 'Seuil d’alerte rouge : ' + (etat.seuilRougeMin || 10) + ' minutes · configurable dans Settings');
  }

  // Fenetre [debut, fin[ en ms selon la periode choisie (journee d'exploitation 06 h - 06 h)
  function fenetre() {
    const j = 86400000;
    const d0 = debutJourMs();
    if (filtrePeriode === 'jour') return [d0, d0 + j];
    if (filtrePeriode === 'hier') return [d0 - j, d0];
    if (filtrePeriode === '7j') return [d0 - 6 * j, d0 + j];
    if (filtrePeriode === 'date' && filtreDate) {
      const p = filtreDate.split('-').map(Number);
      const d = new Date(p[0], p[1] - 1, p[2], 6, 0, 0, 0).getTime();
      return [d, d + j];
    }
    return null;
  }

  // Cycle de vie (offroad / deviation) : ouverte -> le DT est revenu (retour_le) -> cloturee quand l'Office l'a aussi vue
  const AVEC_CYCLE = { offroad: 1, deviation: 1, checkpoint: 1, checkpoint_interdit: 1 };
  function dureeCourte(s) {
    s = Math.max(0, Math.round(Number(s) || 0));
    return s >= 60 ? Math.floor(s / 60) + ' min ' + deux(s % 60) + ' s' : s + ' s';
  }
  function etatCycle(a) {
    if (!AVEC_CYCLE[a.type]) return '';
    if (a.retour_le && a.retour_cause === 'arrivee_da') return 'Mission terminée en dumping area à ' + MSUI.heure(a.retour_le);
    if (a.type === 'checkpoint' || a.type === 'checkpoint_interdit') return '';
    return a.retour_le
      ? 'DT revenu à ' + MSUI.heure(a.retour_le) + ' (après ' + dureeCourte(a.retour_duree_s) + ')'
      : 'DT toujours hors trajet';
  }

  function render(etat) {
    const tbody = document.getElementById('alertsBody');
    if (!tbody) return;
    let alertes = etat.alertes || [];

    if (filtreDt) {
      const f = filtreDt.toUpperCase();
      alertes = alertes.filter((a) => String(a.dt_code || '').toUpperCase().indexOf(f) >= 0);
    }
    const fen = fenetre();
    if (fen) alertes = alertes.filter((a) => ms(a) >= fen[0] && ms(a) < fen[1]);
    if (filtreType) alertes = alertes.filter((a) => a.type === filtreType);
    if (filtreStatut) alertes = alertes.filter((a) => statutDe(a) === filtreStatut);
    const avantNonVues = alertes.length;     // 27/11 : pour expliquer une liste vide
    if (seulementNonVues) alertes = alertes.filter((a) => !Number(a.acquittee) && !estInfo(a));

    const compteur = document.getElementById('alertsCompteur');
    if (compteur) {
      const nonVues = (etat.alertes || []).filter((a) => window.MSUI && MSUI.aTraiter ? MSUI.aTraiter(a) : (!Number(a.acquittee) && !estInfo(a))).length;
      compteur.textContent = nonVues ? nonVues + ' a traiter' : '';
    }

    rendreCompteurs(etat, alertes);

    if (!alertes.length) {
      // 27/11 : « Non vues seulement » masque les alertes deja vues et les informations : on le dit, avec le
      // moyen de les afficher (avant : « Aucune alerte. » alors que le compteur annoncait des alertes du jour)
      const masquees = seulementNonVues ? avantNonVues : 0;
      tbody.innerHTML = '<tr><td colspan="7" class="muted">' +
        (etat.passerelleDown ? 'Passerelle injoignable'
          : masquees ? 'Aucune alerte non vue. ' + masquees + ' alerte' + (masquees > 1 ? 's' : '') +
            ' déjà vue' + (masquees > 1 ? 's' : '') + ' ou informative' + (masquees > 1 ? 's' : '') + ' masquée' + (masquees > 1 ? 's' : '') +
            ' · <button type="button" class="r-lien" id="btnVoirToutes">Afficher toutes les alertes</button>'
          : 'Aucune alerte.') +
        '</td></tr>';
      const voir = document.getElementById('btnVoirToutes');
      if (voir) voir.addEventListener('click', () => {
        const nv = document.getElementById('fltNonVues');
        if (nv) nv.checked = false;
        seulementNonVues = false;
        render(MSUI.etat);
      });
      return;
    }

    const seuilMs = (etat.seuilRougeMin || 10) * 60000;
    tbody.innerHTML = alertes.map((a) => {
      const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
      const age = Date.now() - t;
      const aTraiter = !estInfo(a) && !Number(a.acquittee);
      const urgent = aTraiter && age > seuilMs;
      const cls = urgent ? 'sev-high' : (estInfo(a) ? 'status-done' : 'sev-medium');
      return '<tr' + (urgent ? ' class="alerte-urgente"' : '') + '>' +
        '<td>' + MSUI.heure(a.created_at) + '</td>' +
        '<td>' + MSUI.esc(a.dt_code) + '</td>' +
        '<td>' + (statutDe(a) ? '<span class="alert-statut alert-statut-' + statutDe(a) + '">' + STATUT_LIBELLE[statutDe(a)] + '</span>' : '') + '</td>' +
        '<td>' + MSUI.esc(TYPE_LIBELLE[a.type] || a.type) + '</td>' +
        '<td>' + MSUI.esc(a.message) +
          (etatCycle(a) ? '<br><small class="' + (a.retour_le ? 'muted' : 'sev-high') + '">' + MSUI.esc(etatCycle(a)) + '</small>' : '') + '</td>' +
        '<td>' + (a.acquittee
          ? '<span class="status-done">Vue</span>'
          : (aTraiter ? '<span class="' + cls + '">Non vue</span>'
                      : '<span class="status-done">Info</span>')) + '</td>' +
        '<td>' + (aTraiter ?
          '<button class="team-btn" data-acquit="' + a.id + '">Marquer vu</button>' : (a.acquittee ? '<span class="muted">' + (AVEC_CYCLE[a.type] ? (a.retour_le ? 'Clôturée' : 'Vue · DT hors trajet') : 'Acquittée') + '</span>' : '')) +
        '</td>' +
        '</tr>';
    }).join('');
  }

  function init() {
    if (!document.getElementById('alertsBody')) return;
    if (!window.MSUI) return;

    const dt = document.getElementById('fltDt');
    if (dt) dt.addEventListener('input', () => { filtreDt = dt.value.trim(); render(MSUI.etat); });
    const st = document.getElementById('fltStatut');
    if (st) st.addEventListener('change', () => { filtreStatut = st.value; render(MSUI.etat); });
    const ty = document.getElementById('fltType');
    if (ty) ty.addEventListener('change', () => { filtreType = ty.value; render(MSUI.etat); });
    const per = document.getElementById('fltPeriode');
    const dateBox = document.getElementById('fltDateBox');
    const dat = document.getElementById('fltDate');
    if (per) per.addEventListener('change', () => {
      filtrePeriode = per.value;
      if (dateBox) dateBox.hidden = per.value !== 'date';
      render(MSUI.etat);
    });
    if (dat) dat.addEventListener('change', () => { filtreDate = dat.value; render(MSUI.etat); });
    const nv = document.getElementById('fltNonVues');
    if (nv) nv.addEventListener('change', () => { seulementNonVues = nv.checked; render(MSUI.etat); });

    const tout = document.getElementById('btnAcquitTout');
    if (tout) tout.addEventListener('click', () => {
      const sp = Splash.bloquer('Acquittement global...', 'Toutes les alertes vues');
      acquitter('tous')
        .then(() => { Splash.succes('Toutes les alertes marquees vues'); return MSUI.rafraichir(); })
        .catch(() => Splash.erreur('Acquittement global impossible'));
    });

    // delegation : bouton "Vu" d'une ligne
    document.getElementById('alertsBody').addEventListener('click', (e) => {
      const b = e.target.closest('[data-acquit]');
      if (b) {
        b.disabled = true;
        acquitter(b.dataset.acquit)
          .then(() => { Splash.succes('Alerte marquee comme vue'); return MSUI.rafraichir(); })   // rendu immediate, pas d'attente du tick
          .catch(() => Splash.erreur('Acquittement impossible'));
      }
    });

    MSUI.onTick(render);
    MSUI.demarrer(5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

/* 27/11 : page Alerts en onglets (Alertes · Dumping report · Off-road report), meme modele que Parametres.
   Les filtres engin / periode servent aux trois ; statut, type, « non vues » et « tout marquer vu » ne concernent
   que la liste des alertes : caches sur les rapports. L'onglet choisi est garde pour la session. */
(function initOngletsAlertes() {
  const barre = document.getElementById('alertsOnglets');
  if (!barre) return;
  const CLE = 'oreTrackingAlertsOnglet';
  function montrer(cible) {
    const boutons = barre.querySelectorAll('.onglet');
    if (![...boutons].some((b) => b.dataset.onglet === cible)) cible = 'alertes';
    boutons.forEach((b) => {
      const actif = b.dataset.onglet === cible;
      b.classList.toggle('actif', actif);
      b.setAttribute('aria-selected', actif ? 'true' : 'false');
    });
    document.querySelectorAll('#page-alerts .al-volet').forEach((v) => { v.hidden = v.dataset.onglet !== cible; });
    document.querySelectorAll('#page-alerts .al-seul-alertes').forEach((el) => { el.hidden = cible !== 'alertes'; });
    try { sessionStorage.setItem(CLE, cible); } catch (e) { /* stockage indisponible */ }
  }
  barre.addEventListener('click', (e) => {
    const b = e.target.closest('.onglet');
    if (b) montrer(b.dataset.onglet);
  });
  let depart = 'alertes';
  try { depart = sessionStorage.getItem(CLE) || 'alertes'; } catch (e) { /* stockage indisponible */ }
  montrer(depart);
})();
