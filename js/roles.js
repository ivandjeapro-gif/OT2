/* =====================================================================
 * roles.js — profils de l'application (20/11, refonte 28/11).
 *
 *   admin     : toutes les pages (Settings, Sync compris).
 *   senor     : « Senor_Geology » = Géologue + la page Senor (consignes délicates).
 *               UN SEUL compte à la fois (decision 28/11 : jamais plusieurs géologues capables de modifier
 *               les consignes) ; CONNEXION UNIQUE : un seul poste à la fois sur ce compte.
 *   geologue  : Viewer + Alerts, Inbox, Dig Plan, Aide & raccourcis.
 *   viewer    : Home (lecture) et Live (Périmètre de suivi, Salle de contrôle, PRESENTER) — rien à modifier.
 *
 * Ce qui n'est pas accessible est CACHÉ (pages du menu et éléments marqués data-acces="geologue|senor|admin").
 * Profil vide ou inconnu : viewer (le moins de droits).
 * Module PUR (aucune dépendance Firebase / DOM) : testé en Node (scripts/test-roles.mjs), branché dans auth.js.
 * ===================================================================== */

const PAGES_VIEWER = ['home', 'live'];
const PAGES_GEOLOGUE = PAGES_VIEWER.concat(['alerts', 'inbox', 'digplan', 'aide']);

export const ROLES = {
  admin:    { libelle: 'Admin',          rang: 3, pages: '*' },
  senor:    { libelle: 'Senor_Geology',  rang: 2, pages: PAGES_GEOLOGUE.concat(['senor']) },
  geologue: { libelle: 'Géologue',       rang: 1, pages: PAGES_GEOLOGUE },
  viewer:   { libelle: 'Viewer',         rang: 0, pages: PAGES_VIEWER },
};

/** Rôles proposés à la création / au changement de rôle, dans l'ordre du cycle. */
export const ORDRE_ROLES = ['admin', 'senor', 'geologue', 'viewer'];

/** Profil Firestore → rôle connu (vide / inconnu → viewer : le moins de droits). */
export function normaliserRole(role) {
  const r = String(role || '').trim().toLowerCase();
  if (r === 'admin') return 'admin';
  if (r === 'senor' || r === 'senor_geology' || r === 'senorgeology') return 'senor';
  if (r === 'geologue' || r === 'géologue' || r === 'geologist') return 'geologue';
  return 'viewer';
}

export const libelleRole = (role) => ROLES[normaliserRole(role)].libelle;

/** Rang du rôle (viewer 0 < geologue 1 < senor 2 < admin 3). */
export const rangRole = (role) => ROLES[normaliserRole(role)].rang;

/** Le rôle atteint-il le niveau demandé (« geologue », « senor », « admin ») ? Niveau inconnu : non. */
export function atteint(role, niveau) {
  const n = ROLES[String(niveau || '').trim().toLowerCase()];
  return !!n && rangRole(role) >= n.rang;
}

/** La page (data-page de la sidebar : home, live, alerts, …) est-elle permise à ce rôle ? */
export function peutAcceder(role, page) {
  const p = ROLES[normaliserRole(role)].pages;
  return p === '*' || p.includes(String(page || '').toLowerCase());
}

/**
 * 28/11 : UN SEUL compte Senor (hors administrateurs). `comptes` = [{ id, role, desactive }].
 * Rend le compte qui détient déjà le rôle (actif, autre que `id`), ou null si le rôle est libre.
 */
export function senorDejaPris(comptes, id) {
  return (comptes || []).find((c) => c && c.id !== id && !c.desactive && normaliserRole(c.role) === 'senor') || null;
}

/** Page vers laquelle renvoyer un rôle qui a ouvert une page interdite. */
export const PAGE_REPLI = 'home';

/**
 * Rôle suivant dans le cycle admin → senor → geologue → viewer → admin (bouton « changer le rôle »).
 * `senorLibre` = false : le rôle Senor est déjà tenu par un autre compte, le cycle le saute.
 */
export function roleSuivant(role, senorLibre = true) {
  let i = ORDRE_ROLES.indexOf(normaliserRole(role));
  do { i = (i + 1) % ORDRE_ROLES.length; } while (ORDRE_ROLES[i] === 'senor' && !senorLibre);
  return ORDRE_ROLES[i];
}

/** Rôle à connexion unique ? */
export const connexionUnique = (role) => normaliserRole(role) === 'senor';

/* ------------------------------------------------------------------
 * Connexion unique (profil « senor »).
 *
 * Un document par compte (Firestore `sessions_unique/{uid}`) : { sid, vu, appareil }.
 *   - ouvrir : refuse si un AUTRE poste (sid différent) a émis un battement
 *     il y a moins de TTL ; sinon prend la main.
 *   - battement : toutes les 30 s ; false = un autre poste a pris la main
 *     (il faut alors se déconnecter).
 *   - fermer : libère à la déconnexion. Poste éteint sans déconnexion :
 *     le compte se libère tout seul après TTL.
 * `store` = { lire(uid) → doc|null, ecrire(uid, doc), supprimer(uid) } ;
 * en production la lecture + écriture passent par une transaction Firestore.
 * ------------------------------------------------------------------ */
export const SESSION_BATTEMENT_MS = 30_000;
export const SESSION_TTL_MS = 90_000;

export async function ouvrirSessionUnique(store, uid, sid, appareil, now = Date.now()) {
  const cur = await store.lire(uid);
  if (cur && cur.sid && cur.sid !== sid && now - Number(cur.vu || 0) < SESSION_TTL_MS) {
    return { ok: false, raison: 'deja_connecte', depuis: Number(cur.vu || 0), appareil: cur.appareil || '' };
  }
  await store.ecrire(uid, { sid, vu: now, appareil: appareil || '' });
  return { ok: true };
}

export async function battementSessionUnique(store, uid, sid, appareil, now = Date.now()) {
  const cur = await store.lire(uid);
  if (cur && cur.sid && cur.sid !== sid && now - Number(cur.vu || 0) < SESSION_TTL_MS) {
    return { ok: false, raison: 'reprise_ailleurs', appareil: cur.appareil || '' };
  }
  await store.ecrire(uid, { sid, vu: now, appareil: appareil || '' });
  return { ok: true };
}

export async function fermerSessionUnique(store, uid, sid) {
  const cur = await store.lire(uid);
  if (cur && cur.sid === sid) await store.supprimer(uid);   // jamais la session d'un autre poste
}
