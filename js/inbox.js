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

  let filtreMateriel = '';
  let filtrePeriode = '';
  let filtreDate = '';

  // Matiere et grade : calcules par la passerelle (/api/voyages : champs `sterile` et `grade`, meme regle
  // que les KPI du Dashboard). La page ne devine rien : ni seuil, ni nom de dumping area.
  const estWaste = (v) => v.sterile === true;
  const LIBELLE_GRADE = { high: 'High Grade', medium: 'Medium Grade', low: 'Low Grade', marginal: 'Marginal Ore' };
  function grade(v) {
    if (estWaste(v)) return { cle: 'waste', lbl: 'Waste' };
    return LIBELLE_GRADE[v.grade] ? { cle: v.grade, lbl: LIBELLE_GRADE[v.grade] } : { cle: 'inconnu', lbl: 'Non classé' };
  }

  // Conformite : conforme / a controler (compromis) / non conforme
  const CONF = {
    conforme: { lbl: 'Conforme', cls: '' },
    compromis: { lbl: 'À contrôler', cls: 'a-controler' },
    non_conforme: { lbl: 'Non conforme', cls: 'non-conforme' },
  };

  // Explication lisible du verdict, composee a partir du detail enregistre avec le voyage
  // (conformite_detail, JSON ecrit par la passerelle). Ouverte par un clic sur la conformite.
  const ouverts = new Set();
  function detailVoyage(v) {
    try { return v.conformite_detail ? JSON.parse(v.conformite_detail) : null; } catch (e) { return null; }
  }
  function explication(v) {
    const d = detailVoyage(v);
    const q = (t) => '«\u00a0' + MSUI.esc(t) + '\u00a0»';
    if (!d) {
      return ['Aucun détail enregistré pour ce voyage.',
        v.conformite ? 'Le verdict a été posé avant l\u2019enregistrement des explications.'
          : 'Il a été enregistré sans vérification de la dumping area (voyage ancien ou bloc inconnu).'];
    }
    const lignes = [];
    if (d.attendue) {
      lignes.push(d.regle === 'teneur'
        ? 'Zone attendue : ' + q(d.attendue) + ' \u2014 la teneur du bloc' + (d.teneur != null ? ' (' + d.teneur + ' g/t)' : '') + ' impose cette dumping area.'
        : 'Zone attendue : ' + q(d.attendue) + ' \u2014 aucune règle de teneur ne couvre ce bloc, c\u2019est la zone affectée à la mission.');
    } else {
      lignes.push('Aucune zone attendue : ni règle de teneur, ni dumping area affectée à la mission.');
    }
    if (d.position === 'dans_da') lignes.push('Fin de mission : le camion était dans ' + q(d.reelle) + '.');
    else if (d.position === 'hors_da') lignes.push('Fin de mission : le camion n\u2019était dans aucune dumping area connue.');
    else if (d.position === 'absente') lignes.push('Fin de mission : position du camion indisponible ou trop ancienne (plus de 2 minutes).');
    const verdict = {
      zone_attendue: 'Conforme : le camion a déchargé dans la zone attendue.',
      autre_zone: 'Non conforme : le camion devait décharger dans ' + q(d.attendue) + ' mais a déchargé dans ' + q(d.reelle) + '. Une alerte a été créée.',
      hors_da: 'À contrôler : on ne peut pas confirmer que le déchargement a eu lieu dans la bonne zone. À vérifier avec le chauffeur ou la trace GPS.',
      position_absente: 'À contrôler : sans position récente du camion, le lieu du déchargement n\u2019est pas confirmé. À vérifier avec le chauffeur ou la trace GPS.',
      aucune_zone_attendue: 'Pas de verdict : il n\u2019y avait aucune zone à comparer.',
    }[d.raison];
    if (verdict) lignes.push(verdict);
    return lignes;
  }

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
      const ouvert = ouverts.has(v.id);
      const libConf = c ? c.lbl : 'Non évaluée';
      const ligne = '<tr class="' + (c ? c.cls : '') + (ouvert ? ' ouvert' : '') + '">' +
        '<td class="r-id">V-' + String(v.id).padStart(4, '0') + '</td>' +
        '<td class="r-engin">' + MSUI.esc(v.dt_code) + '</td>' +
        '<td class="r-heure">' + MSUI.heure(v.created_at) + '</td>' +
        '<td class="r-orebloc">' + MSUI.esc(v.orebloc_nom || ('#' + v.orebloc_id)) + '</td>' +
        '<td>' + MSUI.esc(v.da_nom || ('#' + v.dumping_area_id)) + '</td>' +
        '<td>' + (Math.round((Number(v.volume_t) || 0) * 10) / 10).toLocaleString('fr-FR') + ' t</td>' +
        '<td><span class="r-grade"' + (g.cle === 'inconnu' ? ' title="Grade inconnu : bloc ' + MSUI.esc(v.orebloc_nom ? 'sans code de teneur reconnu' : 'introuvable') + '"' : '') + '><i class="g-' + g.cle + '"></i>' + g.lbl + '</span></td>' +
        '<td class="r-conf"><button type="button" class="r-conf-btn" data-voyage="' + v.id + '" aria-expanded="' + ouvert +
          '" title="Cliquer pour comprendre ce verdict">' + libConf + ' <i class="r-conf-i">' + (ouvert ? '\u25B4' : '\u24D8') + '</i></button></td>' +
        '</tr>';
      if (!ouvert) return ligne;
      return ligne + '<tr class="r-detail"><td colspan="8"><div class="r-detail-box"><b>Pourquoi «\u00a0' + libConf + '\u00a0» ?</b>' +
        explication(v).map((t) => '<p>' + t + '</p>').join('') + '</div></td></tr>';
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

    // clic sur une conformite : ouvre / ferme l'explication
    const corps = document.getElementById('inboxVoyages');
    if (corps) corps.addEventListener('click', (e) => {
      const b = e.target.closest('.r-conf-btn');
      if (!b) return;
      const id = Number(b.dataset.voyage);
      if (ouverts.has(id)) ouverts.delete(id); else ouverts.add(id);
      render(MSUI.etat);
    });

    // periode
    const per = document.getElementById('inboxPeriode');
    const dat = document.getElementById('inboxDateSel');
    if (per) per.addEventListener('change', () => {
      filtrePeriode = per.value;
      if (dat) dat.hidden = per.value !== 'date';
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
