/* =====================================================================
 * inbox.js — page Inbox : registre des voyages (donnees reelles
 * passerelle via missions-ui.js). Lecture seule.
 * Chaque voyage est croise avec la Dumping Instruction : la colonne
 * Conformite dit si le minerai est alle au bon endroit.
 * ===================================================================== */
(function () {
  'use strict';

  function gradeTag(teneur) {
    if (teneur >= 8) return '<span class="grade-tag high">High</span>';
    if (teneur >= 4) return '<span class="grade-tag medium">Medium</span>';
    return '<span class="grade-tag low">Low</span>';
  }

  /* Filtre Ore / Waste : selon la DESTINATION du voyage. Une Dumping Area
   * dont le nom contient waste/dump/sterile = Waste ; tout le reste
   * (ROM pads, destinations de teneur) = Ore. Valeur du filtre :
   * '' (tout), 'ore', 'waste'. */
  let filtreMateriel = '';

  function estWaste(v) {
    const dest = String(v.da_nom || '').toLowerCase();
    return /waste|dump|sterile/.test(dest);
  }

  function render(etat) {
    const tbody = document.getElementById('inboxVoyages');
    if (!tbody) return;
    let voyages = etat.voyages || [];
    if (filtreMateriel === 'ore') voyages = voyages.filter((v) => !estWaste(v));
    else if (filtreMateriel === 'waste') voyages = voyages.filter(estWaste);
    if (!voyages.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="muted">' +
        (etat.passerelleDown ? 'Passerelle injoignable' : 'Aucun voyage enregistre.') +
        '</td></tr>';
      const recap = document.getElementById('inboxRecap');
      if (recap) recap.innerHTML = '';
      return;
    }
    tbody.innerHTML = voyages.map((v) => {
      const conf = v.conformite || '';
      const c = (window.MSUI && MSUI.CONFORMITE[conf]) || null;
      const confHtml = c
        ? '<span class="' + c.cls + '">' + c.lbl + '</span>'
        : '<span class="status-pending">—</span>';
      return '<tr>' +
        '<td>#' + v.id + '</td>' +
        '<td>' + MSUI.esc(v.dt_code) + '</td>' +
        '<td>' + MSUI.heure(v.created_at) + '</td>' +
        '<td>' + MSUI.esc(v.orebloc_nom || ('#' + v.orebloc_id)) + '</td>' +
        '<td>' + MSUI.esc(v.da_nom || ('#' + v.dumping_area_id)) + '</td>' +
        '<td>' + (Math.round(v.volume_t * 10) / 10) + ' t</td>' +
        '<td>' + gradeTag(v.teneur) + '</td>' +
        '<td>' + confHtml + '</td>' +
        '</tr>';
    }).join('');

    // pied de tableau : recap du registre affiche
    const tonnes = voyages.reduce((s, v) => s + (v.volume_t || 0), 0);
    const nonConf = voyages.filter((v) => v.conformite === 'non_conforme').length;
    const conforms = voyages.filter((v) => v.conformite === 'conforme').length;
    const recap = document.getElementById('inboxRecap');
    if (recap) {
      recap.innerHTML =
        '<span><b>' + voyages.length + '</b> voyages</span>' +
        '<span><b>' + Math.round(tonnes) + ' t</b> deplacees</span>' +
        '<span class="ok"><b>' + conforms + '</b> conformes</span>' +
        '<span class="' + (nonConf ? 'ko' : '') + '"><b>' + nonConf + '</b> non conformes</span>';
    }
    const compteur = document.getElementById('inboxCompteur');
    if (compteur) {
      compteur.textContent = filtreMateriel
        ? voyages.length + ' voyage(s) (' + (filtreMateriel === 'ore' ? 'ore' : 'waste') + ')'
        : '';
    }
  }

  function init() {
    if (!document.getElementById('inboxVoyages')) return;
    if (!window.MSUI) return;

    // filtre Ore / Waste
    const sel = document.getElementById('inboxFiltreMateriel');
    if (sel) sel.addEventListener('change', () => {
      filtreMateriel = sel.value;
      // re-rendu immediat avec les dernieres donnees connues
      render(MSUI.etat);
    });

    MSUI.onTick(render);
    MSUI.demarrer(5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
