document.addEventListener('DOMContentLoaded', () => {

    // ===== SIMULATED DATA =====
    const data = {
        minerais: { current: 53500, planned: 55000, target: 58000 },
        ore: { current: 1500, planned: 1600, target: 1800 },
        waste: { current: 26000, planned: 25000, target: 24000 },
        offroad: { count: 0, max: 8 },
        fuel: {
            DT01: { tank: 300, level: 72, consumption: 25 },
            DT02: { tank: 300, level: 45, consumption: 25 },
            DT03: { tank: 300, level: 12, consumption: 25 },
            DT04: { tank: 300, level: 88, consumption: 25 },
            DT05: { tank: 300, level: 56, consumption: 25 }
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
    const thresholds = {
        offroadAlert: 1,
        gradeVariance: 10,
        fuelLow: 15
    };

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
        animateValue(document.getElementById('offroadCount'), 0, data.offroad.count, 1500);

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
        const e = data.env, th = data.envThresholds;

        document.getElementById('envTemp').textContent = e.temperature.toFixed(1) + '\u00B0C';
        document.getElementById('envHumid').textContent = Math.round(e.humidity) + '%';
        document.getElementById('envRain').textContent = e.rainfall.toFixed(1) + 'mm';
        document.getElementById('envNoise').textContent = Math.round(e.noise) + ' dB';

        const tempLvl = e.temperature >= th.tempHigh ? 2 : e.temperature >= th.tempWarn ? 1 : 0;
        const humLvl = e.humidity >= th.humidityHigh ? 2 : e.humidity >= th.humidityWarn ? 1 : 0;
        const rainLvl = e.rainfall >= th.rainDanger ? 2 : e.rainfall >= th.rainWarn ? 1 : 0;
        const noiseLvl = e.noise >= th.noiseHigh ? 2 : e.noise >= th.noiseWarn ? 1 : 0;
        const worst = Math.max(tempLvl, humLvl, rainLvl, noiseLvl);

        const el = document.getElementById('voyantEnv');
        if (el) el.className = 'voyant-bar ' + (worst === 2 ? 'danger' : worst === 1 ? 'warning' : '');
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
    function cycleAlert() {
        const track = document.getElementById('alertTrack');
        const alert = alerts[alertIndex % alerts.length];

        track.innerHTML = `<span class="alert-text ${alert.type}">${alert.text}</span>`;
        alertIndex++;

        // Activate bell
        const bell = document.getElementById('notificationBell');
        const badge = document.getElementById('bellBadge');
        bell.classList.add('active');
        badge.classList.remove('hidden');
        badge.textContent = alertIndex;

        setTimeout(() => {
            bell.classList.remove('active');
        }, 3000);
    }

    // ===== SIMULATE DATA CHANGES =====
    function simulateDataChange() {
        // KPI fluctuations
        data.minerais.current += Math.round((Math.random() - 0.4) * 200);
        data.ore.current += Math.round((Math.random() - 0.4) * 30);
        data.waste.current += Math.round((Math.random() - 0.4) * 150);

        data.ore.current = Math.max(0, data.ore.current);
        data.minerais.current = Math.max(0, data.minerais.current);
        data.waste.current = Math.max(0, data.waste.current);

        // Grade fluctuations
        data.grades.high.actuel += Math.round((Math.random() - 0.45) * 50);
        data.grades.medium.actuel += Math.round((Math.random() - 0.45) * 40);
        data.grades.low.actuel += Math.round((Math.random() - 0.45) * 30);
        data.grades.marginal.actuel += Math.round((Math.random() - 0.45) * 20);

        // Offroad count (tends to be 0-2, rarely higher)
        if (Math.random() > 0.85) {
            data.offroad.count = Math.max(0, Math.min(3, data.offroad.count + (Math.random() > 0.4 ? 1 : -1)));
        }

        // Environnement
        data.env.temperature = Math.max(0, data.env.temperature + (Math.random() - 0.5) * 2);
        data.env.humidity = Math.max(0, Math.min(100, data.env.humidity + (Math.random() - 0.5) * 4));
        data.env.rainfall = Math.max(0, data.env.rainfall + (Math.random() - 0.75) * 2);
        data.env.noise = Math.max(40, Math.min(110, data.env.noise + (Math.random() - 0.5) * 6));
        renderEnvironment();

        // Reservoirs (consommation, ravitaillement quand vide)
        data.vehicles.forEach(v => {
            v.fuel = Math.max(0, v.fuel - Math.random() * 0.6);
            if (v.fuel < 2) v.fuel = 80 + Math.random() * 15;
        });
        renderFuelAlerts();

        // Update DOM - KPIs
        animateValue(document.getElementById('kpiMinerais'),
            parseInt(document.getElementById('kpiMinerais').textContent.replace(/\s/g, '')) || 0,
            data.minerais.current, 800);
        animateValue(document.getElementById('kpiOre'),
            parseInt(document.getElementById('kpiOre').textContent.replace(/\s/g, '')) || 0,
            data.ore.current, 800);
        animateValue(document.getElementById('kpiWaste'),
            parseInt(document.getElementById('kpiWaste').textContent.replace(/\s/g, '')) || 0,
            data.waste.current, 800);
        document.getElementById('offroadCount').textContent = data.offroad.count;

        // Grades
        setGrade('hg', data.grades.high.actuel, data.grades.high.planned);
        setGrade('mg', data.grades.medium.actuel, data.grades.medium.planned);
        setGrade('lg', data.grades.low.actuel, data.grades.low.planned);
        setGrade('mo', data.grades.marginal.actuel, data.grades.marginal.planned);

        // Voyants
        updateVoyants();
        updateKpiCardStates();
    }

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
                const resp = await fetch('data/Mining Areas.csv');
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
                const resp = await fetch('data/Trails.geojson.geojson');
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
                        ? `<img src="assets/icon Fosse.png" style="width:60px;height:60px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />${showLabels ? `<div class="zone-label">${z.name}</div>` : ''}`
                        : isRomPad
                        ? `<img src="assets/RomPad.png" style="width:50px;height:50px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));" />${showLabels ? `<div class="zone-label">${z.name}</div>` : ''}`
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

        if (!trails.length) return map;

        // VEHICLES - 1 DT par trail
        const vehicles = [];
        let dtNum = 0;
        trails.forEach((t, ti) => {
            dtNum++;
            vehicles.push({ id: 'DT-' + String(dtNum).padStart(2, '0'), online: true, trailIdx: ti, start: 0.1, dir: 1 });
        });

        // Niveaux de carburant par DT (reservoirs)
        const fuelLevels = [72, 45, 12, 88, 56, 8, 62, 30, 18, 10, 66, 52];
        data.vehicles = vehicles.map((v, idx) => ({ id: v.id, fuel: fuelLevels[idx % fuelLevels.length] }));
        renderFuelAlerts();

        const vehicleState = {};
        const vehicleMarkers = {};

        // Calculer la longueur reelle de chaque trail pour normaliser la vitesse
        function trailLength(waypoints) {
            let len = 0;
            for (let i = 1; i < waypoints.length; i++) {
                const dLat = waypoints[i][0] - waypoints[i-1][0];
                const dLng = waypoints[i][1] - waypoints[i-1][1];
                len += Math.sqrt(dLat * dLat + dLng * dLng);
            }
            return len;
        }
        const baseSpeed = 0.000012;

        vehicles.forEach(v => {
            const trail = trails[v.trailIdx % trails.length];
            const startPos = pointAt(trail.waypoints, v.start);

            const icon = L.divIcon({
                className: 'zone-marker',
                html: `<img src="assets/icon DT.png" class="dt-icon" data-dir="${v.dir}" style="width:24px;height:24px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.5));transform:scaleX(${v.dir === -1 ? -1 : 1});" />`,
                iconSize: [24, 24],
                iconAnchor: [12, 12]
            });
            const marker = L.marker([startPos[0], startPos[1]], { icon }).addTo(map);
            marker.bindTooltip(v.id, {
                permanent: showLabels,
                direction: 'top',
                offset: [0, -12],
                className: 'vehicle-tooltip'
            });
            vehicleMarkers[v.id] = marker;

            vehicleState[v.id] = {
                trail,
                progress: v.start,
                direction: v.dir,
                step: baseSpeed / Math.max(trailLength(trail.waypoints), 0.001),
                lastPos: startPos
            };
        });

        // ANIMATE: advance along the SAME waypoints, bounce at each end (aller-retour)
        const moveTimer = setInterval(() => {
            vehicles.forEach(v => {
                const st = vehicleState[v.id];
                st.progress += st.step * st.direction;

                if (st.progress >= 1) { st.progress = 1; st.direction = -1; }
                else if (st.progress <= 0) { st.progress = 0; st.direction = 1; }

                const pos = pointAt(st.trail.waypoints, st.progress);
                vehicleMarkers[v.id].setLatLng([pos[0], pos[1]]);

                // Symetrie horizontale selon le sens de deplacement (pas de rotation)
                const img = vehicleMarkers[v.id].getElement()?.querySelector('img');
                if (img) {
                    img.style.transform = `scaleX(${st.direction === -1 ? -1 : 1})`;
                }

                st.lastPos = pos;
            });
        }, 200);

        // Store timer so it can be cleared on re-init
        map._moveTimer = moveTimer;

        // Zoom controls
        document.getElementById('zoomIn').addEventListener('click', () => map.zoomIn());
        document.getElementById('zoomOut').addEventListener('click', () => map.zoomOut());
        document.getElementById('zoomCenter').addEventListener('click', () => map.setView([centerLat, centerLng], zoomVal));

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
