#!/usr/bin/env node
/** Profils (Admin / Senor_Geology / Géologue / Viewer) et connexion unique — js/roles.js. node scripts/test-roles.mjs */
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const R = await import(pathToFileURL(resolve(dirname(fileURLToPath(import.meta.url)), '../js/roles.js')).href);
let ok = 0, ko = 0;
const check = (l, c, d = '') => { c ? ok++ : ko++; console.log(`${c ? 'OK  ' : 'FAIL'} ${l}${d ? ' — ' + d : ''}`); };
const PAGES = ['home', 'live', 'alerts', 'inbox', 'senor', 'digplan', 'settings', 'sync', 'aide'];
const vues = (r) => PAGES.filter((p) => R.peutAcceder(r, p));

check('admin : toutes les pages', vues('admin').length === PAGES.length);
check('viewer : Home et Live seulement', JSON.stringify(vues('viewer')) === JSON.stringify(['home', 'live']), vues('viewer').join());
check('geologue : viewer + Alerts, Inbox, DigPlan, Aide (ni Senor, ni Settings, ni Sync)',
  JSON.stringify(vues('geologue')) === JSON.stringify(['home', 'live', 'alerts', 'inbox', 'digplan', 'aide']), vues('geologue').join());
check('senor : pages du geologue + Senor (ni Settings ni Sync)',
  JSON.stringify(vues('senor')) === JSON.stringify(['home', 'live', 'alerts', 'inbox', 'senor', 'digplan', 'aide']), vues('senor').join());
check('Sync : admin seulement', PAGES.filter(() => true).length && ['viewer', 'geologue', 'senor'].every((r) => !R.peutAcceder(r, 'sync')) && R.peutAcceder('admin', 'sync'));
check('« viewer » est un vrai viewer', R.normaliserRole('viewer') === 'viewer' && !R.peutAcceder('viewer', 'alerts'));
check('role vide / inconnu : viewer (le moins de droits)', R.normaliserRole('') === 'viewer' && R.normaliserRole('xyz') === 'viewer');
check('« geologue » / « Géologue » reconnus', R.normaliserRole('geologue') === 'geologue' && R.normaliserRole('Géologue') === 'geologue');
check('« Senor_Geology » reconnu', R.normaliserRole('Senor_Geology') === 'senor' && R.normaliserRole('SENOR') === 'senor');
check('rangs', R.rangRole('viewer') === 0 && R.rangRole('geologue') === 1 && R.rangRole('senor') === 2 && R.rangRole('admin') === 3);
check('atteint : niveaux', R.atteint('geologue', 'geologue') && !R.atteint('viewer', 'geologue') && R.atteint('admin', 'senor')
  && !R.atteint('geologue', 'senor') && !R.atteint('admin', 'inconnu'));
check('cycle de roles', R.roleSuivant('admin') === 'senor' && R.roleSuivant('senor') === 'geologue'
  && R.roleSuivant('geologue') === 'viewer' && R.roleSuivant('viewer') === 'admin');
check('cycle : Senor deja pris -> saute', R.roleSuivant('admin', false) === 'geologue');
const comptes = [{ id: 'a', role: 'admin' }, { id: 'b', role: 'senor' }, { id: 'c', role: 'geologue' }, { id: 'd', role: 'senor', desactive: true }];
check('senor deja pris par b', R.senorDejaPris(comptes, 'c')?.id === 'b');
check('b peut garder son role', R.senorDejaPris(comptes, 'b') === null);
check('compte desactive ignore', R.senorDejaPris([{ id: 'd', role: 'senor', desactive: true }], 'c') === null);
check('connexion unique : senor seulement', R.connexionUnique('senor') && !R.connexionUnique('admin') && !R.connexionUnique('geologue'));

// --- connexion unique : deux postes, un compte
const base = new Map();
const store = { lire: async (u) => base.get(u) ?? null, ecrire: async (u, d) => { base.set(u, d); }, supprimer: async (u) => { base.delete(u); } };
const T0 = 1_000_000;
let r = await R.ouvrirSessionUnique(store, 'u1', 'PC-A', 'PC A', T0);
check('PC A se connecte', r.ok);
r = await R.ouvrirSessionUnique(store, 'u1', 'PC-B', 'PC B', T0 + 10_000);
check('PC B refuse pendant que PC A est actif', !r.ok && r.raison === 'deja_connecte' && r.appareil === 'PC A');
r = await R.ouvrirSessionUnique(store, 'u1', 'PC-A', 'PC A', T0 + 20_000);
check('PC A recharge une page : meme session, accepte', r.ok);
r = await R.battementSessionUnique(store, 'u1', 'PC-A', 'PC A', T0 + 50_000);
check('battement de PC A', r.ok && base.get('u1').vu === T0 + 50_000);
r = await R.ouvrirSessionUnique(store, 'u1', 'PC-B', 'PC B', T0 + 50_000 + R.SESSION_TTL_MS - 1);
check('PC B toujours refuse juste avant l\'expiration', !r.ok);
r = await R.ouvrirSessionUnique(store, 'u1', 'PC-B', 'PC B', T0 + 50_000 + R.SESSION_TTL_MS + 1);
check('PC A eteint sans deconnexion : apres le delai PC B peut se connecter', r.ok && base.get('u1').sid === 'PC-B');
r = await R.battementSessionUnique(store, 'u1', 'PC-A', 'PC A', T0 + 50_000 + R.SESSION_TTL_MS + 10_000);
check('PC A revient : sa session a ete reprise -> deconnexion demandee', !r.ok && r.raison === 'reprise_ailleurs');
await R.fermerSessionUnique(store, 'u1', 'PC-A');
check('PC A ne peut pas liberer la session de PC B', base.get('u1')?.sid === 'PC-B');
await R.fermerSessionUnique(store, 'u1', 'PC-B');
check('deconnexion de PC B : compte libere', !base.has('u1'));
r = await R.ouvrirSessionUnique(store, 'u1', 'PC-A', 'PC A', T0 + 999_999);
check('puis PC A peut se reconnecter', r.ok);
console.log(`\n${ok} OK, ${ko} echec(s)`); process.exit(ko ? 1 : 0);
