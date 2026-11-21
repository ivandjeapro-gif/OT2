/* OreTracking V2 - Preferences globales
 * Charge AVANT api.js/auth.js/app.js. Expose window.PREFS.
 * Gere : langue (traduction auto de toutes les pages), unites, fuseau,
 * formats, theme, contraste, animations, son, page d'accueil, zoom police.
 */
(function (global) {
  'use strict';

  var LS_KEY = 'otPrefs';
  var DEFAULTS = {
    lang: 'fr',
    units: 'metric',
    timezone: 'Europe/Paris',
    dateFormat: 'dd/mm/yyyy',
    numberFormat: 'comma',
    sound: false,
    homePage: 'home',
    fontSize: 100,
    highContrast: false,
    reducedMotion: false,
    theme: 'dark',
    animations: true,
    refresh: 5
  };

  var prefs = {};

  function cloneDefaults() {
    var o = {};
    for (var k in DEFAULTS) o[k] = DEFAULTS[k];
    return o;
  }

  function load() {
    try {
      var saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      prefs = cloneDefaults();
      for (var k in saved) if (k in DEFAULTS) prefs[k] = saved[k];
    } catch (e) { prefs = cloneDefaults(); }
  }

  function save() {
    try { localStorage.setItem(LS_KEY, JSON.stringify(prefs)); } catch (e) {}
  }

  function get(key) { return prefs[key]; }

  function set(key, value) {
    prefs[key] = value;
    save();
    apply();
  }

  // ---- helpers de formatage ----
  function formatNumber(n, decimals) {
    if (n == null || isNaN(n)) return '—';
    var d = (decimals != null) ? decimals : 1;
    var s = Number(n).toFixed(d);
    if (prefs.numberFormat === 'comma') s = s.replace('.', ',');
    return s;
  }

  function formatDate(input) {
    var d = (input instanceof Date) ? input : new Date(input);
    if (isNaN(d)) return '—';
    var dd = String(d.getDate()).padStart(2, '0');
    var mm = String(d.getMonth() + 1).padStart(2, '0');
    var yyyy = d.getFullYear();
    switch (prefs.dateFormat) {
      case 'mm/dd/yyyy': return mm + '/' + dd + '/' + yyyy;
      case 'yyyy-mm-dd': return yyyy + '-' + mm + '-' + dd;
      default: return dd + '/' + mm + '/' + yyyy;
    }
  }

  function formatTime(input) {
    var d = (input instanceof Date) ? input : new Date(input);
    if (isNaN(d)) return '—';
    try {
      return d.toLocaleTimeString('fr-FR', {
        timeZone: prefs.timezone, hour: '2-digit', minute: '2-digit', second: '2-digit'
      });
    } catch (e) {
      return String(d.getHours()).padStart(2, '0') + ':' +
             String(d.getMinutes()).padStart(2, '0') + ':' +
             String(d.getSeconds()).padStart(2, '0');
    }
  }

  function formatDateTime(input) { return formatDate(input) + ' ' + formatTime(input); }

  function convertUnit(value, type) {
    if (value == null || isNaN(value)) return value;
    if (prefs.units === 'metric') return value;
    switch (type) {
      case 'temp': return (value * 9 / 5) + 32;
      case 'fuel': return value * 0.264172;
      default: return value;
    }
  }

  function unitLabel(type) {
    if (prefs.units === 'metric') return { tonnes: 't', temp: '°C', fuel: 'L' }[type] || '';
    return { tonnes: 't', temp: '°F', fuel: 'gal' }[type] || '';
  }

  // ---- notifications sonores (Web Audio) ----
  var audioCtx = null;
  function beep(freq, dur, delay) {
    if (!prefs.sound) return;
    try {
      if (!audioCtx) audioCtx = new (global.AudioContext || global.webkitAudioContext)();
      var t0 = audioCtx.currentTime + (delay || 0);
      var o = audioCtx.createOscillator();
      var g = audioCtx.createGain();
      o.connect(g); g.connect(audioCtx.destination);
      o.frequency.value = freq || 800;
      o.type = 'sine';
      g.gain.setValueAtTime(0.25, t0);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + (dur || 0.25));
      o.start(t0); o.stop(t0 + (dur || 0.25));
    } catch (e) {}
  }

  function alertSound() { beep(880, 0.35); beep(660, 0.35, 0.4); }
  function notifySound() { beep(660, 0.18); }

  // ---- dictionnaire FR -> EN ----
  var I18N_EN = {
    // login
    'Connexion a votre compte': 'Sign in to your account',
    'Email ou nom': 'Email or username',
    'Mot de passe': 'Password',
    'Se connecter': 'Sign in',
    // menu bar
    'Files': 'Files', 'Edit': 'Edit', 'Export': 'Export',
    'Synchronisation': 'Synchronization', 'Help': 'Help',
    // sidebar / nav
    'Deconnexion': 'Sign out',
    'Settings': 'Settings', 'Sync': 'Sync',
    // home
    'Bienvenue sur OreTracking V2': 'Welcome to OreTracking V2',
    'Systeme de tracking minier en temps reel': 'Real-time mining tracking system',
    'Engins actifs': 'Active engines',
    'Alertes': 'Alerts',
    'Minerais today': 'Minerals today',
    'Ore today': 'Ore today',
    // live - map
    'Map Tracking': 'Map Tracking',
    'Modifier': 'Edit', 'Confirmer': 'Confirm', 'Annuler': 'Cancel',
    'Carte de fond': 'Basemap',
    'Centre & Zoom': 'Center & Zoom',
    'Latitude': 'Latitude', 'Longitude': 'Longitude', 'Zoom': 'Zoom',
    'Systeme de coordonnees': 'Coordinate system',
    'Definir manuellement': 'Set manually',
    'Zones minieres': 'Mining zones',
    'Trails (chemins)': 'Trails (paths)',
    'Ajouter zone': 'Add zone', 'Ajouter trail': 'Add trail',
    'Importer CSV': 'Import CSV', 'Importer fichier': 'Import file',
    'Glissez un fichier ici ou': 'Drag a file here or',
    'parcourir': 'browse',
    'Afficher alertes': 'Show alerts',
    'Afficher trails': 'Show trails',
    'Labels engins': 'Engine labels',
    // live - kpi
    'KPI - Configuration': 'KPI - Configuration',
    'Minerais - Tonnage': 'Minerals - Tonnage',
    'Ore - onces': 'Ore - ounces',
    'Waste - Tonnage': 'Waste - Tonnage',
    'Offroad Count': 'Offroad Count',
    'High Grade': 'High Grade', 'Medium Grade': 'Medium Grade',
    'Low Grade': 'Low Grade', 'Marginal Ore': 'Marginal Ore',
    // live - filtres
    'Filtres': 'Filters',
    'Type': 'Type', 'Statut': 'Status', 'Zone': 'Zone',
    'Periode': 'Period', 'Date': 'Date',
    'Tous': 'All', 'Toutes': 'All',
    'Jour (06h-18h)': 'Day (06:00-18:00)',
    'Nuit (18h-06h)': 'Night (18:00-06:00)',
    'Dump Truck': 'Dump Truck', 'Excavator': 'Excavator', 'Bulldozer': 'Bulldozer',
    'Actif': 'Active', 'Idle': 'Idle', 'Maintenance': 'Maintenance',
    'Appliquer': 'Apply', 'Reinitialiser': 'Reset',
    // live - rendu
    'Rendu': 'Rendering',
    'Mode': 'Mode', 'Automatique': 'Automatic',
    'Ecran cible': 'Target screen', 'Ecran 1': 'Screen 1', 'Ecran 2': 'Screen 2',
    'Double ecran': 'Dual screen', 'Fenetre': 'Window',
    'Plein ecran etendu': 'Extended fullscreen',
    'Animations': 'Animations',
    'PRESENTER': 'PRESENT',
    // layers
    'Couches': 'Layers',
    'Ajouter une couche': 'Add a layer',
    'Configuration et lancement de la projection etendue': 'Extended projection setup and launch',
    // alerts page
    'Alerts du jour': "Today's Alerts",
    'Heure': 'Time', 'Engin': 'Engine', 'Description': 'Description',
    'Severite': 'Severity', 'Chemin': 'Path', 'Offroad': 'Offroad',
    'Carburant': 'Fuel', 'Meteo': 'Weather', 'Grade': 'Grade',
    'Haute': 'High', 'Moyenne': 'Medium',
    'Termine': 'Done', 'En cours': 'In progress', 'En attente': 'Pending',
    // inbox page
    'Inbox': 'Inbox',
    'Depart': 'Departure', 'Destination': 'Destination',
    'Tonnage': 'Tonnage', 'Rompad': 'ROM pad',
    // settings - panels
    'Preferences': 'Preferences',
    'Connection': 'Connection',
    'Securite': 'Security',
    'Synchronisation': 'Synchronization',
    // preferences
    'Affichage': 'Display',
    'Theme': 'Theme', 'Sombre': 'Dark', 'Minuit': 'Midnight',
    'Taille police (%)': 'Font size (%)',
    'Contraste eleve': 'High contrast',
    'Mouvement reduit': 'Reduced motion',
    'Format': 'Format',
    'Langue': 'Language',
    'Unites': 'Units',
    'Format date': 'Date format',
    'Format nombre': 'Number format',
    'Fuseau horaire': 'Time zone',
    'Comportement': 'Behavior',
    'Notifications sonores': 'Sound notifications',
    "Page d'accueil": 'Home page',
    'Mode simulation': 'Simulation mode',
    'Rafraichissement (sec)': 'Refresh (sec)',
    // connection
    'URL API': 'API URL',
    'Source de donnees': 'Data source',
    'Google App Script': 'Google App Script',
    'Azure PostgreSQL (bientot)': 'Azure PostgreSQL (soon)',
    "URL de l'application": 'Application URL',
    "Token d'acces": 'Access token',
    'Tester': 'Test',
    'Enregistrer les parametres': 'Save settings',
    'Serveur': 'Server', 'Port': 'Port', 'Base': 'Database',
    'User': 'User', 'Base de donnees': 'Database',
    'Disponible prochainement': 'Available soon',
    // securite
    'Auth': 'Auth', 'LDAP': 'LDAP',
    'Timeout (min)': 'Timeout (min)',
    'Comptes': 'Accounts',
    'Admin': 'Admin', 'Viewer': 'Viewer',
    // alertes & seuils
    // sync
    'Synchroniser maintenant': 'Synchronize now',
    'Derniere sync': 'Last sync',
    'Serveur: Connecte': 'Server: Connected',
    'Base de donnees: OK': 'Database: OK',
    'Synchronisation des donnees avec le serveur central': 'Data synchronization with central server',
    // affectation / tablette
    'Affectation': 'Assignment',
    'Tablette': 'Tablet',
    'Tablettes': 'Tablets',
    'GNSS vers equipement': 'GNSS to equipment',
    'Tablette vers equipe': 'Tablet to team',
    'ID Tablette (TAB-001)': 'Tablet ID (TAB-001)',
    'ID GNSS (G-001)': 'GNSS ID (G-001)',
    'ID engin (DT-001)': 'Machine ID (DT-001)',
    'Aucune tablette': 'No tablet',
    'Aucune affectation': 'No assignment',
    'Radio XBee': 'XBee Radio',
    'Connecter la radio XBee (cable)': 'Connect XBee radio (cable)',
    // wizard provisioning GNSS (Web Serial)
    'Provisioning GNSS': 'GNSS Provisioning',
    'Liez le module GNSS a ce terminal via le port serie (115200 bauds).':
      'Pair the GNSS module to this terminal over the serial port (115200 baud).',
    'Unlock and Initialize': 'Unlock and Initialize',
    'GNSS actifs': 'Active GNSS',
    'Aucun GNSS lie': 'No GNSS paired',
    'GNSS Identification Required': 'GNSS Identification Required',
    'Automatic detection timed out. Please manually select the hardware ID attached to this terminal.':
      'Automatic detection timed out. Please manually select the hardware ID attached to this terminal.',
    'Confirm': 'Confirm',
    // equipements
    'Spotter': 'Spotter',
    'Driving Truck': 'Driving Truck',
    'Excavator': 'Excavator',
    'Configuration': 'Configuration',
    'ID Spotter': 'Spotter ID',
    'Mode': 'Mode',
    'Automatique': 'Automatic',
    'Manuel': 'Manual',
    'Rayon recherche (m)': 'Search radius (m)',
    'Actif': 'Active',
    'Alerte position': 'Position alert',
    'ID Truck': 'Truck ID',
    'Capacite (t)': 'Capacity (t)',
    'Vitesse max (km/h)': 'Max speed (km/h)',
    'Tracking GPS': 'GPS tracking',
    'ID Excavator': 'Excavator ID',
    'Benne (m3)': 'Bucket (m3)',
    'Cycle moyen (sec)': 'Average cycle (sec)',
    'Alerte carburant': 'Fuel alert',
    'Enregistrer': 'Save',
    'ID requis': 'ID required',
    'Erreur': 'Error',
    'Sheet': 'Sheet',
    'Sync...': 'Sync...',
    'Spotters': 'Spotters',
    'Trucks': 'Trucks',
    'Excavators': 'Excavators',
    'Aucun equipement': 'No equipment',
    'Ajoute': 'Added',
    'Desactive': 'Disabled',
    'Reactive': 'Enabled',
    'Supprime': 'Deleted',
    'Supprimer cet equipement ?': 'Delete this equipment?',
    'desactive': 'disabled',
    'Aucune team': 'No team',
    'Aucun membre': 'No member',
    'Nom de la team': 'Team name',
    'Nom du membre': 'Member name',
    'Chef d equipe': 'Team leader',
    'Operateur': 'Operator',
    'Serveur indisponible': 'Server unavailable',
    'Connectez-vous pour gerer les equipements': 'Sign in to manage equipment',
    'Rayon (m)': 'Radius (m)',
    'Capacite (t)': 'Capacity (t)',
    'Vitesse (km/h)': 'Speed (km/h)',
    'Benne (m3)': 'Bucket (m3)',
    'Cycle (sec)': 'Cycle (sec)',
    // divers
    'Tous': 'All', 'Chargement...': 'Loading...',
    'Temperature': 'Temperature', 'Temps pluvieux': 'Rainy weather',
    'Humidite a 67%': 'Humidity at 67%',
    'Commande serveur': 'Server command',
    'Fichier MBTiles': 'MBTiles file',
    'URL tile server': 'Tile server URL',
    'MBTiles (serveur local)': 'MBTiles (local server)',
    'Satellite (ESRI World Imagery)': 'Satellite (ESRI World Imagery)',
    'OpenStreetMap (HOT)': 'OpenStreetMap (HOT)',
    'Topographique (ESRI)': 'Topographic (ESRI)',
    'Sombre (ESRI)': 'Dark (ESRI)',
    'UTM (Easting / Northing entiers)': 'UTM (integer Easting / Northing)',
    'WGS84 (Latitude / Longitude decimaux)': 'WGS84 (decimal Latitude / Longitude)',
    'Site': 'Site', 'Shift': 'Shift',
    'Jour': 'Day', 'Nuit': 'Night',
    'Actuel': 'Current', 'Planned': 'Planned',
    'Max': 'Max', 'Min': 'Min',
    'High': 'High', 'Medium': 'Medium', 'Low': 'Low', 'Marginal': 'Marginal',
    'Haute': 'High', 'Moyenne': 'Medium', 'Basse': 'Low',
    'Chemin': 'Path', 'Carburant': 'Fuel',
  };

  var currentLang = 'fr';
  var _origMap = (typeof Map !== 'undefined') ? new Map() : null;
  var _origArr = []; // fallback si Map absent : [{node, text}]

  function storeOrig(node, text) {
    if (_origMap) _origMap.set(node, text);
    else _origArr.push({ node: node, text: text });
  }
  function getOrig(node) {
    if (_origMap) return _origMap.get(node);
    for (var i = 0; i < _origArr.length; i++) if (_origArr[i].node === node) return _origArr[i].text;
    return null;
  }

  function applyLang() {
    currentLang = prefs.lang;
    if (document.documentElement) document.documentElement.lang = currentLang;

    var walker = document.createTreeWalker(
      document.body || document.documentElement,
      NodeFilter.SHOW_TEXT, null, false);
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentElement;
      if (parent) {
        var tag = parent.tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA') continue;
        // ne pas traduire la valeur des inputs (placeholder gere a part)
        if (tag === 'INPUT' && parent.type !== 'submit' && parent.type !== 'button') continue;
      }
      var text = node.textContent;
      var trimmed = text.replace(/\s+/g, ' ').trim();
      if (!trimmed || !/[A-Za-zÀ-ÿ]/.test(trimmed)) continue;

      if (!getOrig(node)) storeOrig(node, text);
      var orig = getOrig(node) || text;
      var origNorm = orig.replace(/\s+/g, ' ').trim();

      if (currentLang === 'en' && I18N_EN[origNorm]) {
        var lead = text.match(/^\s*/)[0];
        var trail = text.match(/\s*$/)[0];
        node.textContent = lead + I18N_EN[origNorm] + trail;
      } else {
        node.textContent = orig;
      }
    }

    // placeholders des inputs (login, etc.)
    translatePlaceholders();
  }

  function translatePlaceholders() {
    var ph = {
      'gaston.albert@oresys.com': 'gaston.albert@oresys.com',
    };
    // rien a faire pour l'instant : placeholders deja bilingues / techniques
  }

  // ---- application visuelle ----
  function apply() {
    var b = document.body;
    if (!b) return;

    // zoom police (%)
    var z = Number(prefs.fontSize);
    b.style.zoom = (z && z !== 100) ? (z / 100) : '';

    // classes CSS
    b.classList.toggle('theme-midnight', prefs.theme === 'midnight');
    b.classList.toggle('high-contrast', !!prefs.highContrast);
    var noMotion = !!prefs.reducedMotion || !prefs.animations;
    b.classList.toggle('reduced-motion', noMotion);
    b.classList.toggle('no-animations', !prefs.animations);

    applyLang();
  }

  // init
  load();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { apply(); });
  } else {
    apply();
  }

  global.PREFS = {
    get: get, set: set, load: load, save: save, apply: apply,
    formatNumber: formatNumber,
    formatDate: formatDate,
    formatTime: formatTime,
    formatDateTime: formatDateTime,
    convertUnit: convertUnit,
    unitLabel: unitLabel,
    alertSound: alertSound,
    notifySound: notifySound,
    all: function () { var o = {}; for (var k in prefs) o[k] = prefs[k]; return o; }
  };
})(window);
