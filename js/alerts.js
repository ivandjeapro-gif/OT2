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
    arrivee: 'Arrivee DA',
    mission: 'Nouvelle mission',
  };

  // alertes d'information : rien a traiter
  const TYPES_INFO = { fin_mission: 1, arrivee: 1, mission: 1, retour_piste: 1, retour_trajet: 1 };

  let filtreDt = '';
  let filtreType = '';
  let seulementNonVues = true;

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

  function render(etat) {
    const tbody = document.getElementById('alertsBody');
    if (!tbody) return;
    let alertes = etat.alertes || [];

    if (filtreDt) {
      const f = filtreDt.toUpperCase();
      alertes = alertes.filter((a) => String(a.dt_code || '').toUpperCase().indexOf(f) >= 0);
    }
    if (filtreType) alertes = alertes.filter((a) => a.type === filtreType);
    if (seulementNonVues) alertes = alertes.filter((a) => !a.acquittee);

    const compteur = document.getElementById('alertsCompteur');
    if (compteur) {
      const nonVues = (etat.alertes || []).filter((a) => !a.acquittee && !TYPES_INFO[a.type]).length;
      compteur.textContent = nonVues ? nonVues + ' a traiter' : '';
    }

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
        '<td>' + MSUI.esc(a.message) + '</td>' +
        '<td>' + (a.acquittee
          ? '<span class="status-done">Vue</span>'
          : (aTraiter ? '<span class="' + cls + '">A traiter</span>'
                      : '<span class="status-done">Info</span>')) + '</td>' +
        '<td>' + (aTraiter ?
          '<button class="team-btn" data-acquit="' + a.id + '"><i class="fas fa-check"></i> Vu</button>' : '') +
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
