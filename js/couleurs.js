/* =====================================================================
 * couleurs.js — couleurs des teneurs, UNE SEULE source pour toutes les pages.
 * Lues sur la passerelle (/api/couleurs) :
 *   - une couleur par GRADE (high / medium / low / marginal / waste), reglable dans
 *     Settings > Terrain > Codes de teneur ;
 *   - chaque famille de teneur (F4LP, F7LP...) herite de la couleur du grade de son
 *     premier code de bloc, sauf couleur propre (Senor > Sources & teneurs).
 * Expose OTCouleurs.{grade, famille, code, pret} et pose les variables CSS
 * --c-high, --c-medium, --c-low, --c-marginal, --c-waste sur :root.
 * Un evenement « couleurs:pret » est emis des qu'elles sont connues (les pages
 * qui dessinent avant cela se redessinent). Sans passerelle : valeurs de repli.
 * ===================================================================== */
(function () {
  'use strict';

  const REPLI = { high: '#e8641c', medium: '#1d6fd1', low: '#e0b413', marginal: '#8a5cc2', waste: '#8d887a' };
  const INCONNUE = '#b8b3a2';
  const CLE = 'ore-couleurs-teneur';
  const VALIDITE_MS = 5 * 60 * 1000;

  let etat = { grades: REPLI, familles: {}, codes: {}, inconnue: INCONNUE };

  function base() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    let b = '';
    try { b = localStorage.getItem('sgApiBase') || ''; } catch (e) { /* stockage indisponible */ }
    return (b || (location.protocol + '//' + (location.hostname || 'localhost') + ':8101')).replace(/\/+$/, '');
  }

  function poserVariables() {
    const racine = document.documentElement;
    Object.keys(REPLI).forEach((g) => racine.style.setProperty('--c-' + g, (etat.grades || REPLI)[g] || REPLI[g]));
  }

  function appliquer(d) {
    etat = {
      grades: Object.assign({}, REPLI, d.grades || {}),
      familles: d.familles || {},
      codes: d.codes || {},
      inconnue: d.inconnue || INCONNUE,
    };
    poserVariables();
    window.dispatchEvent(new CustomEvent('couleurs:pret', { detail: etat }));
  }

  // Cache de session : evite un appel a chaque changement de page ; relu en arriere-plan.
  try {
    const c = JSON.parse(sessionStorage.getItem(CLE) || 'null');
    if (c && c.donnees) { etat = Object.assign({}, etat, c.donnees, { grades: Object.assign({}, REPLI, c.donnees.grades || {}) }); poserVariables(); }
  } catch (e) { /* cache illisible : ignore */ }

  function charger() {
    return fetch(base() + '/api/couleurs', { cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return etat;
        try { sessionStorage.setItem(CLE, JSON.stringify({ t: Date.now(), donnees: d })); } catch (e) { /* ignore */ }
        appliquer(d);
        return etat;
      })
      .catch(() => etat);
  }

  const cherche = (table, cle) => {
    const k = String(cle || '').trim();
    if (!k) return null;
    if (table[k]) return table[k];
    const u = k.toUpperCase();
    const trouve = Object.keys(table).find((x) => x.toUpperCase() === u);
    return trouve ? table[trouve] : null;
  };

  window.OTCouleurs = {
    pret: charger(),
    recharger: charger,
    grade: (g) => (etat.grades && etat.grades[g]) || REPLI[g] || etat.inconnue,
    famille: (nom) => cherche(etat.familles, nom) || etat.inconnue,
    code: (c) => cherche(etat.codes, c) || etat.inconnue,
    estConnue: (nom) => !!cherche(etat.familles, nom),
  };
  void VALIDITE_MS;
})();
