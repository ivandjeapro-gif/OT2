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
        const dts = ((suivi && suivi.etat() && suivi.etat().engins) || []).filter((x) => x.type === 'DT');
        const nb = (k) => dts.filter((x) => CS.fluxDe(x) === k).length;
        texte($('fltEtat'), (filtre.length ? 'Filtre actif : ' + filtre.join(' · ') + ' — ' : '') +
            fmt(r.affiches) + ' DT affiché(s) sur ' + fmt(r.total) + ' (' + nb('minerai') + ' minerai · ' + nb('waste') + ' waste · ' +
            (dts.length - nb('minerai') - nb('waste')) + ' sans chargement), ici et sur la salle de contrôle.');
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
        if (e.canal) lignes.push(['Reçue par', e.canal_libelle + (e.auth_libelle ? ' · ' + e.auth_libelle : '')]);
        if ((e.canaux || []).length > 1) lignes.push(['Canaux (5 min)', e.canaux.map((c) => c.libelle.replace(/ \(.*\)/, '') + ' ' + c.age_s + ' s').join(' · ')]);
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
        return { type: $('fltType').value, flux: $('fltFlux').value, statut: $('fltStatut').value || 'tous', zone: $('fltZone').value || 'toutes',
                 shift: $('fltShift').value, date: $('fltDate').value, teneurs };
    }
    function remplirFormulaire(f) {
        $('fltType').value = f.type; $('fltFlux').value = f.flux || 'tous'; majOptionsStatut(null); $('fltStatut').value = f.statut;
        $('fltZone').value = f.zone; $('fltShift').value = f.shift; $('fltDate').value = f.date || '';
        document.querySelectorAll('[data-flt-teneur]').forEach((c) => { c.checked = f.teneurs[c.dataset.fltTeneur] !== false; });
    }
    function appliquer(f) {
        CS.ecrireFiltres(f);               // le dashboard ouvert le recoit par l'evenement storage
        remplirFormulaire(f);
        if (suivi) suivi.appliquerFiltres(f);
    }
    // chaque changement de filtre s'applique tout de suite (le bouton reste un secours)
    ['fltType', 'fltFlux', 'fltStatut', 'fltZone', 'fltShift', 'fltDate'].forEach((id) => $(id).addEventListener('change', () => appliquer(lireFormulaire())));
    document.querySelectorAll('[data-flt-teneur]').forEach((c) => c.addEventListener('change', () => appliquer(lireFormulaire())));
    $('btnFltAppliquer').addEventListener('click', () => appliquer(lireFormulaire()));
    $('btnFltReset').addEventListener('click', () => appliquer(JSON.parse(JSON.stringify(CS.FILTRES_DEFAUT))));
    { const d = new Date(); if (d.getHours() < 6) d.setDate(d.getDate() - 1);   // jour de production en cours (06h->06h)
      $('fltDate').max = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
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

    // ---- ecrans (API Window Management, Chrome / Edge) -------------------
    // Comme PowerPoint / VideoPSlam : « Automatique » = le premier ecran
    // SECONDAIRE (sinon l'ecran principal). Avec 3 ecrans ou plus, on choisit
    // un ecran precis ou « tous les ecrans secondaires ». L'ecran est memorise
    // par sa position/taille (stable), pas par son rang (qui change au
    // branchement). Sans l'API : la fenetre s'ouvre ou le systeme veut.
    let ecrans = [];
    const cleEcran = (e) => e.left + ',' + e.top + ',' + e.width + 'x' + e.height;
    const apiEcrans = 'getScreenDetails' in window;
    const etatEcrans = (t, ok) => { const e = $('cfgDiffEtat'); e.textContent = t; e.classList.toggle('ok', !!ok); };

    function decrireEcran(e, i) {
        const p = ecrans.find((x) => x.isPrimary);
        let pos = 'principal';
        if (!e.isPrimary && p) {
            const dx = e.left - p.left, dy = e.top - p.top;
            pos = (Math.abs(dx) >= Math.abs(dy) ? (dx > 0 ? 'à droite' : 'à gauche') : (dy > 0 ? 'en dessous' : 'au-dessus')) + ' du principal';
        }
        return 'Écran ' + (i + 1) + (e.label ? ' · ' + e.label : '') + ' · ' + e.width + '×' + e.height + ' · ' + pos;
    }
    function remplirEcrans() {
        const sel = $('cfgDiffEcran'), val = CS.lireDiffusion().ecran;
        const sec = ecrans.filter((e) => !e.isPrimary);
        const opts = [new Option(sec.length ? 'Automatique : premier écran secondaire' : 'Automatique : écran principal', 'auto')];
        if (sec.length > 1) opts.push(new Option('Tous les écrans secondaires (' + sec.length + ')', 'tous'));
        ecrans.forEach((e, i) => opts.push(new Option(decrireEcran(e, i), cleEcran(e))));
        sel.replaceChildren(...opts);
        sel.value = [...sel.options].some((o) => o.value === val) ? val : 'auto';
    }
    async function listerEcrans(demander) {
        if (!apiEcrans) return false;
        try {
            if (!demander && navigator.permissions) {
                const st = await navigator.permissions.query({ name: 'window-management' }).catch(() => null);
                if (!st || st.state !== 'granted') return false;
            }
            const d = await window.getScreenDetails();
            const maj = () => {
                ecrans = d.screens.slice().sort((a, b) => a.left - b.left || a.top - b.top);
                remplirEcrans();
                etatEcrans(ecrans.length + ' écran(s) détecté(s). Appliqué en direct à la salle de contrôle.', false);
            };
            maj();
            d.addEventListener('screenschange', maj);   // branchement / debranchement a chaud
            return true;
        } catch (e) {
            etatEcrans("Détection des écrans refusée : autorisez « Gestion des fenêtres » dans le navigateur.", false);
            return false;
        }
    }
    function ciblesProjection() {
        const d = CS.lireDiffusion();
        const sec = ecrans.filter((e) => !e.isPrimary);
        if (d.ecran === 'tous') return sec.length ? sec : ecrans.slice(0, 1);
        const choisi = ecrans.find((e) => cleEcran(e) === d.ecran);
        if (choisi) return [choisi];
        // automatique : le secondaire le plus proche du principal (a egalite : a droite)
        const p = ecrans.find((e) => e.isPrimary) || ecrans[0];
        const dist = (e) => Math.hypot(e.left - p.left, e.top - p.top);
        sec.sort((a, b) => dist(a) - dist(b) || b.left - a.left);
        return sec.length ? [sec[0]] : ecrans.slice(0, 1);
    }
    if (!apiEcrans) {
        $('btnDiffDetecter').disabled = $('btnDiffIdentifier').disabled = true;
        etatEcrans("Ce navigateur ne gère pas les écrans multiples (utilisez Chrome ou Edge) : la fenêtre s'ouvre où le système le décide.", false);
    }
    listerEcrans(false);
    $('btnDiffDetecter').addEventListener('click', () => listerEcrans(true));
    // identification : un grand numero sur l'ecran pendant 3 s. Le navigateur
    // n'autorise souvent qu'UNE fenetre par clic : on affiche donc les ecrans
    // un par un (chaque clic sur « Identifier » passe au suivant), et choisir
    // un ecran dans la liste affiche son numero.
    let identSuivant = 0;
    function afficherNumero(e, i) {
        const w = window.open('', 'ot-ident-' + i, 'left=' + e.left + ',top=' + e.top + ',width=' + e.width + ',height=' + e.height + ',popup=yes');
        if (!w) return false;
        w.document.write('<title>Écran ' + (i + 1) + '</title><body style="margin:0;background:#1a1a1a;color:#fcd525;font:700 22vw/100vh sans-serif;text-align:center">' + (i + 1) +
            '<div style="position:fixed;left:0;right:0;bottom:6vh;font:400 3vw sans-serif;color:#ddd;line-height:1.2">' + (e.isPrimary ? 'principal' : 'secondaire') + ' · ' + e.width + '×' + e.height + '</div></body>');
        try { w.moveTo(e.left, e.top); w.resizeTo(e.width, e.height); } catch (x) { /* position imposee par le systeme */ }
        setTimeout(() => w.close(), 3000);
        return true;
    }
    $('btnDiffIdentifier').addEventListener('click', async () => {
        if (!ecrans.length && !(await listerEcrans(true))) return;
        if (identSuivant >= ecrans.length) identSuivant = 0;
        const montres = [];
        while (identSuivant < ecrans.length) {
            if (!afficherNumero(ecrans[identSuivant], identSuivant)) break;     // 2e fenetre refusee : au clic suivant
            montres.push(identSuivant + 1);
            identSuivant++;
        }
        if (identSuivant < ecrans.length) {
            etatEcrans(montres.length
                ? 'Écran ' + montres.join(', ') + ' affiché. Cliquez de nouveau sur « Identifier » pour l\'écran ' + (identSuivant + 1) + ' sur ' + ecrans.length + '.'
                : 'Fenêtre bloquée par le navigateur : autorisez les pop-ups pour ce site.', false);
        } else {
            etatEcrans('Écran ' + montres.join(', ') + ' affiché — tous les écrans ont été identifiés.', true);
            identSuivant = 0;
        }
    });
    $('cfgDiffEcran').addEventListener('change', () => {
        const e = ciblesProjection()[0];
        if (e && ecrans.length > 1) afficherNumero(e, ecrans.indexOf(e));
    });

    window.LiveDiffusion = {
        /** Lance la projection ; retourne le nombre de fenetres ouvertes. */
        async ouvrir(url) {
            const d = CS.lireDiffusion();
            const plein = d.mode === 'etendu';
            if (!ecrans.length && apiEcrans) await listerEcrans(true);   // 1re fois : demande d'autorisation
            const cs = ciblesProjection();
            const cibles = cs.length ? cs : [null];
            let ouvertes = 0, bloquees = 0;
            cibles.forEach((e, i) => {
                let traits = plein ? 'width=1920,height=1080' : 'width=1600,height=900';
                if (e) {
                    traits = plein
                        ? 'left=' + e.left + ',top=' + e.top + ',width=' + e.width + ',height=' + e.height
                        : 'left=' + (e.left + Math.max(0, (e.width - 1600) / 2)) + ',top=' + (e.top + Math.max(0, (e.height - 900) / 2)) +
                          ',width=' + Math.min(1600, e.width) + ',height=' + Math.min(900, e.height);
                }
                const w = window.open(url + (plein ? '?plein=1' : ''), 'oreTrackingSalle' + (cibles.length > 1 ? '-' + i : ''), traits);
                if (!w) { bloquees++; return; }
                ouvertes++;
                if (e && plein) {
                    try { w.moveTo(e.left, e.top); w.resizeTo(e.width, e.height); } catch (x) { /* fenetre deja ouverte ailleurs */ }
                    // plein ecran sans clic quand le navigateur l'autorise ; sinon
                    // la fenetre affiche « Cliquer pour passer en plein ecran »
                    setTimeout(() => { try { w.document.documentElement.requestFullscreen({ screen: e }).catch(() => {}); } catch (x) { /* refuse */ } }, 900);
                }
            });
            const noms = cibles.map((e) => (e ? decrireEcran(e, ecrans.indexOf(e)) : 'écran choisi par le système')).join(' | ');
            etatEcrans(bloquees ? bloquees + ' fenêtre(s) bloquée(s) : autorisez les pop-ups, puis relancez PRESENTER.' : 'Projection lancée : ' + noms, !bloquees);
            return ouvertes;
        },
    };

    // ------------------------------------------------ enregistrements ---
    // CSV du tracking (un fichier par jour, DT + excavatrices) et videos de shift, sur le PC de la
    // passerelle. La periode est celle du « Perimetre de suivi » (Date de production + Shift).
    function majEnregPeriode() {
        const p = CS.periode(CS.lireFiltres());
        const jusqua = p.jusqua || new Date().toLocaleString('sv-SE').replace('T', ' ');
        texte($('enregPeriode'), p.libelle + ' · ' + p.depuis.slice(11, 16) + ' → ' + jusqua.slice(11, 16));
        $('btnEnregCsv').href = PASSERELLE() + '/api/tracking/export.csv?depuis=' + encodeURIComponent(p.depuis) +
            '&jusqua=' + encodeURIComponent(jusqua);
        return { depuis: p.depuis, jusqua: jusqua };
    }
    const mo = (o) => (o / 1048576).toLocaleString('fr-FR', { maximumFractionDigits: 1 }) + ' Mo';
    async function listerEnreg() {
        try {
            const d = await fetch(PASSERELLE() + '/api/enregistrements', { cache: 'no-store' }).then((r) => r.json());
            const per = majEnregPeriode();
            // la liste suit le « Perimetre de suivi » : videos dont la plage recoupe la periode, CSV des jours concernes
            const noms = (re, f) => { const m = re.exec(f.nom); return m ? m : null; };
            const recoupe = (f) => {
                const m = noms(/^tracking-(\d{4}-\d{2}-\d{2})_(\d{2})h(\d{2})-(\d{4}-\d{2}-\d{2})_(\d{2})h(\d{2})\./, f);
                if (!m) return true;
                const deb = m[1] + ' ' + m[2] + ':' + m[3] + ':00', fin = m[4] + ' ' + m[5] + ':' + m[6] + ':00';
                return deb < per.jusqua && fin > per.depuis;
            };
            const jourCsv = (f) => { const m = /^(\d{4}-\d{2}-\d{2})\.csv$/.exec(f.nom); return !m || (m[1] >= per.depuis.slice(0, 10) && m[1] <= per.jusqua.slice(0, 10)); };
            d.videos = (d.videos || []).filter(recoupe);
            d.csv = (d.csv || []).filter(jourCsv);
            const lien = (f) => PASSERELLE() + '/api/enregistrements/fichier?nom=' + encodeURIComponent(f.nom);
            const remplir = (ul, liste, vide) => {
                ul.replaceChildren(...(liste.length ? liste.map((f) => {
                    const li = document.createElement('li');
                    const a = document.createElement('a'); a.href = lien(f); a.textContent = f.nom; a.setAttribute('download', f.nom);
                    const sp = document.createElement('span'); sp.textContent = mo(f.octets);
                    if (/\.(webm|mp4)$/.test(f.nom)) {          // video : clic = lecteur integre ; telechargement a cote
                        a.className = 'lv-lire';
                        a.addEventListener('click', (ev) => { ev.preventDefault(); ouvrirLecteur(f.nom, lien(f)); });
                        const dl = document.createElement('a'); dl.className = 'lv-dl'; dl.href = lien(f); dl.textContent = '⬇'; dl.title = 'Télécharger';
                        dl.setAttribute('download', f.nom); sp.append(dl);
                    }
                    li.append(a, sp); return li;
                }) : [Object.assign(document.createElement('li'), { className: 'vide', textContent: vide })]));
            };
            remplir($('enregVideos'), d.videos || [], 'Aucune vidéo sur cette période');
            remplir($('enregCsv'), d.csv || [], 'Aucun CSV sur cette période');
            if ((d.en_cours || []).length) texte($('enregEtat'), 'Génération en cours : ' + d.en_cours.join(', '));
            else if (/^Génération en cours/.test($('enregEtat').textContent)) texte($('enregEtat'), 'Vidéo prête : elle figure dans la liste.');
            return (d.en_cours || []).length > 0;
        } catch (e) { texte($('enregEtat'), 'Passerelle injoignable.'); return false; }
    }
    // lecteur video integre : fenetre au-dessus de la page, lecture directe depuis la passerelle (plages d'octets)
    const lecteur = $('lecteurVideo'), player = $('lecteurPlayer');
    function ouvrirLecteur(nom, url) {
        texte($('lecteurTitre'), nom);
        $('lecteurTelecharger').href = url;
        $('lecteurTelecharger').setAttribute('download', nom);
        $('lecteurErreur').hidden = true;
        player.src = url + '&inline=1';
        lecteur.hidden = false;
        player.play().catch(() => { /* lecture auto refusee : le bouton lecture reste disponible */ });
    }
    function fermerLecteur() { player.pause(); player.removeAttribute('src'); player.load(); lecteur.hidden = true; }
    player.addEventListener('error', () => {
        const e = $('lecteurErreur'); e.hidden = false;
        e.textContent = /\.mp4/.test(player.currentSrc)
            ? "Cette ancienne vidéo MP4 n'est pas lisible dans le navigateur : télécharge-la, ou régénère-la (format WebM)."
            : 'Lecture impossible : fichier introuvable ou passerelle injoignable.';
    });
    $('lecteurFermer').addEventListener('click', fermerLecteur);
    lecteur.addEventListener('click', (ev) => { if (ev.target === lecteur) fermerLecteur(); });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !lecteur.hidden) fermerLecteur(); });

    let enregMinuteur = null;
    async function surveillerEnreg() {
        clearTimeout(enregMinuteur);
        const encore = await listerEnreg();
        const visible = !document.querySelector('[data-volet="enreg"]').hidden;
        if (visible) enregMinuteur = setTimeout(surveillerEnreg, encore ? 3000 : 15000);
    }
    document.querySelectorAll('.lv-onglets [data-onglet]').forEach((b) => b.addEventListener('click', () => {
        if (b.dataset.onglet === 'enreg') { majEnregPeriode(); surveillerEnreg(); }
    }));
    // le perimetre change : la liste se met a jour tout de suite (videos et CSV de la nouvelle periode)
    ['fltShift', 'fltDate'].forEach((id) => $(id).addEventListener('change', () => { majEnregPeriode(); surveillerEnreg(); }));
    window.addEventListener('storage', (ev) => { if (ev.key === 'oreTrackingFiltres') surveillerEnreg(); });
    // bouton REC (section Enregistrements) : la passerelle filme la carte du dashboard en arriere-plan (js/enregistreur-carte.js)
    if (window.EnregistreurCarte) {
        EnregistreurCarte.attacher({
            bouton: $('btnEnregRec'), chrono: $('enregChrono'), passerelle: PASSERELLE, vitesse: () => $('enregVitesse').value, qualite: () => $('enregQualite').value,
            selecteurs: [$('enregVitesse'), $('enregQualite')], selVitesse: $('enregVitesse'), selQualite: $('enregQualite'),
            message: (t) => texte($('enregEtat'), t),
        });
    }
    document.addEventListener('enregistrements:maj', () => surveillerEnreg());

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
