/* =====================================================================
 * carte-suivi.js — carte de la mine et suivi des DT en temps reel (19/11).
 *
 * Partage par la page Live (carte « Tracking de la flotte ») et le
 * dashboard (salle de controle) : meme fond, memes zones, memes pistes,
 * memes DT, memes filtres. Les filtres se reglent sur Live (« Perimetre de
 * suivi ») et s'appliquent en direct au dashboard (localStorage +
 * evenement `storage`).
 *
 * Source : passerelle :8101 /api/suivi_dt (etat de chaque engin, vitesse,
 * cap, mission, teneur du chargement, trajets d'une periode).
 * ===================================================================== */
(function (global) {
    'use strict';

    if (typeof proj4 !== 'undefined' && !proj4.defs('EPSG:32630')) {
        proj4.defs('EPSG:32630', '+proj=utm +zone=30 +datum=WGS84 +units=m +no_defs');
    }

    const PASSERELLE = () => (global.OTCFG && OTCFG.passerelle)
        ? OTCFG.passerelle()
        : 'http://' + (location.hostname || 'localhost') + ':8101';

    // ----------------------------------------------------------- etats ---
    // Couleurs tirees du logo Lafigue ; rouge reserve a la sortie de route.
    const ETATS = {
        chargement:   { lib: 'En chargement',              court: 'Chargement',   couleur: '#FCD525' },
        mission:      { lib: 'En mission (chargé)',        court: 'En mission',   couleur: '#EE7A2A' },
        roulage:      { lib: 'Roulage / attente',          court: 'Roulage',      couleur: '#4BA3DA' },
        attente_loin: { lib: 'Attente loin des pelles',    court: 'Attente loin', couleur: '#B7B3A3' },
        hors_piste:   { lib: 'Hors piste',                 court: 'Hors piste',   couleur: '#E5482F' },
        signal_perdu: { lib: 'Signal perdu (> 2 min)',     court: 'Signal perdu', couleur: '#6E6C66' },
        minage:       { lib: 'Excavatrice en minage',      court: 'Minage',       couleur: '#FCD525' },
        attente:      { lib: 'Excavatrice en attente',     court: 'Attente',      couleur: '#B7B3A3' },
    };
    const ETATS_DT = ['chargement', 'mission', 'roulage', 'attente_loin', 'hors_piste', 'signal_perdu'];
    const TENEURS = { high: 'High Grade', medium: 'Medium Grade', low: 'Low Grade', marginal: 'Marginal Ore' };

    // --------------------------------------------------------- filtres ---
    const LS_FILTRES = 'oreTrackingFiltres';
    const LS_DIFFUSION = 'oreTrackingDiffusion';
    const FILTRES_DEFAUT = {
        type: 'tous', statut: 'tous', zone: 'toutes', shift: 'encours', date: '',
        teneurs: { high: true, medium: true, low: true, marginal: true },
    };
    const DIFFUSION_DEFAUT = { trajets: true, animations: true, mode: 'etendu', ecran: 'auto' };
    const RAYON_ZONE_M = 400;          // un engin « est dans » une zone a moins de 400 m de son point

    function lireJSON(cle, defaut) {
        let v = null;
        try { v = JSON.parse(localStorage.getItem(cle)); } catch (e) { v = null; }
        return Object.assign({}, defaut, v || {}, {
            teneurs: Object.assign({}, defaut.teneurs || {}, (v && v.teneurs) || {}),
        });
    }
    const lireFiltres = () => lireJSON(LS_FILTRES, FILTRES_DEFAUT);
    const ecrireFiltres = (f) => localStorage.setItem(LS_FILTRES, JSON.stringify(f));
    const lireDiffusion = () => { const d = lireJSON(LS_DIFFUSION, DIFFUSION_DEFAUT); delete d.teneurs; return d; };
    const ecrireDiffusion = (d) => localStorage.setItem(LS_DIFFUSION, JSON.stringify(d));

    const deux = (n) => String(n).padStart(2, '0');
    const jourLocal = (d) => d.getFullYear() + '-' + deux(d.getMonth() + 1) + '-' + deux(d.getDate());
    const horodatage = (d) => jourLocal(d) + ' ' + deux(d.getHours()) + ':' + deux(d.getMinutes()) + ':' + deux(d.getSeconds());

    /** Periode des trajets : shift jour 06h-18h, nuit 18h-06h (lendemain),
     *  journee 00h-24h ; « encours » = shift en cours, jusqu'a maintenant. */
    function periode(f) {
        const maintenant = new Date();
        let shift = f.shift || 'encours';
        let base = f.date ? new Date(f.date + 'T00:00:00') : new Date(maintenant);
        base.setHours(0, 0, 0, 0);
        if (shift === 'encours') {
            const h = maintenant.getHours();
            base = new Date(maintenant); base.setHours(0, 0, 0, 0);
            if (h < 6) { base.setDate(base.getDate() - 1); shift = 'nuit'; }
            else shift = h < 18 ? 'jour' : 'nuit';
        }
        const debut = new Date(base), fin = new Date(base);
        if (shift === 'jour') { debut.setHours(6); fin.setHours(18); }
        else if (shift === 'nuit') { debut.setHours(18); fin.setDate(fin.getDate() + 1); fin.setHours(6); }
        else { fin.setDate(fin.getDate() + 1); }
        const enDirect = fin > maintenant;
        return { depuis: horodatage(debut), jusqua: enDirect ? '' : horodatage(fin), enDirect,
                 libelle: (shift === 'jour' ? 'Shift jour' : shift === 'nuit' ? 'Shift nuit' : 'Journée') +
                          ' du ' + debut.toLocaleDateString('fr-FR') };
    }

    const normaliser = (t) => String(t || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    function distanceM(a, b) {
        const r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLng = (b.lng - a.lng) * r;
        const s = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLng / 2) ** 2;
        return 6371000 * 2 * Math.asin(Math.sqrt(s));
    }

    /** L'engin passe-t-il les filtres ? `zones` : zones de la carte (nom, lat, lng). */
    function correspond(e, f, zones) {
        if (f.type !== 'tous' && e.type !== f.type) return false;
        if (f.statut !== 'tous' && e.etat !== f.statut) return false;
        if (e.type === 'DT' && e.teneur && f.teneurs && f.teneurs[e.teneur] === false) return false;
        if (f.zone && f.zone !== 'toutes') {
            const z = (zones || []).find((x) => x.name === f.zone);
            if (!z) return true;
            const versZone = e.mission && normaliser(e.mission.dumping_area) === normaliser(z.name);
            if (!versZone && distanceM(e, z) > RAYON_ZONE_M) return false;
        }
        return true;
    }

    function filtresActifs(f) {
        const n = [];
        if (f.type !== 'tous') n.push(f.type === 'DT' ? 'DT' : 'Excavatrices');
        if (f.statut !== 'tous') n.push((ETATS[f.statut] || {}).court || f.statut);
        if (f.zone && f.zone !== 'toutes') n.push(f.zone);
        const off = Object.keys(TENEURS).filter((k) => f.teneurs && f.teneurs[k] === false);
        if (off.length) n.push('sans ' + off.map((k) => TENEURS[k]).join(', '));
        return n;
    }

    // ---------------------------------------------------- fond + decor ---
    const BASEMAPS = {
        'osm':       { nom: 'Plan · OpenStreetMap', url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', maxZoom: 19 },
        'esri-sat':  { nom: 'Vue satellite', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', maxZoom: 19 },
        'esri-topo': { nom: 'Vue topographique', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', maxZoom: 19 },
        'esri-dark': { nom: 'Vue sombre', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', maxZoom: 16 },
    };

    function convertCRS(x, y) {
        if (Math.abs(x) > 180 || Math.abs(y) > 90) {
            if (typeof proj4 !== 'undefined') {
                try { const ll = proj4('EPSG:32630', 'EPSG:4326', [x, y]); return { lat: ll[1], lng: ll[0] }; } catch (e) {}
            }
            return { lat: 0, lng: 0 };
        }
        return { lat: x, lng: y };
    }

    async function chargerZones(config) {
        if (config && config.zones && config.zones.length) return config.zones;
        try {
            const resp = await fetch('../data/Mining Areas.csv');
            if (resp.ok) {
                const lines = (await resp.text()).replace(/^﻿/, '').replace(/\r\n?/g, '\n').trim().split('\n');
                if (lines.length >= 2) {
                    const header = lines[0].toLowerCase();
                    const sep = header.includes('\t') ? '\t' : (header.includes(';') ? ';' : ',');
                    const cols = header.split(sep).map((c) => c.trim()).filter((c) => c);
                    const nameIdx = cols.findIndex((c) => c === 'name' || c === 'nom' || c === 'zone');
                    let xIdx = cols.findIndex((c) => c === 'x' || c === 'easting');
                    let yIdx = cols.findIndex((c) => c === 'y' || c === 'northing');
                    if (xIdx === -1 || yIdx === -1) { xIdx = 0; yIdx = 1; }
                    const zones = [];
                    for (let i = 1; i < lines.length; i++) {
                        const parts = lines[i].trim().split(sep).map((p) => p.trim());
                        const rx = parseFloat(parts[xIdx]), ry = parseFloat(parts[yIdx]);
                        if (!isFinite(rx) || !isFinite(ry)) continue;
                        const ll = convertCRS(rx, ry);
                        zones.push({ name: (nameIdx >= 0 && parts[nameIdx]) || 'Zone ' + i, lat: ll.lat, lng: ll.lng });
                    }
                    if (zones.length) return zones;
                }
            }
        } catch (e) { console.warn('[carte] zones CSV illisibles :', e); }
        return [];
    }

    async function chargerPistes(config) {
        if (config && config.trails && config.trails.length) return config.trails;
        try {
            const saved = JSON.parse(localStorage.getItem('oreTrackingTrails') || 'null');
            if (saved && saved.length) return saved;
        } catch (e) {}
        try {
            const resp = await fetch('../data/Trails.geojson.geojson');
            if (resp.ok) {
                const geo = await resp.json();
                return (geo.features || [])
                    .filter((f) => f.geometry && f.geometry.coordinates && f.geometry.coordinates.length >= 2)
                    .map((f) => ({
                        type: 'imported', name: (f.properties && f.properties.name) || 'Trail',
                        color: '#EE7A2A', useColor: true,
                        waypoints: f.geometry.coordinates.map((c) => [c[1], c[0]]),
                    }));
            }
        } catch (e) { console.warn('[carte] pistes GeoJSON illisibles :', e); }
        return [];
    }

    /** « West Pit · Main Pit · ROM Pads 1, 2 et 3 » */
    function resumeZones(zones) {
        const noms = zones.map((z) => z.name);
        const rom = /^rom\s*pad\s*\d+$/i;
        const roms = noms.filter((n) => rom.test(n)).map((n) => n.match(/\d+/)[0]);
        const autres = noms.filter((n) => !rom.test(n));
        if (roms.length > 1) autres.push('ROM Pads ' + roms.slice(0, -1).join(', ') + ' et ' + roms[roms.length - 1]);
        else if (roms.length) autres.push('ROM Pad ' + roms[0]);
        return autres.join(' · ') || 'Aucune zone configurée';
    }

    const PIN_SVG = '<svg class="pin" viewBox="0 0 28 36"><path d="M14 1C6.8 1 1 6.7 1 13.8 1 23 14 35 14 35s13-12 13-21.2C27 6.7 21.2 1 14 1z" fill="#0E74B2" stroke="#FCF9EA" stroke-width="1.5"/><circle cx="14" cy="13.5" r="5.2" fill="#FCF9EA"/></svg>';

    function iconeZone(z, labels) {
        const role = /pit|fosse/i.test(z.name) ? ['load', 'Chargement DT']
                   : /rom|pad|dump|stock/i.test(z.name) ? ['unload', 'Déchargement DT'] : null;
        const html = document.createElement('div');
        html.className = 'zone-pin';
        html.innerHTML = PIN_SVG + (labels
            ? '<span class="tags"><span class="name"></span>' +
              (role ? '<span class="role ' + role[0] + '">' + role[1] + '</span>' : '') + '</span>'
            : '');
        const nom = html.querySelector('.name');
        if (nom) nom.textContent = z.name;          // nom saisi : texte, jamais HTML
        return L.divIcon({ className: 'zone-marker', html, iconSize: [0, 0], iconAnchor: [0, 0] });
    }

    /**
     * Construit la carte dans `el` d'apres `config` (format PRESENTER de Live).
     * Rend { map, zones, segments, fond, centre, zoom, afficher(opts), detruire() }.
     */
    async function construire(el, config, options) {
        const opts = Object.assign({ zones: true, pistes: true, labels: true }, options || {});
        config = config || {};
        const centre = [(config.center && config.center.lat) || 8.2626, (config.center && config.center.lng) || -4.6301];
        const zoom = config.zoom || 14;
        const zones = await chargerZones(config);
        const pistes = await chargerPistes(config);

        const map = L.map(el, { center: centre, zoom, zoomControl: false, attributionControl: false });
        let fond;
        if (config.basemap === 'mbtiles' && config.mbtilesUrl) {
            L.tileLayer(config.mbtilesUrl.replace(/\/$/, '') + '/{z}/{x}/{y}.png', { maxZoom: 19, errorTileUrl: '' }).addTo(map);
            fond = 'Fond local · MBTiles';
        } else {
            const bm = BASEMAPS[config.basemap] || BASEMAPS['esri-sat'];
            L.tileLayer(bm.url, { crossOrigin: true, maxZoom: bm.maxZoom }).addTo(map);
            fond = bm.nom;
        }

        const mbtilesBase = (config.mbtilesUrl || 'http://localhost:8080').replace(/\/$/, '');
        [].concat(config.layers || [], config.tiffLayers || []).forEach((layer) => {
            if (!layer.visible) return;
            const op = layer.opacity || 0.8;
            if ((layer.type === 'tiff' || layer.type === 'georef' || !layer.type) && layer.dataUrl && layer.bounds) {
                L.imageOverlay(layer.dataUrl, layer.bounds, { opacity: op, interactive: false }).addTo(map);
            } else if (layer.type === 'geojson' && layer.geoJSON) {
                L.geoJSON(layer.geoJSON, {
                    style: () => ({ color: '#EE7A2A', weight: 2, opacity: op, fillColor: '#EE7A2A', fillOpacity: op * 0.35 }),
                    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, color: '#EE7A2A', weight: 2, fillOpacity: op * 0.6 }),
                }).addTo(map);
            } else if (layer.type === 'mbtiles' && layer.mbtilesName) {
                L.tileLayer(mbtilesBase + '/' + encodeURIComponent(layer.mbtilesName) + '/{z}/{x}/{y}.png',
                    { maxZoom: 19, errorTileUrl: '', opacity: op }).addTo(map);
            }
        });

        const couchePistes = L.layerGroup(), coucheZones = L.layerGroup();
        let segments = 0;
        pistes.forEach((t) => {
            let wp = (t.waypoints && t.waypoints.length >= 2) ? t.waypoints : null;
            if (!wp) {
                const a = zones.find((z) => z.name === t.from), b = zones.find((z) => z.name === t.to);
                if (!a || !b) return;
                wp = [[a.lat, a.lng], [b.lat, b.lng]];
            }
            segments += wp.length - 1;
            const couleur = t.useColor === false ? '#B7B3A3' : (t.color || '#EE7A2A');
            L.polyline(wp, { color: '#000', weight: 7, opacity: 0.35, interactive: false }).addTo(couchePistes);
            L.polyline(wp, { color: couleur, weight: 4, opacity: 0.9, interactive: false }).addTo(couchePistes);
            L.polyline(wp, { color: '#FCF9EA', weight: 1.2, opacity: 0.7, dashArray: '2 10', interactive: false }).addTo(couchePistes);
        });

        function dessinerZones(labels) {
            coucheZones.clearLayers();
            zones.forEach((z) => {
                if (z.lat && z.lng) L.marker([z.lat, z.lng], { icon: iconeZone(z, labels), zIndexOffset: 200 }).addTo(coucheZones);
            });
        }

        function afficher(o) {
            Object.assign(opts, o || {});
            if (opts.pistes) couchePistes.addTo(map); else map.removeLayer(couchePistes);
            dessinerZones(opts.labels);
            if (opts.zones) coucheZones.addTo(map); else map.removeLayer(coucheZones);
        }
        afficher();

        new ResizeObserver(() => map.invalidateSize()).observe(el);
        const majZoom = () => el.classList.toggle('z-low', map.getZoom() < 15);
        map.on('zoomend', majZoom); majZoom();

        return { map, zones, segments, fond, centre, zoom, afficher,
                 recentrer: () => map.setView(centre, zoom),
                 detruire: () => map.remove() };
    }

    // ------------------------------------------------------ suivi DT ---
    function iconeEngin(e, choisi) {
        const etat = ETATS[e.etat] || ETATS.roulage;
        const exca = e.type !== 'DT';
        const fleche = (!exca && e.cap_deg !== null && e.cap_deg !== undefined && e.vitesse_kmh >= 3)
            ? '<span class="trk-cap" style="transform:rotate(' + e.cap_deg + 'deg)"></span>' : '';
        return L.divIcon({
            className: 'trk-engin' + (exca ? ' exca' : '') + ' etat-' + e.etat + (choisi ? ' choisi' : ''),
            html: '<span class="trk-anneau" style="--c:' + etat.couleur + '"></span>' + fleche +
                  (e.etat === 'hors_piste' ? '<span class="trk-halo"></span>' : '') +
                  '<img class="trk-icone" alt="" src="../assets/' + (exca ? 'excavator.png' : 'icon DT.png') + '">',
            iconSize: [34, 34], iconAnchor: [17, 17],
        });
    }

    function texteEtiquette(e) {
        if (e.type !== 'DT') return e.code;
        if (e.etat === 'hors_piste') return e.code + ' · ' + e.offroad_distance + ' m hors piste';
        if (e.etat === 'signal_perdu') return e.code + ' · signal perdu';
        return e.code + (e.vitesse_kmh >= 3 ? ' · ' + Math.round(e.vitesse_kmh) + ' km/h' : '');
    }

    /**
     * Suivi en direct sur une carte construite par `construire`.
     * options : { labels, trajets, animations, intervalle_ms, onMaj(resume), onChoix(fiche|null) }
     */
    function suivre(carte, options) {
        const opts = Object.assign({ labels: true, trajets: true, animations: true, intervalle_ms: 2000 }, options || {});
        const map = carte.map;
        const marqueurs = {};               // code -> L.marker
        const coucheTrajets = L.layerGroup().addTo(map);
        let filtres = lireFiltres();
        let dernier = null;                 // derniere reponse /api/suivi_dt
        let choisi = null;                  // code suivi par la camera
        let minuteurs = [];
        let trajetsCache = { cle: '', data: {}, lu: 0 };

        function animer(m, cible) {
            const depart = m.getLatLng();
            if (!opts.animations || depart.distanceTo(cible) > 800) { m.setLatLng(cible); return; }
            const t0 = performance.now(), d = Math.min(opts.intervalle_ms, 2500);
            cancelAnimationFrame(m._raf);
            const pas = (t) => {
                const k = Math.min((t - t0) / d, 1);
                m.setLatLng([depart.lat + (cible[0] - depart.lat) * k, depart.lng + (cible[1] - depart.lng) * k]);
                if (choisi && m._code === choisi) map.panTo(m.getLatLng(), { animate: false });
                if (k < 1) m._raf = requestAnimationFrame(pas);
            };
            m._raf = requestAnimationFrame(pas);
        }

        function dessiner() {
            if (!dernier) return;
            const visibles = new Set();
            const compte = {};
            let dt = 0, dtVisibles = 0;
            dernier.engins.forEach((e) => {
                if (e.type === 'DT') { dt++; compte[e.etat] = (compte[e.etat] || 0) + 1; }
                if (!correspond(e, filtres, carte.zones)) return;
                if (e.type === 'DT') dtVisibles++;
                visibles.add(e.code);
                const cle = e.etat + '|' + (e.cap_deg || '') + '|' + (e.vitesse_kmh >= 3) + '|' + (choisi === e.code);
                let m = marqueurs[e.code];
                if (!m) {
                    m = L.marker([e.lat, e.lng], { zIndexOffset: e.type === 'DT' ? 600 : 400 }).addTo(map);
                    m._code = e.code;
                    m.on('click', () => choisir(choisi === e.code ? null : e.code));
                    marqueurs[e.code] = m;
                } else {
                    animer(m, [e.lat, e.lng]);
                }
                if (m._cle !== cle) {
                    m.setIcon(iconeEngin(e, choisi === e.code));
                    m.setZIndexOffset(e.etat === 'hors_piste' || choisi === e.code ? 1200 : e.type === 'DT' ? 600 : 400);
                    m._cle = cle;
                }
                const permanent = opts.labels || e.etat === 'hors_piste' || choisi === e.code;
                if (!m.getTooltip() || m._permanent !== permanent || m._etatTt !== e.etat) {
                    m.unbindTooltip();
                    m.bindTooltip('', { permanent, direction: 'top', offset: [0, -16],
                        className: 'trk-etiquette etat-' + e.etat });
                    m._permanent = permanent; m._etatTt = e.etat;
                }
                m.setTooltipContent(texteEtiquette(e));
                m._fiche = e;
            });
            Object.keys(marqueurs).forEach((code) => {
                if (!visibles.has(code)) { map.removeLayer(marqueurs[code]); delete marqueurs[code]; }
            });
            dessinerTrajets(visibles);
            if (opts.onMaj) opts.onMaj({ total: dt, affiches: dtVisibles, compte, engins: dernier.engins.length,
                                         visibles: Array.from(visibles), filtres, periode: periode(filtres) });
            if (choisi && opts.onChoix) {
                const f = dernier.engins.find((e) => e.code === choisi);
                opts.onChoix(f || null);
            }
        }

        function dessinerTrajets(visibles) {
            coucheTrajets.clearLayers();
            if (!opts.trajets) return;
            const data = trajetsCache.data || {};
            Object.keys(data).forEach((code) => {
                if (!visibles.has(code)) return;
                const pts = data[code];
                if (!pts || pts.length < 2) return;
                const fiche = dernier && dernier.engins.find((e) => e.code === code);
                const couleur = (ETATS[fiche ? fiche.etat : 'roulage'] || ETATS.roulage).couleur;
                L.polyline(pts, { color: '#000', weight: 5, opacity: 0.3, interactive: false }).addTo(coucheTrajets);
                L.polyline(pts, { color: couleur, weight: 2.5, opacity: choisi && choisi !== code ? 0.35 : 0.9,
                                  interactive: false }).addTo(coucheTrajets);
            });
        }

        function majPositions() {
            return fetch(PASSERELLE() + '/api/suivi_dt', { cache: 'no-store' })
                .then((r) => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
                .then((d) => { dernier = d; dessiner(); })
                .catch((e) => { if (opts.onErreur) opts.onErreur(e); });
        }

        function majTrajets(forcer) {
            if (!opts.trajets) return Promise.resolve();
            const p = periode(filtres);
            const cle = p.depuis + '|' + p.jusqua;
            // periode passee : lue une fois ; shift en cours : relue toutes les 15 s
            if (!forcer && cle === trajetsCache.cle && (!p.enDirect || Date.now() - trajetsCache.lu < 15000)) {
                return Promise.resolve();
            }
            const url = PASSERELLE() + '/api/suivi_dt?depuis=' + encodeURIComponent(p.depuis) +
                        (p.jusqua ? '&jusqua=' + encodeURIComponent(p.jusqua) : '');
            return fetch(url, { cache: 'no-store' })
                .then((r) => r.json())
                .then((d) => { trajetsCache = { cle, data: d.trajets || {}, lu: Date.now() }; dessiner(); })
                .catch(() => {});
        }

        function choisir(code) {
            choisi = code;
            Object.values(marqueurs).forEach((m) => { m._cle = null; });
            if (code && marqueurs[code]) map.panTo(marqueurs[code].getLatLng());
            if (!code && opts.onChoix) opts.onChoix(null);
            dessiner();
        }

        function demarrer() {
            majPositions(); majTrajets(true);
            minuteurs.push(setInterval(majPositions, opts.intervalle_ms));
            minuteurs.push(setInterval(() => majTrajets(false), 5000));
        }

        // reglages venus de Live (autre onglet) : appliques en direct
        function surStockage(ev) {
            if (ev.key === LS_FILTRES) appliquerFiltres(lireFiltres());
            if (ev.key === LS_DIFFUSION && opts.suivreDiffusion) {
                const d = lireDiffusion();
                reglages({ trajets: d.trajets, animations: d.animations });
            }
        }
        global.addEventListener('storage', surStockage);

        function appliquerFiltres(f) {
            filtres = Object.assign({}, FILTRES_DEFAUT, f);
            if (choisi && dernier) {
                const e = dernier.engins.find((x) => x.code === choisi);
                if (e && !correspond(e, filtres, carte.zones)) choisi = null;
            }
            majTrajets(true);
            dessiner();
        }

        function reglages(o) {
            Object.assign(opts, o || {});
            Object.values(marqueurs).forEach((m) => { m._permanent = null; });
            if (o && 'trajets' in o) majTrajets(true);
            dessiner();
        }

        demarrer();
        return {
            appliquerFiltres, reglages, choisir,
            filtres: () => filtres,
            etat: () => dernier,
            arreter: () => { minuteurs.forEach(clearInterval); global.removeEventListener('storage', surStockage); },
        };
    }

    global.CarteSuivi = {
        ETATS, ETATS_DT, TENEURS, FILTRES_DEFAUT, DIFFUSION_DEFAUT,
        lireFiltres, ecrireFiltres, lireDiffusion, ecrireDiffusion,
        periode, correspond, filtresActifs, resumeZones, construire, suivre,
    };
})(window);
