/* =====================================================================
 * ot-settings.js - panneau "Serveur_OT" de la page Settings.
 * Pilote OTCFG : saisie des endpoints, test de connexion, creation de
 * l'arborescence Drive et envoi du GeoJSON des pistes.
 * ===================================================================== */
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  if (!$('#btnOtSave')) return; // panneau absent de cette page

  const CLE_RACINE = 'otRacineId';

  function statut(msg, ok) {
    const el = $('#otStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' ok' : ' err') : '');
  }

  function diag(objet) {
    const el = $('#otDiag');
    if (!el) return;
    if (objet === null) { el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.textContent = JSON.stringify(objet, null, 2);
  }

  /** Enveloppe commune : bouton desactive pendant l'appel, erreurs affichees. */
  async function action(bouton, libelle, fn) {
    const el = $(bouton);
    const avant = el.textContent;
    el.disabled = true;
    el.textContent = libelle;
    statut('', true);
    try {
      await fn();
    } catch (e) {
      statut(e.message, false);
      diag(null);
    } finally {
      el.disabled = false;
      el.textContent = avant;
    }
  }

  function charger() {
    try {
      $('#otUrl').value = localStorage.getItem('dpOtUrl') || '';
      $('#otToken').value = localStorage.getItem('dpOtToken') || '';
      $('#otRacineId').value = localStorage.getItem(CLE_RACINE) || '';
      $('#otBackendPage').value = (window.OTCFG && OTCFG.backend()) || 'sg';
    } catch (e) { /* storage indisponible */ }
  }

  function enregistrer() {
    OTCFG.definir({
      otUrl: $('#otUrl').value.trim(),
      otToken: $('#otToken').value.trim(),
      backend: $('#otBackendPage').value,
    });
    try { localStorage.setItem(CLE_RACINE, $('#otRacineId').value.trim()); } catch (e) {}
    const surOt = $('#otBackendPage').value === 'ot';
    statut('Configuration enregistree. Les pages Dig Plan visent ' +
           (surOt ? 'le Serveur_OT.' : 'la passerelle locale.'), true);
  }

  async function tester() {
    const d = await OTCFG.appelOt('GET', 'sante');
    const manque = d.proprietes_manquantes || {};
    const total = (manque.manuelles || []).length + (manque.automatiques || []).length;
    statut(total
      ? 'Connexion OK, mais configuration incomplete : ' + (d.prochaine_etape || '')
      : 'Connexion OK, configuration complete.', total === 0);
    diag(d);
  }

  async function initDrive() {
    const racine = $('#otRacineId').value.trim();
    const d = await OTCFG.appelOt('GET', 'init_drive', racine ? { racine_id: racine } : null);
    statut('Arborescence prete sous "' + d.racine.nom +
           '". Les identifiants de dossier sont enregistres cote serveur.', true);
    diag(d);
  }

  async function pousserReferentiel() {
    const d = await OTCFG.pousserReferentiel();
    // d.dumping_areas vaut null si une passerelle ancienne n'a rien envoye :
    // on le dit explicitement plutot que d'afficher "0 zone" a tort.
    const zones = d.dumping_areas === null || d.dumping_areas === undefined
      ? 'zones non transmises' : d.dumping_areas + ' zone(s)';
    const repere = d.geo ? ', repere pose' : '';
    const chk = d.checkpoints === null || d.checkpoints === undefined
      ? '' : ', ' + d.checkpoints + ' checkpoint(s)';
    const envoye = 'Referentiel envoye a Serveur_OT : ' + d.sources + ' source(s), ' + d.teneurs +
           ' teneur(s), ' + zones + chk + repere + '.';
    diag(d);
    // Puis Serveur_SG recopie ce referentiel (sync_ref, lance par la
    // passerelle qui garde le jeton SG). Sans cette etape, les missions
    // restaient calculees sur l'ancien referentiel.
    statut(envoye + ' Mise a jour de Serveur_SG en cours (jusqu\'a 2 min)...', true);
    let s = {};
    try {
      const r = await fetch(OTCFG.passerelle() + '/api/sg/sync_ref', { method: 'POST' });
      s = await r.json();
      if (!r.ok || s.ok === false) throw new Error(s.error || ('HTTP ' + r.status));
    } catch (e) {
      statut(envoye + ' Serveur_SG NON mis a jour : ' + e.message +
             '. Relancer « Pousser le referentiel » ou sync_ref a la main.', false);
      return;
    }
    const sansConsigne = (s.teneurs_sans_consigne || []).length;
    statut(envoye + ' Serveur_SG a jour : ' + s.dumping_areas + ' zone(s), ' + s.teneurs +
           ' teneur(s), ' + s.oreblocs + ' orebloc(s), ' + s.pistes + ' piste(s)' +
           (s.checkpoints === null || s.checkpoints === undefined ? '' : ', ' + s.checkpoints + ' checkpoint(s)') +
           (sansConsigne ? ' ; ' + sansConsigne + ' teneur(s) sans zone de deversement.' : '.'),
           !sansConsigne);
  }

  async function pousserPistes() {
    const d = await OTCFG.pousserPistes();
    statut('Pistes envoyees : ' + d.lignes + ' trace(s). FICHIER_PISTES_ID enregistre.', true);
    diag(d);
  }

  $('#btnOtSave').addEventListener('click', () => action('#btnOtSave', 'Enregistrement...', async () => enregistrer()));
  $('#btnOtTest').addEventListener('click', () => action('#btnOtTest', 'Test...', tester));
  $('#btnOtInitDrive').addEventListener('click', () => action('#btnOtInitDrive', 'Creation...', initDrive));
  $('#btnOtPushRef').addEventListener('click', () => action('#btnOtPushRef', 'Envoi...', pousserReferentiel));
  $('#btnOtPushPistes').addEventListener('click', () => action('#btnOtPushPistes', 'Envoi...', pousserPistes));

  charger();
})();
