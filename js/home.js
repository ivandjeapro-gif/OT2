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

  function rendreVoyant(etat) {
    const box = document.getElementById('homeVoyant');
    if (!box) return;
    const v = MSUI.voyant();
    const pastille = box.querySelector('.voyant-pastille');
    const texte = box.querySelector('.voyant-texte');
    if (pastille) pastille.className = 'voyant-pastille ' + v.niveau;
    if (texte) {
      texte.textContent = v.raison;
      if (etat.passerelleDown && etat.dernierReussi) {
        texte.textContent += ' — donnees de ' + etat.dernierReussi.toLocaleTimeString();
      }
    }
  }

  function rendreStats(etat) {
    const engins = (etat.engins || []).filter((e) => e.actif !== 0);
    const alertes = (etat.alertes || []).filter((a) => !a.acquittee && a.type !== 'fin_mission');
    const missions = (etat.missions || []).filter((m) => m.statut === 'en_cours');
    const aujourdhui = new Date().toISOString().slice(0, 10);
    const voy = (etat.voyages || []).filter(
      (v) => String(v.created_at || '').slice(0, 10) === aujourdhui);
    const tonnes = voy.reduce((s, v) => s + (v.volume_t || 0), 0);

    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    set('statEngins', engins.length + ' / ' + (window.PARC_ENGINS || '?'));
    set('statAlertes', alertes.length);
    set('statMissions', missions.length);
    set('statTonnage', Math.round(tonnes) + ' t');
  }

  // File du matin : regles simples, chaque item mene a la page qui traite.
  function rendreFile(etat) {
    const box = document.getElementById('homeFile');
    if (!box) return;
    const items = [];
    const seuilMs = (etat.seuilRougeMin || 10) * 60000;

    for (const a of (etat.alertes || [])) {
      if (a.acquittee || a.type === 'fin_mission') continue;
      const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
      const vieux = Date.now() - t > seuilMs;
      items.push({
        urgence: vieux ? 2 : 1,
        html: '<b>' + MSUI.esc(a.dt_code) + '</b> — ' + MSUI.esc(a.message) +
          ' <span class="file-age">' + MSUI.ilYA(a.created_at) + '</span>',
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
          html: '<b>' + MSUI.esc(e.code) + '</b> est charge (ODT) mais n\'a pas de mission',
          lien: 'senor.html',
        });
      }
    }
    // missions deviees
    for (const m of (etat.missions || [])) {
      if (m.statut === 'en_cours' && Number(m.devie) === 1 || m.statut === 'devoie') {
        items.push({
          urgence: 1,
          html: '<b>' + MSUI.esc(m.dt_code) + '</b> a devie de sa mission (' +
            Math.round(m.ecart_m || 0) + ' m du trajet, bloc #' + m.orebloc_id + ')',
          lien: 'senor.html',
        });
      }
    }

    if (!items.length) {
      box.innerHTML = '<div class="member-empty">Rien a signaler. Bon shift !</div>';
      return;
    }
    items.sort((a, b) => b.urgence - a.urgence);
    box.innerHTML = items.slice(0, 8).map((it) =>
      '<a class="file-item u' + it.urgence + '" href="' + it.lien + '">' +
      '<i class="fas fa-' + (it.urgence === 2 ? 'fire' : 'bell') + '"></i>' +
      '<span>' + it.html + '</span></a>').join('');
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

  function init() {
    if (!document.getElementById('homeVoyant')) return;
    if (!window.MSUI) return;
    lireParc();
    setInterval(lireParc, 60000);
    MSUI.onTick((etat) => { rendreVoyant(etat); rendreStats(etat); rendreFile(etat); });
    MSUI.demarrer(5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
