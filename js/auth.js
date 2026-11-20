/* OreTracking V2 - Authentification Firebase (module)
 * - Ecran de connexion : signInWithEmailAndPassword
 * - Session : onAuthStateChanged (persistance native Firebase)
 * - Deconnexion : confirm() + reset des champs
 * - Panneau Settings > Securite : comptes dans Firestore (users)
 */
import { auth, authAdmin, db } from './firebase-config.js';
import {
  signInWithEmailAndPassword,
  signOut,
  onAuthStateChanged,
  createUserWithEmailAndPassword,
} from "https://www.gstatic.com/firebasejs/13.0.0/firebase-auth.js";
import {
  collection, getDocs, getDoc, doc, setDoc, query, orderBy, runTransaction,
} from "https://www.gstatic.com/firebasejs/13.0.0/firebase-firestore.js";
import {
  normaliserRole, libelleRole, peutAcceder, roleSuivant, connexionUnique, PAGE_REPLI,
  ouvrirSessionUnique, battementSessionUnique, fermerSessionUnique, SESSION_BATTEMENT_MS,
} from './roles.js';

function $(sel) { return document.querySelector(sel); }

// -------------------------------------------- ecritures vers la passerelle ---
// La passerelle (:8101) exige un jeton Firebase pour toute ECRITURE (POST / PUT / DELETE) :
// elle verifie le profil (admin / senor / geologue) aupres du SaaS. Ce relais ajoute l'en-tete
// Authorization a ces requetes, pour TOUTES les pages qui chargent auth.js ; les lectures
// (GET) restent sans jeton.
(function relaisJetonPasserelle() {
  const nativeFetch = window.fetch.bind(window);
  function basePasserelle() {
    let base = '';
    try { base = localStorage.getItem('sgApiBase') || ''; } catch (e) { /* stockage indisponible */ }
    return (base || (location.protocol + '//' + (location.hostname || 'localhost') + ':8101')).replace(/\/+$/, '');
  }
  window.fetch = async function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const methode = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (methode !== 'GET' && methode !== 'HEAD' && methode !== 'OPTIONS' && url.indexOf(basePasserelle() + '/') === 0) {
        const jeton = await window.OreTrackingAuth.getToken();
        if (jeton) {
          const h = new Headers((init && init.headers) || (input && input.headers) || undefined);
          if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + jeton);
          init = Object.assign({}, init || {}, { headers: h });
        }
      }
    } catch (e) { /* sans jeton : la passerelle repondra 401, rien ne casse ici */ }
    return nativeFetch(input, init);
  };
})();

// profil Firestore de l'utilisateur courant (role, desactive)
let myProfile = null;

// ------------------------------------------------- session / timeout ------
let sessionTimeoutMin = 30;   // duree d'inactivite avant deconnexion (min)
let lastActivity = Date.now();
let inactivityTimer = null;
let pendingLoginMsg = null;   // message affiche apres deconnexion (timeout)

function noteActivity() { lastActivity = Date.now(); }

async function loadSessionTimeout() {
  try {
    const { params } = await API.getConfig();
    const v = parseInt(params.auth_timeout, 10);
    if (v >= 5 && v <= 480) sessionTimeoutMin = v;
  } catch (e) { /* offline : defaut 30 */ }
}

function startInactivityWatch() {
  stopInactivityWatch();
  lastActivity = Date.now();
  ['mousedown', 'keydown', 'touchstart', 'wheel', 'mousemove'].forEach(ev =>
    document.addEventListener(ev, noteActivity, { passive: true }));
  console.debug('[session] veille active, timeout=' + sessionTimeoutMin + 'min');
  inactivityTimer = setInterval(() => {
    if (!auth.currentUser) return;
    const idleMin = (Date.now() - lastActivity) / 60000;
    if (idleMin >= sessionTimeoutMin) {
      stopInactivityWatch();
      pendingLoginMsg = { text: 'Session expiree apres ' + sessionTimeoutMin + ' min d inactivite', cls: 'err' };
      signOut(auth).catch(() => {});
    }
  }, 10000); // verification toutes les 10 s
}

function stopInactivityWatch() {
  if (inactivityTimer) { clearInterval(inactivityTimer); inactivityTimer = null; }
  ['mousedown', 'keydown', 'touchstart', 'wheel', 'mousemove'].forEach(ev =>
    document.removeEventListener(ev, noteActivity));
}

function setLoginStatus(msg, cls) {
  const st = $('#loginStatus');
  if (!st) return;
  st.textContent = msg || '';
  st.className = 'login-status ' + (cls || '');
}

// Firestore peut etre lent (negociation WebChannel au 1er appel) :
// on borne chaque operation pour ne jamais bloquer le panneau.
function fsOp(p, ms) {
  let timer;
  const timeout = new Promise((_, rej) => {
    timer = setTimeout(() => rej(new Error('delai depasse')), ms || 45000);
  });
  return Promise.race([p, timeout]).finally(() => clearTimeout(timer));
}

// ------------------------------------------------------------ ecran login --
function showLogin() {
  const ov = $('#loginOverlay');
  const app = $('.app');
  if (ov) ov.style.display = 'flex';
  if (app) app.style.visibility = 'hidden';
}

function hideLogin() {
  const ov = $('#loginOverlay');
  const app = $('.app');
  if (ov) ov.style.display = 'none';
  if (app) app.style.visibility = 'visible';
}

function renderUser(user) {
  const name = $('.user-name');
  const email = $('.user-email');
  if (name) name.textContent = user ? (user.nom || user.displayName || user.email || '') : '';
  if (email) email.textContent = user ? (user.email || '') : '';
}

function resetLoginForm() {
  const ident = $('#loginIdentifiant');
  const pwd = $('#loginPassword');
  const status = $('#loginStatus');
  if (ident) ident.value = '';
  if (pwd) pwd.value = '';
  if (status) { status.textContent = ''; status.className = 'login-status'; }
}

function loginError(e) {
  const code = (e && e.code) || '';
  if (code === 'auth/invalid-credential' || code === 'auth/wrong-password' ||
      code === 'auth/user-not-found') return 'Email ou mot de passe incorrect';
  if (code === 'auth/invalid-email') return 'Adresse email invalide';
  if (code === 'auth/too-many-requests') return 'Trop de tentatives, reessayez plus tard';
  if (code === 'auth/network-request-failed') return 'Reseau indisponible';
  return (e && e.message) || 'Echec de connexion';
}

async function doLogin(identifiant, password) {
  const status = $('#loginStatus');
  const btn = $('#btnLogin');
  if (btn) { btn.disabled = true; btn.textContent = 'Connexion...'; }
  if (status) { status.textContent = ''; status.className = 'login-status'; }
  try {
    await signInWithEmailAndPassword(auth, (identifiant || '').trim(), password || '');
    if (status) { status.textContent = 'Connecte !'; status.className = 'login-status ok'; }
    setTimeout(hideLogin, 250);
    return true;
  } catch (e) {
    if (status) {
      status.textContent = loginError(e);
      status.className = 'login-status err';
    }
    return false;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Se connecter'; }
  }
}

async function doLogout() {
  if (!confirm('Se deconnecter ?')) return;
  await libererSessionUnique();
  try { await signOut(auth); } catch (e) { /* deja deconnecte */ }
  renderUser(null);
  resetLoginForm();
  showLogin();
}

// ----------------------------------------- connexion unique (Senor_Geology) ---
// Un seul poste a la fois sur un compte « senor » : document Firestore
// sessions_unique/{uid} (regle a ajouter : lecture/ecriture par le proprietaire).
// Identifiant de poste = par NAVIGATEUR (localStorage) : plusieurs onglets d'un meme poste
// partagent la session, un autre PC est refuse tant que le premier est actif (90 s sans
// battement = libere). Firestore injoignable : on laisse entrer (jamais de blocage total),
// avec un avertissement.
let battementTimer = null;
let sessionUniqueUid = null;

function identifiantPoste() {
  let sid = null;
  try { sid = localStorage.getItem('ore-poste-id'); } catch (e) { /* stockage indisponible */ }
  if (!sid) {
    sid = (self.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
    try { localStorage.setItem('ore-poste-id', sid); } catch (e) { /* idem */ }
  }
  return sid;
}
const nomPoste = () => (navigator.platform || 'poste') + ' · ' + screen.width + 'x' + screen.height + ' · ' + location.hostname;

function storeSessionUnique(tx) {
  const ref = (uid) => doc(db, 'sessions_unique', uid);
  return {
    lire: async (uid) => { const s = await tx.get(ref(uid)); return s.exists() ? s.data() : null; },
    ecrire: async (uid, d) => { tx.set(ref(uid), d); },
    supprimer: async (uid) => { tx.delete(ref(uid)); },
  };
}

async function liberer(uid) {
  try { await fsOp(runTransaction(db, (tx) => fermerSessionUnique(storeSessionUnique(tx), uid, identifiantPoste())), 8000); }
  catch (e) { /* le delai de 90 s libere de toute facon */ }
}

async function libererSessionUnique() {
  if (battementTimer) { clearInterval(battementTimer); battementTimer = null; }
  if (sessionUniqueUid) { const u = sessionUniqueUid; sessionUniqueUid = null; await liberer(u); }
}

/** Rend true si l'utilisateur peut entrer. Refus : message + deconnexion. */
async function prendreSessionUnique(user, role) {
  if (!connexionUnique(role)) return true;
  let r;
  try {
    r = await fsOp(runTransaction(db, (tx) => ouvrirSessionUnique(storeSessionUnique(tx), user.uid, identifiantPoste(), nomPoste())), 15000);
  } catch (e) {
    console.warn('[connexion unique] Firestore injoignable ou regle absente : controle ignore (' + (e.code || e.message) + ')');
    return true;
  }
  if (!r.ok) {
    const depuis = r.depuis ? new Date(r.depuis).toLocaleTimeString('fr-FR') : '?';
    pendingLoginMsg = { text: 'Ce compte Senor_Geology est deja connecte sur un autre poste' +
      (r.appareil ? ' (' + r.appareil + ')' : '') + ' — derniere activite ' + depuis, cls: 'err' };
    await signOut(auth).catch(() => {});
    return false;
  }
  sessionUniqueUid = user.uid;
  if (battementTimer) clearInterval(battementTimer);
  battementTimer = setInterval(async () => {
    try {
      const b = await fsOp(runTransaction(db, (tx) => battementSessionUnique(storeSessionUnique(tx), user.uid, identifiantPoste(), nomPoste())), 15000);
      if (!b.ok) {
        clearInterval(battementTimer); battementTimer = null; sessionUniqueUid = null;
        pendingLoginMsg = { text: 'Session reprise sur un autre poste' + (b.appareil ? ' (' + b.appareil + ')' : ''), cls: 'err' };
        await signOut(auth).catch(() => {});
      }
    } catch (e) { /* reseau coupe : on reessaie au prochain battement */ }
  }, SESSION_BATTEMENT_MS);
  return true;
}

// ------------------------------------------------- panneau Securite ------
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

async function upsertProfile(user) {
  if (!user) return null;
  const ref = doc(db, 'users', user.uid);
  const snap = await fsOp(getDoc(ref));
  if (snap.exists()) return snap.data();
  // premier profil vu = admin, sinon geologue (profil d'usage le plus restreint)
  let role = 'geologue';
  try {
    const tous = await fsOp(getDocs(collection(db, 'users')));
    if (tous.empty) role = 'admin';
  } catch (e) { /* liste refusee : on reste geologue */ }
  const prof = {
    email: user.email || '',
    nom: user.displayName || (user.email || '').split('@')[0],
    role: role,
    cree: new Date().toISOString(),
  };
  await fsOp(setDoc(ref, prof));
  return prof;
}

// Navigation selon le profil (js/roles.js) : admin = tout ; senor = geologue + Senor ;
// geologue = Home, Live, Alerts, Inbox, Dig Plan, Sync, Aide. Retourne false si la page
// courante est interdite : on renvoie alors sur Home (et l'ecran n'est pas devoile).
function applyNavAccess(role) {
  document.querySelectorAll('.sidebar-link[data-page]').forEach(l => {
    l.classList.toggle('nav-hidden', !peutAcceder(role, l.dataset.page));
  });
  const active = document.querySelector('.sidebar-link.active');
  const page = active && active.dataset.page;
  if (page && !peutAcceder(role, page)) {
    const home = document.querySelector('.sidebar-link[data-page="' + PAGE_REPLI + '"]');
    if (home) { naviguerVers(home); return false; }
  }
  return true;
}

/**
 * Suit un lien de la sidebar, SAUF s'il designe la page deja affichee.
 *
 * Chaque page de la sidebar est un fichier HTML distinct : cliquer le lien
 * de la page courante la rechargerait, et l'initialisation qui suit
 * recliquerait ce meme lien -> boucle de rechargement infinie.
 */
function naviguerVers(link) {
  if (!link) return;
  const href = link.getAttribute('href');
  if (href) {
    let cible = href;
    try { cible = new URL(href, window.location.href).pathname; } catch (e) { /* href relatif */ }
    const courante = window.location.pathname;
    const norm = (p) => (p || '').replace(/\/+$/, '');
    if (norm(cible) === norm(courante)) return;   // deja sur la page
  }
  link.click();
}

async function loadAccounts() {
  const box = $('#accountsContainer');
  if (!box) return;
  const user = auth.currentUser;
  if (!user) {
    box.innerHTML = '<div class="account-item muted">Connectez-vous pour gerer les comptes</div>';
    return;
  }
  const isAdmin = !!myProfile && myProfile.role === 'admin';
  const addForm = $('#accountAddForm');
  if (addForm) addForm.style.display = isAdmin ? '' : 'none';
  try {
    const snap = await fsOp(getDocs(query(collection(db, 'users'), orderBy('nom'))));
    box.innerHTML = snap.docs.map(d => {
      const c = d.data();
      const accAdmin = c.role === 'admin';
      const isSelf = d.id === user.uid;
      const roleCls = accAdmin ? 'account-role admin' : 'account-role';
      let html = '<div class="account-item' + (c.desactive ? ' desactive' : '') + '" data-id="' + d.id + '">' +
        '<i class="fas fa-user-circle"></i>' +
        '<span class="account-nom">' + escapeHtml(c.nom || c.email || d.id) + '</span>' +
        '<span class="account-email">' + escapeHtml(c.email || '') + '</span>' +
        '<span class="' + roleCls + '">' + libelleRole(c.role) + '</span>';
      if (c.desactive) html += '<span class="account-flag">desactive</span>';
      if (isSelf) html += '<span class="account-self" title="Vous">vous</span>';
      if (isAdmin && !isSelf) {
        html += '<span class="account-actions">' +
          '<button class="account-btn" data-action="role" title="Changer le role"><i class="fas fa-exchange-alt"></i></button>' +
          '<button class="account-btn" data-action="toggle-active" title="' + (c.desactive ? 'Reactiver' : 'Desactiver') + '">' +
          '<i class="fas fa-' + (c.desactive ? 'check' : 'ban') + '"></i></button>' +
          '</span>';
      }
      return html + '</div>';
    }).join('') ||
      '<div class="account-item muted">Aucun compte</div>';
  } catch (e) {
    box.innerHTML = '<div class="account-item muted">Firestore indisponible : ' +
      escapeHtml(e.message || 'erreur') + '</div>';
  }
}

function setAccountStatus(msg, cls) {
  const el = $('#accountStatus');
  if (!el) return;
  el.textContent = msg;
  el.className = 'account-status ' + (cls || '');
  if (msg) setTimeout(() => { el.textContent = ''; el.className = 'account-status'; }, 4000);
}

async function addAccount(e) {
  e.preventDefault();
  const nom = (($('#accNom') || {}).value || '').trim();
  const email = (($('#accEmail') || {}).value || '').trim();
  const role = normaliserRole(($('#accRole') || {}).value || 'geologue');
  const password = ($('#accPassword') || {}).value || '';
  if (!nom || !email || !password) {
    setAccountStatus('Nom, email et mot de passe requis', 'err');
    return;
  }
  if (password.length < 6) {
    setAccountStatus('Mot de passe : 6 caracteres minimum (Firebase)', 'err');
    return;
  }
  try {
    // instance secondaire : ne change pas la session de l'admin connecte
    const sp = Splash.bloquer('Creation du compte...', nom);
    Splash.etape('Creation Firebase Auth...');
    const cred = await createUserWithEmailAndPassword(authAdmin, email, password);
    await setDoc(doc(db, 'users', cred.user.uid), {
      email: email,
      nom: nom,
      role: role,
      cree: new Date().toISOString(),
    });
    // si c'est l'utilisateur connecte qui a cree son propre compte, ne pas rester
    // connecte sous le nouveau compte : on reste sur la session courante
    sp.fermer();
    Splash.succes('Compte cree', nom + ' (' + role + ')');
    setAccountStatus('Compte cree.', 'ok');
    const f = $('#accountAddForm');
    if (f) f.reset();
    loadAccounts();
  } catch (err) {
    Splash.erreur('Creation impossible', (err.code === 'auth/email-already-in-use') ? 'Cet email est deja utilise'
      : (err.code === 'auth/weak-password') ? 'Mot de passe : 6 caracteres minimum'
      : (err.code === 'auth/operation-not-allowed') ? 'Activez Email/Password dans Firebase Auth'
      : (err.message || 'Echec creation'));
    const msg = (err.code === 'auth/email-already-in-use') ? 'Cet email est deja utilise'
      : (err.code === 'auth/weak-password') ? 'Mot de passe : 6 caracteres minimum'
      : (err.code === 'auth/operation-not-allowed') ? 'Activez Email/Password dans Firebase Auth'
      : (err.message || 'Echec creation');
    setAccountStatus(msg, 'err');
  }
}

// clics sur les boutons role / desactiver de la liste des comptes
async function onAccountAction(e) {
  const btn = e.target.closest('.account-btn');
  if (!btn) return;
  const item = btn.closest('.account-item');
  const uid = item && item.dataset.id;
  const user = auth.currentUser;
  if (!uid || !user) return;
  if (!myProfile || myProfile.role !== 'admin') {
    setAccountStatus('Action reservee aux admins', 'err');
    return;
  }
  if (uid === user.uid) {
    setAccountStatus('Impossible de modifier son propre compte', 'err');
    return;
  }
  // Nom affiche pour les confirmations (extrait de la carte deja rendue)
  const nomCompte = (item.querySelector('.account-nom') || {}).textContent || 'ce compte';
  const ref = doc(db, 'users', uid);
  try {
    const snap = await fsOp(getDoc(ref));
    if (!snap.exists()) { setAccountStatus('Compte introuvable', 'err'); return; }
    const data = snap.data();
    if (btn.dataset.action === 'role') {
      const newRole = roleSuivant(data.role);
      if (data.role === 'admin' && newRole !== 'admin') {
        // ne jamais retirer le dernier admin actif
        const tous = await fsOp(getDocs(query(collection(db, 'users'))));
        const autres = tous.docs.filter(d =>
          d.id !== uid && d.data().role === 'admin' && !d.data().desactive);
        if (autres.length === 0) {
          setAccountStatus('Impossible : dernier admin actif', 'err');
          return;
        }
      }
      // action sensible : confirmation avant application (popup maison)
      if (!await Splash.confirmer(
          'Changer le role de "' + nomCompte + '" ?',
          { titre: 'Changement de role', danger: false,
            details: 'Role actuel : ' + libelleRole(data.role) + ' -> ' + libelleRole(newRole) +
                     (newRole === 'admin' ? ' (toutes les pages)'
                       : newRole === 'senor' ? ' (pages du Geologue + Senor, connexion unique)'
                       : ' (Home, Live, Alerts, Inbox, Dig Plan, Sync, Aide)') + '. Continuer ?',
            libelleOk: 'Changer le role' })) return;
      const sp = Splash.bloquer('Modification du compte...', nomCompte);
      await fsOp(setDoc(ref, { role: newRole }, { merge: true }));
      sp.fermer();
      Splash.succes('Role modifie', nomCompte + ' : ' + libelleRole(newRole));
      setAccountStatus('Role mis a jour : ' + newRole, 'ok');
    } else if (btn.dataset.action === 'toggle-active') {
      const willDisable = !data.desactive;
      if (willDisable && data.role === 'admin') {
        const tous = await fsOp(getDocs(query(collection(db, 'users'))));
        const autres = tous.docs.filter(d =>
          d.id !== uid && d.data().role === 'admin' && !d.data().desactive);
        if (autres.length === 0) {
          setAccountStatus('Impossible : dernier admin actif', 'err');
          return;
        }
      }
      // action sensible : confirmation avant application (popup maison)
      if (willDisable && !await Splash.confirmer(
          'Desactiver le compte de "' + nomCompte + '" ?',
          { titre: 'Desactivation de compte', danger: true,
            details: 'Cette personne perdra l\'acces a l\'application immediatement.',
            libelleOk: 'Desactiver' })) return;
      if (!willDisable && !await Splash.confirmer(
          'Reactiver le compte de "' + nomCompte + '" ?',
          { titre: 'Reactivation de compte', danger: false, libelleOk: 'Reactiver' })) return;
      const sp = Splash.bloquer(willDisable ? 'Desactivation...' : 'Reactivation...', nomCompte);
      await fsOp(setDoc(ref, { desactive: willDisable }, { merge: true }));
      sp.fermer();
      Splash.succes(willDisable ? 'Compte desactive' : 'Compte reactive', nomCompte);
      setAccountStatus(willDisable ? 'Compte desactive.' : 'Compte reactive.', 'ok');
    }
    loadAccounts();
  } catch (err) {
    Splash.erreur('Operation impossible', err.message || 'Echec');
    setAccountStatus(err.message || 'Echec', 'err');
  }
}

// params Securite (auth_mode, timeout) : persists sur le serveur app
async function loadSecurityPanel() {
  loadAccounts();
  try {
    const { params } = await API.getConfig();
    const mode = $('#cfgAuthMode');
    const timeout = $('#cfgAuthTimeout');
    if (mode && params.auth_mode) mode.value = params.auth_mode;
    if (timeout && params.auth_timeout) timeout.value = params.auth_timeout;
    if (params.auth_timeout) {
      const v = parseInt(params.auth_timeout, 10);
      if (v >= 5 && v <= 480) sessionTimeoutMin = v;
    }
  } catch (e) { /* offline : valeurs defaut */ }
}

async function saveSecurityParams() {
  if (!auth.currentUser) return;
  const timeoutVal = (($('#cfgAuthTimeout') || {}).value || '30');
  const params = {
    auth_mode: ($('#cfgAuthMode') || {}).value || 'password',
    auth_timeout: timeoutVal,
  };
  const v = parseInt(timeoutVal, 10);
  if (v >= 5 && v <= 480) sessionTimeoutMin = v;
  try { await API.saveConfig(params); } catch (e) { /* offline tolerate */ }
}

// ---------------------------------------------------------------- init ----
function initAuth() {
  const form = $('#loginForm');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      await doLogin(
        ($('#loginIdentifiant') || {}).value || '',
        ($('#loginPassword') || {}).value || ''
      );
    });
  }

  document.querySelectorAll('.btn-logout').forEach(btn => {
    btn.addEventListener('click', doLogout);
  });

  const accForm = $('#accountAddForm');
  if (accForm) accForm.addEventListener('submit', addAccount);

  const accBox = $('#accountsContainer');
  if (accBox) accBox.addEventListener('click', onAccountAction);

  ['cfgAuthMode', 'cfgAuthTimeout'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', saveSecurityParams);
  });

  loadSessionTimeout(); // valeur initiale depuis le serveur

  const settings = $('#page-settings');
  if (settings) {
    const rafraichir = () => {
      const visible = !settings.hidden && getComputedStyle(settings).display !== 'none';
      if (visible && auth.currentUser) loadSecurityPanel();
    };
    const obs = new MutationObserver(rafraichir);
    obs.observe(settings, { attributes: true, attributeFilter: ['class', 'style'] });
    // La page Settings est un fichier a part : elle est deja visible au
    // chargement, l'observeur ne se declenche donc pas tout seul.
    rafraichir();
  }

  // etat Firebase : au chargement et a chaque changement (login/logout/expiry)
  onAuthStateChanged(auth, async (user) => {
    if (user) {
      // profil d'abord (role + desactive) pour appliquer la nav des le debut
      try {
        myProfile = await upsertProfile(user);
      } catch (e) {
        console.warn('profile', e.code || e.message);
        myProfile = null;
      }
      // compte desactive : deconnexion immediate
      if (myProfile && myProfile.desactive) {
        setAccountStatus('Compte desactive', 'err');
        await signOut(auth).catch(() => {});
        return;
      }
      renderUser(myProfile && myProfile.nom
        ? { nom: myProfile.nom, email: user.email }
        : user);
      const roleCourant = normaliserRole(myProfile ? myProfile.role : '');
      // profil « Senor_Geology » : un seul poste a la fois
      if (!(await prendreSessionUnique(user, roleCourant))) return;
      const pageAutorisee = applyNavAccess(roleCourant);
      if (pageAutorisee) hideLogin();      // page interdite : renvoi sur Home, rien n'est devoile
      // page d'accueil par defaut (Preferences) — seulement si on n'est pas déjà sur une page de l'app
      try {
        var home = (typeof PREFS !== 'undefined') ? PREFS.get('homePage') : 'home';
        var link = document.querySelector('.sidebar-link[data-page="' + home + '"]');
        var dejaSurUnePage = Array.from(document.querySelectorAll('.sidebar-link')).some(function (l) {
          try { return new URL(l.getAttribute('href'), window.location.href).pathname === window.location.pathname; } catch (e) { return false; }
        });
        if (link && !dejaSurUnePage) naviguerVers(link);
      } catch (e) {}
      // veille d'inactivite : demarre immediatement avec la valeur courante
      // (la config n'est rechargee qu'au boot et a l'ouverture du panneau,
      //  pour ne pas ecraser une valeur modifiee entre-temps)
      startInactivityWatch();
      // upsert deja fait : on ne recharge que la liste
      loadAccounts();
      // signaler la session etablie (server-ui recharge ses listes protegees)
      try {
        window.dispatchEvent(new CustomEvent('app:auth-ready', { detail: { user: user, profile: myProfile } }));
      } catch (e) { /* noop */ }
    } else {
      stopInactivityWatch();
      await libererSessionUnique();
      myProfile = null;
      if (window.API && API.clearFirebaseToken) API.clearFirebaseToken();
      applyNavAccess('admin'); // ecran login : nav complete par defaut
      renderUser(null);
      resetLoginForm();
      try { window.dispatchEvent(new CustomEvent('app:auth-lost')); } catch (e) { /* noop */ }
      if (pendingLoginMsg) {
        setLoginStatus(pendingLoginMsg.text, pendingLoginMsg.cls);
        pendingLoginMsg = null;
      }
      showLogin();
    }
  });
  // masqué d'emblée : l'overlay n'apparait que si l'utilisateur n'a pas de session valide
  hideLogin();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initAuth);
} else {
  initAuth();
}

// getToken : jeton Firebase du compte connecte (en-tete Authorization des ecritures vers la passerelle)
window.OreTrackingAuth = { doLogin, doLogout, loadAccounts, loadSecurityPanel,
  getToken: async () => (auth.currentUser ? auth.currentUser.getIdToken() : null),
  role: () => normaliserRole(myProfile ? myProfile.role : '') };
