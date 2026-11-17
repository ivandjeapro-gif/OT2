/* =====================================================================
 * sync-pill.js — indicateur de synchronisation GLOBAL.
 *
 * Une pastille dans la barre du haut de CHAQUE page, visible par tous
 * les comptes (y compris ceux sans acces aux Parametres) :
 *   vert   = lie au serveur, dernier echange < 30 s
 *   orange = reponse lente (2-5 s)
 *   rouge  = hors ligne (dernier echange connu affiche au survol)
 *
 * Principe : "tous les comptes voient la meme info en temps reel"
 * commence par rendre l'etat du lien visible partout, tout le temps.
 * ===================================================================== */
(function () {
  'use strict';

  function base() {
    if (typeof API !== 'undefined' && API.getApiBase) return API.getApiBase();
    return 'http://' + location.hostname + ':8100';
  }

  let dernierOk = null;   // Date du dernier ping reussi
  let etat = 'ko';

  function rendre() {
    const pill = document.getElementById('syncPill');
    if (!pill) return;
    const txt = pill.querySelector('.sync-pill-txt');

    pill.classList.remove('ok', 'warn', 'ko');
    if (etat === 'ok') {
      pill.classList.add('ok');
      if (txt) txt.textContent = dernierOk
        ? 'Sync ' + dernierOk.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : 'Sync';
    } else if (etat === 'warn') {
      pill.classList.add('warn');
      if (txt) txt.textContent = 'Sync lente';
    } else {
      pill.classList.add('ko');
      if (txt) txt.textContent = dernierOk
        ? 'Hors ligne ' + dernierOk.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : 'Hors ligne';
    }
    // l'etat pour l'accessibilite / survol
    pill.title = etat === 'ok' ? ('Synchronise a ' + (dernierOk ? dernierOk.toLocaleTimeString() : '?'))
      : etat === 'warn' ? 'Serveur lent — synchronisation en cours'
        : 'Serveur injoignable' + (dernierOk ? ' — dernier echange ' + dernierOk.toLocaleTimeString() : '');
  }

  function ping() {
    const debut = Date.now();
    fetch(base() + '/api/health', { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('http ' + r.status);
        etat = (Date.now() - debut) > 2000 ? 'warn' : 'ok';
        dernierOk = new Date();
      })
      .catch(() => { etat = 'ko'; })
      .finally(rendre);
  }

  function init() {
    if (!document.getElementById('syncPill')) return;
    appliquerVisibilite();
    lireVisibilite();
    setInterval(lireVisibilite, 15000);
    ping();
    setInterval(ping, 10000);
    // statut informe a chaque retour d'onglet
    document.addEventListener('visibilitychange', () => { if (!document.hidden) ping(); });
  }

  /** La pastille peut etre masquee (Parametres > Connection >
   * "Afficher l'etat de connexion sur toutes les pages"). */
  function appliquerVisibilite() {
    const pill = document.getElementById('syncPill');
    if (!pill) return;
    pill.style.display = (window.syncCouches && window.syncCouches.bandeau === false) ? 'none' : '';
  }

  function lireVisibilite() {
    if (typeof API === 'undefined' || !API.getConfig) return;
    API.getConfig().then((d) => {
      const v = d && d.params && d.params.sync_bandeau_visible;
      if (v !== undefined) {
        window.syncCouches = window.syncCouches || {};
        window.syncCouches.bandeau = v === '1';
        appliquerVisibilite();
      }
    }).catch(() => {});
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
