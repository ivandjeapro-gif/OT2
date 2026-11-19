/* =====================================================================
 * live-tracking.js — page Live « Suivi en direct » (Zone 1, 19/11).
 *
 *  - onglets « Carte & tracking » / « KPI & panneaux » ;
 *  - carte « Tracking de la flotte » : DT en temps reel (js/carte-suivi.js),
 *    legende par etat (clic = filtre), fiche du DT choisi ;
 *  - resumes « Reglages de la carte » (l'editeur detaille reste celui
 *    d'app.js, visible en mode Modifier) ;
 *  - « Perimetre de suivi » : filtres appliques ici ET au dashboard ;
 *  - « Diffusion » : trajets / animations / alertes en direct, mode et
 *    ecran cible au clic sur PRESENTER.
 * ===================================================================== */
document.addEventListener('DOMContentLoaded', () => {
    'use strict';
    const $ = (id) => document.getElementById(id);
    if (!$('liveTrackingMap') || typeof CarteSuivi === 'undefined' || typeof L === 'undefined') return;

    const CS = CarteSuivi;
    const PASSERELLE = () => (window.OTCFG && OTCFG.passerelle)
        ? OTCFG.passerelle() : 'http://' + (location.hostname || 'localhost') + ':8101';
    const fmt = (n) => Number(n || 0).toLocaleString('fr-FR');
    const texte = (el, t) => { if (el) el.textContent = t; };

    // ------------------------------------------------------- onglets ---
    document.querySelectorAll('.lv-onglets [data-onglet]').forEach((b) => {
        b.addEventListener('click', () => {
            document.querySelectorAll('.lv-onglets [data-onglet]').forEach((x) => {
                x.classList.toggle('actif', x === b); x.setAttribute('aria-selected', x === b);
            });
            document.querySelectorAll('[data-volet]').forEach((v) => {
                const ok = v.dataset.volet === b.dataset.onglet;
                v.classList.toggle('actif', ok); v.hidden = !ok;
            });
            if (carte) setTimeout(() => carte.map.invalidateSize(), 50);
        });
    });

    // ------------------------------------------- verrou de la config ---
    const mapConfig = $('mapConfig');
    const majVerrou = () => {
        const ouvert = mapConfig && !mapConfig.classList.contains('locked');
        texte($('lvVerrou'), ouvert ? 'Modification en cours — Confirmer pour appliquer' : 'Configuration verrouillée par défaut');
        $('lvVerrou').classList.toggle('ouvert', !!ouvert);
        texte($('lvReglagesAide'), ouvert
            ? 'Modification en cours · Confirmer applique la configuration à la carte.'
            : 'Lecture seule · activez Modifier pour changer la configuration.');
    };
    if (mapConfig) new MutationObserver(majVerrou).observe(mapConfig, { attributes: true, attributeFilter: ['class'] });
    majVerrou();
    // un resume cliquable ouvre (en mode Modifier) la section correspondante
    document.querySelectorAll('.lv-resume[data-section]').forEach((b) => b.addEventListener('click', () => {
        if (mapConfig.classList.contains('locked')) { $('btnUnlockMap').click(); }
        const sec = $(b.dataset.section);
        if (sec) { sec.scrollIntoView({ behavior: 'smooth', block: 'center' }); sec.classList.add('lv-flash'); setTimeout(() => sec.classList.remove('lv-flash'), 1200); }
    }));

    function resumer(cfg) {
        const fonds = { 'osm': 'OpenStreetMap', 'esri-sat': 'Satellite ESRI', 'esri-topo': 'Topographique ESRI',
                        'esri-dark': 'Sombre ESRI', 'mbtiles': 'MBTiles local' };
        texte($('sumFond'), fonds[cfg.basemap] || cfg.basemap || '--');
        const utm = ($('cfgCRS') || {}).value === 'utm';
        texte($('sumCrs'), utm ? 'UTM · EPSG:' + (($('cfgEPSG') || {}).value || '32630') : 'WGS84 · EPSG:4326');
        texte($('sumCentre'), (($('cfgCenterLat') || {}).value || '--') + ' / ' + (($('cfgCenterLng') || {}).value || '--') + ' · zoom ' + (cfg.zoom || '--'));
        const couches = (cfg.layers || []);
        texte($('sumCouches'), couches.filter((l) => l.visible).length + ' couche(s) active(s) sur ' + couches.length);
        texte($('sumZones'), (cfg.zones || []).length + ' zone(s)');
        texte($('sumTrails'), (cfg.trails || []).length + ' piste(s)');
    }

    // ------------------------------------------------- carte de suivi ---
    let carte = null, suivi = null, cfgCourante = null, choisi = null;

    async function construireCarte() {
        const cfg = window.LiveCarte ? window.LiveCarte.config() : {};
        cfgCourante = cfg;
        resumer(cfg);
        if (suivi) suivi.arreter();
        if (carte) carte.detruire();
        const el = $('liveTrackingMap');
        el.innerHTML = '';
        carte = await CS.construire(el, cfg, {
            zones: $('trkZones').checked, pistes: cfg.showTrails !== false, labels: $('trkLabels').checked,
        });
        texte($('trkFond'), carte.fond);
        remplirZones(carte.zones);
        suivi = CS.suivre(carte, {
            labels: $('trkLabels').checked, trajets: $('trkTrajets').checked,
            animations: CS.lireDiffusion().animations,
            onMaj: majResume, onChoix: afficherFiche,
            onErreur: () => texte($('liveTrackingCompteur'), 'Passerelle :8101 injoignable'),
        });
        suivi.appliquerFiltres(CS.lireFiltres());
    }
    document.addEventListener('live:config', () => { construireCarte().catch((e) => console.error('[Live] carte :', e)); });
    // si app.js ne signale rien (pas de donnees par defaut), on construit quand meme
    setTimeout(() => { if (!carte) construireCarte(); }, 1500);

    $('trkZoomIn').addEventListener('click', () => carte && carte.map.zoomIn());
    $('trkZoomOut').addEventListener('click', () => carte && carte.map.zoomOut());
    $('trkRecentrer').addEventListener('click', () => carte && carte.recentrer());
    $('trkZones').addEventListener('change', () => carte && carte.afficher({ zones: $('trkZones').checked }));
    $('trkLabels').addEventListener('change', () => {
        if (carte) carte.afficher({ labels: $('trkLabels').checked });
        if (suivi) suivi.reglages({ labels: $('trkLabels').checked });
    });
    $('trkTrajets').addEventListener('change', () => suivi && suivi.reglages({ trajets: $('trkTrajets').checked }));

    function majResume(r) {
        const filtre = CS.filtresActifs(r.filtres);
        texte($('liveTrackingCompteur'),
            (r.affiches === r.total ? fmt(r.total) : fmt(r.affiches) + ' / ' + fmt(r.total)) + ' DT · ' +
            fmt(carte ? carte.segments : 0) + ' segments surveillés');
        texte($('trkPeriode'), 'Trajets : ' + r.periode.libelle + (r.periode.enDirect ? ' (en direct)' : ''));
        // legende = compteurs par etat, un clic filtre (re-clic = tous)
        const leg = $('trkLegende');
        leg.replaceChildren(...CS.ETATS_DT.map((etat) => {
            const b = document.createElement('button');
            b.type = 'button';
            b.className = 'lv-leg' + (r.filtres.statut === etat ? ' actif' : '') + (r.compte[etat] ? '' : ' vide');
            b.title = CS.ETATS[etat].lib + ' — cliquer pour filtrer';
            const p = document.createElement('span'); p.className = 'trk-pastille'; p.style.setProperty('--c', CS.ETATS[etat].couleur);
            const n = document.createElement('b'); n.textContent = r.compte[etat] || 0;
            b.append(p, ' ' + CS.ETATS[etat].court + ' ', n);
            b.addEventListener('click', () => {
                const f = CS.lireFiltres();
                f.statut = f.statut === etat ? 'tous' : etat;
                appliquer(f);
            });
            return b;
        }));
        majOptionsStatut(r.compte);
        texte($('fltEtat'), (filtre.length ? 'Filtre actif : ' + filtre.join(' · ') + ' — ' : '') +
            fmt(r.affiches) + ' DT affiché(s) sur ' + fmt(r.total) + ', ici et sur la salle de contrôle.');
    }

    // ------------------------------------------------- fiche du DT ---
    function afficherFiche(e) {
        const fiche = $('trkFiche');
        choisi = e ? e.code : null;
        if (!e) { fiche.hidden = true; return; }
        const etat = CS.ETATS[e.etat] || {};
        const lignes = [
            ['État', etat.lib || e.etat],
            ['Statut terrain', e.statut || '--'],
            ['Vitesse', e.type === 'DT' ? Math.round(e.vitesse_kmh || 0) + ' km/h' : '--'],
            ['Dernière position', e.age_s === null ? '--' : e.age_s < 60 ? 'il y a ' + e.age_s + ' s' : 'il y a ' + Math.round(e.age_s / 60) + ' min'],
        ];
        if (e.mission) {
            lignes.push(['Mission', (e.mission.orebloc || '?') + ' → ' + (e.mission.dumping_area || '?')]);
            if (e.teneur) lignes.push(['Teneur', CS.TENEURS[e.teneur] || (e.teneur === 'waste' ? 'Waste' : e.teneur)]);
        }
        if (e.etat === 'hors_piste') lignes.push(['Hors piste', e.offroad_distance + ' m de la piste']);
        const titre = document.createElement('div'); titre.className = 'lv-fiche-tete';
        const p = document.createElement('span'); p.className = 'trk-pastille'; p.style.setProperty('--c', etat.couleur || '#999');
        const h = document.createElement('b'); h.textContent = e.code;
        const x = document.createElement('button'); x.type = 'button'; x.textContent = '×'; x.title = 'Ne plus suivre';
        x.addEventListener('click', () => suivi && suivi.choisir(null));
        titre.append(p, h, x);
        const dl = document.createElement('dl');
        lignes.forEach(([k, v]) => { const dt = document.createElement('dt'); dt.textContent = k; const dd = document.createElement('dd'); dd.textContent = v; dl.append(dt, dd); });
        const pied = document.createElement('p'); pied.className = 'lv-fiche-pied'; pied.textContent = 'La carte suit ce DT. × pour arrêter.';
        fiche.replaceChildren(titre, dl, pied);
        fiche.hidden = false;
    }

    // ------------------------------------------ perimetre de suivi ---
    function majOptionsStatut(compte) {
        const sel = $('fltStatut');
        const val = sel.value || CS.lireFiltres().statut;
        const opts = [['tous', 'Tous']].concat(CS.ETATS_DT.map((k) => [k, CS.ETATS[k].lib + (compte ? ' (' + (compte[k] || 0) + ')' : '')]));
        if (sel.options.length !== opts.length) {
            sel.replaceChildren(...opts.map(([v, t]) => new Option(t, v)));
            sel.value = val;
        } else {
            // mise a jour des compteurs sans reconstruire la liste (liste ouverte)
            opts.forEach(([, t], i) => { if (sel.options[i].text !== t) sel.options[i].text = t; });
        }
    }
    function remplirZones(zones) {
        const sel = $('fltZone'), val = CS.lireFiltres().zone;
        sel.replaceChildren(new Option('Toutes les zones', 'toutes'), ...zones.map((z) => new Option(z.name, z.name)));
        sel.value = zones.some((z) => z.name === val) ? val : 'toutes';
    }
    function lireFormulaire() {
        const teneurs = {};
        document.querySelectorAll('[data-flt-teneur]').forEach((c) => { teneurs[c.dataset.fltTeneur] = c.checked; });
        return { type: $('fltType').value, statut: $('fltStatut').value || 'tous', zone: $('fltZone').value || 'toutes',
                 shift: $('fltShift').value, date: $('fltDate').value, teneurs };
    }
    function remplirFormulaire(f) {
        $('fltType').value = f.type; majOptionsStatut(null); $('fltStatut').value = f.statut;
        $('fltZone').value = f.zone; $('fltShift').value = f.shift; $('fltDate').value = f.date || '';
        document.querySelectorAll('[data-flt-teneur]').forEach((c) => { c.checked = f.teneurs[c.dataset.fltTeneur] !== false; });
    }
    function appliquer(f) {
        CS.ecrireFiltres(f);               // le dashboard ouvert le recoit par l'evenement storage
        remplirFormulaire(f);
        if (suivi) suivi.appliquerFiltres(f);
    }
    $('btnFltAppliquer').addEventListener('click', () => appliquer(lireFormulaire()));
    $('btnFltReset').addEventListener('click', () => appliquer(JSON.parse(JSON.stringify(CS.FILTRES_DEFAUT))));
    $('fltDate').max = new Date().toISOString().slice(0, 10);
    remplirFormulaire(CS.lireFiltres());

    // ----------------------------------------------------- diffusion ---
    const diff = CS.lireDiffusion();
    $('cfgDiffMode').value = diff.mode;
    $('cfgDiffTrajets').checked = diff.trajets !== false;
    $('cfgDiffAnimations').checked = diff.animations !== false;
    function enregistrerDiffusion() {
        const d = Object.assign(CS.lireDiffusion(), {
            mode: $('cfgDiffMode').value, ecran: $('cfgDiffEcran').value,
            trajets: $('cfgDiffTrajets').checked, animations: $('cfgDiffAnimations').checked,
            alertes: $('cfgShowAlerts').checked,
        });
        CS.ecrireDiffusion(d);
        if (suivi) suivi.reglages({ animations: d.animations });
        const etat = $('cfgDiffEtat');
        etat.textContent = 'Enregistré — appliqué en direct à la salle de contrôle.';
        etat.classList.add('ok'); clearTimeout(etat._t);
        etat._t = setTimeout(() => { etat.textContent = 'Appliqué en direct à la salle de contrôle.'; etat.classList.remove('ok'); }, 2500);
    }
    ['cfgDiffMode', 'cfgDiffEcran', 'cfgDiffTrajets', 'cfgDiffAnimations', 'cfgShowAlerts']
        .forEach((id) => $(id).addEventListener('change', enregistrerDiffusion));

    // ecrans disponibles (API Window Management, Chrome / Edge) : sans
    // elle, « Automatique » seulement — la fenetre s'ouvre ou le systeme veut.
    let ecrans = [];
    async function listerEcrans(demander) {
        if (!('getScreenDetails' in window)) return;
        try {
            if (!demander && navigator.permissions) {
                const st = await navigator.permissions.query({ name: 'window-management' }).catch(() => null);
                if (!st || st.state !== 'granted') return;
            }
            const d = await window.getScreenDetails();
            ecrans = d.screens;
            const sel = $('cfgDiffEcran'), val = CS.lireDiffusion().ecran;
            sel.replaceChildren(new Option('Automatique', 'auto'),
                ...ecrans.map((s, i) => new Option('Écran ' + (i + 1) + (s.isPrimary ? ' (principal)' : '') + ' · ' + s.width + '×' + s.height, String(i))));
            sel.value = ecrans[val] ? val : 'auto';
        } catch (e) { /* refuse par l'utilisateur */ }
    }
    listerEcrans(false);
    $('cfgDiffEcran').addEventListener('focus', () => { if (!ecrans.length) listerEcrans(true); }, { once: true });

    window.LiveDiffusion = {
        ouvrir(url) {
            const d = CS.lireDiffusion();
            const s = ecrans[d.ecran];
            const plein = d.mode === 'etendu';
            let traits = 'width=1920,height=1080';
            if (s) traits = 'left=' + s.availLeft + ',top=' + s.availTop + ',width=' + s.availWidth + ',height=' + s.availHeight;
            else if (!plein) traits = 'width=1600,height=900';
            // plein ecran : le navigateur exige un clic dans la fenetre ouverte
            // (dashboard : invite « cliquer pour passer en plein ecran »)
            window.open(url + (plein ? '?plein=1' : ''), 'oreTrackingSalle', traits);
        },
    };

    // ------------------------------------------- pied : passerelle ---
    async function majPied() {
        const el = $('lvPasserelle');
        try {
            const r = await fetch(PASSERELLE() + '/api/health', { cache: 'no-store' });
            if (!r.ok) throw new Error();
            el.className = 'lv-pied-lien ok'; texte(el.querySelector('span'), 'connectée');
        } catch (e) {
            el.className = 'lv-pied-lien ko'; texte(el.querySelector('span'), 'injoignable');
        }
    }
    majPied(); setInterval(majPied, 10000);
});
