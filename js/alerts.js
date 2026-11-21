/* =====================================================================
 * alerts.js — page Alerts : flux d'alertes reel (passerelle via
 * missions-ui.js), filtres et acquittement.
 * Types reels : deviation, retour_trajet, offroad, retour_piste,
 * non_conforme, fin_mission, arrivee, mission.
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
    arrivee: 'Arrivee DA',
    mission: 'Nouvelle mission',
  };

  // alertes d'information : rien a traiter
  const TYPES_INFO = { fin_mission: 1, arrivee: 1, mission: 1, retour_piste: 1, retour_trajet: 1 };

  let filtreDt = '';
  let filtreType = '';
  let seulementNonVues = true;
  let filtrePeriode = '';
  let filtreDate = '';

  function passerelle() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return 'http://' + location.hostname + ':8101';
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
    const nonVues = jour.filter((a) => !a.acquittee && !TYPES_INFO[a.type]);
    const enRetard = nonVues.filter((a) => Date.now() - ms(a) > seuilMs);
    set('rCountTotal', deux(jour.length));
    set('rCountUnseen', deux(nonVues.length));
    set('rCountLate', deux(enRetard.length));
    set('rCountLateLabel', 'au-delà de ' + (etat.seuilRougeMin || 10) + ' min');
    const n = new Date();
    set('rCounterDate', n.getDate() + ' ' + MOIS[n.getMonth()] + ' ' + n.getFullYear() + ' · ' + deux(n.getHours()) + ':' + deux(n.getMinutes()));
    const aVoir = affichees.filter((a) => !a.acquittee && !TYPES_INFO[a.type]).length;
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
  const AVEC_CYCLE = { offroad: 1, deviation: 1, checkpoint: 1 };
  function dureeCourte(s) {
    s = Math.max(0, Math.round(Number(s) || 0));
    return s >= 60 ? Math.floor(s / 60) + ' min ' + deux(s % 60) + ' s' : s + ' s';
  }
  function etatCycle(a) {
    if (!AVEC_CYCLE[a.type]) return '';
    if (a.retour_le && a.retour_cause === 'arrivee_da') return 'Mission terminée en dumping area à ' + MSUI.heure(a.retour_le);
    if (a.type === 'checkpoint') return '';
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
    if (seulementNonVues) alertes = alertes.filter((a) => !a.acquittee && !TYPES_INFO[a.type]);

    const compteur = document.getElementById('alertsCompteur');
    if (compteur) {
      const nonVues = (etat.alertes || []).filter((a) => !a.acquittee && !TYPES_INFO[a.type]).length;
      compteur.textContent = nonVues ? nonVues + ' a traiter' : '';
    }

    rendreCompteurs(etat, alertes);

    if (!alertes.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">' +
        (etat.passerelleDown ? 'Passerelle injoignable' : 'Aucune alerte.') +
        '</td></tr>';
      return;
    }

    const seuilMs = (etat.seuilRougeMin || 10) * 60000;
    tbody.innerHTML = alertes.map((a) => {
      const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
      const age = Date.now() - t;
      const aTraiter = !TYPES_INFO[a.type] && !a.acquittee;
      const urgent = aTraiter && age > seuilMs;
      const cls = urgent ? 'sev-high' : (TYPES_INFO[a.type] ? 'status-done' : 'sev-medium');
      return '<tr' + (urgent ? ' class="alerte-urgente"' : '') + '>' +
        '<td>' + MSUI.heure(a.created_at) + '</td>' +
        '<td>' + MSUI.esc(a.dt_code) + '</td>' +
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
