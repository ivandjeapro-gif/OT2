document.addEventListener('DOMContentLoaded', () => {

    // ===== PROJ4 EPSG DEFINITIONS =====
    if (typeof proj4 !== 'undefined') {
        if (!proj4.defs('EPSG:32630')) {
            proj4.defs('EPSG:32630', '+proj=utm +zone=30 +datum=WGS84 +units=m +no_defs +ellps=WGS84 +towgs84=0,0,0');
        }
        if (!proj4.defs('EPSG:32629')) {
            proj4.defs('EPSG:32629', '+proj=utm +zone=29 +datum=WGS84 +units=m +no_defs +ellps=WGS84 +towgs84=0,0,0');
        }
        if (!proj4.defs('EPSG:32631')) {
            proj4.defs('EPSG:32631', '+proj=utm +zone=31 +datum=WGS84 +units=m +no_defs +ellps=WGS84 +towgs84=0,0,0');
        }
    }

    // ===== NAVIGATION =====
    const sidebarLinks = document.querySelectorAll('.sidebar-link');
    const pages = document.querySelectorAll('.page');

    sidebarLinks.forEach(link => {
        link.addEventListener('click', () => {
            const page = link.dataset.page;
            sidebarLinks.forEach(l => l.classList.remove('active'));
            link.classList.add('active');
            pages.forEach(p => p.classList.remove('active'));
            document.getElementById(`page-${page}`).classList.add('active');
            closeSidebar();
        });
    });

    // ===== HAMBURGER MENU (MOBILE) =====
    const hamburgerBtn = document.getElementById('hamburgerBtn');
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebarOverlay');

    function openSidebar() {
        sidebar.classList.add('open');
        sidebarOverlay.classList.add('active');
    }
    function closeSidebar() {
        sidebar.classList.remove('open');
        sidebarOverlay.classList.remove('active');
    }

    if (hamburgerBtn) {
        hamburgerBtn.addEventListener('click', () => {
            if (sidebar.classList.contains('open')) closeSidebar();
            else openSidebar();
        });
    }
    if (sidebarOverlay) {
        sidebarOverlay.addEventListener('click', closeSidebar);
    }

    // ===== STATUS BAR CLOCK =====
    function updateClock() {
        const now = new Date();
        const days = ['Dimanche', 'Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi'];
        const months = ['Janvier', 'Fevrier', 'Mars', 'Avril', 'Mai', 'Juin',
                         'Juillet', 'Aout', 'Septembre', 'Octobre', 'Novembre', 'Decembre'];
        document.getElementById('statusDate').textContent = `${days[now.getDay()]} ${now.getDate()} ${months[now.getMonth()]}`;
        const h = String(now.getHours()).padStart(2, '0');
        const m = String(now.getMinutes()).padStart(2, '0');
        const s = String(now.getSeconds()).padStart(2, '0');
        document.getElementById('statusTime').textContent = `${h} h ${m} m ${s} S`;
    }
    updateClock();
    setInterval(updateClock, 1000);

    // ===== LAYERS CONFIG (TIFF / MBTiles / GeoJSON / Georef) =====
    let layers = [];

    async function saveLayers() {
        try {
            const toSave = [];
            for (const l of layers) {
                let imgData = l.imageData;
                if (l.type === 'georef' && imgData && !l._compressed) {
                    imgData = await compressImage(imgData, 800);
                    l._compressed = true;
                }
                toSave.push({
                    id: l.id, name: l.name, type: l.type, visible: l.visible, opacity: l.opacity,
                    dataUrl: l.dataUrl, bounds: l.bounds, geoJSON: l.geoJSON, mbtilesName: l.mbtilesName,
                    controlPoints: l.controlPoints,
                    imageData: l.type === 'georef' ? imgData : null,
                    imageWidth: l.imageWidth, imageHeight: l.imageHeight
                });
            }
            localStorage.setItem('oreTrackingLayers', JSON.stringify(toSave));
        } catch(e) {
            console.warn('Sauvegarde trop grosse, tentative sans images...');
            try {
                const slim = layers.map(l => ({
                    id: l.id, name: l.name, type: l.type, visible: l.visible, opacity: l.opacity,
                    dataUrl: l.dataUrl || null,
                    bounds: l.bounds, geoJSON: l.geoJSON || null, mbtilesName: l.mbtilesName || '',
                    controlPoints: l.controlPoints || [],
                    imageData: null,
                    imageWidth: l.imageWidth || 0, imageHeight: l.imageHeight || 0
                }));
                localStorage.setItem('oreTrackingLayers', JSON.stringify(slim));
            } catch(e2) {
                console.warn('Sauvegarde impossible');
            }
        }
    }

    async function restoreLayers() {
        try {
            const saved = JSON.parse(localStorage.getItem('oreTrackingLayers'));
            if (!saved || !saved.length) return;
            for (const s of saved) {
                const layer = {
                    id: s.id, name: s.name, type: s.type, visible: s.visible !== false,
                    opacity: s.opacity || 0.8,
                    dataUrl: s.dataUrl || null, bounds: s.bounds || null,
                    geoJSON: s.geoJSON || null, mbtilesName: s.mbtilesName || '',
                    controlPoints: s.controlPoints || [], imageData: s.imageData || null,
                    imageWidth: s.imageWidth || 0, imageHeight: s.imageHeight || 0,
                    status: '', loaded: false
                };
                if (layer.type === 'georef' && layer.controlPoints.length === 4) {
                    if (layer.dataUrl) {
                        layer.loaded = true;
                        layer.status = 'Image georeferencee OK';
                    } else if (layer.imageData) {
                        try {
                            layer.status = 'Rechargement...';
                            const img = await loadImage(layer.imageData);
                            const result = warpImage(img, layer.controlPoints, 1024);
                            if (result) {
                                layer.dataUrl = result.dataUrl;
                                layer.bounds = result.bounds;
                                layer.loaded = true;
                                layer.status = 'Image georeferencee OK';
                            }
                        } catch(e) {
                            layer.status = 'Re-georeferencer';
                            layer.loaded = false;
                        }
                    } else {
                        layer.status = 'Re-georeferencer';
                        layer.loaded = false;
                    }
                } else if (layer.type === 'mbtiles' && layer.mbtilesName) {
                    layer.loaded = true;
                    layer.status = 'Serveur: python serve_tiles.py';
                } else if (layer.type === 'tiff' && layer.dataUrl) {
                    layer.loaded = true;
                    layer.status = 'TIFF OK';
                } else if (layer.type === 'geojson' && layer.geoJSON) {
                    layer.loaded = true;
                    layer.status = 'GeoJSON OK';
                }
                layers.push(layer);
            }
            renderLayerList();
        } catch(e) { console.warn('Erreur restauration couches:', e); }
    }

    const LAYER_TYPES = [
        { id: 'tiff', label: 'TIFF / GeoTIFF', icon: 'fa-file-image', accept: '.tif,.tiff' },
        { id: 'geojson', label: 'GeoJSON', icon: 'fa-draw-polygon', accept: '.geojson,.json' },
        { id: 'mbtiles', label: 'MBTiles', icon: 'fa-layer-group', accept: '.mbtiles' },
        { id: 'georef', label: 'Image georeferencee', icon: 'fa-map', accept: '.jpg,.jpeg,.png,.bmp,.webp' }
    ];

    function layerTypeInfo(type) {
        return LAYER_TYPES.find(t => t.id === type) || LAYER_TYPES[0];
    }

    function addLayer(type) {
        layers.push({
            id: 'ly_' + Date.now() + '_' + Math.floor(Math.random() * 1e4),
            name: 'Couche ' + (layers.length + 1),
            type: type || 'geojson',
            visible: true,
            opacity: 0.8,
            dataUrl: null,
            bounds: null,
            geoJSON: null,
            mbtilesName: '',
            controlPoints: [],
            imageData: null,
            imageWidth: 0,
            imageHeight: 0,
            status: '',
            loaded: false
        });
        renderLayerList();
    }

    function removeLayer(idx) {
        layers.splice(idx, 1);
        saveLayers();
        renderLayerList();
    }

    function setLayerOpacity(idx, opacity) {
        layers[idx].opacity = opacity;
        saveLayers();
    }

    function renderLayerList() {
        const list = document.getElementById('layerList');
        if (!list) return;
        list.innerHTML = '';
        layers.forEach((layer, i) => {
            const t = layerTypeInfo(layer.type);
            const div = document.createElement('div');
            div.className = 'tiff-layer-item';
            div.innerHTML = `
                <div class="tiff-layer-row">
                    <select class="form-input-sm layer-type-select" title="Type de couche">
                        ${LAYER_TYPES.map(lt => `<option value="${lt.id}" ${lt.id === layer.type ? 'selected' : ''}>${lt.label}</option>`).join('')}
                    </select>
                    <input type="text" class="form-input-sm tiff-layer-name" value="${layer.name}" placeholder="Nom de la couche" />
                    <div class="form-check">
                        <input type="checkbox" id="vis-${i}" class="tiff-visible-check" ${layer.visible ? 'checked' : ''} />
                        <label for="vis-${i}">Visible</label>
                    </div>
                    <div class="tiff-layer-actions">
                        <button class="btn-icon-sm tiff-layer-zoom" title="Zoom sur la couche"><i class="fas fa-magnifying-glass-plus"></i></button>
                        <button class="btn-icon-sm tiff-layer-del" title="Retirer"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="tiff-layer-row">
                    <button class="btn-tiff tiff-layer-pick"><i class="fas ${t.icon}"></i> Charger ${t.label}</button>
                    <span class="tiff-status ${layer.loaded ? 'loaded' : ''}">${layer.status}</span>
                    <div class="tiff-layer-op">
                        <label>Opacite</label>
                        <input type="range" min="0" max="1" step="0.05" value="${layer.opacity}" class="tiff-opacity-slider" />
                        <span class="op-val">${Math.round(layer.opacity * 100)}%</span>
                    </div>
                </div>`;
            list.appendChild(div);

            const typeSelect = div.querySelector('.layer-type-select');
            typeSelect.addEventListener('change', () => {
                layer.type = typeSelect.value;
                layer.status = '';
                layer.loaded = false;
                saveLayers();
                renderLayerList();
            });

            const nameInput = div.querySelector('.tiff-layer-name');
            nameInput.addEventListener('input', (e) => { layer.name = e.target.value; saveLayers(); });

            const visInput = div.querySelector('.tiff-visible-check');
            visInput.addEventListener('change', () => { layer.visible = visInput.checked; saveLayers(); });

            div.querySelector('.tiff-layer-del').addEventListener('click', () => removeLayer(i));

            div.querySelector('.tiff-layer-zoom').addEventListener('click', () => onLayerZoom(i));

            div.querySelector('.tiff-layer-pick').addEventListener('click', () => {
                const fileInput = document.createElement('input');
                fileInput.type = 'file';
                fileInput.accept = t.accept;
                fileInput.addEventListener('change', () => {
                    if (fileInput.files[0]) loadLayerFile(i, fileInput.files[0]);
                });
                fileInput.click();
            });

            const opacitySlider = div.querySelector('.tiff-opacity-slider');
            opacitySlider.addEventListener('input', (e) => {
                setLayerOpacity(i, parseFloat(e.target.value));
                e.target.nextElementSibling.textContent = Math.round(e.target.value * 100) + '%';
            });
        });
        // Icon toggle global
        const icon = document.querySelector('#btnToggleAllLayers i');
        if (icon) {
            icon.className = layers.some(l => l.visible) ? 'fas fa-eye' : 'fas fa-eye-slash';
        }
    }

    // ===== LAYER LOAD / DISPATCH =====
    async function loadLayerFile(idx, file) {
        const layer = layers[idx];
        try {
            if (layer.type === 'tiff') {
                await loadTiffFile(layer, file);
            } else if (layer.type === 'geojson') {
                await loadGeoJSONFile(layer, file);
            } else if (layer.type === 'mbtiles') {
                await loadMbtilesFile(layer, file);
            } else if (layer.type === 'georef') {
                await loadGeorefFile(layer, file);
            }
        } catch (err) {
            layer.status = 'Erreur: ' + err.message;
            layer.loaded = false;
        }
        saveLayers();
        renderLayerList();
    }

    async function loadTiffFile(layer, file) {
        layer.status = 'Lecture du TIFF...';
        const tiff = await GeoTIFF.fromBlob(file);
        const image = await tiff.getImage();
        const epsg = typeof image.getEPSG === 'function' ? image.getEPSG() : undefined;
        let bbox = null;
        try { bbox = image.getBoundingBox(); } catch(e) {}
        if (!bbox) throw new Error('TIFF non geo-reference');

        let bounds;
        try {
            bounds = tiffBoundsToLatLng(bbox, epsg);
        } catch (e) {
            bounds = [ [bbox[1], bbox[0]], [bbox[3], bbox[2]] ];
        }

        const w = image.getWidth(), h = image.getHeight();
        const MAX = 2048;
        const scale = Math.min(1, MAX / Math.max(w, h));
        const outW = Math.max(1, Math.round(w * scale));
        const outH = Math.max(1, Math.round(h * scale));
        const rasters = await image.readRasters({ width: outW, height: outH, resampleMethod: 'bilinear' });
        const dataUrl = renderRastersToDataUrl(rasters, outW, outH);

        layer.name = file.name.replace(/\.(tif|tiff)$/i, '');
        layer.dataUrl = dataUrl;
        layer.bounds = bounds;
        layer.status = (outW < w ? (outW + 'x' + outH + 'px ') : '') + 'TIFF OK';
        layer.loaded = true;
    }

    async function loadGeoJSONFile(layer, file) {
        layer.status = 'Lecture du GeoJSON...';
        const text = await file.text();
        let geo;
        try { geo = JSON.parse(text); } catch (e) { throw new Error('JSON invalide'); }

        let fc;
        if (geo.type === 'FeatureCollection' || geo.type === 'Feature') {
            fc = geo;
        } else if (geo.type && geo.type.toLowerCase() === 'geometry') {
            fc = { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: geo }] };
        } else {
            throw new Error('GeoJSON invalide (Feature/FeatureCollection attendu)');
        }
        if (fc.type === 'Feature') fc = { type: 'FeatureCollection', features: [fc] };

        reprojectGeoJSON(fc);
        const count = (fc.features || []).length;
        layer.name = file.name.replace(/\.(geojson|json)$/i, '');
        layer.geoJSON = fc;
        layer.status = count + ' entite' + (count > 1 ? 's' : '') + ' importee' + (count > 1 ? 's' : '');
        layer.loaded = true;
    }

    // Reprojette les coordonnees projettees (selon la config EPSG) vers WGS84
    function reprojectGeoJSON(geo) {
        if (!geo || !geo.features) return;
        const epsg = 'EPSG:' + (document.getElementById('cfgEPSG').value || '32630');
        const def = (typeof proj4 !== 'undefined') ? proj4.defs(epsg) : null;
        if (!def) return;
        const projected = c => (Math.abs(c[0]) > 180 || Math.abs(c[1]) > 90);
        function walk(coords) {
            if (typeof coords[0] === 'number') {
                if (projected(coords)) {
                    const ll = proj4(epsg, 'EPSG:4326', coords);
                    coords[0] = ll[0]; coords[1] = ll[1];
                }
                return;
            }
            coords.forEach(sub => walk(sub));
        }
        geo.features.forEach(f => {
            if (f.geometry && f.geometry.coordinates) walk(f.geometry.coordinates);
        });
    }

    // MBTiles : seule la reference au fichier est transmise (lu par le serveur de tuiles local)
    async function loadMbtilesFile(layer, file) {
        layer.mbtilesName = file.name.replace(/\.mbtiles$/i, '');
        layer.name = file.name;
        layer.status = 'Serveur: python serve_tiles.py';
        layer.loaded = true;
    }

    // ===== IMAGE GEOREFERENCEE =====
    let georefState = null;

    function compressImage(dataUrl, maxSize) {
        return new Promise((resolve) => {
            const img = new Image();
            img.onload = () => {
                const canvas = document.createElement('canvas');
                let w = img.naturalWidth, h = img.naturalHeight;
                if (w > maxSize || h > maxSize) {
                    const scale = maxSize / Math.max(w, h);
                    w = Math.round(w * scale);
                    h = Math.round(h * scale);
                }
                canvas.width = w;
                canvas.height = h;
                canvas.getContext('2d').drawImage(img, 0, 0, w, h);
                resolve(canvas.toDataURL('image/jpeg', 0.6));
            };
            img.onerror = () => resolve(dataUrl);
            img.src = dataUrl;
        });
    }

    async function loadGeorefFile(layer, file) {
        const dataUrl = await fileToDataUrl(file);
        const img = await loadImage(dataUrl);
        layer.imageData = dataUrl;
        layer.imageWidth = img.naturalWidth;
        layer.imageHeight = img.naturalHeight;
        layer.name = file.name.replace(/\.(jpg|jpeg|png|bmp|webp)$/i, '');
        layer.controlPoints = [];
        layer.loaded = false;
        layer.status = '4 points de controle requis';
        openGeorefModal(layer, img);
    }

    function fileToDataUrl(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    function loadImage(src) {
        return new Promise((resolve, reject) => {
            const img = new Image();
            img.onload = () => resolve(img);
            img.onerror = () => reject(new Error('Image invalide'));
            img.src = src;
        });
    }

    // ---- Modal de georeferencement ----
    function openGeorefModal(layer, img) {
        const layerIdx = layers.indexOf(layer);
        const pts = layer.controlPoints.length ? [...layer.controlPoints] : [];

        // Calculer l'affichage de l'image (max 700px de large)
        const maxW = 700, maxH = 500;
        let dispW = img.naturalWidth, dispH = img.naturalHeight;
        if (dispW > maxW) { dispH *= maxW / dispW; dispW = maxW; }
        if (dispH > maxH) { dispW *= maxH / dispH; dispH = maxH; }

        const modal = document.createElement('div');
        modal.className = 'georef-modal';
        modal.innerHTML = `
        <div class="georef-backdrop"></div>
        <div class="georef-dialog">
            <div class="georef-header">
                <h3><i class="fas fa-map-pin"></i> Georeferencement — ${layer.name}</h3>
                <button class="georef-close" title="Fermer"><i class="fas fa-times"></i></button>
            </div>
            <div class="georef-body">
                <div class="georef-info">
                    Cliquez sur l'image pour placer un point, puis saisissez ses coordonnees GPS (Latitude, Longitude).
                    <br><small>4 points minimum. Les coordonnees en projection (UTM) sont automatiquement converties.</small>
                </div>
                <div class="georef-canvas-wrap" id="georefCanvasWrap">
                    <canvas class="georef-canvas" width="${Math.round(dispW)}" height="${Math.round(dispH)}"></canvas>
                </div>
                <div class="georef-points-list" id="georefPointsList"></div>
                <div class="georef-add-manual">
                    <button class="btn-tiff georef-add-pt-btn"><i class="fas fa-plus-circle"></i> Ajouter un point</button>
                </div>
                <div class="georef-input-row" id="georefInputRow" style="display:none">
                    <label>Point <span id="georefPtNum">1</span></label>
                    <div class="georef-fields">
                        <div><label>Pixel X</label><input type="number" id="georefPx" /></div>
                        <div><label>Pixel Y</label><input type="number" id="georefPy" /></div>
                        <div><label>Latitude</label><input type="text" id="georefLat" placeholder="ex: 8.2691" /></div>
                        <div><label>Longitude</label><input type="text" id="georefLng" placeholder="ex: -4.6312" /></div>
                    </div>
                    <button class="btn-tiff georef-confirm-pt"><i class="fas fa-check"></i> Valider le point</button>
                </div>
                <div class="georef-status" id="georefStatus">${pts.length}/4 points</div>
            </div>
            <div class="georef-footer">
                <button class="btn-tiff georef-undo" ${pts.length ? '' : 'disabled'}><i class="fas fa-undo"></i> Annuler dernier point</button>
                <button class="btn-confirm georef-apply" disabled><i class="fas fa-check-double"></i> Appliquer</button>
                <button class="btn-cancel georef-cancel">Annuler</button>
            </div>
        </div>`;

        document.body.appendChild(modal);

        const canvas = modal.querySelector('.georef-canvas');
        const ctx = canvas.getContext('2d');
        const scaleX = img.naturalWidth / dispW;
        const scaleY = img.naturalHeight / dispH;

        georefState = { layerIdx, modal, canvas, ctx, img, dispW, dispH, scaleX, scaleY, points: pts };

        // Dessiner l'image
        ctx.drawImage(img, 0, 0, dispW, dispH);
        drawGeorefPoints();

        // Events
        canvas.addEventListener('click', onGeorefCanvasClick);
        // Fallback: click sur le wrap aussi
        modal.querySelector('#georefCanvasWrap').addEventListener('click', (e) => {
            if (e.target === canvas) return; // deja gere
            onGeorefCanvasClick(e);
        });
        modal.querySelector('.georef-add-pt-btn').addEventListener('click', () => showGeorefInput());
        modal.querySelector('.georef-close').addEventListener('click', closeGeorefModal);
        modal.querySelector('.georef-cancel').addEventListener('click', closeGeorefModal);
        modal.querySelector('.georef-backdrop').addEventListener('click', closeGeorefModal);
        modal.querySelector('.georef-confirm-pt').addEventListener('click', onGeorefConfirmPoint);
        modal.querySelector('.georef-undo').addEventListener('click', onGeorefUndo);
        modal.querySelector('.georef-apply').addEventListener('click', onGeorefApply);

        // Enter dans les champs lat/lng valide le point
        modal.querySelector('#georefLat').addEventListener('keydown', e => { if (e.key === 'Enter') onGeorefConfirmPoint(); });
        modal.querySelector('#georefLng').addEventListener('keydown', e => { if (e.key === 'Enter') onGeorefConfirmPoint(); });
    }

    function closeGeorefModal() {
        if (georefState) {
            georefState.modal.remove();
            georefState = null;
        }
    }

    function onGeorefCanvasClick(e) {
        if (!georefState) return;
        const st = georefState;
        if (st.points.length >= 4) return;

        const rect = st.canvas.getBoundingClientRect();
        const dx = e.clientX - rect.left;
        const dy = e.clientY - rect.top;

        // Coords pixel sur l'image source
        const px = Math.round(dx * st.scaleX);
        const py = Math.round(dy * st.scaleY);

        showGeorefInput(px, py);

        // Dessiner le point temporaire
        drawGeorefPoints([{ px, py, temp: true }]);
    }

    function showGeorefInput(px, py) {
        if (!georefState) return;
        const st = georefState;
        if (st.points.length >= 4) return;

        const row = st.modal.querySelector('#georefInputRow');
        row.style.display = '';
        st.modal.querySelector('#georefPtNum').textContent = st.points.length + 1;
        st.modal.querySelector('#georefPx').value = (px !== undefined) ? px : '';
        st.modal.querySelector('#georefPy').value = (py !== undefined) ? py : '';
        st.modal.querySelector('#georefLat').value = '';
        st.modal.querySelector('#georefLng').value = '';
        st.modal.querySelector('#georefLat').focus();
    }

    function onGeorefConfirmPoint() {
        if (!georefState) return;
        const st = georefState;
        const px = parseInt(st.modal.querySelector('#georefPx').value);
        const py = parseInt(st.modal.querySelector('#georefPy').value);
        const lat = parseFloat(st.modal.querySelector('#georefLat').value.replace(',', '.'));
        const lng = parseFloat(st.modal.querySelector('#georefLng').value.replace(',', '.'));

        if (isNaN(lat) || isNaN(lng)) {
            alert('Coordonnees invalides. Saisissez Latitude et Longitude.');
            return;
        }

        // Reprojection si UTM (valeurs > 180 ou <-180)
        let finalLat = lat, finalLng = lng;
        if (Math.abs(lng) > 180 || Math.abs(lat) > 90) {
            const epsg = 'EPSG:' + (document.getElementById('cfgEPSG').value || '32630');
            if (typeof proj4 !== 'undefined' && proj4.defs(epsg)) {
                try {
                    const ll = proj4(epsg, 'EPSG:4326', [lng, lat]);
                    finalLng = ll[0]; finalLat = ll[1];
                } catch (e) { /*garder valeurs originales */ }
            }
        }

        st.points.push({ px, py, lat: finalLat, lng: finalLng });
        st.modal.querySelector('#georefInputRow').style.display = 'none';

        drawGeorefPoints();
        updateGeorefStatus();
    }

    function onGeorefUndo() {
        if (!georefState || !georefState.points.length) return;
        georefState.points.pop();
        drawGeorefPoints();
        updateGeorefStatus();
    }

    function drawGeorefPoints(tempPoints) {
        if (!georefState) return;
        const st = georefState;
        st.ctx.drawImage(st.img, 0, 0, st.dispW, st.dispH);

        const colors = ['#22c55e', '#f97316', '#3b82f6', '#ef4444'];
        const allPts = [...st.points, ...(tempPoints || [])];

        allPts.forEach((pt, i) => {
            const cx = pt.px / st.scaleX;
            const cy = pt.py / st.scaleY;

            st.ctx.beginPath();
            st.ctx.arc(cx, cy, 7, 0, Math.PI * 2);
            st.ctx.fillStyle = pt.temp ? '#ffffff88' : (colors[i] || '#fff');
            st.ctx.fill();
            st.ctx.strokeStyle = '#fff';
            st.ctx.lineWidth = 2;
            st.ctx.stroke();

            if (!pt.temp) {
                st.ctx.fillStyle = '#fff';
                st.ctx.font = 'bold 11px sans-serif';
                st.ctx.fillText((i + 1).toString(), cx + 10, cy - 5);
            }
        });

        // Dessiner les lignes entre les points valides
        if (st.points.length >= 2) {
            st.ctx.beginPath();
            st.ctx.strokeStyle = '#22c55e88';
            st.ctx.lineWidth = 1;
            st.ctx.setLineDash([4, 4]);
            st.points.forEach((pt, i) => {
                const cx = pt.px / st.scaleX, cy = pt.py / st.scaleY;
                if (i === 0) st.ctx.moveTo(cx, cy);
                else st.ctx.lineTo(cx, cy);
            });
            if (st.points.length === 4) st.ctx.closePath();
            st.ctx.stroke();
            st.ctx.setLineDash([]);
        }
    }

    function updateGeorefStatus() {
        if (!georefState) return;
        const st = georefState;
        const n = st.points.length;
        st.modal.querySelector('#georefStatus').textContent = n + '/4 points' + (n === 4 ? ' — pret !' : '');
        st.modal.querySelector('.georef-apply').disabled = (n < 4);
        st.modal.querySelector('.georef-undo').disabled = (n === 0);

        // Afficher la liste des points
        const list = st.modal.querySelector('#georefPointsList');
        list.innerHTML = st.points.map((pt, i) => {
            const colors = ['#22c55e', '#f97316', '#3b82f6', '#ef4444'];
            return `<div class="georef-pt-item" style="border-left: 3px solid ${colors[i]}">
                <span>P${i+1}: pixel(${pt.px}, ${pt.py}) → ${pt.lat.toFixed(6)}, ${pt.lng.toFixed(6)}</span>
            </div>`;
        }).join('');
    }

    // ---- Transformation perspective ----
    function computePerspectiveMatrix(src, dst) {
        // Resout l'equation de perspective: dst = H * src
        // src/dst: [{x,y}] de longueur 4
        // Retourne la matrice H 3x3 (tableau plat de 9 elements)
        const A = [], b = [];
        for (let i = 0; i < 4; i++) {
            const sx = src[i].x, sy = src[i].y;
            const dx = dst[i].x, dy = dst[i].y;
            A.push([sx, sy, 1, 0, 0, 0, -dx*sx, -dx*sy]);
            b.push(dx);
            A.push([0, 0, 0, sx, sy, 1, -dy*sx, -dy*sy]);
            b.push(dy);
        }
        const h = gaussElimination(A, b);
        return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1];
    }

    function gaussElimination(A, b) {
        const n = A.length;
        for (let i = 0; i < n; i++) A[i].push(b[i]);
        for (let col = 0; col < n; col++) {
            let maxRow = col;
            for (let row = col + 1; row < n; row++) {
                if (Math.abs(A[row][col]) > Math.abs(A[maxRow][col])) maxRow = row;
            }
            [A[col], A[maxRow]] = [A[maxRow], A[col]];
            if (Math.abs(A[col][col]) < 1e-10) continue;
            for (let row = col + 1; row < n; row++) {
                const f = A[row][col] / A[col][col];
                for (let j = col; j <= n; j++) A[row][j] -= f * A[col][j];
            }
        }
        const x = new Array(n).fill(0);
        for (let i = n - 1; i >= 0; i--) {
            x[i] = A[i][n];
            for (let j = i + 1; j < n; j++) x[i] -= A[i][j] * x[j];
            x[i] /= A[i][i];
        }
        return x;
    }

    function warpImage(img, points, maxOutputSize) {
        // points: [{px, py, lat, lng}] (4 points)
        // Transforme l'image source vers les coords geographiques
        // Retourne {dataUrl, bounds: [[lat,lng],[lat,lng]]}

        // Projeter les lat/lng en metres (pseudo-projection centree)
        const centerLng = points.reduce((s, p) => s + p.lng, 0) / 4;
        const centerLat = points.reduce((s, p) => s + p.lat, 0) / 4;
        const metersPerDegLat = 111320;
        const metersPerDegLng = 111320 * Math.cos(centerLat * Math.PI / 180);

        function lngToM(lng) { return (lng - centerLng) * metersPerDegLng; }
        function latToM(lat) { return (lat - centerLat) * metersPerDegLat; }

        // Source: pixels image, Destination: metres
        const src = points.map(p => ({ x: p.px, y: p.py }));
        const dst = points.map(p => ({ x: lngToM(p.lng), y: latToM(p.lat) }));

        // Matrice de transformation pixel -> metres
        const H = computePerspectiveMatrix(src, dst);
        const Hinv = invertMatrix3x3(H);

        // Bounds en metres -> lat/lng
        const corners = [
            transformPoint(H, 0, 0),
            transformPoint(H, img.naturalWidth, 0),
            transformPoint(H, 0, img.naturalHeight),
            transformPoint(H, img.naturalWidth, img.naturalHeight)
        ];
        const mnx = Math.min(...corners.map(c => c.x));
        const mxx = Math.max(...corners.map(c => c.x));
        const mny = Math.min(...corners.map(c => c.y));
        const mxy = Math.max(...corners.map(c => c.y));

        const bounds = [
            [centerLat + mny / metersPerDegLat, centerLng + mnx / metersPerDegLng],
            [centerLat + mxy / metersPerDegLat, centerLng + mxx / metersPerDegLng]
        ];

        // Dimensions de sortie
        let outW = Math.round(mxx - mnx);
        let outH = Math.round(mxy - mny);
        if (outW <= 0 || outH <= 0) return null;
        const maxDim = maxOutputSize || 2048;
        if (outW > maxDim || outH > maxDim) {
            const scale = maxDim / Math.max(outW, outH);
            outW = Math.round(outW * scale);
            outH = Math.round(outH * scale);
        }

        // Canvas de sortie
        const canvas = document.createElement('canvas');
        canvas.width = outW;
        canvas.height = outH;
        const ctx = canvas.getContext('2d');
        const imgData = ctx.createImageData(outW, outH);
        const pixels = imgData.data;

        // Source image -> pixels
        const srcCanvas = document.createElement('canvas');
        srcCanvas.width = img.naturalWidth;
        srcCanvas.height = img.naturalHeight;
        const srcCtx = srcCanvas.getContext('2d');
        srcCtx.drawImage(img, 0, 0);
        const srcData = srcCtx.getImageData(0, 0, img.naturalWidth, img.naturalHeight);
        const srcPixels = srcData.data;
        const srcW = img.naturalWidth, srcH = img.naturalHeight;

        // Warp: pour chaque pixel de sortie, trouver le pixel source
        for (let oy = 0; oy < outH; oy++) {
            for (let ox = 0; ox < outW; ox++) {
                // Coords metres du pixel de sortie
                const mx = mnx + (ox / outW) * (mxx - mnx);
                const my = mny + (oy / outH) * (mxy - mny);

                // Vers pixel source via Hinv
                const sp = transformPoint(Hinv, mx, my);
                const sx = Math.round(sp.x);
                const sy = Math.round(sp.y);

                if (sx >= 0 && sx < srcW && sy >= 0 && sy < srcH) {
                    const si = (sy * srcW + sx) * 4;
                    const di = (oy * outW + ox) * 4;
                    pixels[di] = srcPixels[si];
                    pixels[di + 1] = srcPixels[si + 1];
                    pixels[di + 2] = srcPixels[si + 2];
                    pixels[di + 3] = srcPixels[si + 3];
                }
            }
        }

        ctx.putImageData(imgData, 0, 0);
        return { dataUrl: canvas.toDataURL('image/png'), bounds };
    }

    function transformPoint(H, x, y) {
        const w = H[6] * x + H[7] * y + H[8];
        return {
            x: (H[0] * x + H[1] * y + H[2]) / w,
            y: (H[3] * x + H[4] * y + H[5]) / w
        };
    }

    function invertMatrix3x3(m) {
        const [a, b, c, d, e, f, g, h, i] = m;
        const det = a*(e*i - f*h) - b*(d*i - f*g) + c*(d*h - e*g);
        if (Math.abs(det) < 1e-10) return m;
        const inv = 1 / det;
        return [
            (e*i - f*h)*inv, (c*h - b*i)*inv, (b*f - c*e)*inv,
            (f*g - d*i)*inv, (a*i - c*g)*inv, (c*d - a*f)*inv,
            (d*h - e*g)*inv, (b*g - a*h)*inv, (a*e - b*d)*inv
        ];
    }

    // ---- Appliquer le georeferencement ----
    function onGeorefApply() {
        if (!georefState || georefState.points.length < 4) return;
        const st = georefState;
        const layer = layers[st.layerIdx];

        layer.controlPoints = st.points.map(p => ({ px: p.px, py: p.py, lat: p.lat, lng: p.lng }));
        layer.status = 'Warp en cours...';
        renderLayerList();

        // Warp dans un timeout pour liberer le modal
        setTimeout(() => {
            try {
                const result = warpImage(st.img, st.points, 1024);
                if (result) {
                    layer.dataUrl = result.dataUrl;
                    layer.bounds = result.bounds;
                    layer.loaded = true;
                    layer.status = 'Image georeferencee OK';
                } else {
                    layer.status = 'Erreur: zone invalide';
                    layer.loaded = false;
                }
            } catch (err) {
                layer.status = 'Erreur: ' + err.message;
                layer.loaded = false;
            }
            closeGeorefModal();
            saveLayers();
            renderLayerList();
        }, 50);
    }

    function onLayerZoom(i) {
        const layer = layers[i];
        if (typeof map === 'undefined') return;
        if ((layer.type === 'tiff' || layer.type === 'georef') && layer.bounds) {
            map.fitBounds(layer.bounds);
        } else if (layer.type === 'geojson' && layer.geoJSON) {
            try {
                const gj = L.geoJSON(layer.geoJSON);
                if (gj.getBounds && gj.getBounds().isValid()) map.fitBounds(gj.getBounds());
            } catch (e) { /* ignore */ }
        }
    }

    function tiffBoundsToLatLng(bbox, epsgCode) {
        let sw = [bbox[0], bbox[1]];
        let ne = [bbox[2], bbox[3]];
        if (epsgCode && epsgCode !== 4326 && typeof proj4 !== 'undefined') {
            const from = 'EPSG:' + epsgCode;
            try { if (proj4.defs(from)) { sw = proj4(from, 'EPSG:4326', sw); ne = proj4(from, 'EPSG:4326', ne); } } catch(e) {}
        }
        return [[sw[1], sw[0]], [ne[1], ne[0]]];
    }

    function renderRastersToDataUrl(rasters, outW, outH) {
        const canvas = document.createElement('canvas');
        canvas.width = outW; canvas.height = outH;
        const ctx = canvas.getContext('2d');
        const imgData = ctx.createImageData(outW, outH);
        const n = outW * outH, spp = rasters.length;
        const D0 = rasters[0];
        const needsStretch = rasters.some(r => !(r instanceof Uint8Array));
        let lo = 0, hi = 255, span = 0;
        if (needsStretch) {
            let sum = 0, sum2 = 0, cnt = 0;
            for (let i = 0; i < n; i += 2) { const v = Number(D0[i]); if (isFinite(v)) { sum += v; sum2 += v*v; cnt++; } }
            if (cnt) { const mean = sum/cnt; const std = Math.sqrt(Math.max(0, sum2/cnt - mean*mean)); lo = mean - 2*std; hi = mean + 2*std; if (hi<=lo){lo=-1;hi=1;} span = hi-lo; }
        }
        function cv(v) { v = Number(v); if (!isFinite(v)) return 0; if (span>0) v=(v-lo)/span*255; return v<0?0:(v>255?255:Math.round(v)); }
        function raw(v) { v=Math.round(Number(v)); return v<0?0:(v>255?255:v); }
        const fn = needsStretch ? cv : raw;
        if (spp >= 3) {
            const R=rasters[0],G=rasters[1],B=rasters[2],A=spp>=4?rasters[3]:null;
            for (let i=0;i<n;i++) { const az=A&&A[i]===0; imgData.data[i*4]=az?0:fn(R[i]); imgData.data[i*4+1]=az?0:fn(G[i]); imgData.data[i*4+2]=az?0:fn(B[i]); imgData.data[i*4+3]=A?raw(A[i]):255; }
        } else { for (let i=0;i<n;i++) { const g=fn(D0[i]); imgData.data[i*4]=g; imgData.data[i*4+1]=g; imgData.data[i*4+2]=g; imgData.data[i*4+3]=255; } }
        ctx.putImageData(imgData, 0, 0);
        return canvas.toDataURL('image/png');
    }

    // ===== LOCK / UNLOCK =====
    const mapConfig = document.getElementById('mapConfig');
    const btnUnlock = document.getElementById('btnUnlockMap');
    const btnConfirm = document.getElementById('btnConfirmMap');
    const btnCancel = document.getElementById('btnCancelMap');

    function setMapConfigLocked(locked) {
        if (locked) {
            mapConfig.classList.add('locked');
            btnUnlock.style.display = '';
            btnConfirm.style.display = 'none';
            btnCancel.style.display = 'none';
        } else {
            mapConfig.classList.remove('locked');
            btnUnlock.style.display = 'none';
            btnConfirm.style.display = '';
            btnCancel.style.display = '';
        }
    }

    if (btnUnlock) btnUnlock.addEventListener('click', () => setMapConfigLocked(false));
    if (btnConfirm) btnConfirm.addEventListener('click', () => setMapConfigLocked(true));
    if (btnCancel) btnCancel.addEventListener('click', () => setMapConfigLocked(true));
    setMapConfigLocked(true);

    // Restaurer les couches sauvegardees
    restoreLayers();

    // ===== TOGGLE ALL LAYERS =====
    document.getElementById('btnToggleAllLayers').addEventListener('click', () => {
        const anyVisible = layers.some(l => l.visible);
        const newState = !anyVisible;
        layers.forEach(l => l.visible = newState);
        saveLayers();
        renderLayerList();
        const icon = document.querySelector('#btnToggleAllLayers i');
        icon.className = newState ? 'fas fa-eye' : 'fas fa-eye-slash';
    });

    // ===== ADD LAYER BUTTON =====
    document.getElementById('btnAddLayer').addEventListener('click', () => {
        addLayer();
    });

    // ===== DIAG LAYERS BUTTON =====
    document.getElementById('btnDiagLayers').addEventListener('click', () => {
        const saved = localStorage.getItem('oreTrackingConfig');
        const savedConfig = saved ? JSON.parse(saved) : null;
        const msg = [
            '=== COUCHES EN MEMOIRE ===',
            'layers[] (' + layers.length + '):',
            ...layers.map((l,i) => '  ['+i+'] type='+l.type+' name="'+l.name+'" mbtilesName="'+l.mbtilesName+'" visible='+l.visible+' loaded='+l.loaded+' status="'+l.status+'"'),
            '',
            '=== CONFIG SAUVEGARDEE ===',
            savedConfig ? 'layers: ' + JSON.stringify(savedConfig.layers || [], null, 2) : 'AUCUNE CONFIG',
            '',
            'mbtilesUrl: ' + (savedConfig && savedConfig.mbtilesUrl ? savedConfig.mbtilesUrl : '(vide)')
        ].join('\n');
        alert(msg);
    });

    // ===== MBTILES PANEL =====
    const cfgBasemap = document.getElementById('cfgBasemap');
    const mbtilesPanel = document.getElementById('mbtilesPanel');
    const mbtilesFileInput = document.getElementById('mbtilesFileInput');
    const cfgMbtilesFile = document.getElementById('cfgMbtilesFile');
    const cfgMbtilesUrl = document.getElementById('cfgMbtilesUrl');
    const mbtilesStatusDot = document.getElementById('mbtilesStatusDot');
    const mbtilesCmd = document.getElementById('mbtilesCmd');
    let mbtilesFileName = '';

    function toggleMbtilesPanel() {
        mbtilesPanel.style.display = cfgBasemap.value === 'mbtiles' ? '' : 'none';
    }
    cfgBasemap.addEventListener('change', toggleMbtilesPanel);
    toggleMbtilesPanel();

    // File picker
    document.getElementById('btnPickMbtiles').addEventListener('click', () => mbtilesFileInput.click());
    mbtilesFileInput.addEventListener('change', () => {
        const file = mbtilesFileInput.files[0];
        if (file) {
            mbtilesFileName = file.name;
            cfgMbtilesFile.value = file.name;
            mbtilesCmd.textContent = 'python serve_tiles.py';
        }
        mbtilesFileInput.value = '';
    });

    // Test server connection
    document.getElementById('btnTestMbtiles').addEventListener('click', async () => {
        const url = cfgMbtilesUrl.value.replace(/\/$/, '') + '/list';
        mbtilesStatusDot.className = 'mbtiles-status-dot testing';
        try {
            const resp = await fetch(url, { method: 'HEAD', mode: 'no-cors' });
            mbtilesStatusDot.className = 'mbtiles-status-dot online';
        } catch (e) {
            mbtilesStatusDot.className = 'mbtiles-status-dot offline';
        }
    });

    // Copy command
    document.getElementById('btnCopyCmd').addEventListener('click', () => {
        navigator.clipboard.writeText(mbtilesCmd.textContent).catch(() => {});
    });

    // ===== ZONE MANAGEMENT =====
    const zoneList = document.getElementById('zoneList');
    const trailList = document.getElementById('trailList');
    const cfgCRS = document.getElementById('cfgCRS');
    const utmGroup = document.getElementById('utmGroup');
    const cfgEPSG = document.getElementById('cfgEPSG');

    function toFloat(v) {
        const n = parseFloat(String(v).replace(/,/g, '.'));
        return isFinite(n) ? n : NaN;
    }

    function convertCRS(x, y) {
        const isUTMmode = cfgCRS.value === 'utm';
        const isLikelyUTM = isUTMmode || Math.abs(x) > 90 || Math.abs(y) > 180;
        if (isLikelyUTM) {
            const epsg = 'EPSG:' + cfgEPSG.value;
            if (typeof proj4 !== 'undefined' && proj4.defs(epsg)) {
                try {
                    const ll = proj4(epsg, 'EPSG:4326', [x, y]);
                    return { lat: ll[1], lng: ll[0] };
                } catch(e) { console.warn('Erreur conversion CRS:', e); }
            }
        }
        return { lat: x, lng: y };
    }

    function onCRSChange() {
        const isUTM = cfgCRS.value === 'utm';
        utmGroup.style.display = isUTM ? '' : 'none';
        document.getElementById('cfgCenterLatLabel').textContent = isUTM ? 'Easting' : 'Latitude';
        document.getElementById('cfgCenterLngLabel').textContent = isUTM ? 'Northing' : 'Longitude';
        const ph = isUTM ? 'Easting' : 'Latitude';
        const ph2 = isUTM ? 'Northing' : 'Longitude';
        document.querySelectorAll('.zone-lat').forEach(el => el.placeholder = ph);
        document.querySelectorAll('.zone-lng').forEach(el => el.placeholder = ph2);
    }

    cfgCRS.addEventListener('change', onCRSChange);

    function getZones() {
        const zones = [];
        zoneList.querySelectorAll('.zone-item').forEach(item => {
            const rawX = toFloat(item.querySelector('.zone-lat').value);
            const rawY = toFloat(item.querySelector('.zone-lng').value);
            const ll = convertCRS(rawX, rawY);
            zones.push({
                name: item.querySelector('.zone-name').value,
                lat: ll.lat,
                lng: ll.lng,
                color: item.querySelector('.zone-color').value
            });
        });
        return zones;
    }

    // ===== IMPORTED TRAILS =====
    let importedTrails = [];

    function saveImportedTrails() {
        try { localStorage.setItem('oreTrackingTrails', JSON.stringify(importedTrails)); } catch(e) {}
    }

    function restoreImportedTrails() {
        try {
            const saved = JSON.parse(localStorage.getItem('oreTrackingTrails'));
            if (saved && saved.length) {
                importedTrails = saved.filter(t => t && t.waypoints && t.waypoints.length >= 2);
            }
        } catch(e) {}
        updateImportedTrailList();
    }

    function getTrails() {
        const trails = [];
        trailList.querySelectorAll('.trail-item').forEach(item => {
            trails.push({
                type: 'manual',
                from: item.querySelector('.trail-from').value,
                to: item.querySelector('.trail-to').value,
                color: item.querySelector('.trail-color').value
            });
        });
        importedTrails.forEach(t => {
            trails.push({
                type: 'imported',
                name: t.name,
                color: t.useColor === false ? DEFAULT_TRAIL_COLOR : t.color,
                useColor: t.useColor !== false,
                waypoints: t.waypoints
            });
        });
        return trails;
    }

    function addZone(name = 'Nouvelle zone', lat = 320460, lng = 913696, color = 'blue') {
        const div = document.createElement('div');
        div.className = 'zone-item';
        div.innerHTML = `
            <input type="text" class="form-input-sm zone-name" value="${name}" />
            <input type="text" inputmode="decimal" class="form-input-sm zone-lat" value="${lat}" placeholder="${cfgCRS.value === 'utm' ? 'Easting' : 'Latitude'}" />
            <input type="text" inputmode="decimal" class="form-input-sm zone-lng" value="${lng}" placeholder="${cfgCRS.value === 'utm' ? 'Northing' : 'Longitude'}" />
            <select class="form-select-sm zone-color">
                <option value="blue" ${color === 'blue' ? 'selected' : ''}>Bleu</option>
                <option value="green" ${color === 'green' ? 'selected' : ''}>Vert</option>
                <option value="yellow" ${color === 'yellow' ? 'selected' : ''}>Jaune</option>
                <option value="orange" ${color === 'orange' ? 'selected' : ''}>Orange</option>
            </select>
            <button class="btn-icon-sm zone-del"><i class="fas fa-times"></i></button>
        `;
        zoneList.appendChild(div);
    }

    function addTrail(from = 'Main Pit', to = 'ROM Pad 1', color = '#f97316') {
        const div = document.createElement('div');
        div.className = 'trail-item';
        div.innerHTML = `
            <select class="form-select-sm trail-from">${getZones().map(z => `<option ${z.name===from?'selected':''}>${z.name}</option>`).join('')}</select>
            <i class="fas fa-arrow-right trail-arrow"></i>
            <select class="form-select-sm trail-to">${getZones().map(z => `<option ${z.name===to?'selected':''}>${z.name}</option>`).join('')}</select>
            <input type="color" class="trail-color" value="${color}" />
            <button class="btn-icon-sm trail-del"><i class="fas fa-times"></i></button>
        `;
        trailList.appendChild(div);
    }

    document.getElementById('btnAddZone').addEventListener('click', () => addZone());
    document.getElementById('btnAddTrail').addEventListener('click', () => addTrail());

    // ===== ZONE CSV IMPORT =====
    const zoneFileInput = document.getElementById('zoneFileInput');
    document.getElementById('btnImportZones').addEventListener('click', () => zoneFileInput.click());
    zoneFileInput.addEventListener('change', () => {
        const file = zoneFileInput.files[0];
        if (file) importZonesCSV(file);
        zoneFileInput.value = '';
    });

    function importZonesCSV(file) {
        const reader = new FileReader();
        reader.onload = (e) => {
            try {
                const text = e.target.result.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim();
                const lines = text.split('\n');
                if (lines.length < 2) { showToast('Fichier CSV vide ou sans donnees', true); return; }

                const header = lines[0].toLowerCase();
                const sep = header.includes('\t') ? '\t' : (header.includes(';') ? ';' : ',');
                const cols = header.split(sep).map(c => c.trim()).filter(c => c);

                let nameIdx = cols.findIndex(c => c === 'name' || c === 'nom' || c === 'zone');
                let latIdx = cols.findIndex(c => c === 'lat' || c === 'latitude' || c === 'latidude' || c === 'y');
                let lngIdx = cols.findIndex(c => c === 'lng' || c === 'lon' || c === 'long' || c === 'longitude' || c === 'x' || c === 'ing');
                let colorIdx = cols.findIndex(c => c === 'color' || c === 'couleur');

                console.log('CSV cols:', cols, 'lat:', latIdx, 'lng:', lngIdx, 'sep:', sep === '\t' ? 'TAB' : sep);

                if (latIdx === -1 || lngIdx === -1) { latIdx = 0; lngIdx = 1; }

                const colorMap = {
                    'bleu': 'blue', 'blue': 'blue',
                    'vert': 'green', 'green': 'green',
                    'jaune': 'yellow', 'yellow': 'yellow',
                    'orange': 'orange',
                    'rouge': 'blue', 'red': 'blue',
                    'marron': 'orange', 'brown': 'orange',
                    'gris': 'blue', 'gray': 'blue', 'grey': 'blue'
                };

                let count = 0;
                for (let i = 1; i < lines.length; i++) {
                    const line = lines[i].trim();
                    if (!line) continue;
                    const parts = line.split(sep).map(p => p.trim());
                    const rawLat = parseFloat(parts[latIdx]);
                    const rawLng = parseFloat(parts[lngIdx]);
                    if (!isFinite(rawLat) || !isFinite(rawLng)) continue;

                    const isUTM = cfgCRS.value === 'utm' || Math.abs(rawLat) > 180 || Math.abs(rawLng) > 180;
                    const x = isUTM ? rawLng : rawLat;
                    const y = isUTM ? rawLat : rawLng;

                    const name = (nameIdx >= 0 && parts[nameIdx]) ? parts[nameIdx] : 'Zone ' + i;
                    const colorRaw = (colorIdx >= 0 && parts[colorIdx]) ? parts[colorIdx].toLowerCase().trim() : 'blue';
                    const color = colorMap[colorRaw] || 'blue';

                    addZone(name, x, y, color);
                    count++;
                }
                if (count === 0) {
                    showToast('Aucune zone valide. En-tetes: ' + cols.join(', '), true);
                } else {
                    showToast(count + ' zone' + (count > 1 ? 's' : '') + ' importee' + (count > 1 ? 's' : '') + ' (' + file.name + ')');
                }
            } catch(err) {
                showToast('Erreur CSV: ' + err.message, true);
            }
        };
        reader.readAsText(file);
    }

    function showToast(msg, isError) {
        const t = document.createElement('div');
        t.className = 'toast-msg' + (isError ? ' toast-error' : '');
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(() => t.classList.add('show'), 10);
        setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 300); }, 3000);
    }

    zoneList.addEventListener('click', (e) => {
        const del = e.target.closest('.zone-del');
        if (del) del.closest('.zone-item').remove();
    });
    trailList.addEventListener('click', (e) => {
        const del = e.target.closest('.trail-del');
        if (del) del.closest('.trail-item').remove();
    });

    // ===== TRAIL MODE SWITCHING =====
    const trailModeTabs = document.querySelectorAll('.trail-mode-tab');
    const trailModePanels = document.querySelectorAll('.trail-mode-panel');
    trailModeTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            trailModeTabs.forEach(t => t.classList.remove('active'));
            trailModePanels.forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(tab.dataset.mode === 'manual' ? 'trailModeManual' : 'trailModeImport').classList.add('active');
        });
    });

    // ===== TRAIL FILE IMPORT =====
    const trailDropZone = document.getElementById('trailDropZone');
    const trailFileInput = document.getElementById('trailFileInput');
    const trailImportList = document.getElementById('trailImportList');

    trailDropZone.addEventListener('click', () => trailFileInput.click());
    trailDropZone.addEventListener('dragover', (e) => { e.preventDefault(); trailDropZone.classList.add('dragover'); });
    trailDropZone.addEventListener('dragleave', () => trailDropZone.classList.remove('dragover'));
    trailDropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        trailDropZone.classList.remove('dragover');
        if (e.dataTransfer.files[0]) parseTrailFile(e.dataTransfer.files[0]);
    });
    trailFileInput.addEventListener('change', () => {
        if (trailFileInput.files[0]) parseTrailFile(trailFileInput.files[0]);
    });

    const TRAIL_COLORS = ['#f97316','#22d3ee','#a3e635','#fbbf24','#34d399','#f472b6','#818cf8','#fb923c'];
    const DEFAULT_TRAIL_COLOR = '#94a3b8';

    async function parseTrailFile(file) {
        const ext = file.name.split('.').pop().toLowerCase();
        try {
            if (ext === 'geojson' || ext === 'json') {
                const text = await file.text();
                if (!text.trim()) {
                    showToast('Fichier vide: ' + file.name, true);
                } else {
                    let geo = null;
                    try { geo = JSON.parse(text); }
                    catch(e) { showToast('GeoJSON invalide (JSON illisible): ' + file.name, true); }
                    if (geo) parseGeoJSON(geo, file.name);
                }
            } else if (ext === 'csv') {
                const text = await file.text();
                parseCSV(text, file.name);
            } else {
                showToast('Format non supporte (.geojson/.json/.csv): ' + file.name, true);
            }
        } catch (err) {
            console.warn('Erreur import trail:', err);
            showToast('Erreur import: ' + err.message, true);
        }
        trailFileInput.value = '';
    }

    // Convertit une coordonnee GeoJSON [x, y] en [lat, lng].
    // WGS84 : [lng, lat] ; UTM (valeurs > 180) : [easting, northing] -> conversion proj4
    function geoJsonCoordToLatLng(c) {
        if (!Array.isArray(c) || c.length < 2) return null;
        const x = Number(c[0]), y = Number(c[1]);
        if (!isFinite(x) || !isFinite(y)) return null;
        if (Math.abs(x) > 180 || Math.abs(y) > 180) {
            const ll = convertCRS(x, y); // x = easting, y = northing
            return [ll.lat, ll.lng];
        }
        return [y, x];
    }

    function parseGeoJSON(geo, filename) {
        if (!geo || !geo.type) {
            showToast('GeoJSON sans geometrie: ' + filename, true);
            return;
        }
        const features = geo.features || ((geo.type === 'Feature' || geo.type === 'FeatureCollection') ? [] : [geo]);
        let count = 0;

        features.forEach((feat, i) => {
            const root = feat.geometry || feat;
            const lineSets = [];
            const collect = (g) => {
                if (!g || !g.type) return;
                if (g.type === 'LineString') lineSets.push(g.coordinates);
                else if (g.type === 'Polygon') lineSets.push((g.coordinates && g.coordinates[0]) || []);
                else if (g.type === 'MultiLineString') (g.coordinates || []).forEach(c => lineSets.push(c));
                else if (g.type === 'MultiPolygon') (g.coordinates || []).forEach(p => lineSets.push((p && p[0]) || []));
                else if (g.type === 'GeometryCollection') (g.geometries || []).forEach(collect);
            };
            collect(root);

            lineSets.forEach((coords, ci) => {
                const waypoints = (coords || []).map(geoJsonCoordToLatLng).filter(Boolean);
                if (waypoints.length < 2) return;
                const baseName = (feat.properties && feat.properties.name) || (filename + ' #' + (i + 1));
                const name = lineSets.length > 1 ? (baseName + ' #' + (ci + 1)) : baseName;
                const color = TRAIL_COLORS[importedTrails.length % TRAIL_COLORS.length];
                importedTrails.push({ name, color, waypoints, useColor: true });
                count++;
            });
        });

        if (count) {
            showToast(count + ' trail' + (count > 1 ? 's' : '') + ' importe' + (count > 1 ? 's' : '') + ' (' + filename + ')');
        } else {
            showToast('Aucun LineString trouve dans ' + filename, true);
        }
        saveImportedTrails();
        updateImportedTrailList();
    }

    function parseCSV(text, filename) {
        const lines = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim().split('\n');
        if (lines.length < 2) return;
        const header = lines[0].toLowerCase();

        // Detect columns
        const cols = header.split(/[;,]/).map(c => c.trim().replace(/^"|"$/g, ''));
        let latIdx = cols.findIndex(c => c === 'lat' || c === 'latitude' || c === 'y');
        let lngIdx = cols.findIndex(c => c === 'lng' || c === 'lon' || c === 'long' || c === 'longitude' || c === 'x');

        // Trail name column: si presente, le fichier est decoupe en plusieurs trails
        let nameIdx = cols.findIndex(c => c === 'name' || c === 'nom' || c === 'trail' || c === 'trail_name' || c === 'trailname' || c === 'route' || c === 'chemin' || c === 'track' || c === 'trace' || c === 'libelle' || c === 'label');

        if (latIdx === -1 || lngIdx === -1) {
            // Fallback: assume first two numeric columns are x,y (UTM) or lat,lng
            latIdx = 0;
            lngIdx = 1;
        }

        const points = [];
        for (let i = 1; i < lines.length; i++) {
            const parts = lines[i].split(/[;,]/).map(p => p.trim().replace(/^"|"$/g, ''));
            const rawLat = parseFloat(parts[latIdx]);
            const rawLng = parseFloat(parts[lngIdx]);
            if (isFinite(rawLat) && isFinite(rawLng)) {
                const csvUTM = Math.abs(rawLat) > 180 || Math.abs(rawLng) > 180;
                const ll = convertCRS(csvUTM ? rawLng : rawLat, csvUTM ? rawLat : rawLng);
                points.push({ lat: ll.lat, lng: ll.lng, name: nameIdx !== -1 ? (parts[nameIdx] || '').trim() : '' });
            }
        }
        if (points.length < 2) {
            showToast('CSV: au moins 2 points de coordonnees valides requis (' + filename + ')', true);
            return;
        }

        // Grouper par nom de trail, sinon un seul trail nomme comme le fichier
        const groups = new Map();
        points.forEach(p => {
            const key = nameIdx !== -1 ? (p.name || '(sans nom)') : filename;
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push([p.lat, p.lng]);
        });

        let added = 0;
        groups.forEach((waypoints, name) => {
            if (waypoints.length < 2) return;
            const color = TRAIL_COLORS[importedTrails.length % TRAIL_COLORS.length];
            importedTrails.push({ name, color, waypoints, useColor: true });
            added++;
        });
        if (added) {
            showToast(added + ' trail' + (added > 1 ? 's' : '') + ' importe' + (added > 1 ? 's' : '') + ' (' + filename + ')');
        } else {
            showToast('Aucun trail valide (2 points min par trail) dans ' + filename, true);
        }
        saveImportedTrails();
        updateImportedTrailList();
    }

    function updateImportedTrailList() {
        trailImportList.innerHTML = '';
        importedTrails.forEach((t, i) => {
            const div = document.createElement('div');
            div.className = 'trail-imported-item';
            const useColor = t.useColor !== false;
            const shownColor = useColor ? t.color : DEFAULT_TRAIL_COLOR;
            div.innerHTML = `
                <span class="trail-imported-name"><i class="fas fa-route" style="color:${shownColor}"></i> ${t.name}</span>
                <span class="trail-imported-info">${t.waypoints.length} points</span>
                <label class="trail-color-toggle" title="Activer / desactiver la couleur"><input type="checkbox" class="trail-imported-color-toggle" data-idx="${i}" ${useColor ? 'checked' : ''} /></label>
                <input type="color" class="trail-imported-color" value="${t.color}" data-idx="${i}" ${useColor ? '' : 'disabled'} />
                <button class="btn-icon-sm trail-imported-del" data-idx="${i}"><i class="fas fa-times"></i></button>`;
            trailImportList.appendChild(div);
        });

        trailImportList.querySelectorAll('.trail-imported-color-toggle').forEach(chk => {
            chk.addEventListener('change', () => {
                importedTrails[parseInt(chk.dataset.idx)].useColor = chk.checked;
                saveImportedTrails();
                updateImportedTrailList();
            });
        });

        trailImportList.querySelectorAll('.trail-imported-del').forEach(btn => {
            btn.addEventListener('click', () => {
                importedTrails.splice(parseInt(btn.dataset.idx), 1);
                saveImportedTrails();
                updateImportedTrailList();
            });
        });
        trailImportList.querySelectorAll('.trail-imported-color').forEach(input => {
            input.addEventListener('input', () => {
                importedTrails[parseInt(input.dataset.idx)].color = input.value;
                saveImportedTrails();
                updateImportedTrailList();
            });
        });
    }

    restoreImportedTrails();

    // ===== AUTO-LOAD DEFAULT DATA =====
    async function autoLoadDefaultData() {
        // 1. Charger les zones depuis Mining Areas.csv
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

                    const colorMap = {
                        'bleu': 'blue', 'blue': 'blue',
                        'vert': 'green', 'green': 'green',
                        'jaune': 'yellow', 'yellow': 'yellow',
                        'orange': 'orange',
                        'violet': 'blue', 'violet': 'blue',
                        'rouge': 'red', 'red': 'red',
                        'or': 'orange', 'marron': 'orange'
                    };

                    for (let i = 1; i < lines.length; i++) {
                        const line = lines[i].trim();
                        if (!line) continue;
                        const parts = line.split(sep).map(p => p.trim());
                        const rawX = parseFloat(parts[xIdx]);
                        const rawY = parseFloat(parts[yIdx]);
                        if (!isFinite(rawX) || !isFinite(rawY)) continue;

                        const name = (nameIdx >= 0 && parts[nameIdx]) ? parts[nameIdx] : 'Zone ' + i;
                        const colorRaw = (colorIdx >= 0 && parts[colorIdx]) ? parts[colorIdx].toLowerCase().trim() : 'blue';
                        const color = colorMap[colorRaw] || 'blue';

                        addZone(name, rawX, rawY, color);
                    }
                    showToast('Zones chargees depuis Mining Areas.csv');
                }
            }
        } catch(e) {
            console.warn('Auto-load zones failed:', e);
        }

        // 2. Charger les trails depuis Trails.geojson.geojson
        try {
            const resp = await fetch('data/Trails.geojson.geojson');
            if (resp.ok) {
                const geo = await resp.json();
                if (geo) {
                    const features = geo.features || [];
                    let count = 0;
                    features.forEach((feat, i) => {
                        const root = feat.geometry || feat;
                        const collect = (g) => {
                            if (!g || !g.type) return;
                            let lineCoords = null;
                            if (g.type === 'LineString') lineCoords = g.coordinates;
                            else if (g.type === 'MultiLineString') (g.coordinates || []).forEach(c => {
                                if (c && c.length >= 2) {
                                    const wp = c.map(geoJsonCoordToLatLng).filter(Boolean);
                                    if (wp.length >= 2) {
                                        const name = (feat.properties && feat.properties.name) || ('Trail ' + (i + 1));
                                        const color = TRAIL_COLORS[importedTrails.length % TRAIL_COLORS.length];
                                        importedTrails.push({ name, color, waypoints: wp, useColor: true });
                                        count++;
                                    }
                                }
                            });
                            if (g.type === 'Polygon') lineCoords = (g.coordinates && g.coordinates[0]) || [];
                            if (g.type === 'GeometryCollection') (g.geometries || []).forEach(collect);

                            if (lineCoords && lineCoords.length >= 2) {
                                const waypoints = lineCoords.map(geoJsonCoordToLatLng).filter(Boolean);
                                if (waypoints.length >= 2) {
                                    const name = (feat.properties && feat.properties.name) || ('Trail ' + (i + 1));
                                    const color = TRAIL_COLORS[importedTrails.length % TRAIL_COLORS.length];
                                    importedTrails.push({ name, color, waypoints, useColor: true });
                                    count++;
                                }
                            }
                        };
                        collect(root);
                    });
                    saveImportedTrails();
                    updateImportedTrailList();
                    if (count) showToast(count + ' trail' + (count > 1 ? 's' : '') + ' charge' + (count > 1 ? 's' : '') + ' depuis Trails.geojson');
                }
            }
        } catch(e) {
            console.warn('Auto-load trails failed:', e);
        }
    }
    autoLoadDefaultData();

    // ===== PRESENT BUTTON =====
    const btnPresent = document.getElementById('btnPresent');
    btnPresent.addEventListener('click', () => {
        const centerCRS = convertCRS(
            toFloat(document.getElementById('cfgCenterLat').value),
            toFloat(document.getElementById('cfgCenterLng').value)
        );

        const basemapVal = document.getElementById('cfgBasemap').value;

        const trailsForPresent = getTrails();
        // Trails dans une cle dediee (petite) pour ne pas dependre du gros blob config
        try { localStorage.setItem('oreTrackingTrails', JSON.stringify(trailsForPresent)); } catch(e) {}

        const config = {
            center: { lat: centerCRS.lat, lng: centerCRS.lng },
            zoom: parseInt(document.getElementById('cfgZoom').value),
            zones: getZones(),
            trails: trailsForPresent,
            showZones: document.getElementById('cfgShowZones').checked,
            showTrails: document.getElementById('cfgShowTrails').checked,
            showLabels: document.getElementById('cfgShowLabels').checked,
            basemap: basemapVal,
            mbtilesUrl: document.getElementById('cfgMbtilesUrl').value,
            layers: layers
                .filter(l => {
                    if (l.type === 'mbtiles') return !!l.mbtilesName;
                    if (l.type === 'georef') return l.loaded && l.dataUrl;
                    return l.dataUrl || l.geoJSON;
                })
                .map(l => ({
                    type: l.type,
                    name: l.name,
                    bounds: l.bounds,
                    dataUrl: l.dataUrl,
                    geoJSON: l.geoJSON,
                    mbtilesName: l.mbtilesName,
                    controlPoints: l.controlPoints,
                    opacity: l.opacity,
                    visible: l.visible
                }))
        };

        // Sauvegarder et ouvrir
        try {
            localStorage.setItem('oreTrackingConfig', JSON.stringify(config));
        } catch(e) {
            config.layers.forEach(l => { if (l.type === 'georef') delete l.dataUrl; });
            try { localStorage.setItem('oreTrackingConfig', JSON.stringify(config)); } catch(e2) {}
        }

        window.open('dashboard.html', '_blank', 'width=1920,height=1080');
    });

    // ===== SYNC BUTTON =====
    const btnSyncNow = document.querySelector('.btn-sync-now');
    if (btnSyncNow) {
        btnSyncNow.addEventListener('click', () => {
            btnSyncNow.textContent = 'Synchronisation...';
            btnSyncNow.disabled = true;
            setTimeout(() => {
                btnSyncNow.textContent = 'Synchronise !';
                setTimeout(() => {
                    btnSyncNow.textContent = 'Synchroniser maintenant';
                    btnSyncNow.disabled = false;
                }, 2000);
            }, 1500);
        });
    }

    // ===== TEST DB BUTTON =====
    const btnTestDb = document.querySelector('.btn-test-db');
    if (btnTestDb) {
        btnTestDb.addEventListener('click', () => {
            btnTestDb.textContent = 'Test en cours...';
            btnTestDb.disabled = true;
            setTimeout(() => {
                const ok = Math.random() > 0.3;
                btnTestDb.textContent = ok ? 'Connecte !' : 'Echec';
                btnTestDb.style.background = ok ? '#22c55e' : '#ef4444';
                setTimeout(() => {
                    btnTestDb.textContent = 'Tester la connexion';
                    btnTestDb.style.background = '';
                    btnTestDb.disabled = false;
                }, 2000);
            }, 1200);
        });
    }

    // ===== MENU ITEMS =====
    document.querySelectorAll('.menu-item').forEach(item => {
        item.addEventListener('click', () => {
            item.style.background = 'rgba(255,255,255,0.1)';
            setTimeout(() => item.style.background = '', 200);
        });
    });
});
