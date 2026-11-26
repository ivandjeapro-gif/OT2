/* ============================================================
 * missions-ui.js — donnees passerelle partagees
 *
 * Toutes les surfaces (Home, Alerts, Inbox, Suivi des missions)
 * lisent ici la passerelle Senor Geology (:8101) :
 *   /api/missions  /api/alertes  /api/voyages  /api/engins  /api/cumul
 *
 * Un seul polling pour toute la page : chaque vue declare
 * MSUI.onTick(fn) et recoit les donnees fraiches a chaque cycle.
 * Le polling se met en pause quand l'onglet est cache.
 *
 * Le voyant du site est calcule ici aussi (Home le rend, les
 * autres pages peuvent le reutiliser).
 * ============================================================ */
(function () {
  'use strict';

  function basePasserelle() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return 'http://' + location.hostname + ':8101';
  }

  const ETAT = {
    missions: [], alertes: [], voyages: [], engins: [],
    dernierReussi: null,      // Date du dernier fetch complet OK
    passerelleDown: false,
    seuilRougeMin: 10,
    _abonnes: [],
    _timer: null,
  };

  // ---------------- seuil rouge (Parametres > Connection) ----------------
  // Le reglage vit dans params du SaaS (alerte_rouge_min) : on le lit une
  // fois puis toutes les 2 min, pour prendre en compte un changement sans
  // rechargement.
  function lireSeuil() {
    if (typeof API === 'undefined' || !API.getConfig) return;
    API.getConfig().then((d) => {
      const v = parseInt(d && d.params && d.params.alerte_rouge_min, 10);
      if (v >= 1 && v <= 120) ETAT.seuilRougeMin = v;
    }).catch(() => {});
  }

  // ---------------- fetchs ----------------
  function jget(url) {
    return fetch(url).then((r) => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function chargerTout() {
    const base = basePasserelle();
    return Promise.all([
      jget(base + '/api/missions').catch(() => null),
      jget(base + '/api/alertes').catch(() => null),
      jget(base + '/api/voyages').catch(() => null),
      jget(base + '/api/engins').catch(() => null),
    ]).then(([m, a, v, e]) => {
      const ok = !!(m && a && v && e);
      if (m) ETAT.missions = m.missions || [];
      if (a) ETAT.alertes = a.alertes || [];
      if (v) ETAT.voyages = v.voyages || [];
      if (e) ETAT.engins = e.engins || [];
      ETAT.passerelleDown = !ok;
      if (ok) ETAT.dernierReussi = new Date();
      ETAT._abonnes.forEach((fn) => { try { fn(ETAT); } catch (err) {} });
    });
  }

  // ---------------- abonnement + polling ----------------
  function onTick(fn) {
    ETAT._abonnes.push(fn);
    // premier rendu immediate avec ce qu'on a (peut etre vide au 1er tick)
    try { fn(ETAT); } catch (e) {}
  }

  /** Re-fetch immédiat + notifie les abonnés (apres une action, par ex.). */
  function rafraichir() { return chargerTout(); }

  function demarrer(intervalMs) {
    lireSeuil();
    setInterval(lireSeuil, 120000);
    chargerTout();
    ETAT._timer = setInterval(chargerTout, intervalMs || 5000);
    // pause quand l'onglet est cache : economise la passerelle
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        clearInterval(ETAT._timer); ETAT._timer = null;
      } else {
        chargerTout();
        ETAT._timer = setInterval(chargerTout, intervalMs || 5000);
      }
    });
  }

  // ---------------- formatage partage ----------------
  function heure(iso) {
    if (!iso) return '—';
    const d = new Date(iso.replace(' ', 'T') + (iso.length === 19 ? 'Z' : ''));
    if (isNaN(d)) return iso;
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function ilYA(iso) {
    if (!iso) return '—';
    const d = new Date(iso.replace(' ', 'T') + (iso.length === 19 ? 'Z' : ''));
    if (isNaN(d)) return iso;
    const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (s < 60) return 'il y a ' + s + ' s';
    if (s < 3600) return 'il y a ' + Math.floor(s / 60) + ' min';
    return 'il y a ' + Math.floor(s / 3600) + ' h';
  }

  function duree(isoDebut, isoFin) {
    if (!isoDebut) return '—';
    const d1 = new Date(isoDebut.replace(' ', 'T') + 'Z');
    const d2 = isoFin ? new Date(isoFin.replace(' ', 'T') + 'Z') : new Date();
    const m = Math.max(0, Math.round((d2 - d1) / 60000));
    return m >= 60 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min' : m + ' min';
  }

  // ---------------- voyant du site (calcule, pas declare) ----------------
  // vert   : rien a signaler
  // orange : alertes actives ou DT devie, ca tourne mais surveille
  // rouge  : alerte haute non acquittee > seuil, ou DT ODT sans mission,
  //          ou passerelle muette (pas de donnees fraiches)
  // alertes d'information : rien a traiter (fin, arrivee, retours a la normale)
  // 25/11 (calibrage) : la passerelle marque chaque alerte (champ `info`, ALERTES_INFO) ; cette liste identique
  // ne sert que face a une passerelle plus ancienne. Toutes les pages comptent avec estInfo / aTraiter.
  const TYPES_INFO = { fin_mission: 1, arrivee: 1, mission: 1, retour_piste: 1, retour_trajet: 1, retour_direction: 1,
    demi_tour: 1, checkpoint_valide: 1, zone_approche: 1, blast_rejete: 1 };
  const estInfo = (a) => !!(a && (a.info !== undefined ? Number(a.info) : TYPES_INFO[a.type]));

  // Rejet d'import Dig Plan : erreur de donnees, pas une urgence terrain.
  const TYPES_HORS_FILE = { blast_rejete: 1 };

  // Debut du shift en cours (jour 06 h-18 h, nuit 18 h-06 h), heure locale, en ms.
  function debutShiftMs() {
    const d = new Date();
    const h = d.getHours();
    d.setMinutes(0, 0, 0);
    if (h >= 6 && h < 18) d.setHours(6);
    else if (h >= 18) d.setHours(18);
    else { d.setDate(d.getDate() - 1); d.setHours(18); }
    return d.getTime();
  }

  // Alerte a traiter MAINTENANT : non acquittee, ni info, ni rejet de blast, et du shift en cours
  // (les anciennes restent consultables dans Alerts).
  function aTraiter(a) {
    if (!a || Number(a.acquittee) || estInfo(a) || TYPES_HORS_FILE[a.type]) return false;
    const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
    return !(t < debutShiftMs());
  }

  function voyant() {
    if (ETAT.passerelleDown) {
      return { niveau: 'rouge', raison: 'Passerelle injoignable' };
    }
    const maintenant = Date.now();
    const seuilMs = ETAT.seuilRougeMin * 60000;

    // alertes hautes non acquittees trop vieilles
    for (const a of ETAT.alertes) {
      if (!aTraiter(a)) continue;
      const t = new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime();
      if (maintenant - t > seuilMs) {
        return { niveau: 'rouge', raison: a.type + ' non vue (' + a.dt_code + ')' };
      }
    }
    // DT declare ODT (charge, dispo) mais sans mission en cours
    const dtEnMission = new Set(
      ETAT.missions.filter((m) => m.statut === 'en_cours').map((m) => m.dt_code));
    for (const e of ETAT.engins) {
      if (e.type === 'DT' && e.statut === 'ODT' && !dtEnMission.has(e.code)) {
        return { niveau: 'rouge', raison: e.code + ' charge sans mission' };
      }
    }
    // orange : alerte active (non acquittee) ou mission deviee
    if (ETAT.alertes.some(aTraiter)) {
      return { niveau: 'orange', raison: 'Alerte a traiter' };
    }
    // devie = DT ecarte du trajet, mission toujours en cours (devoie = legacy)
    if (ETAT.missions.some((m) => m.statut === 'en_cours' && Number(m.devie) === 1)) {
      return { niveau: 'orange', raison: 'DT devie' };
    }
    return { niveau: 'vert', raison: 'Tout va bien' };
  }

  // libelles/badges communs
  const STATUT_MISSION = {
    en_cours: { lbl: 'En cours', cls: 'status-progress' },
    termine: { lbl: 'Termine', cls: 'status-done' },
    devoie: { lbl: 'Devie', cls: 'sev-high' },
    // abandonnee : le DT a recommence un cycle (Serveur_SG) ou la mission n'a jamais abouti
    abandonnee: { lbl: 'Abandonnee', cls: 'status-pending' },
    // 26/11 : annulee chez Serveur_SG (tablette remise a zero, cycle perime) : statut garde tel quel
    annulee: { lbl: 'Annulee', cls: 'status-pending' },
  };
  const CONFORMITE = {
    conforme: { lbl: 'Conforme', cls: 'status-done' },
    compromis: { lbl: 'Compromis', cls: 'status-progress' },
    non_conforme: { lbl: 'Non conforme', cls: 'sev-high' },
  };

  window.MSUI = {
    etat: ETAT, onTick, demarrer, voyant, rafraichir,
    heure, ilYA, duree,
    STATUT_MISSION, CONFORMITE,
    // alerte d'information (fin, arrivee, retour a la normale) : rien a traiter
    aTraiter,
    estInfo,
    debutShiftMs,
    // 26/11 (calibrage) : mission « en cours » depuis plus de 3 h = cycle jamais clos (tablette non revenue), comme
    // « cycles jamais clos » du dashboard ; elle n'est plus comptee parmi les missions en cours.
    missionActive: (m) => !!m && m.statut === 'en_cours' &&
      !(Date.now() - new Date(String(m.debut || '').replace(' ', 'T')).getTime() > 3 * 3600000),
    missionNonClose: (m) => !!m && m.statut === 'en_cours' &&
      Date.now() - new Date(String(m.debut || '').replace(' ', 'T')).getTime() > 3 * 3600000,
    esc: (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
  };
})();
