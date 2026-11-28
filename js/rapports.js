/*
 * Rapports (page Alerts, 23/11) : dumping report (verdict par mission) et off-road report.
 * Mêmes filtres que la liste des alertes : engin (fltDt) et période (fltPeriode / fltDate,
 * journée d'exploitation 06 h - 06 h). Sans période : les 7 derniers jours.
 * Passerelle : GET /api/rapports/dumping | /api/rapports/offroad ?depuis=&jusqua= (ms) &dt= &format=csv
 */
(function () {
  'use strict';

  const VERDICTS = {
    correct: ['Correct', 'var(--r-green, #16a34a)'],
    mauvais_area: ['Mauvais area (confirmé)', 'var(--r-red, #dc2626)'],
    hors_zone_probable: ['Hors zone probable', 'var(--r-orange, #d97706)'],
    a_verifier: ['À vérifier', 'var(--r-orange, #d97706)'],
    en_cours: ['En cours', 'var(--r-text-3, #6b7280)'],
  };

  function passerelle() {
    if (window.OTCFG && OTCFG.passerelle) return OTCFG.passerelle();
    return (location.port === '8100' ? location.protocol + '//' + (location.hostname || 'localhost') + ':8101' : location.origin + '/passerelle');
  }
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const deux = (n) => String(n).padStart(2, '0');
  const val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };

  function debutJourMs() {
    const d = new Date();
    if (d.getHours() < 6) d.setDate(d.getDate() - 1);
    d.setHours(6, 0, 0, 0);
    return d.getTime();
  }

  /** [depuis, jusqua] en ms d'après le filtre Période (défaut : 7 derniers jours). */
  function fenetre() {
    const j = 86400000;
    const d0 = debutJourMs();
    const per = val('fltPeriode');
    if (per === 'jour') return [d0, d0 + j];
    if (per === 'hier') return [d0 - j, d0];
    if (per === 'date' && val('fltDate')) {
      const p = val('fltDate').split('-').map(Number);
      const d = new Date(p[0], p[1] - 1, p[2], 6, 0, 0, 0).getTime();
      return [d, d + j];
    }
    return [d0 - 6 * j, d0 + j];
  }

  function requete(type, csv) {
    const [a, b] = fenetre();
    const dt = val('fltDt').trim().toUpperCase();
    return passerelle() + '/api/rapports/' + type + '?depuis=' + a + '&jusqua=' + b +
      (dt ? '&dt=' + encodeURIComponent(dt) : '') + (csv ? '&format=csv' : '');
  }

  // Heures : missions en heure locale de la passerelle ; alertes en UTC (created_at SQLite).
  const heureLocale = (s) => (s ? String(s).slice(5, 16).replace('-', '/') : '');
  function heureUtc(s) {
    if (!s) return '';
    const d = new Date(String(s).replace(' ', 'T') + 'Z');
    return deux(d.getDate()) + '/' + deux(d.getMonth() + 1) + ' ' + deux(d.getHours()) + ':' + deux(d.getMinutes());
  }
  const duree = (s) => { s = Math.max(0, Math.round(Number(s) || 0)); return s >= 60 ? Math.floor(s / 60) + ' min ' + deux(s % 60) + ' s' : s + ' s'; };

  async function chargerDumping() {
    const body = document.getElementById('rapDumpBody');
    if (!body) return;
    try {
      const d = await (await fetch(requete('dumping'), { cache: 'no-store' })).json();
      const ms = d.missions || [];
      const r = d.reglages || {};
      const info = document.getElementById('rapDumpInfo');
      if (info) info.textContent = '· dumping = arrêt ≥ ' + (r.arret_s || 45) + ' s dans un rayon de ' + (r.rayon_m || 10) + ' m';
      body.innerHTML = ms.length ? ms.map((m) => {
        const v = VERDICTS[m.verdict] || [m.verdict, ''];
        const titre = (m.visites || []).map((x) => x.zone_nom + ' (' + x.role + ') : arrêt max ' + duree(x.arret_max_s)).join('\n');
        return '<tr title="' + esc(titre) + '"><td>' + esc(heureLocale(m.debut)) + '</td><td>' + esc(m.dt_code) + '</td><td>' + esc(m.matiere) +
          '</td><td>' + esc(m.zone_prevue) + '</td><td><b style="color:' + v[1] + '">' + esc(v[0]) + '</b></td><td>' + esc(m.zone_dumping) +
          '</td><td>' + esc((m.demi_tours || []).join(', ')) + '</td></tr>';
      }).join('') : '<tr><td colspan="7" class="muted">Aucune mission sur la période.</td></tr>';
    } catch (e) {
      body.innerHTML = '<tr><td colspan="7" class="muted">Passerelle injoignable.</td></tr>';
    }
  }

  async function chargerOffroad() {
    const body = document.getElementById('rapOffBody');
    if (!body) return;
    try {
      const d = await (await fetch(requete('offroad'), { cache: 'no-store' })).json();
      const l = d.offroad || [];
      const info = document.getElementById('rapOffInfo');
      if (info) info.textContent = '· ' + l.length + ' sortie(s), ' + duree(l.reduce((t, x) => t + (Number(x.retour_duree_s) || 0), 0)) + ' hors piste';
      body.innerHTML = l.length ? l.map((a) =>
        '<tr title="' + esc(a.message) + '"><td>' + esc(heureUtc(a.created_at)) + '</td><td>' + esc(a.dt_code) + '</td><td>' +
        (a.distance_m != null ? Math.round(a.distance_m) + ' m' : '') + '</td><td>' +
        (a.retour_le ? esc(heureUtc(a.retour_le)) + (a.retour_cause === 'arrivee_da' ? ' (fin de mission)' : '') : '<em>toujours hors piste</em>') +
        '</td><td>' + (a.retour_le ? duree(a.retour_duree_s) : '') + '</td></tr>').join('')
        : '<tr><td colspan="5" class="muted">Aucune sortie de route sur la période.</td></tr>';
    } catch (e) {
      body.innerHTML = '<tr><td colspan="5" class="muted">Passerelle injoignable.</td></tr>';
    }
  }

  function charger() { chargerDumping(); chargerOffroad(); }

  function init() {
    if (!document.getElementById('rapDumpBody')) return;
    ['fltDt', 'fltPeriode', 'fltDate'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener(id === 'fltDt' ? 'input' : 'change', charger);
    });
    const csvD = document.getElementById('btnRapDumpCsv');
    if (csvD) csvD.addEventListener('click', () => { location.href = requete('dumping', true); });
    const csvO = document.getElementById('btnRapOffCsv');
    if (csvO) csvO.addEventListener('click', () => { location.href = requete('offroad', true); });
    charger();
    setInterval(charger, 30000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
