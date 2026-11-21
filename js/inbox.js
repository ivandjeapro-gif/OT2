/* =====================================================================
 * inbox.js — page Inbox (« Journal des voyages ») : registre des voyages
 * reels (passerelle via missions-ui.js). Lecture seule.
 * Chaque voyage est croise avec la Dumping Instruction : la colonne
 * Conformite dit si le minerai est alle au bon endroit.
 * Tout est calcule sur les voyages recus : aucun chiffre en dur.
 * ===================================================================== */
(function () {
  'use strict';

  const LIMITE_LIGNES = 200;
  const deux = (n) => String(n).padStart(2, '0');
  const MOIS = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin', 'juillet', 'août',
    'septembre', 'octobre', 'novembre', 'décembre'];
  const ms = (v) => new Date(String(v.created_at).replace(' ', 'T') + 'Z').getTime();

  /* Filtre Ore / Waste : selon la DESTINATION du voyage. Une Dumping Area
   * dont le nom contient waste/dump/sterile = Waste ; tout le reste
   * (ROM pads, destinations de teneur) = Ore. */
  let filtreMateriel = '';
  let filtrePeriode = '';
  let filtreDate = '';

  function estWaste(v) {
    return /waste|dump|sterile/.test(String(v.da_nom || '').toLowerCase());
  }

  // Grade : un voyage Waste n'a pas de grade ; sinon seuils de teneur (g/t)
  function grade(v) {
    if (estWaste(v)) return { cle: 'waste', lbl: 'Waste' };
    const t = Number(v.teneur) || 0;
    if (t >= 8) return { cle: 'high', lbl: 'High Grade' };
    if (t >= 4) return { cle: 'medium', lbl: 'Medium Grade' };
    return { cle: 'low', lbl: 'Low Grade' };
  }

  // Conformite : conforme / a controler (compromis) / non conforme
  const CONF = {
    conforme: { lbl: 'Conforme', cls: '' },
    compromis: { lbl: 'À contrôler', cls: 'a-controler' },
    non_conforme: { lbl: 'Non conforme', cls: 'non-conforme' },
  };

  // Journee d'exploitation 06 h - 06 h (comme Home, Alerts et le tonnage)
  function debutJourMs() {
    const d = new Date();
    if (d.getHours() < 6) d.setDate(d.getDate() - 1);
    d.setHours(6, 0, 0, 0);
    return d.getTime();
  }

  function fenetre() {
    const j = 86400000;
    const d0 = debutJourMs();
    if (filtrePeriode === 'jour') return [d0, d0 + j];
    if (filtrePeriode === 'hier') return [d0 - j, d0];
    if (filtrePeriode === '7j') return [d0 - 6 * j, d0 + j];
    if (filtrePeriode === 'date' && filtreDate) {
      const p = filtreDate.split('-').map(Number);
      const d = new Date(p[0], p[1] - 1, p[2], 6, 0, 0, 0).getTime();
      return [d, d + j];
    }
    return null;
  }

  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const html = (id, v) => { const el = document.getElementById(id); if (el) el.innerHTML = v; };
  const tonnes = (n) => Math.round(n).toLocaleString('fr-FR') + ' t';

  function entete() {
    const n = new Date();
    set('inboxDate', n.getDate() + ' ' + MOIS[n.getMonth()] + ' ' + n.getFullYear());
    const h = n.getHours();
    set('inboxShift', (h >= 6 && h < 18 ? 'Shift de jour' : 'Shift de nuit') + ' · Tous les engins');
  }

  function render(etat) {
    const tbody = document.getElementById('inboxVoyages');
    if (!tbody) return;
    entete();

    // 1) periode, 2) matiere (les puces comptent sur la periode, pas sur la matiere)
    let periode = etat.voyages || [];
    const fen = fenetre();
    if (fen) periode = periode.filter((v) => ms(v) >= fen[0] && ms(v) < fen[1]);
    const nbWaste = periode.filter(estWaste).length;
    set('chipTous', String(periode.length));
    set('chipOre', String(periode.length - nbWaste));
    set('chipWaste', String(nbWaste));

    let voyages = periode;
    if (filtreMateriel === 'ore') voyages = periode.filter((v) => !estWaste(v));
    else if (filtreMateriel === 'waste') voyages = periode.filter(estWaste);

    set('inboxCompteur', deux(voyages.length));
    set('inboxVoyagesMot', voyages.length > 1 ? 'voyages' : 'voyage');

    if (!voyages.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="muted">' +
        (etat.passerelleDown ? 'Passerelle injoignable' : 'Aucun voyage enregistré.') + '</td></tr>';
      set('inboxPied', '0 voyage');
      html('inboxRecap', '');
      return;
    }

    const affiches = voyages.slice(0, LIMITE_LIGNES);
    tbody.innerHTML = affiches.map((v) => {
      const c = CONF[v.conformite] || null;
      const g = grade(v);
      return '<tr class="' + (c ? c.cls : '') + '">' +
        '<td class="r-id">V-' + String(v.id).padStart(4, '0') + '</td>' +
        '<td class="r-engin">' + MSUI.esc(v.dt_code) + '</td>' +
        '<td class="r-heure">' + MSUI.heure(v.created_at) + '</td>' +
        '<td class="r-orebloc">' + MSUI.esc(v.orebloc_nom || ('#' + v.orebloc_id)) + '</td>' +
        '<td>' + MSUI.esc(v.da_nom || ('#' + v.dumping_area_id)) + '</td>' +
        '<td>' + (Math.round((Number(v.volume_t) || 0) * 10) / 10).toLocaleString('fr-FR') + ' t</td>' +
        '<td><span class="r-grade"><i class="g-' + g.cle + '"></i>' + g.lbl + '</span></td>' +
        '<td class="r-conf">' + (c ? c.lbl : '—') + '</td>' +
        '</tr>';
    }).join('');
    set('inboxPied', '1–' + affiches.length + ' sur ' + voyages.length + (voyages.length > 1 ? ' voyages' : ' voyage'));

    // bandeau de recap : sur la selection affichee
    const vOre = voyages.filter((v) => !estWaste(v));
    const vWaste = voyages.filter(estWaste);
    const somme = (l) => l.reduce((s, v) => s + (Number(v.volume_t) || 0), 0);
    const aControler = voyages.filter((v) => v.conformite === 'compromis' || v.conformite === 'non_conforme').length;
    const bloc = (lbl, val) => '<div><span class="r-recap-lbl">' + lbl + '</span><b class="r-recap-val">' + val + '</b></div>';
    html('inboxRecap',
      bloc('Tonnage affiché', tonnes(somme(voyages))) +
      bloc('Ore · ' + vOre.length + (vOre.length > 1 ? ' voyages' : ' voyage'), tonnes(somme(vOre))) +
      bloc('Waste · ' + vWaste.length + (vWaste.length > 1 ? ' voyages' : ' voyage'), tonnes(somme(vWaste))) +
      bloc('Conformité à contrôler', deux(aControler)));
  }

  function init() {
    if (!document.getElementById('inboxVoyages')) return;
    if (!window.MSUI) return;

    // puces Materiau
    document.querySelectorAll('#page-inbox .r-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        filtreMateriel = chip.getAttribute('data-chip') || '';
        document.querySelectorAll('#page-inbox .r-chip').forEach((c) =>
          c.classList.toggle('is-active', c === chip));
        render(MSUI.etat);
      });
    });

    // periode
    const per = document.getElementById('inboxPeriode');
    const dateBox = document.getElementById('inboxDateBox');
    const dat = document.getElementById('inboxDateSel');
    if (per) per.addEventListener('change', () => {
      filtrePeriode = per.value;
      if (dateBox) dateBox.hidden = per.value !== 'date';
      render(MSUI.etat);
    });
    if (dat) dat.addEventListener('change', () => { filtreDate = dat.value; render(MSUI.etat); });

    MSUI.onTick(render);
    MSUI.demarrer(5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
