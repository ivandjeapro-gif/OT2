document.addEventListener('DOMContentLoaded', () => {

    // proj4 ne connait que WGS84 et Web Mercator par defaut : sans cette
    // definition, toute conversion UTM (zones CSV, positions des DT) echoue
    // en silence et les objets tombent en (0, 0).
    if (typeof proj4 !== 'undefined' && !proj4.defs('EPSG:32630')) {
        proj4.defs('EPSG:32630', '+proj=utm +zone=30 +datum=WGS84 +units=m +no_defs');
    }

    // ===== SIMULATED DATA =====
    const data = {
        minerais: { current: 53500, planned: 55000, target: 58000 },
        ore: { current: 1500, planned: 1600, target: 1800 },
        waste: { current: 26000, planned: 25000, target: 24000 },
        offroad: { count: 0, max: 8 },
        fuel: {
            DT01: { tank: 300, level: 72, consumption: 25 },
            DT02: { tank: 300, level: 45, consumption: 25 },
            DT03: { tank: 300, level: 12, consumption: 25 }
        },
        fuelThreshold: 15,
        vehicles: [],
        env: { temperature: 34, humidity: 62, rainfall: 0, noise: 72 },
        envThresholds: { tempHigh: 40, tempWarn: 36, humidityHigh: 80, humidityWarn: 70, rainWarn: 5, rainDanger: 15, noiseHigh: 85, noiseWarn: 75 },
        grades: {
            high: { actuel: 4200, planned: 4500 },
            medium: { actuel: 3100, planned: 3000 },
            low: { actuel: 2800, planned: 2600 },
            marginal: { actuel: 900, planned: 1000 }
        }
    };

    // ALERTS POOL (metier)
    const alerts = [
        { text: 'DT-03: Mauvais chemin - pas le bon trail vers ROM Pad 1', type: 'danger', cat: 'chemin' },
        { text: 'DT-01: Offroad detecte hors piste autorisee', type: 'danger', cat: 'offroad' },
        { text: 'DT-03: Niveau carburant bas - 12%', type: 'warn', cat: 'carburant' },
        { text: 'Temperature atmospherique elevee - 42 C', type: 'danger', cat: 'meteo' },
        { text: 'Humidite elevee - 85% - Piste glissante', type: 'warn', cat: 'meteo' },
        { text: 'Grade 24h: High Grade -15% vs Planned', type: 'warn', cat: 'grade' },
        { text: 'DT-02: Dumping non conforme - mauvais Rompad', type: 'danger', cat: 'chemin' },
        { text: 'DT-05: Offroad - zone non autorisee', type: 'warn', cat: 'offroad' },
        { text: 'Temperature basse - 8 C - Risque de gel', type: 'warn', cat: 'meteo' },
        { text: 'DT-04: Carburant critique - 8%', type: 'danger', cat: 'carburant' },
    ];

    let alertIndex = 0;

    // THRESHOLDS
    // Valeurs par defaut ; ecrasees par les parametres du serveur (table params)
    // via applyServerParams() ci-dessous.
    const thresholds = {
        offroadAlert: 1,
        gradeVariance: 10,
        fuelLow: 15
    };

    // ===== PARAMETRES SERVEUR =====
    // Recupere les seuils depuis /api/config (table params) et remplace les
    // valeurs en dur. Re-appelle periodiquement : un changement dans Settings
    // se propage au dashboard sans rechargement.
    async function applyServerParams() {
        if (typeof API === 'undefined') return;
        try {
            const p = await API.config();
            if (!p) return;
            const num = (v, d) => { const n = parseFloat(v); return isNaN(n) ? d : n; };
            if (p.seuil_grade !== undefined)  thresholds.gradeVariance = num(p.seuil_grade, thresholds.gradeVariance);
            if (p.fuel_seuil !== undefined) {
                thresholds.fuelLow = num(p.fuel_seuil, thresholds.fuelLow);
                data.fuelThreshold = thresholds.fuelLow;
            }
            if (p.temp_max !== undefined)  data.envThresholds.tempHigh = num(p.temp_max, data.envThresholds.tempHigh);
            if (p.temp_min !== undefined)  data.envThresholds.tempWarn = num(p.temp_min, data.envThresholds.tempWarn);
            if (p.humidite_max !== undefined)  data.envThresholds.humidityHigh = num(p.humidite_max, data.envThresholds.humidityHigh);
        } catch (e) { /* serveur hors ligne : valeurs par defaut */ }
    }
    applyServerParams();
    setInterval(applyServerParams, 60000); // re-synchronise toutes les minutes

    // ===== ANIMATED COUNTER =====
    function animateValue(el, start, end, duration, suffix = '') {
        const range = end - start;
        const startTime = performance.now();

        function update(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            const value = Math.round(start + range * eased);
            el.textContent = value.toLocaleString('fr-FR') + suffix;
            if (progress < 1) requestAnimationFrame(update);
        }
        requestAnimationFrame(update);
    }

    // ===== INIT KPI VALUES =====
    function initKPIs() {
        animateValue(document.getElementById('kpiMinerais'), 0, data.minerais.current, 2000);
        animateValue(document.getElementById('kpiOre'), 0, data.ore.current, 2000);
        animateValue(document.getElementById('kpiWaste'), 0, data.waste.current, 2000);
        // offroadCount : pas d'animation, la valeur reelle arrive par refreshOffroad()

        setTimeout(() => {
            setGrade('hg', data.grades.high.actuel, data.grades.high.planned);
            setGrade('mg', data.grades.medium.actuel, data.grades.medium.planned);
            setGrade('lg', data.grades.low.actuel, data.grades.low.planned);
            setGrade('mo', data.grades.marginal.actuel, data.grades.marginal.planned);
        }, 500);
    }

    function setGrade(prefix, actuel, planned) {
        document.getElementById(`${prefix}Actuel`).textContent = actuel.toLocaleString('fr-FR');
        document.getElementById(`${prefix}Planned`).textContent = planned.toLocaleString('fr-FR');
        const diff = actuel - planned;
        const varEl = document.getElementById(`${prefix}Var`);
        varEl.textContent = (diff >= 0 ? '+' : '') + diff.toLocaleString('fr-FR');
        varEl.className = 'gc-val var ' + (diff > 0 ? 'positive' : diff < 0 ? 'negative' : 'neutral');
    }

    // ===== VOYANT STATUS =====
    function updateVoyants() {
        // KPI voyants
        const minVar = data.minerais.current - data.minerais.planned;
        setVoyant('voyantMinerais', minVar, 0.1);

        const oreVar = data.ore.current - data.ore.planned;
        setVoyant('voyantOre', oreVar, 0.1);

        const wasteVar = data.waste.planned - data.waste.current;
        setVoyant('voyantWaste', wasteVar, 0.1);

        // Offroad voyant
        const offroadEl = document.getElementById('offroadCount');
        const offroadCard = offroadEl.closest('.offroad-card');
        const offroadVoyant = document.getElementById('voyantOffroad');
        offroadCard.classList.toggle('alert-state', data.offroad.count >= thresholds.offroadAlert);
        if (offroadVoyant) {
            offroadVoyant.className = 'voyant-bar ' + (data.offroad.count >= thresholds.offroadAlert ? 'danger' : '');
        }

        // Grade voyants
        setVoyantBar('voyantHG', data.grades.high.actuel, data.grades.high.planned);
        setVoyantBar('voyantMG', data.grades.medium.actuel, data.grades.medium.planned);
        setVoyantBar('voyantLG', data.grades.low.actuel, data.grades.low.planned);
        setVoyantBar('voyantMO', data.grades.marginal.actuel, data.grades.marginal.planned);

        // Signal icon status
        const signalIcon = document.getElementById('signalIcon');
        signalIcon.classList.remove('offline');
    }

    function setVoyant(id, diff, threshold) {
        const el = document.getElementById(id);
        const ratio = Math.abs(diff) / (data.minerais.planned * threshold);
        el.className = 'kpi-voyant ' + (ratio > 1.5 ? 'danger' : ratio > 0.8 ? 'warning' : '');
    }

    function setVoyantBar(id, actuel, planned) {
        const el = document.getElementById(id);
        const diff = actuel - planned;
        const ratio = Math.abs(diff) / planned;
        el.className = 'voyant-bar ' + (ratio > 0.15 ? 'danger' : ratio > 0.08 ? 'warning' : '');
    }

    // ===== ENVIRONNEMENT =====
    function renderEnvironment() {
        // Reel uniquement : aucune sonde meteo physique n'est branchee
        // aujourd'hui, on affiche l'absence de mesure au lieu d'inventer.
        const e = data.env, th = data.envThresholds;

        document.getElementById('envTemp').textContent = '--\u00B0C';
        document.getElementById('envHumid').textContent = '--%';
        document.getElementById('envRain').textContent = '--mm';
        document.getElementById('envNoise').textContent = '-- dB';

        const el = document.getElementById('voyantEnv');
        if (el) el.className = 'voyant-bar';
    }

    function renderFuelAlerts() {
        const list = document.getElementById('fuelAlertList');
        const badge = document.getElementById('fuelAlertCount');
        if (!list || !badge) return;

        const alerts = (data.vehicles || [])
            .filter(v => v.fuel < data.fuelThreshold)
            .sort((a, b) => a.fuel - b.fuel);

        badge.textContent = alerts.length;
        badge.classList.toggle('ok', alerts.length === 0);

        if (!alerts.length) {
            list.innerHTML = '<div class="fuel-no-alert">Aucun reservoir en alerte</div>';
        } else {
            list.innerHTML = alerts
                .map(v => `<div class="fuel-alert-item"><span class="fuel-dt">${v.id}</span><span class="fuel-pct">${Math.round(v.fuel)}%</span></div>`)
                .join('');
        }

        const el = document.getElementById('voyantFuel');
        if (el) el.className = 'voyant-bar ' + (alerts.length ? 'danger' : '');
    }

    // ===== KPI CARD ALERT STATE =====
    function updateKpiCardStates() {
        const cards = {
            kpiMineraisCard: data.minerais,
            kpiOreCard: data.ore,
            kpiWasteCard: data.waste
        };

        Object.entries(cards).forEach(([id, d]) => {
            const el = document.getElementById(id);
            const diff = Math.abs(d.current - d.planned) / d.planned;
            el.classList.toggle('alert-state', diff > 0.1);
        });
    }

    // ===== ALERT TICKER =====
    // Config transmise par la page Live (bouton PRESENTER)
    let configLive = {};
    try { configLive = JSON.parse(localStorage.getItem('oreTrackingConfig')) || {}; } catch (e) {}
    const afficherAlertes = configLive.showAlerts !== false;
    if (!afficherAlertes) {
        const banner = document.getElementById('alertBanner');
        if (banner) banner.style.visibility = 'hidden';
    }

    function jouerSonDanger() {
        if (!afficherAlertes || typeof PREFS === 'undefined') return;
        const now = Date.now();
        if (!jouerSonDanger._last || now - jouerSonDanger._last > 10000) {
            PREFS.alertSound();
            jouerSonDanger._last = now;
        }
    }

    function afficherDansBandeau(texte, type) {
        const track = document.getElementById('alertTrack');
        if (!track) return;
        const span = document.createElement('span');
        span.className = 'alert-text ' + (type || '');
        span.textContent = texte;           // jamais d'HTML venant du serveur
        track.replaceChildren(span);
    }

    // Bandeau fixe et rouge tant qu'au moins un DT est hors piste ; sinon
    // defilement normal des alertes de la passerelle.
    function bandeauOffroad() {
        const banner = document.getElementById('alertBanner');
        const dts = data.offroad.dts || [];
        const actif = afficherAlertes && dts.length > 0;
        if (banner) banner.classList.toggle('offroad', actif);
        if (!actif) return false;
        const codes = dts.map((d) => d.code).join(', ');
        const n = dts.length;
        afficherDansBandeau('ATTENTION ATTENTION !!!!! ' + n + ' DT (' + codes + ') ' +
            (n > 1 ? 'ont fait une sortie de route.' : 'a fait une sortie de route.'), 'danger');
        return true;
    }

    function cycleAlert() {
        if (!afficherAlertes || bandeauOffroad()) return;
        // Alertes REELLES de la passerelle (plus aucun pool simule)
        if (!alertesReelles.length) return;
        const alert = alertesReelles[alertIndex % alertesReelles.length];
        afficherDansBandeau(alert.text, alert.type);
        alertIndex++;
        if (alert.type === 'danger') jouerSonDanger();
    }

    // Cloche : nombre d'alertes non acquittees (pas un compteur de defilement)
    function majCloche(nonVues, nouvelles) {
        const bell = document.getElementById('notificationBell');
        const badge = document.getElementById('bellBadge');
        if (!bell || !badge) return;
        badge.textContent = nonVues;
        badge.classList.toggle('hidden', nonVues === 0);
        if (nouvelles) {
            bell.classList.add('active');
            setTimeout(() => bell.classList.remove('active'), 3000);
        }
    }

    // ===== SIMULATE DATA CHANGES =====
    function simulateDataChange() {
        /* ===== MODE REEL (decision du 07/11 : 0% de donnees simulees) =====
         * L'ancien corps incrementait des valeurs au hasard toutes les 3 s
         * (KPI, grades, environnement, carburant, offroad). TOUT supprime :
         * les KPI reels sont geres par missions-ui.js (passerelle), les
         * alertes reelles arrivent via refreshAlertesReelles() ci-dessous,
         * et l'environnement affiche "--" tant qu'aucune sonde physique
         * n'existe. Rien n'est invente. */
    }

    /* ---- Alertes REELLES de la passerelle dans le bandeau defilant ---- */
    let alertesReelles = [];
    let dernierIdAlerte = null;
    const TYPES_INFO = { fin_mission: 1, arrivee: 1, retour_piste: 1, retour_trajet: 1, mission: 1 };

    function refreshAlertesReelles() {
        const host = window.location.hostname || 'localhost';
        fetch('http://' + host + ':8101/api/alertes', { cache: 'no-store' })
            .then((r) => r.json())
            .then((d) => {
                const toutes = d.alertes || [];
                alertesReelles = toutes.slice(0, 10).map((a) => ({
                    text: (a.dt_code && a.dt_code !== '-' ? a.dt_code + ' : ' : '') + (a.message || a.type),
                    type: TYPES_INFO[a.type] ? 'success' : (a.acquittee ? 'warn' : 'danger'),
                }));
                if (!alertesReelles.length) {
                    alertesReelles = [{ text: 'Aucune alerte active', type: 'success' }];
                }
                const nonVues = toutes.filter((a) => !a.acquittee && !TYPES_INFO[a.type]).length;
                const plusRecent = toutes.length ? toutes[0].id : 0;
                majCloche(nonVues, dernierIdAlerte !== null && plusRecent > dernierIdAlerte);
                dernierIdAlerte = plusRecent;
            })
            .catch(() => {
                alertesReelles = [{ text: 'Passerelle injoignable', type: 'warn' }];
            });
    }
    refreshAlertesReelles();
    setInterval(refreshAlertesReelles, 8000);

    /* ---- Compteur OFFROAD reel (detection passerelle, /api/offroad) ---- */
    let majCamionsMaintenant = null;   // branche par initMap()
    let codesHorsPiste = '';

    function refreshOffroad() {
        const host = window.location.hostname || 'localhost';
        fetch('http://' + host + ':8101/api/offroad', { cache: 'no-store' })
            .then((r) => r.json())
            .then((d) => {
                const n = d.count || 0;
                data.offroad.count = n;
                data.offroad.dts = d.dts || [];
                const el = document.getElementById('offroadCount');
                if (el) el.textContent = n;
                updateVoyants();
                const codes = data.offroad.dts.map((x) => x.code).sort().join(',');
                if (codes !== codesHorsPiste) {
                    // un nouveau DT hors piste : son + bandeau + carte tout de suite
                    const nouveau = data.offroad.dts.some((x) => codesHorsPiste.split(',').indexOf(x.code) < 0);
                    codesHorsPiste = codes;
                    if (nouveau) jouerSonDanger();
                    if (!bandeauOffroad()) cycleAlert();
                    if (majCamionsMaintenant) majCamionsMaintenant();
                }
            })
            .catch(() => { /* passerelle hors ligne : on garde la derniere valeur */ });
    }
    refreshOffroad();
    setInterval(refreshOffroad, 3000);

    // La page Live a relance PRESENTER (zones, trails, fond, options) :
    // on recharge pour appliquer la nouvelle configuration sans fermer la fenetre.
    window.addEventListener('storage', (e) => {
        if (e.key === 'oreTrackingConfig' || e.key === 'oreTrackingTrails') {
            setTimeout(() => location.reload(), 300);
        }
    });

    // ===== MAP =====
    async function initMap() {
        // Read config from localStorage (set by software Live page)
        let config = null;
        try {
            config = JSON.parse(localStorage.getItem('oreTrackingConfig'));
        } catch(e) {}

        console.log('[Dashboard] config brute localStorage:', config ? JSON.stringify(config).substring(0,500) : 'NULL');

        // Defaults if no config
        const centerLat = (config && config.center && config.center.lat) ? config.center.lat : 8.2626;
        const centerLng = (config && config.center && config.center.lng) ? config.center.lng : -4.6301;
        const zoomVal = (config && config.zoom) ? config.zoom : 14;

        // Zones: config > CSV file > hardcoded
        let zones = (config && config.zones && config.zones.length) ? config.zones : null;
        if (!zones) {
            try {
                const resp = await fetch('../data/Mining Areas.csv');
                if (resp.ok) {
                    const text = await resp.text();
                    const lines = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim().split('\n');
                    if (lines.length >= 2) {
                        const header = lines[0].toLowerCase();
                        const sep = header.includes('\t') ? '\t' : (header.includes(';') ? ';' : ',');
                        const cols = header.split(sep).map(c => c.trim()).filter(c => c);
                        let nameIdx = cols.findIndex(c => c === 'name' || c === 'nom' || c === 'zone');
                        let xIdx = cols.findIndex(c => c === 'x' || c === 'easting');
                        let yIdx = cols.findIndex(c => c === 'y' || c === 'northing');
                        let colorIdx = cols.findIndex(c => c === 'color' || c === 'couleur');
                        if (xIdx === -1 || yIdx === -1) { xIdx = 0; yIdx = 1; }
                        const colorMap = { 'vert':'green','green':'green','or':'orange','orange':'orange','violet':'blue','bleu':'blue','blue':'blue','jaune':'yellow','yellow':'yellow' };
                        zones = [];
                        for (let i = 1; i < lines.length; i++) {
                            const line = lines[i].trim();
                            if (!line) continue;
                            const parts = line.split(sep).map(p => p.trim());
                            const rawX = parseFloat(parts[xIdx]);
                            const rawY = parseFloat(parts[yIdx]);
                            if (!isFinite(rawX) || !isFinite(rawY)) continue;
                            const ll = convertCRS(rawX, rawY);
                            zones.push({ name: (nameIdx >= 0 && parts[nameIdx]) ? parts[nameIdx] : 'Zone ' + i, lat: ll.lat, lng: ll.lng, color: colorMap[(colorIdx >= 0 && parts[colorIdx] || '').toLowerCase().trim()] || 'blue' });
                        }
                    }
                }
            } catch(e) { console.warn('Fetch CSV zones failed:', e); }
        }

        function convertCRS(x, y) {
            if (Math.abs(x) > 180 || Math.abs(y) > 90) {
                if (typeof proj4 !== 'undefined') {
                    try { const ll = proj4('EPSG:32630', 'EPSG:4326', [x, y]); return { lat: ll[1], lng: ll[0] }; } catch(e) {}
                }
                return { lat: 0, lng: 0 };
            }
            return { lat: x, lng: y };
        }

        if (!zones) zones = [
            { name: 'Main Pit', lat: 8.2691, lng: -4.6312, color: 'blue' },
            { name: 'West Pit', lat: 8.2606, lng: -4.6375, color: 'green' },
            { name: 'ROM Pad 1', lat: 8.2716, lng: -4.6381, color: 'yellow' },
            { name: 'ROM Pad 2', lat: 8.2688, lng: -4.6432, color: 'orange' },
            { name: 'ROM Pad 3', lat: 8.2652, lng: -4.6437, color: 'yellow' },
        ];

        // Trails: config > localStorage > GeoJSON file > hardcoded
        let trailsDef = null;
        try {
            const savedTrails = localStorage.getItem('oreTrackingTrails');
            if (savedTrails) {
                const parsed = JSON.parse(savedTrails);
                if (parsed && parsed.length) trailsDef = parsed;
            }
        } catch(e) {}
        if (!trailsDef) trailsDef = (config && config.trails && config.trails.length) ? config.trails : null;
        if (!trailsDef) {
            try {
                const resp = await fetch('../data/Trails.geojson.geojson');
                if (resp.ok) {
                    const geo = await resp.json();
                    const features = geo.features || [];
                    trailsDef = [];
                    features.forEach(feat => {
                        const coords = feat.geometry && feat.geometry.coordinates;
                        if (!coords || coords.length < 2) return;
                        const waypoints = coords.map(c => [c[1], c[0]]);
                        trailsDef.push({ type: 'imported', name: (feat.properties && feat.properties.name) || 'Trail', color: '#f97316', waypoints, useColor: true });
                    });
                }
            } catch(e) { console.warn('Fetch GeoJSON trails failed:', e); }
        }
        console.log('[Dashboard] trailsDef:', trailsDef.map(t => (t.name ? (t.name + ' (' + (t.waypoints ? t.waypoints.length : 'manual') + ' pts)') : (t.from + ' -> ' + t.to))));

        const showZones = !config || config.showZones !== false;
        const showTrails = !config || config.showTrails !== false;
        const showLabels = !config || config.showLabels !== false;

        const map = L.map('map', {
            center: [centerLat, centerLng],
            zoom: zoomVal,
            zoomControl: false,
            attributionControl: false
        });

        // ---- BASEMAP ----
        const basemaps = {
            'osm': { url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, Tiles: HOT', options: { crossOrigin: true, maxZoom: 19 } },
            'esri-sat': { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attribution: '&copy; ESRI World Imagery', options: { crossOrigin: true, maxZoom: 19 } },
            'esri-topo': { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', attribution: '&copy; ESRI Topo', options: { crossOrigin: true, maxZoom: 19 } },
            'esri-dark': { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', attribution: '&copy; ESRI Dark', options: { crossOrigin: true, maxZoom: 16 } }
        };
        const basemapId = (config && config.basemap && basemaps[config.basemap]) ? config.basemap : 'esri-sat';

        if (basemapId === 'mbtiles' && config && config.mbtilesUrl) {
            const tileUrl = config.mbtilesUrl.replace(/\/$/, '') + '/{z}/{x}/{y}.png';
            L.tileLayer(tileUrl, { attribution: 'MBTiles local', maxZoom: 19, errorTileUrl: '' }).addTo(map);
        } else {
            const bm = basemaps[basemapId] || basemaps['esri-sat'];
            L.tileLayer(bm.url, bm.options).addTo(map);
        }

        // ---- CUSTOM LAYERS (TIFF / GeoJSON / MBTiles) ----
        const mbtilesBase = (config && config.mbtilesUrl ? config.mbtilesUrl : 'http://localhost:8080').replace(/\/$/, '');
        console.log('[Dashboard] config.layers:', config && config.layers ? config.layers.map(l => ({type:l.type, name:l.name, visible:l.visible, mbtilesName:l.mbtilesName})) : 'AUCUN');
        console.log('[Dashboard] mbtilesBase:', mbtilesBase);
        if (config && config.layers && config.layers.length) {
            config.layers.forEach(layer => {
                if (!layer.visible) return;
                if ((layer.type === 'tiff' || layer.type === 'georef') && layer.dataUrl && layer.bounds) {
                    L.imageOverlay(layer.dataUrl, layer.bounds, { opacity: layer.opacity || 0.8, interactive: false }).addTo(map);
                } else if (layer.type === 'geojson' && layer.geoJSON) {
                    L.geoJSON(layer.geoJSON, {
                        style: () => ({
                            color: '#f97316', weight: 2, opacity: layer.opacity || 0.9,
                            fillColor: '#f97316', fillOpacity: (layer.opacity || 0.9) * 0.35
                        }),
                        pointToLayer: (f, latlng) => L.circleMarker(latlng, {
                            radius: 5, color: '#f97316', weight: 2,
                            opacity: layer.opacity || 0.9,
                            fillColor: '#f97316', fillOpacity: (layer.opacity || 0.9) * 0.6
                        })
                    }).addTo(map);
                } else if (layer.type === 'mbtiles' && layer.mbtilesName) {
                    const url = mbtilesBase + '/' + encodeURIComponent(layer.mbtilesName) + '/{z}/{x}/{y}.png';
                    console.log('[Dashboard] MBTiles layer URL:', url);
                    const tl = L.tileLayer(url, {
                        attribution: 'MBTiles', maxZoom: 19, errorTileUrl: '', opacity: layer.opacity || 0.8
                    });
                    tl.on('tileerror', function(e) { console.warn('[Dashboard] tile error:', e.tile && e.tile.src); });
                    tl.addTo(map);
                }
            });
        }
        // backward compat with old tiffLayers configs
        if (config && config.tiffLayers && config.tiffLayers.length) {
            config.tiffLayers.forEach(layer => {
                if (layer.visible && layer.dataUrl && layer.bounds) {
                    L.imageOverlay(layer.dataUrl, layer.bounds, { opacity: layer.opacity || 0.8, interactive: false }).addTo(map);
                }
            });
        }

        // ZONES
        if (showZones) {
            zones.forEach(z => {
                if (!z.lat || !z.lng) return;
                const isPit = /pit/i.test(z.name);
                const isRomPad = /rom/i.test(z.name);
                const icon = L.divIcon({
                    className: 'zone-marker',
                    html: isPit
                        ? `<img src="../assets/icon Fosse.png" style="width:60px;height:60px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />${showLabels ? `<div class="zone-label">${z.name}</div>` : ''}`
                        : isRomPad
                        ? `<img src="../assets/RomPad.png" style="width:50px;height:50px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />${showLabels ? `<div class="zone-label">${z.name}</div>` : ''}`
                        : `<div class="zone-pin ${z.color || 'blue'}"><svg viewBox="0 0 24 24" fill="white" width="12" height="12"><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z"/></svg></div>${showLabels ? `<div class="zone-label">${z.name}</div>` : ''}`,
                    iconSize: isPit ? [60, 60] : isRomPad ? [50, 50] : [24, 24],
                    iconAnchor: isPit ? [30, 30] : isRomPad ? [25, 25] : [12, 12]
                });
                L.marker([z.lat, z.lng], { icon }).addTo(map);
            });
        }

        // TRAILS
        const trails = [];
        trailsDef.forEach((t, ti) => {
            let waypoints;

            if (t.type === 'imported' && t.waypoints && t.waypoints.length >= 2) {
                waypoints = t.waypoints;
            } else {
                const fromZone = zones.find(z => z.name === t.from);
                const toZone = zones.find(z => z.name === t.to);
                if (!fromZone || !toZone) return;
                waypoints = buildTrailWaypoints(fromZone, toZone, ti);
            }
            if (!waypoints.length) return;

            const trailColor = t.useColor === false ? '#94a3b8' : t.color;

            if (showTrails) {
                L.polyline(waypoints, { color: trailColor, weight: 3, opacity: 0.75, dashArray: '8, 6' }).addTo(map);
            }

            const fromZone = zones.find(z => z.name === t.from) || zones[0] || { lat: waypoints[0][0], lng: waypoints[0][1] };
            const toZone = zones.find(z => z.name === t.to) || zones[1] || { lat: waypoints[waypoints.length-1][0], lng: waypoints[waypoints.length-1][1] };
            trails.push({ from: fromZone, to: toZone, color: trailColor, waypoints });
        });

        // If no trails could be built, show at least one straight line between first two zones
        if (!trails.length && zones.length >= 2) {
            const wp = [
                [zones[0].lat, zones[0].lng],
                [zones[1].lat, zones[1].lng]
            ];
            if (showTrails) L.polyline(wp, { color: '#f97316', weight: 3, opacity: 0.75, dashArray: '8, 6' }).addTo(map);
            trails.push({ from: zones[0], to: zones[1], color: '#f97316', waypoints: wp });
        }

        // Zoom controls (avant tout retour anticipe)
        document.getElementById('zoomIn').addEventListener('click', () => map.zoomIn());
        document.getElementById('zoomOut').addEventListener('click', () => map.zoomOut());
        document.getElementById('zoomCenter').addEventListener('click', () => map.setView([centerLat, centerLng], zoomVal));

        // ===== DT REELS (passerelle :8101, /api/engins) =====
        // Plus aucun camion simule (decision du 07/11) : chaque DT actif est
        // place a sa derniere position connue (UTM -> WGS84). Un DT hors piste
        // (/api/offroad) prend une etiquette rouge et un halo pulse.
        const dtMarkers = {};
        const POSITION_PERIMEE_S = 120;   // au-dela, DT grise (signal perdu)

        function utmVersLatLng(x, y) {
            if (typeof proj4 === 'undefined') return null;
            try { const ll = proj4('EPSG:32630', 'EPSG:4326', [x, y]); return [ll[1], ll[0]]; }
            catch (e) { return null; }
        }

        function iconeDT(code, offroad, perime) {
            return L.divIcon({
                className: 'zone-marker dt-marker' + (offroad ? ' offroad' : '') + (perime ? ' perime' : ''),
                html: (offroad ? '<span class="dt-halo"></span>' : '') +
                      `<img src="../assets/icon DT.png" class="dt-icon" alt="${code}" />`,
                iconSize: [28, 28],
                iconAnchor: [14, 14]
            });
        }

        function secondesDepuis(horodatage) {
            const t = Date.parse(String(horodatage || '').replace(' ', 'T'));
            return isNaN(t) ? Infinity : (Date.now() - t) / 1000;
        }

        function majCamions() {
            const host = window.location.hostname || 'localhost';
            fetch('http://' + host + ':8101/api/engins', { cache: 'no-store' })
                .then((r) => r.json())
                .then((d) => {
                    const horsPiste = new Set((data.offroad.dts || []).map((x) => x.code));
                    const vus = new Set();
                    (d.engins || [])
                        .filter((e) => e.type === 'DT' && Number(e.actif) !== 0)
                        .forEach((e) => {
                            const ll = utmVersLatLng(Number(e.x), Number(e.y));
                            if (!ll) return;
                            vus.add(e.code);
                            const offroad = horsPiste.has(e.code) || Number(e.offroad) === 1;
                            const perime = secondesDepuis(e.derniere_maj) > POSITION_PERIMEE_S;
                            const etat = (offroad ? 'o' : '') + (perime ? 'p' : '');
                            let m = dtMarkers[e.code];
                            if (!m) {
                                m = L.marker(ll, { icon: iconeDT(e.code, offroad, perime), zIndexOffset: 500 }).addTo(map);
                                m.bindTooltip(e.code, { permanent: showLabels || offroad, direction: 'top', offset: [0, -14], className: 'vehicle-tooltip' });
                                m._etat = null;
                                dtMarkers[e.code] = m;
                            } else {
                                m.setLatLng(ll);
                            }
                            if (m._etat !== etat) {
                                m.setIcon(iconeDT(e.code, offroad, perime));
                                m.setZIndexOffset(offroad ? 1000 : 500);
                                // etiquette rouge toujours visible quand le DT est hors piste
                                m.unbindTooltip();
                                m.bindTooltip(e.code, {
                                    permanent: showLabels || offroad, direction: 'top', offset: [0, -14],
                                    className: 'vehicle-tooltip' + (offroad ? ' offroad' : '') + (perime ? ' perime' : '')
                                });
                                m._etat = etat;
                            }
                            const dist = offroad && e.offroad_distance ? ' · ' + Math.round(e.offroad_distance) + ' m hors piste' : '';
                            m.getTooltip().setContent(e.code + dist);
                        });
                    Object.keys(dtMarkers).forEach((code) => {
                        if (!vus.has(code)) { map.removeLayer(dtMarkers[code]); delete dtMarkers[code]; }
                    });
                })
                .catch(() => { /* passerelle hors ligne : on garde les dernieres positions */ });
        }
        majCamions();
        map._moveTimer = setInterval(majCamions, 3000);
        majCamionsMaintenant = majCamions;

        // Niveaux de carburant par DT (reservoirs) : valeurs REELLES lues
        // dans la table engins du serveur. Aucun reservoir invente.
        if (typeof API !== 'undefined') {
          API.getEngins().then((d) => {
            const engins = (d && d.rows) || [];
            data.vehicles = engins
              .filter((e) => !e.statut || e.statut === 'actif')
              .map((e) => ({ id: e.code, fuel: parseFloat(e.fuel_pct) || 0 }));
            renderFuelAlerts();
          }).catch(() => { /* serveur hors ligne */ });
        }
        renderFuelAlerts();

        return map;
    }

    // ===== TRAIL HELPERS (deterministic) =====
    // Build waypoints as [lat, lng] pairs between two zones.
    // The offset depends ONLY on the trail index => always identical,
    // so the drawn polyline and the vehicle path are the SAME line.
    function buildTrailWaypoints(from, to, trailIndex) {
        const points = [[from.lat, from.lng], [to.lat, to.lng]];
        const steps = 10;
        const offset = 0.0006 + (trailIndex % 4) * 0.0004;
        const sign = (trailIndex % 2 === 0) ? 1 : -1;

        // Perpendicular offset for a slight arc so trails don't overlap exactly
        for (let i = 1; i < steps; i++) {
            const t = i / steps;
            const lat = from.lat + (to.lat - from.lat) * t;
            const lng = from.lng + (to.lng - from.lng) * t;
            const bulge = Math.sin(Math.PI * t) * offset * sign;
            points.splice(i, 0, [lat + bulge, lng - bulge]);
        }
        return points;
    }

    function pointAt(waypoints, progress) {
        const segCount = waypoints.length - 1;
        const idx = Math.max(0, Math.min(segCount, progress * segCount));
        const i = Math.floor(idx);
        const frac = idx - i;
        if (i >= segCount) return waypoints[segCount];
        const a = waypoints[i], b = waypoints[i + 1];
        return [
            a[0] + (b[0] - a[0]) * frac,
            a[1] + (b[1] - a[1]) * frac
        ];
    }

    // ===== INIT =====
    initKPIs();
    updateVoyants();
    updateKpiCardStates();
    renderEnvironment();
    renderFuelAlerts();

    setTimeout(async () => {
        const m = await initMap();
        m.setZoom(12);
        setTimeout(() => m.invalidateSize(), 300);
    }, 300);

    // Data update every 3s
    setInterval(simulateDataChange, 3000);

    // Alert cycle every 6s
    setInterval(cycleAlert, 6000);

    // First alert after 2s
    setTimeout(cycleAlert, 2000);
});

// ===== SYNC SERVEUR (OreTracking) =====
// Si le serveur repond, les KPI/grades sont pilotes par la base de donnees
// (/api/stats). Sinon, fallback simulation locale.
(function () {
  function startServerSync() {
    if (typeof API === 'undefined') return;
    async function syncFromServer() {
      try {
        const s = await API.stats();
        // format selon Preferences (nombre)
        const fmt = (typeof PREFS !== 'undefined')
          ? (v => PREFS.formatNumber(v, 0))
          : (v => (v || 0).toLocaleString('fr-FR'));
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = fmt(v || 0); };
        set('kpiMinerais', (s.minerais_t || 0) + (s.waste_t || 0));
        set('kpiOre', s.minerais_t || 0);
        set('kpiWaste', s.waste_t || 0);
      } catch (e) { /* serveur hors ligne : simulation locale */ }
    }
    setInterval(syncFromServer, 5000);
    setTimeout(syncFromServer, 500);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', startServerSync);
  } else {
    startServerSync();
  }
})();
