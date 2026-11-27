/* =====================================================================
 * roles.js — profils de l'application (20/11).
 *
 *   admin     : toutes les pages (Settings compris).
 *   senor     : « Senor_Geology » = Géologue + la page Senor.
 *               CONNEXION UNIQUE : un seul poste à la fois sur ce compte.
 *   geologue  : Home, Live, Alerts, Inbox, Dig Plan, Sync, Aide & raccourcis.
 *
 * L'ancien profil « viewer » est lu comme « geologue » (compatibilité).
 * Module PUR (aucune dépendance Firebase / DOM) : testé en Node
 * (scripts/test-roles.mjs), puis branché dans auth.js.
 * ===================================================================== */

export const ROLES = {
  admin:    { libelle: 'Admin',          pages: '*' },
  senor:    { libelle: 'Senor_Geology',  pages: ['home', 'live', 'alerts', 'inbox', 'digplan', 'sync', 'aide', 'senor'] },
  geologue: { libelle: 'Géologue',       pages: ['home', 'live', 'alerts', 'inbox', 'digplan', 'sync', 'aide'] },
};

/** Rôles proposés à la création / au changement de rôle, dans l'ordre du cycle. */
export const ORDRE_ROLES = ['admin', 'senor', 'geologue'];

/** Profil Firestore → rôle connu (viewer / inconnu / vide → geologue : le plus restreint des profils d'usage). */
export function normaliserRole(role) {
  const r = String(role || '').trim().toLowerCase();
  if (r === 'admin') return 'admin';
  if (r === 'senor' || r === 'senor_geology' || r === 'senorgeology') return 'senor';
  return 'geologue';
}

export const libelleRole = (role) => ROLES[normaliserRole(role)].libelle;

/** La page (data-page de la sidebar : home, live, alerts, …) est-elle permise à ce rôle ? */
export function peutAcceder(role, page) {
  const p = ROLES[normaliserRole(role)].pages;
  return p === '*' || p.includes(String(page || '').toLowerCase());
}

/** Page vers laquelle renvoyer un rôle qui a ouvert une page interdite. */
export const PAGE_REPLI = 'home';

/** Rôle suivant dans le cycle admin → senor → geologue → admin (bouton « changer le rôle »). */
export function roleSuivant(role) {
  const i = ORDRE_ROLES.indexOf(normaliserRole(role));
  return ORDRE_ROLES[(i + 1) % ORDRE_ROLES.length];
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
