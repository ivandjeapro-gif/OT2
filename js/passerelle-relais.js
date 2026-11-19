/* =====================================================================
 * passerelle-relais.js — Parametres > « Liaisons de la passerelle ».
 *
 * Regle les liaisons SORTANTES de la passerelle (:8101) : Serveur_OT
 * (OFFICE_URL / OFFICE_TOKEN) et Serveur_SG (SG_URL / SG_TOKEN /
 * SG_INTERVALLE_S), plus le seuil d'alerte rouge. Ecrit dans la base de la
 * passerelle via /api/relais (avant le 17/11 : aucun ecran, seulement l'API).
 *
 * Les jetons ne reviennent JAMAIS dans la page : la passerelle n'en renvoie
 * qu'une empreinte (6 caracteres), pour savoir lequel est en place. Un champ
 * jeton laisse vide n'est pas envoye, donc pas modifie.
 * ===================================================================== */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  if (!$('#panelRelais')) return;

  function base() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return 'http://' + (location.hostname || 'localhost') + ':8101';
  }

  function statut(txt, ok) {
    const el = $('#relaisStatus');
    el.textContent = txt || '';
    el.className = 'account-status' + (txt ? (ok ? ' ok' : ' err') : '');
  }

  function empreinte(el, valeur) {
    el.textContent = valeur ? '(enregistre : ' + valeur + '…)' : '(aucun jeton enregistre)';
    el.classList.toggle('vide', !valeur);
  }

  async function appel(methode, chemin, corps) {
    const opts = { method: methode, cache: 'no-store' };
    if (corps !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(corps);
    }
    const r = await fetch(base() + chemin, opts);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  async function charger() {
    try {
      const d = await appel('GET', '/api/relais');
      $('#relaisOfficeUrl').value = d.office_url || '';
      $('#relaisSgUrl').value = d.sg_url || '';
      $('#relaisSgIntervalle').value = d.sg_intervalle_s || 60;
      $('#relaisAlerteRouge').value = d.alerte_rouge_min || 10;
      $('#relaisRemonteesIntervalle').value = d.remontees_intervalle_s || 10;
      const r = d.remontees || {};
      $('#relaisRemonteesEtat').textContent = !r.actif
        ? 'Lecture des remontees : arretee (passerelle a redemarrer).'
        : 'Lecture des remontees : ' + (r.derniere_erreur ? 'ERREUR — ' + r.derniere_erreur
          : 'OK') + (r.derniere_lecture ? ' — derniere lecture ' + r.derniere_lecture : '') +
          ' — ' + (r.lignes_traitees || 0) + ' position(s) traitee(s) depuis le demarrage' +
          (r.derniere_position ? ' — derniere : ' + r.derniere_position : '') +
          (r.derniere_ligne ? ' — ligne Remontees ' + r.derniere_ligne : '') +
          (r.derniere_ligne_trajet ? ' / trajet ' + r.derniere_ligne_trajet : '');
      $('#relaisChargementsIntervalle').value = d.chargements_intervalle_s || 5;
      const c = d.chargements || {};
      const libC = { ok: 'OK', lent: 'en retard', ko: 'EN ECHEC', attente: 'en attente de la 1re lecture',
                     non_configure: 'Serveur_SG non configure' };
      $('#relaisChargementsEtat').textContent = !d.chargements
        ? 'Lecture de la production : absente (passerelle a mettre a jour puis redemarrer).'
        : 'Lecture de la production : ' + (libC[c.statut] || '?') +
        (c.lu_le ? ' — derniere lecture reussie ' + c.lu_le : '') +
        (c.duree_ms ? ' (' + c.duree_ms + ' ms)' : '') +
        (c.erreur ? ' — ' + c.erreur : '');
      $('#relaisOfficeToken').value = '';
      $('#relaisSgToken').value = '';
      empreinte($('#relaisOfficeEmpreinte'), d.office_token_empreinte);
      empreinte($('#relaisSgEmpreinte'), d.sg_token_empreinte);
      const env = d.surcharge_env || [];
      const zone = $('#relaisEnv');
      zone.hidden = !env.length;
      zone.textContent = env.length
        ? 'Attention : ' + env.join(', ') + ' defini(s) par variable d\'environnement sur ce poste. ' +
          'Cette valeur prime sur ce que vous enregistrez ici.'
        : '';
      return d;
    } catch (e) {
      statut('Passerelle :8101 injoignable (' + e.message + '). Est-elle demarree ?', false);
      return null;
    }
  }

  async function enregistrer() {
    const corps = {
      office_url: $('#relaisOfficeUrl').value.trim(),
      sg_url: $('#relaisSgUrl').value.trim(),
      sg_intervalle_s: $('#relaisSgIntervalle').value.trim(),
      alerte_rouge_min: $('#relaisAlerteRouge').value.trim(),
      remontees_intervalle_s: $('#relaisRemonteesIntervalle').value.trim(),
      chargements_intervalle_s: $('#relaisChargementsIntervalle').value.trim(),
    };
    // URL : uniquement l'adresse /exec, jamais un jeton colle par erreur
    for (const [champ, v] of [['office_url', corps.office_url], ['sg_url', corps.sg_url]]) {
      if (v && !/^https:\/\/\S+$/.test(v)) {
        statut('URL invalide (' + champ + ') : elle doit commencer par https://', false);
        return;
      }
    }
    const jetonOt = $('#relaisOfficeToken').value.trim();
    const jetonSg = $('#relaisSgToken').value.trim();
    if (jetonOt) corps.office_token = jetonOt;      // vide = inchange
    if (jetonSg) corps.sg_token = jetonSg;
    try {
      const d = await appel('POST', '/api/relais', corps);
      await charger();                               // efface les jetons de la page
      const ignores = d.ignores_car_env || [];
      statut('Enregistre (' + (d.enregistres || []).length + ' reglage(s))' +
             (ignores.length ? ' — sans effet tant que ' + ignores.join(', ') + ' est defini dans l\'environnement' : '') +
             '. Testez les liaisons pour verifier.', !ignores.length);
    } catch (e) {
      statut('Enregistrement impossible : ' + e.message, false);
    }
  }

  function ligneResultat(nom, r) {
    const div = document.createElement('div');
    div.className = 'relais-ligne ' + (r.ok ? (r.avertissement ? 'warn' : 'ok') : 'err');
    const titre = document.createElement('b');
    titre.textContent = nom + ' : ';
    div.appendChild(titre);
    let txt;
    if (!r.configure) txt = 'non configure (' + r.erreur + ')';
    else if (!r.ok) txt = 'REFUS — ' + r.erreur;
    else txt = 'OK' + (r.version ? ' — version ' + r.version : '') +
               (r.duree_ms ? ' (' + r.duree_ms + ' ms)' : '') +
               (r.avertissement ? ' — ' + r.avertissement : '');
    div.appendChild(document.createTextNode(txt));
    return div;
  }

  async function tester() {
    const zone = $('#relaisResultats');
    zone.hidden = false;
    zone.replaceChildren(document.createTextNode('Test en cours (jusqu\'a 30 s par serveur)...'));
    try {
      const d = await appel('POST', '/api/relais/tester', {});
      zone.replaceChildren(ligneResultat('Serveur_OT', d.office), ligneResultat('Serveur_SG', d.sg));
      const tout = d.office.ok && d.sg.ok;
      statut(tout ? 'Les deux liaisons repondent.' : 'Au moins une liaison ne repond pas : voir le detail.', tout);
    } catch (e) {
      zone.hidden = true;
      statut('Test impossible : ' + e.message, false);
    }
  }

  async function action(bouton, texte, fn) {
    const el = $(bouton);
    const avant = el.textContent;
    el.disabled = true; el.textContent = texte;
    try { await fn(); } finally { el.disabled = false; el.textContent = avant; }
  }

  // ---- Secours direct du dashboard : localStorage de CE navigateur ----
  // Lu par dashboard.js quand la passerelle ne repond plus. Le jeton n'est
  // jamais reaffiche ; un champ vide le conserve.
  const LS_SECOURS = 'oreTrackingSgDirect';
  function secoursLire() {
    try { return JSON.parse(localStorage.getItem(LS_SECOURS)) || {}; } catch (e) { return {}; }
  }
  function secoursAfficher() {
    const s = secoursLire();
    $('#secoursSgUrl').value = s.url || '';
    $('#secoursSgToken').value = '';
    $('#secoursSgEtat').textContent = s.token ? '(jeton enregistre sur ce poste)' : '(aucun jeton sur ce poste)';
    $('#secoursSgEtat').classList.toggle('vide', !s.token);
  }
  function secoursStatut(txt, ok) {
    const el = $('#secoursStatus');
    el.textContent = txt; el.className = 'account-status ' + (ok ? 'ok' : 'err');
  }
  $('#btnSecoursSave').addEventListener('click', () => {
    const url = $('#secoursSgUrl').value.trim().replace(/\/+$/, '');
    const jeton = $('#secoursSgToken').value.trim();
    const avant = secoursLire();
    if (!/^https:\/\/script\.google(usercontent)?\.com\/\S+$/.test(url)) {
      secoursStatut('URL invalide : adresse /exec de Serveur_SG attendue (https://script.google.com/...)', false);
      return;
    }
    if (!jeton && !avant.token) { secoursStatut('Jeton requis', false); return; }
    localStorage.setItem(LS_SECOURS, JSON.stringify({ url, token: jeton || avant.token }));
    secoursAfficher();
    secoursStatut('Secours direct actif sur ce poste.', true);
  });
  $('#btnSecoursOff').addEventListener('click', () => {
    localStorage.removeItem(LS_SECOURS);
    secoursAfficher();
    secoursStatut('Secours direct desactive sur ce poste (URL et jeton effaces).', true);
  });
  secoursAfficher();

  $('#btnRelaisSave').addEventListener('click', () => action('#btnRelaisSave', 'Enregistrement...', enregistrer));
  $('#btnRelaisTest').addEventListener('click', () => action('#btnRelaisTest', 'Test...', tester));
  charger();
})();
