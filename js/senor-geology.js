// Server_Senor_Geology - client cote page dediee (port 8101)
const SENOR_API = 'http://localhost:8101/api';

function setStatus(id, msg, ok) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.className = 'status-msg ' + (ok ? 'ok' : 'err');
}

async function apiFetch(path, opts = {}) {
  const r = await fetch(SENOR_API + path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    body: opts.body ? JSON.stringify(opts.body) : undefined
  });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// ============================================================
// OREBLOCS (CSV / DXF)
// ============================================================
function parseOreblocCSV(text) {
  const lignes = text.split(/\r?\n/).filter(l => l.trim());
  if (lignes.length === 0) return [];
  // Detecte si première ligne est un entete
  const first = lignes[0].split(/[,;]/).map(c => c.trim());
  let start = 0;
  if (first.some(c => /nom|name|id/i.test(c))) start = 1;

  const blocs = {};
  for (let i = start; i < lignes.length; i++) {
    const cols = lignes[i].split(/[,;]/).map(c => c.trim());
    if (cols.length < 3) continue;
    const nom = cols[0] || 'Orebloc';
    const x = parseFloat(cols[1]);
    const y = parseFloat(cols[2]);
    if (isNaN(x) || isNaN(y)) continue;
    if (!blocs[nom]) blocs[nom] = [];
    blocs[nom].push([x, y]);
  }
  return Object.entries(blocs).map(([nom, sommets]) => ({ nom, sommets }));
}

function parseOreblocDXF(text) {
  // Parser DXF basique : extrait les entites POLYLINE/VERTEX (code groupe 10,20)
  const lignes = text.split(/\r?\n/);
  const blocs = {};
  let courant = 'DXF_Import';
  let pts = [];
  let inVertex = false;
  let vx, vy;

  for (let i = 0; i < lignes.length; i++) {
    const code = lignes[i].trim();
    const val = lignes[i + 1] ? lignes[i + 1].trim() : '';
    if (code === '0') {
      // Fin d'une entite : flush si on etait dans un vertex
      if (inVertex && vx !== undefined && vy !== undefined) {
        pts.push([vx, vy]);
        vx = undefined; vy = undefined; inVertex = false;
      }
      if (val === 'POLYLINE' || val === 'LWPOLYLINE' || val === 'INSERT') {
        if (pts.length > 0) {
          if (!blocs[courant]) blocs[courant] = [];
          blocs[courant].push(...pts);
          pts = [];
        }
        courant = val + '_' + Date.now();
      }
    }
    if (code === '10') vx = parseFloat(val);
    if (code === '20') {
      vy = parseFloat(val);
      inVertex = true;
    }
  }
  if (inVertex && vx !== undefined && vy !== undefined) pts.push([vx, vy]);
  if (pts.length > 0) {
    if (!blocs[courant]) blocs[courant] = [];
    blocs[courant].push(...pts);
  }
  return Object.entries(blocs).map(([nom, sommets]) => ({ nom, sommets }));
}

async function importerOrebloc() {
  const file = document.getElementById('fileOrebloc').files[0];
  if (!file) { setStatus('statusOrebloc', 'Aucun fichier selectionne', false); return; }
  const nom = document.getElementById('inpOreblocNom').value.trim() || file.name.replace(/\.(csv|dxf)$/i, '');
  const grade = parseFloat(document.getElementById('inpOreblocGrade').value || 10);

  try {
    const text = await file.text();
    let blocs;
    if (file.name.toLowerCase().endsWith('.dxf')) {
      blocs = parseOreblocDXF(text);
    } else {
      blocs = parseOreblocCSV(text);
    }
    if (blocs.length === 0) { setStatus('statusOrebloc', 'Aucun orebloc detecte dans le fichier', false); return; }

    let ok = 0;
    for (const b of blocs) {
      try {
        await apiFetch('/oreblocs', {
          method: 'POST',
          body: { nom: b.nom || nom, grade_cutoff: grade, sommets: b.sommets, source_fichier: file.name }
        });
        ok++;
      } catch (e) { console.warn('Echec orebloc ' + b.nom, e); }
    }
    setStatus('statusOrebloc', ok + '/' + blocs.length + ' orebloc(s) importe(s)', true);
  } catch (e) {
    setStatus('statusOrebloc', 'Erreur: ' + e.message, false);
  }
}

// ============================================================
// DUMPING AREAS (CSV)
// ============================================================
async function importerDA() {
  const file = document.getElementById('fileDA').files[0];
  if (!file) { setStatus('statusDA', 'Aucun fichier selectionne', false); return; }

  try {
    const text = await file.text();
    const lignes = text.split(/\r?\n/).filter(l => l.trim());
    let start = 0;
    const first = lignes[0].split(/[,;]/).map(c => c.trim());
    if (first.some(c => /nom|name|id/i.test(c))) start = 1;

    let ok = 0;
    for (let i = start; i < lignes.length; i++) {
      const cols = lignes[i].split(/[,;]/).map(c => c.trim());
      if (cols.length < 3) continue;
      try {
        await apiFetch('/dumping_areas', {
          method: 'POST',
          body: {
            nom: cols[0] || 'DA',
            x: parseFloat(cols[1]),
            y: parseFloat(cols[2]),
            rayon_m: cols[3] ? parseFloat(cols[3]) : 50
          }
        });
        ok++;
      } catch (e) { console.warn('Echec DA ' + cols[0], e); }
    }
    setStatus('statusDA', ok + ' Dumping Area(s) importee(s)', true);
  } catch (e) {
    setStatus('statusDA', 'Erreur: ' + e.message, false);
  }
}

// ============================================================
// TRAVELS ROADS (GeoTIFF)
// ============================================================
async function importerRoads() {
  const file = document.getElementById('fileRoads').files[0];
  if (!file) { setStatus('statusRoads', 'Aucun fichier selectionne', false); return; }
  if (typeof GeoTIFF === 'undefined') { setStatus('statusRoads', 'Librairie GeoTIFF non chargee', false); return; }

  try {
    const tiff = await GeoTIFF.fromBlob(file);
    const image = await tiff.getImage();
    const w = image.getWidth(), h = image.getHeight();
    let bbox;
    try { bbox = image.getBoundingBox(); } catch (e) { bbox = null; }
    const geojson = {
      type: 'Feature',
      properties: { name: file.name.replace(/\.(tif|tiff)$/i, '') },
      geometry: {
        type: 'Polygon',
        coordinates: [[
          [bbox[0], bbox[1]],
          [bbox[2], bbox[1]],
          [bbox[2], bbox[3]],
          [bbox[0], bbox[3]],
          [bbox[0], bbox[1]]
        ]]
      }
    };
    await apiFetch('/travels_roads', {
      method: 'POST',
      body: { nom: file.name.replace(/\.(tif|tiff)$/i, ''), geojson: geojson }
    });
    setStatus('statusRoads', 'Travels Road importe: ' + file.name + ' (' + w + 'x' + h + ')', true);
  } catch (e) {
    setStatus('statusRoads', 'Erreur: ' + e.message, false);
  }
}

// ============================================================
// POSITIONS ENGINS
// ============================================================
async function majPositionEngin() {
  const code = document.getElementById('inpEnginCode').value.trim();
  const type = document.getElementById('inpEnginType').value;
  const x = parseFloat(document.getElementById('inpEnginX').value || 0);
  const y = parseFloat(document.getElementById('inpEnginY').value || 0);
  if (!code) { setStatus('statusEngin', 'Code engin requis', false); return; }

  try {
    await apiFetch('/engins', {
      method: 'POST',
      body: { code, type, x, y }
    });
    setStatus('statusEngin', 'Position envoyee: ' + code + ' (' + type + ') x=' + x + ' y=' + y, true);
    document.getElementById('inpEnginX').value = '';
    document.getElementById('inpEnginY').value = '';
  } catch (e) {
    setStatus('statusEngin', 'Erreur: ' + e.message, false);
  }
}

// ============================================================
// ALERTES / DEVIATION / OFFICE
// ============================================================
async function verifierDeviationDT() {
  const code = document.getElementById('inpEnginCode').value.trim();
  if (!code) { setStatus('statusOffice', 'Code engin requis pour verifier la deviation', false); return; }
  try {
    const res = await apiFetch('/verifier_deviation', { method: 'POST', body: { dt_code: code } });
    if (res.type === 'deviation') {
      setStatus('statusOffice', 'DEVIATION detectee: ' + res.message + ' (voix off + Office)', false);
    } else {
      setStatus('statusOffice', res.message || 'Pas de deviation', true);
    }
    await refreshAlertes();
  } catch (e) {
    setStatus('statusOffice', 'Erreur: ' + e.message, false);
  }
}

async function syncOffice() {
  try {
    const res = await apiFetch('/office/sync');
    const rows = res.office_sync || [];
    setStatus('statusOffice', rows.length + ' enregistrement(s) en attente/envoye vers Office', true);
    await refreshAlertes();
  } catch (e) {
    setStatus('statusOffice', 'Erreur Office: ' + e.message, false);
  }
}

async function refreshAlertes() {
  try {
    const res = await apiFetch('/alertes');
    const tb = document.querySelector('#tabAlertes tbody');
    if (!tb) return;
    tb.innerHTML = '';
    (res.alertes || []).slice().reverse().forEach(a => {
      const cls = a.type === 'deviation' ? 'type-devoie' : 'type-mission';
      tb.innerHTML += `<tr><td>${a.created_at}</td><td class="${cls}">${a.type}</td><td>${a.dt_code}</td><td>${a.message}</td></tr>`;
    });
  } catch (e) { /* ignore */ }
}

// Initialisation des alertes si la page est active
async function checkServerStatus() {
  const el = document.getElementById('server-status');
  if (!el) return;
  try {
    const r = await fetch('http://localhost:8101/api/health', { cache: 'no-store' });
    if (r.ok) {
      el.innerHTML = '<span class="dot"></span><span>Serveur 8101: En ligne</span>';
      el.classList.remove('offline');
    } else {
      el.innerHTML = '<span class="dot"></span><span>Serveur 8101: Hors ligne</span>';
      el.classList.add('offline');
    }
  } catch (e) {
    el.innerHTML = '<span class="dot"></span><span>Serveur 8101: Hors ligne</span>';
    el.classList.add('offline');
  }
}

if (document.getElementById('page-senor-geology')) {
  refreshAlertes();
  checkServerStatus();
  setInterval(refreshAlertes, 10000);
  setInterval(checkServerStatus, 30000);
}
