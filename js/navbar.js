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
 *
 * ANTI-CLIGNOTEMENT (20/11) : chaque page est un document distinct ; pour que
 * la barre paraisse FIXE d'une page à l'autre :
 *   1. le script est placé JUSTE APRÈS les conteneurs (pas en bas de page) :
 *      la barre est écrite avant le premier affichage, jamais « vide puis remplie » ;
 *   2. <link rel="expect" href="#sidebar" blocking="render"> : le navigateur
 *      n'affiche pas la page tant que la barre n'est pas en place ;
 *   3. nom, adresse, pages masquées par le profil et pastille Alerts sont
 *      mémorisés (sessionStorage, par onglet) et réappliqués dès le premier
 *      affichage : plus de nom qui apparaît ou de menu qui change après coup ;
 *   4. css/refonte.css : @view-transition (Chrome / Edge) garde la barre
 *      immobile pendant que seul le contenu change de page.
 * Copie de référence : ELEMENTS CONJOINTS/components/navbar.js (garder identiques).
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

  /* --- mémoire de la barre (par onglet) : nom, adresse, pages masquées, pastille --- */
  var CLE_CACHE = 'ore-nav-cache';
  function lireCache() {
    try { return JSON.parse(sessionStorage.getItem(CLE_CACHE)) || {}; } catch (e) { return {}; }
  }
  function memoriser(patch) {
    var c = lireCache();
    Object.keys(patch || {}).forEach(function (k) { c[k] = patch[k]; });
    try { sessionStorage.setItem(CLE_CACHE, JSON.stringify(c)); } catch (e) { /* stockage indisponible */ }
  }
  window.OreNavbar = { memoriser: memoriser, lire: lireCache };

  // diagnostic : la transition entre pages (barre immobile) est-elle active sur cette navigation ?
  try {
    window.addEventListener('pagereveal', function (e) {
      try { sessionStorage.setItem('ore-nav-transition', e.viewTransition ? 'oui' : 'non'); } catch (x) { /* ignore */ }
    });
  } catch (e) { /* navigateur sans pagereveal */ }

  /* Réapplique la mémoire sur la barre fraîchement écrite (avant le premier affichage). */
  function appliquerCache() {
    var c = lireCache();
    var nom = document.querySelector('.sidebar-user .user-name');
    var mail = document.querySelector('.sidebar-user .user-email');
    if (nom && c.nom) nom.textContent = c.nom;
    if (mail && c.email) mail.textContent = c.email;
    var role = document.querySelector('.sidebar-user .user-role');
    if (role && c.role) role.textContent = c.role;
    (c.masques || []).forEach(function (p) {
      var l = document.querySelector('.sidebar-link[data-page="' + p + '"]');
      if (l) l.classList.add('nav-hidden');
    });
    var badge = document.getElementById('sidebarAlertBadge');
    if (badge && c.badge) badge.textContent = c.badge;
  }

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
          '<span class="user-role"></span>' +
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

  function topbarHTML(actif) {
    var noms = { home: 'Home', live: 'Live', alerts: 'Alerts', inbox: 'Inbox',
      settings: 'Settings', sync: 'Sync', senor: 'Senor', digplan: 'Dig Plan' };
    var courant = (noms[actif] || actif).toUpperCase();
    return '<div class="menu-left">' +
        '<span class="r-crumb-app">ORE TRACKING</span>' +
        '<span class="r-crumb-sep2">/</span>' +
        '<span class="r-crumb-space">Espace opérations</span>' +
      '</div>' +
      '<div class="menu-center">' +
        '<span class="r-crumb-page">' + courant + '</span>' +
        '<span class="r-crumb-version">V2.0</span>' +
      '</div>' +
      '<div class="menu-right">' +
        '<span class="r-net-pill" title="Liaison cloud"><i class="fas fa-cloud"></i> Cloud</span>' +
        '<span class="r-net-pill" title="Liaison réseau local"><i class="fas fa-network-wired"></i> LAN</span>' +
        '<span class="r-net-pill" title="Liaison bureau"><i class="fas fa-building"></i> Office</span>' +
        '<span class="sync-pill" id="syncPill" title="Etat de la synchronisation">' +
          '<span class="sync-dot-mini"></span><span class="sync-pill-txt">Sync</span>' +
        '</span>' +
        '<button class="r-bell" type="button" title="Alertes" aria-label="Alertes">' +
          '<i class="fas fa-bell"></i><span class="r-bell-dot"></span>' +
        '</button>' +
        '<button class="r-site-chip" type="button" title="Site courant">' +
          '<i class="fas fa-map-marker-alt"></i> Lafigué' +
          '<i class="fas fa-chevron-down r-site-chev"></i>' +
        '</button>' +
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
          if (!Array.isArray(liste)) return;
          var n = liste.filter(function (a) {
            if (a.acquittee) return false;
            if (window.MSUI && typeof MSUI.aTraiter === 'function') return MSUI.aTraiter(a);
            // pages sans MSUI : meme regle (ni info, ni rejet de blast, shift en cours)
            if (a.info !== undefined ? Number(a.info) : /^(fin_mission|arrivee|mission|retour_piste|retour_trajet|retour_direction|demi_tour|checkpoint_valide|zone_approche|blast_rejete)$/.test(a.type)) return false;
            var d = new Date(), h = d.getHours();
            d.setMinutes(0, 0, 0);
            if (h >= 6 && h < 18) d.setHours(6); else if (h >= 18) d.setHours(18); else { d.setDate(d.getDate() - 1); d.setHours(18); }
            return !(new Date(String(a.created_at).replace(' ', 'T') + 'Z').getTime() < d.getTime());
          }).length;
          badge.textContent = n > 0 ? String(n).padStart(2, '0') : '\u00a0';
          memoriser({ badge: badge.textContent });
        })
        .catch(function () { /* passerelle injoignable : on garde le dernier compte affiché */ });
    }
    lire();
    setInterval(lire, 10000);
  }

  var ecrite = false;
  function ecrire() {
    var sb = document.getElementById('sidebar');
    var tb = document.querySelector('header.menu-bar');
    if (!sb && !tb) return false;
    var actif = pageCourante();
    if (sb) sb.innerHTML = sidebarHTML(actif);
    if (tb) tb.innerHTML = topbarHTML(actif);
    appliquerCache();
    ecrite = true;
    try { performance.mark('ore-nav-ecrite'); } catch (e) { /* diagnostic seulement */ }
    return true;
  }

  function brancher() {
    if (!ecrite) ecrire();
    brancherSalutation();
    brancherBadgeAlertes();
  }

  // 1) tout de suite si les conteneurs sont deja la (script place juste apres eux)
  ecrire();
  // 2) puis, a la fin du chargement : branchements qui dependent du reste de la page
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', brancher);
  } else {
    brancher();
  }
})();
