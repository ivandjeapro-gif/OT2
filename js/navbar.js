/* =====================================================================
 * navbar.js — COMPOSANT PARTAGÉ (ELEMENTS CONJOINTS/components)
 *
 * Barre de navigation unique : sidebar + topbar générées ici, une seule
 * source de vérité pour toutes les pages et pour toutes les copies du
 * projet (Top, REFONTE). Une page l'utilise ainsi :
 *
 *   <link rel="stylesheet" href="../../ELEMENTS CONJOINTS/components/navbar.css" />
 *   ...
 *   <aside class="sidebar" id="sidebar"></aside>
 *   <header class="menu-bar"></header>
 *   <script src="../../ELEMENTS CONJOINTS/components/navbar.js"></script>
 *
 * Le composant remplit les deux conteneurs, marque la page courante
 * comme active, et branche : #syncPill (sync-pill.js), .btn-logout,
 * .user-name / .user-email (auth.js), et la salutation #homeGreeting.
 * Aucune logique métier ici.
 * ===================================================================== */
(function () {
  'use strict';

  var ENTRÉES = {
    operations: [
      { page: 'home',   href: 'home.html',   icon: 'fa-home',           label: 'Home' },
      { page: 'live',   href: 'live.html',   icon: 'fa-satellite-dish', label: 'Live' },
      { page: 'alerts', href: 'alerts.html', icon: 'fa-bell',           label: 'Alerts', badge: true },
      { page: 'inbox',  href: 'inbox.html',  icon: 'fa-inbox',          label: 'Inbox' }
    ],
    // Métier : les modules d'expertise géologique / plan d'extraction
    metier: [
      { page: 'senor',   href: 'senor.html',   icon: 'fa-layer-group', label: 'Senor' },
      { page: 'digplan', href: 'digplan.html', icon: 'fa-map',         label: 'Dig Plan' }
    ],
    // Outils : administration et assistance
    outils: [
      { page: 'settings', href: 'settings.html', icon: 'fa-cog',             label: 'Settings' },
      { page: 'sync',     href: 'sync.html',     icon: 'fa-sync-alt',        label: 'Sync' },
      { page: 'aide',     href: '#',             icon: 'fa-question-circle', label: 'Aide & raccourcis' }
    ]
  };

  // dossier assets : relatif à la page courante (../assets/ depuis html/)
  var ASSETS = '../assets/';

  function pageCourante() {
    var f = (location.pathname.split('/').pop() || 'home.html').toLowerCase();
    return f.replace('.html', '');
  }

  function lien(item, actif) {
    var badge = item.badge
      ? '<span class="sidebar-badge" id="sidebarAlertBadge">&nbsp;</span>' : '';
    var chev = actif ? '<i class="fas fa-chevron-right sidebar-link-chev"></i>' : '';
    return '<a class="sidebar-link' + (actif ? ' active' : '') + '" data-page="' +
      item.page + '" href="' + item.href + '">' +
      '<i class="fas ' + item.icon + '"></i>' +
      '<span>' + item.label + '</span>' + badge + chev + '</a>';
  }

  function sidebarHTML(actif) {
    return '<div class="sidebar-brand">' +
        '<img src="' + ASSETS + 'Logo Ore Tracking.png" alt="Ore Tracking" class="sidebar-brand-logo" />' +
        '<span class="sidebar-brand-sub">Console opérateur</span>' +
      '</div>' +
      '<nav class="sidebar-nav">' +
        '<div class="nav-top">' +
          '<span class="nav-section-label">Opérations</span>' +
          ENTRÉES.operations.map(function (it) { return lien(it, it.page === actif); }).join('') +
        '</div>' +
        '<div class="nav-bottom">' +
          '<span class="nav-section-label">Métier</span>' +
          ENTRÉES.metier.map(function (it) { return lien(it, it.page === actif); }).join('') +
          '<span class="nav-section-label">Outils</span>' +
          ENTRÉES.outils.map(function (it) { return lien(it, it.page === actif); }).join('') +
        '</div>' +
      '</nav>' +
      '<div class="sidebar-user">' +
        '<div class="user-info">' +
          '<span class="user-name"></span>' +
          '<span class="user-role">Superviseur</span>' +
          '<span class="user-email"></span>' +
        '</div>' +
        '<button class="btn-logout" title="Déconnexion">' +
          '<i class="fas fa-sign-out-alt"></i>' +
        '</button>' +
      '</div>' +
      '<div class="sidebar-mine">' +
        '<img src="' + ASSETS + 'Logo Lafigue.png" alt="Lafigué Mine" class="sidebar-mine-logo" />' +
      '</div>';
  }

  function topbarHTML() {
    return '<div class="menu-left">' +
        '<span class="r-crumb-app">ORE TRACKING</span>' +
        '<span class="r-crumb-sep2">/</span>' +
        '<span class="r-crumb-space">Espace opérations</span>' +
      '</div>' +
      '<div class="menu-right">' +
        '<span class="r-net-pill" title="Liaison cloud"><i class="fas fa-cloud"></i> Cloud</span>' +
        '<span class="r-net-pill" title="Liaison réseau local"><i class="fas fa-network-wired"></i> LAN</span>' +
        '<span class="r-net-pill" title="Liaison bureau"><i class="fas fa-building"></i> Office</span>' +
        '<span class="sync-pill" id="syncPill" title="Etat de la synchronisation">' +
          '<span class="sync-dot-mini"></span><span class="sync-pill-txt">Sync</span>' +
        '</span>' +
        '<button class="r-bell" type="button" title="Alertes" aria-label="Alertes">' +
          '<i class="fas fa-bell"></i>' +
        '</button>' +
        '<span class="r-site-chip"><i class="fas fa-map-marker-alt"></i> Lafigué</span>' +
        '<span class="app-version">V2.0</span>' +
      '</div>';
  }

  /* --- salutation : nom réel de la session (auth.js), jamais inventé --- */
  function brancherSalutation() {
    var titre = document.getElementById('homeGreeting');
    if (!titre) return;
    var cible = document.querySelector('.sidebar-user .user-name');

    function maj() {
      var nom = cible ? String(cible.textContent || '').trim() : '';
      if (!nom || nom === 'Opérateur') {
        titre.textContent = 'Tableau de bord';
      } else {
        titre.textContent = 'Bonjour, ' + nom.split(/\s+/)[0] + '.';
      }
    }
    maj();
    if (cible && typeof MutationObserver !== 'undefined') {
      new MutationObserver(maj).observe(cible, { childList: true, characterData: true, subtree: true });
    }
  }

  /* --- badge Alerts : alimenté par la vraie passerelle, sinon vide ---
     La base est déduite comme missions-ui.js le fait (OTCFG, sinon
     host courant :8101). Le composant attend que la passerelle ait
     répondu au moins une fois : tant pis, le badge reste vide —
     jamais de chiffre inventé. */
  function basePasserelle() {
    if (window.OTCFG && typeof OTCFG.passerelle === 'function') {
      return OTCFG.passerelle();
    }
    return 'http://' + location.hostname + ':8101';
  }

  function brancherBadgeAlertes() {
    var badge = document.getElementById('sidebarAlertBadge');
    if (!badge) return;

    function lire() {
      if (!window.fetch) return;
      fetch(basePasserelle() + '/api/alertes')
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          var liste = (d && (d.alertes || d.data)) || [];
          if (!Array.isArray(liste)) { badge.textContent = '\u00a0'; return; }
          var n = liste.filter(function (a) {
            if (a.acquittee) return false;
            if (window.MSUI && typeof MSUI.estInfo === 'function') return !MSUI.estInfo(a);
            return true;
          }).length;
          badge.textContent = n > 0 ? String(n).padStart(2, '0') : '\u00a0';
        })
        .catch(function () { badge.textContent = '\u00a0'; });
    }
    lire();
    setInterval(lire, 10000);
  }

  function init() {
    var actif = pageCourante();
    var sb = document.getElementById('sidebar');
    if (sb) sb.innerHTML = sidebarHTML(actif);
    var tb = document.querySelector('header.menu-bar');
    if (tb) tb.innerHTML = topbarHTML();

    brancherSalutation();
    brancherBadgeAlertes();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
