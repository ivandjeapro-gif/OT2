/* =====================================================================
 * navbar.js — barre de navigation UNIQUE, injectée sur toutes les pages.
 *
 * Objectif : la sidebar et la topbar ne doivent plus jamais diverger
 * d'une page à l'autre. Le balisage vit ici, une seule fois.
 *
 * Ce fichier ne contient AUCUNE logique métier : il ne fait que :
 *   - écrire le balisage de la sidebar et de la topbar ;
 *   - marquer le lien de la page courante comme actif ;
 *   - brancher les hooks déjà attendus par les autres scripts
 *     (#syncPill, .btn-logout, .user-name, .user-email, #hamburgerBtn).
 * ===================================================================== */
(function () {
  'use strict';

  // --- Les entrées du menu, dans l'ordre de la maquette ------------------
  var OPERATIONS = [
    { page: 'home', href: 'home.html', icon: 'fa-home', label: 'Home' },
    { page: 'live', href: 'live.html', icon: 'fa-satellite-dish', label: 'Live' },
    { page: 'alerts', href: 'alerts.html', icon: 'fa-bell', label: 'Alerts', badge: true },
    { page: 'inbox', href: 'inbox.html', icon: 'fa-inbox', label: 'Inbox' }
  ];

  var OUTILS = [
    { page: 'settings', href: 'settings.html', icon: 'fa-cog', label: 'Settings' },
    { page: 'sync', href: 'sync.html', icon: 'fa-sync-alt', label: 'Sync' },
    { page: 'senor', href: 'senor.html', icon: 'fa-layer-group', label: 'Senor' },
    { page: 'digplan', href: 'digplan.html', icon: 'fa-map', label: 'Dig Plan' },
    { page: 'aide', href: '#', icon: 'fa-question-circle', label: 'Aide & raccourcis' }
  ];

  // La page courante est déduite de l'URL (aucune config par page).
  function pageCourante() {
    var file = (location.pathname.split('/').pop() || 'home.html').toLowerCase();
    return file.replace('.html', '');
  }

  function lien(item, actif) {
    var cls = 'sidebar-link' + (actif ? ' active' : '');
    var badge = item.badge ? '<span class="sidebar-badge" id="sidebarAlertBadge">08</span>' : '';
    var chev = actif ? '<i class="fas fa-chevron-right sidebar-link-chev"></i>' : '';
    var iconClass = item.icon || 'fa-circle';
    return '<a class="' + cls + '" data-page="' + item.page + '" href="' + item.href + '">' +
      '<i class="fas ' + iconClass + '"></i>' +
      '<span>' + item.label + '</span>' +
      badge +
      chev +
      '</a>';
  }

  function rendreSidebar(actif) {
    var html =
      '<div class="sidebar-brand">' +
      '<img src="../assets/Logo Ore Tracking.png" alt="Ore Tracking" class="sidebar-brand-logo" />' +
      '<span class="sidebar-brand-sub">Console opérateur</span>' +
      '</div>' +
      '<nav class="sidebar-nav">' +
      '<div class="nav-top">' +
      '<span class="nav-section-label">Opérations</span>' +
      OPERATIONS.map(function (it) { return lien(it, it.page === actif); }).join('') +
      '</div>' +
      '<div class="nav-bottom">' +
      '<span class="nav-section-label">Outils &amp; métier</span>' +
      OUTILS.map(function (it) { return lien(it, it.page === actif); }).join('') +
      '</div>' +
      '</nav>' +
      '<div class="sidebar-user">' +
      '<div class="user-info">' +
      '<span class="user-name"></span>' +
      '<span class="user-email"></span>' +
      '</div>' +
      '<button class="btn-logout" title="Déconnexion">' +
      '<i class="fas fa-sign-out-alt"></i>' +
      '</button>' +
      '</div>' +
      '<div class="sidebar-mine">' +
      '<img src="../assets/Logo Lafigue.png" alt="Lafigué Mine" class="sidebar-mine-logo" />' +
      '</div>';
    return html;
  }

  function rendreTopbar() {
    return '' +
      '<div class="menu-left">' +
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

  function init() {
    var actif = pageCourante();

    var sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.innerHTML = rendreSidebar(actif);

    var topbar = document.querySelector('header.menu-bar');
    if (topbar) topbar.innerHTML = rendreTopbar();

    // La page Signets peut avoir été ouverte sans #sidebar : on la crée.
    var overlay = document.getElementById('sidebarOverlay');
    if (overlay && overlay.parentNode) {
      overlay.parentNode.setAttribute('id', overlay.parentNode.id || 'appBody');
    }

    // Le titre de Home salue l'utilisateur RÉELLEMENT connecté.
    // auth.js remplit .user-name ; on observe ce noeud pour suivre la
    // connexion et la déconnexion, sans dupliquer sa logique.
    brancherSalutation();
    completerUser();
  }

  // Le nom vient de la session (auth.js -> renderUser). Tant qu'aucun nom
  // n'est connu, on affiche un titre neutre, jamais un nom inventé.
  function brancherSalutation() {
    var titre = document.getElementById('homeGreeting');
    if (!titre) return;

    var cible = document.querySelector('.sidebar-user .user-name');

    function maj() {
      var nom = cible ? String(cible.textContent || '').trim() : '';
      if (!nom || nom === 'Opérateur') {
        titre.textContent = 'Tableau de bord';
      } else {
        // On ne garde que le prénom : « Gaston Albert » -> « Gaston »
        var prenom = nom.split(/\s+/)[0];
        titre.textContent = 'Bonjour, ' + prenom + '.';
      }
    }

    maj();

    if (cible && typeof MutationObserver !== 'undefined') {
      new MutationObserver(maj).observe(cible, {
        childList: true,
        characterData: true,
        subtree: true
      });
    }
  }

  // Le bloc utilisateur de la maquette : nom + rôle + e-mail.
  // Le rôle et l'e-mail sont des replis tant que la session n'en fournit pas.
  function completerUser() {
    var box = document.querySelector('.sidebar-user');
    if (!box) return;
    var email = box.querySelector('.user-email');
    if (email && !email.dataset.replitRole) {
      email.dataset.replitRole = '1';
      email.insertAdjacentHTML('beforebegin',
        '<span class="user-role">Superviseur</span>');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
