/* OreTracking V2 - Client API
 * Couche d'acces au serveur (server.py) pour toute l'application.
 * Endpoint de base configurable depuis la page Settings (localStorage).
 */
(function (global) {
  'use strict';

  const LS_KEY = 'oreTrackingApiBase';
  const LS_TOKEN = 'oreTrackingToken';
  const LS_USER = 'oreTrackingUser';

  function getApiBase() {
    // Priorite : localStorage (Settings) -> meme origine que la page
    try {
      const saved = localStorage.getItem(LS_KEY);
      if (saved) return saved.replace(/\/+$/, '');
    } catch (e) { /* storage indisponible */ }
    return global.location ? global.location.origin : 'http://localhost:8100';
  }

  function setApiBase(base) {
    try { localStorage.setItem(LS_KEY, String(base || '').replace(/\/+$/, '')); }
    catch (e) { /* noop */ }
  }

  function getToken() {
    try { return localStorage.getItem(LS_TOKEN) || ''; } catch (e) { return ''; }
  }

  function setToken(token) {
    try {
      if (token) localStorage.setItem(LS_TOKEN, token);
      else localStorage.removeItem(LS_TOKEN);
    } catch (e) { /* noop */ }
  }

  function getUser() {
    try { return JSON.parse(localStorage.getItem(LS_USER) || 'null'); } catch (e) { return null; }
  }

  function setUser(user) {
    try {
      if (user) localStorage.setItem(LS_USER, JSON.stringify(user));
      else localStorage.removeItem(LS_USER);
    } catch (e) { /* noop */ }
  }

  // ---- ID token Firebase (envoye aux endpoints prives du serveur)
  // Priorite : ancien token session (localStorage) -> token Firebase de la
  // session courante (rafraichi automatiquement par le SDK).
  let fbTok = null;
  let fbTokExp = 0;

  async function firebaseIdToken() {
    try {
      const now = Date.now();
      if (fbTok && now < fbTokExp) return fbTok;
      const mod = await import('./firebase-config.js');
      // attendre la restauration de la session Firebase au boot, sinon
      // currentUser est encore null et les premieres requetes partent a 401
      if (!mod.auth.currentUser && typeof mod.auth.authStateReady === 'function') {
        try { await mod.auth.authStateReady(); } catch (e) { /* noop */ }
      }
      const u = mod.auth && mod.auth.currentUser;
      if (!u) return null;
      fbTok = await u.getIdToken();
      fbTokExp = now + 50 * 60 * 1000; // re-demande dans 50 min
      return fbTok;
    } catch (e) {
      return null;
    }
  }

  // reset du cache token (logout / changement de compte)
  function clearFirebaseToken() { fbTok = null; fbTokExp = 0; }

  async function request(method, path, body) {
    const url = getApiBase() + path;
    const lsTok = getToken();
    const fbTok = lsTok ? null : await firebaseIdToken(); // Firebase seulement si pas de token session
    let tok = lsTok || fbTok;
    const opts = {
      method: method,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    };
    if (tok) opts.headers['Authorization'] = 'Bearer ' + tok;
    if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
    let res = await fetch(url, opts);

    // 401 avec un token session localStorage : il est peut-etre expire.
    // On le purge et on retente une fois avec le token Firebase de la session.
    if (res.status === 401 && lsTok) {
      setToken('');
      const retryTok = await firebaseIdToken();
      if (retryTok) {
        opts.headers['Authorization'] = 'Bearer ' + retryTok;
        res = await fetch(url, opts);
      }
    }

    let data = null;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) {
      const err = new Error((data && data.error) || ('HTTP ' + res.status));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  const API = {
    getApiBase, setApiBase,
    getToken, setToken, getUser, setUser,
    clearFirebaseToken,

    // ---- authentification
    login: (identifiant, password) => request('POST', '/api/auth/login', { identifiant, password }),
    logout: () => request('POST', '/api/auth/logout'),
    me: () => request('GET', '/api/auth/me'),
    isLoggedIn: () => !!getToken(),

    // ---- comptes (panneau Securite)
    listComptes: () => request('GET', '/api/comptes'),
    addCompte: (c) => request('POST', '/api/comptes', c),
    updateCompte: (c) => request('PUT', '/api/comptes', c),
    deleteCompte: (id) => request('DELETE', '/api/comptes?id=' + encodeURIComponent(id)),

    // ---- sante / sync
    health: () => request('GET', '/api/health'),
    sync:   () => request('GET', '/api/sync'),
    stats:  () => request('GET', '/api/stats'),
    syncEquipements: () => request('POST', '/api/sync/equipements'),

    // ---- CRUD generique
    listTables:        () => request('GET', '/api/tables'),
    getRows:   (table) => request('GET', '/api/rows?table=' + encodeURIComponent(table)),
    insertRow: (table, row) => request('POST', '/api/rows', { table, row }),
    updateRow: (table, id, row) => request('PUT', '/api/rows', { table, id, row }),
    deleteRow: (table, id) => request('DELETE', '/api/rows?table=' + encodeURIComponent(table) + '&id=' + encodeURIComponent(id)),

    // ---- SQL libre : supprime (endpoint /api/query retire du serveur)

    // ---- configuration (page Settings)
    getConfig: () => request('GET', '/api/config'),
    saveConfig: (params) => request('POST', '/api/config', { params }),
    testGas: (gasUrl) => request('POST', '/api/gas/test', gasUrl ? { gas_url: gasUrl } : {}),
    pullGas: (sheet) => request('POST', '/api/gas/pull', sheet ? { sheet } : {}),
    pushGas: (datasets) => request('POST', '/api/gas/push', { datasets: datasets || null }),
    gasStatus: () => request('GET', '/api/gas/status'),
    migrateGas: () => request('POST', '/api/gas/migrate'),
    diagnosticGas: () => request('GET', '/api/gas/diagnostic'),
    gasNotification: (profil, message) => request('POST', '/api/gas/notification', { profil, message }),


    // ---- alertes
    getAlerts: () => request('GET', '/api/alerts'),
    addAlert: (text, type, cat, engin) =>
      request('POST', '/api/alerts', { text, type: type || 'warn', cat: cat || 'chemin', engin: engin || '' }),
    deleteAlert: (id) => request('DELETE', '/api/alerts?id=' + encodeURIComponent(id)),

    // ---- raccourcis metier
    getEngins:  () => API.getRows('engins'),
    getTablettes: () => request('GET', '/api/tablettes'),
    getZones:   () => API.getRows('zones'),
    getTrails:  () => API.getRows('trails'),
    getVoyages: () => API.getRows('voyages'),
    addVoyage: (v) => API.insertRow('voyages', v),
    addEngin: (e) => API.insertRow('engins', e),
    updateEngin: (id, e) => API.updateRow('engins', id, e),
    deleteEngin: (id) => API.deleteRow('engins', id),

    // ---- equipements (Spotter / Driving Truck / Excavator)
    getEquipements: (type) => {
      if (type) return API.getRows('equipements').then(d => ({
        ...d, rows: d.rows.filter(r => r.type === type)
      }));
      return API.getRows('equipements');
    },
    saveEquipement: (type, equipId, config) => {
      return API.getRows('equipements').then(d => {
        const existing = d.rows.find(r => r.type === type && r.equip_id === equipId);
        const row = { type, equip_id: equipId, config: JSON.stringify(config), actif: config._enabled !== false ? 1 : 0 };
        if (existing) {
          return API.updateRow('equipements', existing.id, row);
        }
        return API.insertRow('equipements', row);
      });
    },
    deleteEquipement: (id) => API.deleteRow('equipements', id),
  };

  global.API = API;
})(window);
