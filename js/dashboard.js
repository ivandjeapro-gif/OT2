/* =====================================================================
 * dashboard.js — Salle de controle (refonte du 19/11).
 *
 * Sources REELLES uniquement (decision du 07/11 : 0 % de donnees simulees) :
 *   passerelle :8101  /api/kpi, /api/alertes, /api/offroad, /api/engins,
 *                     /api/health, /api/office/sync
 *   serveur    :8100  /api/config (seuils), /api/tablettes, /api/health
 *   page Live         localStorage oreTrackingKpiPlan (objectifs),
 *                     oreTrackingKpi (blocs affiches), oreTrackingConfig (carte)
 * null = pas encore de mesure : on affiche « -- », jamais une valeur inventee.
 * ===================================================================== */
document.addEventListener('DOMContentLoaded', () => {
    'use strict';

    // proj4 ne connait que WGS84 et Web Mercator par defaut : sans cette
    // definition, toute conversion UTM echoue en silence (objets en 0,0).
    if (typeof proj4 !== 'undefined' && !proj4.defs('EPSG:32630')) {
        proj4.defs('EPSG:32630', '+proj=utm +zone=30 +datum=WGS84 +units=m +no_defs');
    }

    const $ = (id) => document.getElementById(id);
    // adresse reglee dans Parametres (ot-config.js) ; par defaut : cette machine, :8101
    const PASSERELLE = (window.OTCFG && OTCFG.passerelle)
        ? OTCFG.passerelle() : 'http://' + (window.location.hostname || 'localhost') + ':8101';
    const getJSON = (url) => fetch(url, { cache: 'no-store' }).then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
    });

    // ===== DONNEES =====
    const data = {
        minerais: { current: null, planned: null, alerte: 10 },
        ore:      { current: null, planned: null, alerte: 10 },
        waste:    { current: null, planned: null, alerte: 10 },
        offroad:  { count: null, dts: [] },
        grades: {
            high:     { actuel: null, planned: null },
            medium:   { actuel: null, planned: null },
            low:      { actuel: null, planned: null },
            marginal: { actuel: null, planned: null }
        },
        liens: { serveur: null, passerelle: null, office: null, cloud: null },
        source: null,          // 'sg' (via passerelle) | 'direct' (secours) | 'voyages' (table locale)
        luLe: null,            // heure de la derniere lecture reussie de la base en ligne
        derniereMaj: null
    };
    const thresholds = { offroadAlert: 1, gradeVariance: 10 };

    // ===== PERIODE LUE (Date + Shift choisis dans Live) =====
    // KPI et alertes ne portent que sur UNE periode : le shift en cours par defaut, ou la
    // journee / le shift choisi dans « Perimetre de suivi ». Journee de production = 06h -> 06h.
    // Recalculee a chaque releve : a 06h et a 18h, « shift en cours » bascule tout seul.
    let periodeCourante = null;
    function lirePeriode() {
        periodeCourante = CarteSuivi.periode(CarteSuivi.lireFiltres());
        const el = $('periodeLue');
        if (el) el.textContent = periodeCourante.libelle + (periodeCourante.enDirect ? '' : ' (terminé)');
        return periodeCourante;
    }
    const paramsPeriode = (p) => 'depuis=' + encodeURIComponent(p.depuis) + '&jusqua=' + encodeURIComponent(p.jusqua);
    // objectifs : saisis par JOURNEE ; un shift (12 h) en vaut la moitie
    const facteurObjectif = () => (periodeCourante && periodeCourante.shift !== 'journee') ? 0.5 : 1;

    // ===== FORMATS =====
    const fmt = (v) => (v === null || v === undefined || isNaN(v))
        ? '--' : Math.round(v).toLocaleString('fr-FR');
    const fmtSigne = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + fmt(Math.abs(v));
    const deux = (n) => String(n).padStart(2, '0');
    const horodatageLocal = (d) => d.getFullYear() + '-' + deux(d.getMonth() + 1) + '-' + deux(d.getDate()) + ' ' + deux(d.getHours()) + ':' + deux(d.getMinutes()) + ':' + deux(d.getSeconds());

    // ===== PARAMETRES SERVEUR (seuils, table params) =====
    async function applyServerParams() {
        if (typeof API === 'undefined') return;
        try {
            const r = await API.getConfig();
            const p = r && (r.params || r);
            if (!p) return;
            const num = (v, d) => { const n = parseFloat(v); return isNaN(n) ? d : n; };
            if (p.seuil_grade !== undefined) thresholds.gradeVariance = num(p.seuil_grade, thresholds.gradeVariance);
            renderKpis();
        } catch (e) { /* serveur hors ligne : valeurs par defaut */ }
    }

    // ===== OBJECTIFS (page Live) =====
    function appliquerObjectifs() {
        let plan = {};
        try { plan = JSON.parse(localStorage.getItem('oreTrackingKpiPlan')) || {}; } catch (e) {}
        const lire = (k) => plan[k] || {};
        ['minerais', 'ore', 'waste'].forEach((k) => {
            const p = parseFloat(lire(k).planned);
            data[k].planned = isNaN(p) ? null : p;
            if (lire(k).alerte !== undefined) data[k].alerte = parseFloat(lire(k).alerte) || 10;
        });
        [['hg', 'high'], ['mg', 'medium'], ['lg', 'low'], ['mo', 'marginal']].forEach(([k, g]) => {
            const p = parseFloat(lire(k).planned);
            data.grades[g].planned = isNaN(p) ? null : p;
            const a = parseFloat(lire(k).alerte);
            data.grades[g].alerte = isNaN(a) ? null : a;
        });
    }

    // Etat d'un realise face a son objectif, selon le seuil d'alerte (%) :
    // ecart <= seuil/2 -> ok ; <= seuil -> warn ; au-dela -> bad.
    function etat(actuel, planned, seuil) {
        if (actuel === null || planned === null || !planned) return '';
        const e = Math.abs(actuel - planned) / planned * 100;
        return e > seuil ? 'bad' : e > seuil / 2 ? 'warn' : 'ok';
    }

    // ===== KPI =====
    // Compteur anime : la valeur « roule » vers la nouvelle mesure.
    function rouler(el, cible) {
        if (cible === null || cible === undefined || isNaN(cible)) { el.textContent = '--'; el._v = null; return; }
        const depart = (el._v === null || el._v === undefined) ? cible * 0.9 : el._v;
        el._v = cible;
        if (Math.round(depart) === Math.round(cible)) { el.textContent = fmt(cible); return; }
        const t0 = performance.now(), d = 1100;
        const pas = (t) => {
            const k = Math.min((t - t0) / d, 1), e = 1 - Math.pow(1 - k, 3);
            el.textContent = fmt(depart + (cible - depart) * e);
            if (k < 1 && el._v === cible) requestAnimationFrame(pas);
        };
        requestAnimationFrame(pas);
    }

    function renderKpiCarte(cle, suffixe, unite) {
        const k = data[cle];
        const plan = k.planned === null ? null : k.planned * facteurObjectif();
        rouler($('kpi' + suffixe), k.current);
        $('plan' + suffixe).textContent = plan === null ? '--' : fmt(plan) + ' ' + unite;

        const pct = (k.current !== null && plan) ? k.current / plan * 100 : null;
        const st = etat(k.current, plan, k.alerte || 10);
        const pctEl = $('pct' + suffixe);
        pctEl.textContent = pct === null ? '--' : pct.toLocaleString('fr-FR', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + ' %';
        pctEl.className = 'pct ' + st;

        const g = $('gauge' + suffixe);
        g.style.setProperty('--p', pct === null ? 0 : Math.min(pct / 100, 1.25));
        g.classList.toggle('empty', pct === null);
        const carte = $('kpi' + suffixe + 'Card');
        carte.classList.remove('ok', 'warn', 'bad');
        if (st) carte.classList.add(st);
    }

    function renderGrade(prefix, gBrut) {
        const g = Object.assign({}, gBrut, { planned: gBrut.planned === null ? null : gBrut.planned * facteurObjectif() });
        rouler($(prefix + 'Actuel'), g.actuel);
        $(prefix + 'Planned').textContent = fmt(g.planned);
        const v = $(prefix + 'Var');
        const ligne = $(prefix + 'Var').closest('.grade');
        const rail = ligne.querySelector('.rail i');
        if (g.actuel === null || g.planned === null) {
            v.textContent = '--'; v.className = 'var';
            rail.style.width = '0';
            ligne.classList.remove('bad');
            return;
        }
        const diff = g.actuel - g.planned;
        v.textContent = fmtSigne(diff);
        v.className = 'var ' + (diff > 0 ? 'pos' : diff < 0 ? 'neg' : '');
        rail.style.width = g.planned ? Math.min(g.actuel / g.planned / 1.25, 1) * 100 + '%' : '0';
        ligne.classList.toggle('bad', etat(g.actuel, g.planned, g.alerte || thresholds.gradeVariance) === 'bad');
    }

    function renderKpis() {
        renderKpiCarte('minerais', 'Minerais', 't');
        renderKpiCarte('ore', 'Ore', 'oz');
        renderKpiCarte('waste', 'Waste', 't');
        renderGrade('hg', data.grades.high);
        renderGrade('mg', data.grades.medium);
        renderGrade('lg', data.grades.low);
        renderGrade('mo', data.grades.marginal);
    }

    // ===== PRODUCTION : base en ligne (Serveur_SG, onglet Chargements) =====
    // 1) via la passerelle, qui releve SG toutes les 5 s (/api/kpi) ;
    // 2) secours : si la passerelle ne repond plus (2 echecs de suite), ce
    //    navigateur lit SG lui-meme, avec l'URL + jeton saisis dans
    //    Parametres > Secours direct (localStorage, jamais la passerelle).
    // Sans secours configure, la derniere mesure reste affichee.
    const LS_SECOURS = 'oreTrackingSgDirect';
    const LS_BLOCS = 'oreTrackingOreblocsAu';
    const GRAMMES_PAR_ONCE = 31.1035;
    const KPI_GRADES = { HG: 'high', SHG: 'high', MG: 'medium', MG1: 'medium', MG2: 'medium',
                         LG: 'low', LG1: 'low', LG2: 'low', MO: 'marginal' };
    let echecsPasserelle = 0;
    let directEnCours = false;

    function appliquerKpi(k, source, luLe) {
        data.minerais.current = k.minerais_t;
        data.ore.current = k.onces;
        data.waste.current = k.waste_t;
        data.grades.high.actuel = k.grades.high.tonnes;
        data.grades.medium.actuel = k.grades.medium.tonnes;
        data.grades.low.actuel = k.grades.low.tonnes;
        data.grades.marginal.actuel = k.grades.marginal.tonnes;
        // chargements lus mais exclus des totaux (bloc ou tonnage manquant / bloc inconnu)
        data.ignores = (Number(k.sans_bloc) || 0) + (Number(k.sans_tonnage) || 0) + (Number(k.blocs_inconnus) || 0);
        data.source = source;
        data.luLe = luLe || null;
        data.derniereMaj = new Date();
        renderKpis();
        renderPied();
    }

    const STATUT_CLOUD = { ok: 'ok', lent: 'warn', ko: 'ko' };

    function refreshKpi() {
        const p = lirePeriode();
        getJSON(PASSERELLE + '/api/kpi?' + paramsPeriode(p))
            .then((k) => {
                if (p !== periodeCourante && (p.depuis !== periodeCourante.depuis || p.jusqua !== periodeCourante.jusqua)) return;   // filtre change entre-temps
                echecsPasserelle = 0;
                const c = k.cloud || {};
                data.liens.cloud = STATUT_CLOUD[c.statut] || null;
                data.liens.cloudTxt = c.statut === 'non_configure' ? 'Serveur_SG non configuré sur la passerelle'
                    : c.erreur ? c.erreur : c.lu_le ? 'lu à ' + c.lu_le.slice(11) : '';
                const lu = c.lu_le ? new Date(c.lu_le.replace(' ', 'T')) : null;
                // passerelle anterieure au 19/11 : pas de champ source
                appliquerKpi(k, k.source === 'sg' ? 'sg' : k.source ? 'voyages' : 'ancienne', lu);
                memoriserBlocs();
            })
            .catch(() => {
                echecsPasserelle++;
                if (echecsPasserelle >= 2) kpiDirect();
                else renderPied();
            });
    }

    // Teneur Au et nature des oreblocs, pour calculer les onces en secours :
    // copie de la table de la passerelle, rafraichie toutes les 10 min.
    function memoriserBlocs() {
        let c = null;
        try { c = JSON.parse(localStorage.getItem(LS_BLOCS)); } catch (e) {}
        if (c && Date.now() - c.ts < 600000) return;
        getJSON(PASSERELLE + '/api/oreblocs').then((d) => {
            const blocs = {};
            (d.oreblocs || []).forEach((b) => { blocs[b.nom] = [Number(b.au_teneur) || 0, Number(b.minerai) ? 1 : 0, b.destination || '']; });
            try { localStorage.setItem(LS_BLOCS, JSON.stringify({ ts: Date.now(), blocs })); } catch (e) { /* stockage plein */ }
        }).catch(() => {});
    }

    // Meme calcul que kpi_chargements() de la passerelle.
    function kpiDepuisChargements(lignes) {
        let blocs = {};
        try { blocs = (JSON.parse(localStorage.getItem(LS_BLOCS)) || {}).blocs || {}; } catch (e) {}
        const res = { minerais_t: 0, waste_t: 0, onces: 0,
                      grades: { high: { tonnes: 0 }, medium: { tonnes: 0 }, low: { tonnes: 0 }, marginal: { tonnes: 0 } } };
        lignes.forEach((r) => {
            const nom = String(r.orebloc || '').trim();
            const t = parseFloat(r.tonnes);
            if (!nom || isNaN(t)) return;
            const b = blocs[nom];
            const code = (nom.split('_')[3] || '').toUpperCase();
            const dest = String(r.teneur || (b ? b[2] : '') || '').trim().toUpperCase();
            const minerai = b ? !!b[1] : (code !== 'WST' && dest !== 'W');
            if ((code === 'WST' || dest === 'W' || !minerai) && !KPI_GRADES[code]) { res.waste_t += t; return; }
            res.minerais_t += t;
            res.onces += t * (b ? b[0] : 0) / GRAMMES_PAR_ONCE;
            if (KPI_GRADES[code]) res.grades[KPI_GRADES[code]].tonnes += t;
        });
        return res;
    }

    function kpiDirect() {
        let cfg = {};
        try { cfg = JSON.parse(localStorage.getItem(LS_SECOURS)) || {}; } catch (e) {}
        if (!cfg.url || !cfg.token) {             // pas de secours sur ce poste
            data.liens.cloud = null;
            data.liens.cloudTxt = 'passerelle injoignable, secours direct non configuré sur ce poste';
            renderPied();
            return;
        }
        if (directEnCours) return;               // un appel Apps Script prend 2 a 6 s
        directEnCours = true;
        const p = lirePeriode();
        const jours = [];
        for (let d = new Date(p.depuis.slice(0, 10) + 'T00:00:00'), fin = new Date((p.jusqua || horodatageLocal(new Date())).slice(0, 10) + 'T00:00:00'); d <= fin; d.setDate(d.getDate() + 1)) {
            jours.push(d.getFullYear() + '-' + deux(d.getMonth() + 1) + '-' + deux(d.getDate()));
        }
        const bornes = [p.depuis, p.jusqua || horodatageLocal(new Date())];
        Promise.all(jours.map((jour) => getJSON(cfg.url + '?action=chargements&jour=' + jour + '&token=' + encodeURIComponent(cfg.token))))
            .then((reps) => {
                const lignes = [];
                reps.forEach((r) => {
                    if (r.ok === false || !Array.isArray(r.lignes)) throw new Error(r.erreur || 'réponse inattendue');
                    r.lignes.forEach((l) => {
                        const h = String(l.debut || '').replace('T', ' ').slice(0, 19);
                        if (h >= bornes[0] && h < bornes[1]) lignes.push(l);
                    });
                });
                data.liens.cloud = 'ok';
                data.liens.cloudTxt = 'secours direct, lu à ' + new Date().toLocaleTimeString('fr-FR');
                appliquerKpi(kpiDepuisChargements(lignes), 'direct', new Date());
            })
            .catch((e) => {
                data.liens.cloud = 'ko';
                data.liens.cloudTxt = 'secours direct en échec : ' + e.message;
                renderPied();
            })
            .finally(() => { directEnCours = false; });
    }

    // Les relevés rapides (0,5 s) ne s'empilent pas : si la passerelle met plus de
    // 0,5 s a repondre, le suivant attend (sinon N fenetres x 2 appels/s s'accumulent).
    const enVol = {};
    function unAUnSeul(nom, fn) {
        if (enVol[nom]) return;
        enVol[nom] = true;
        Promise.resolve(fn()).finally(() => { enVol[nom] = false; });
    }

    // ===== OFFROAD =====
    let codesHorsPiste = '';
    let echecsOffroad = 0;

    function renderOffroad() {
        const n = data.offroad.count;
        $('offroadCount').textContent = n === null ? '--' : deux(n);
        const carte = $('offroadCard');
        const alerte = n !== null && n >= thresholds.offroadAlert;
        carte.classList.toggle('alert', alerte);
        carte.classList.toggle('calm', n !== null && !alerte);
        $('offroadTxt').textContent = alerte
            ? data.offroad.dts.map((d) => d.code).join(' · ') + (n > 1 ? ' hors piste' : ' hors piste en ce moment')
            : 'DT hors piste en ce moment';
    }

    function refreshOffroad() {
        return getJSON(PASSERELLE + '/api/offroad')
            .then((d) => {
                echecsOffroad = 0;
                data.offroad.count = d.count || 0;
                data.offroad.dts = d.dts || [];
                renderOffroad();
                const avant = codesHorsPiste;
                const codes = data.offroad.dts.map((x) => x.code).sort().join(',');
                if (codes !== codesHorsPiste) {
                    const nouveau = data.offroad.dts.some((x) => codesHorsPiste.split(',').indexOf(x.code) < 0);
                    codesHorsPiste = codes;
                    if (nouveau) jouerSonDanger();
                    afficherAlerte();
                }
                annoncerOffroad(codes !== avant && data.offroad.dts.some((x) => avant.split(',').indexOf(x.code) < 0));
            })
            .catch(() => {
                // on garde la derniere valeur 3 s, puis « -- » : un « 00 » calme affiche
                // alors que la passerelle ne repond plus serait trompeur
                if (++echecsOffroad >= 6 && data.offroad.count !== null) {
                    data.offroad.count = null;
                    data.offroad.dts = [];
                    renderOffroad();
                }
            });
    }

    // ===== ENVIRONNEMENT =====
    // Donnees en ligne : meteo Open-Meteo (gratuit, sans cle) au centre de la
    // carte du site. Pluviometrie = cumul du jour. Aucune source en ligne pour
    // le niveau sonore (pas de sonde) : on affiche l'absence de mesure.
    // Hors ligne, la derniere mesure reste affichee.
    const meteo = { temp: null, humid: null, pluie: null };
    function renderEnvironnement() {
        const f = (v, u, d) => v === null ? '-- ' + u
            : v.toLocaleString('fr-FR', { minimumFractionDigits: d, maximumFractionDigits: d }) + ' ' + u;
        $('envTemp').textContent = f(meteo.temp, '°C', 0);
        $('envHumid').textContent = f(meteo.humid, '%', 0);
        $('envRain').textContent = f(meteo.pluie, 'mm', 1);
        $('envNoise').textContent = '-- dB';
    }

    function refreshMeteo() {
        let cfg = {};
        try { cfg = JSON.parse(localStorage.getItem('oreTrackingConfig')) || {}; } catch (e) {}
        const lat = (cfg.center && cfg.center.lat) || 8.2626;
        const lng = (cfg.center && cfg.center.lng) || -4.6301;
        getJSON('https://api.open-meteo.com/v1/forecast?latitude=' + lat + '&longitude=' + lng +
                '&current=temperature_2m,relative_humidity_2m&daily=precipitation_sum&timezone=auto&forecast_days=1')
            .then((d) => {
                const c = d.current || {};
                const num = (v) => (typeof v === 'number' && isFinite(v)) ? v : null;
                meteo.temp = num(c.temperature_2m);
                meteo.humid = num(c.relative_humidity_2m);
                meteo.pluie = num(d.daily && d.daily.precipitation_sum && d.daily.precipitation_sum[0]);
                renderEnvironnement();
            })
            .catch(() => { /* hors ligne : on garde la derniere mesure */ });
    }

    // ===== TABLETTES =====
    // Connectee = vue par le serveur dans les 10 dernieres minutes.
    const TABLETTE_VUE_S = 600;
    // Tablettes connectees = DT dont la tablette est en ligne sur le broker
    // MQTT (presence + positions recentes), lu sur la passerelle : sans
    // session, contrairement a l'ancienne liste /api/tablettes (19/11).
    function refreshTablettes() {
        return getJSON(PASSERELLE + '/api/mqtt/comptes').then((d) => {
            if (!d.configure) {
                $('tabletCount').textContent = '--';
                $('tabletList').textContent = 'Suivi temps réel non installé';
                return;
            }
            const enLigne = (d.comptes || []).filter((c) => c.en_ligne).map((c) => c.code + (c.mqtt_en_ligne === false ? ' (4G)' : ''));
            $('tabletCount').textContent = deux(enLigne.length);
            $('tabletList').textContent = enLigne.length
                ? enLigne.join(' · ')
                : 'Aucune tablette en ligne (' + (d.comptes || []).length + ' DT équipés)';
            const sansMqtt = (d.comptes || []).filter((c) => c.en_ligne && c.mqtt_en_ligne === false).map((c) => c.code);
            if (sansMqtt.length) $('tabletList').title = 'Sans MQTT (positions reçues par 4G uniquement) : ' + sansMqtt.join(', ');
        }).catch(() => {
            $('tabletCount').textContent = '--';
            $('tabletList').textContent = 'Passerelle injoignable';
        });
    }

    // 75 s -> « 1 min », 5 000 s -> « 1 h 23 », 3 jours -> « 3 j »
    function duree(sec) {
        if (!isFinite(sec) || sec < 60) return '';
        const min = Math.round(sec / 60);
        if (min < 60) return min + ' min';
        const h = Math.floor(min / 60);
        if (h < 24) return h + ' h ' + deux(min % 60);
        return Math.floor(h / 24) + ' j';
    }

    // Horodatages « AAAA-MM-JJ HH:MM:SS » : heure locale (now_iso de la passerelle)
    // sauf `utc` = colonnes remplies par DEFAULT datetime('now') (alertes.created_at).
    function secondesDepuis(h, utc) {
        if (!h) return Infinity;
        let s = String(h).replace(' ', 'T');
        if (utc && !/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
        const t = Date.parse(s);
        return isNaN(t) ? Infinity : (Date.now() - t) / 1000;
    }

    // ===== ALERTES (pastille du bandeau + cloche) =====
    let configLive = {};
    try { configLive = JSON.parse(localStorage.getItem('oreTrackingConfig')) || {}; } catch (e) {}
    let afficherAlertes = configLive.showAlerts !== false;
    try {
        const d = JSON.parse(localStorage.getItem('oreTrackingDiffusion'));
        if (d && d.alertes !== undefined) afficherAlertes = d.alertes !== false;
    } catch (e) {}
    $('alertBanner').style.visibility = afficherAlertes ? '' : 'hidden';
    window.addEventListener('storage', (ev) => {
        if (ev.key !== 'oreTrackingDiffusion') return;
        try { afficherAlertes = (JSON.parse(ev.newValue) || {}).alertes !== false; } catch (e) {}
        $('alertBanner').style.visibility = afficherAlertes ? '' : 'hidden';
        const chip = $('inviteSon');
        if (chip && !afficherAlertes) chip.remove();
    });

    // Diffusion « plein ecran etendu » : le navigateur n'autorise le plein
    // ecran qu'apres un clic dans CETTE fenetre -> invite discrete.
    if (/[?&]plein=1/.test(location.search) && document.documentElement.requestFullscreen) {
        const invite = document.createElement('button');
        invite.type = 'button'; invite.className = 'invite-plein';
        invite.textContent = 'Cliquer pour passer en plein écran';
        invite.addEventListener('click', () => {
            document.documentElement.requestFullscreen().catch(() => {});
            invite.remove();
        });
        document.body.appendChild(invite);
        document.addEventListener('fullscreenchange', () => { if (document.fullscreenElement) invite.remove(); });
        setTimeout(() => invite.remove(), 20000);
    }

    const TYPES_INFO = { fin_mission: 1, arrivee: 1, retour_piste: 1, retour_trajet: 1, mission: 1 };
    let alertesReelles = [];
    let alertIndex = 0;
    let dernierIdAlerte = null;

    // Son et voix : le navigateur ne les autorise qu'apres un geste dans CETTE page
    // (fenetre ouverte par PRESENTER comprise) -> pastille « Cliquer pour activer le
    // son » tant qu'il n'y a pas eu de clic. Suit l'interrupteur « Alertes » de Live.
    // (prefs.js n'est pas charge ici : l'ancien code ne sonnait jamais.)
    let audioCtx = null;
    let sonDebloque = false;
    function debloquerSon() {
        if (sonDebloque) return;
        sonDebloque = true;
        try {
            audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            audioCtx.resume();
        } catch (e) { audioCtx = null; }
        try {
            if ('speechSynthesis' in window) {
                const u = new SpeechSynthesisUtterance(' ');
                u.volume = 0;
                speechSynthesis.speak(u);
            }
        } catch (e) { /* synthese indisponible */ }
        const chip = $('inviteSon');
        if (chip) chip.remove();
    }
    ['pointerdown', 'keydown'].forEach((ev) => document.addEventListener(ev, debloquerSon, { once: true }));
    if (afficherAlertes) {
        const chip = document.createElement('div');
        chip.id = 'inviteSon'; chip.className = 'invite-son';
        chip.textContent = 'Cliquer pour activer le son et la voix des alertes';
        document.body.appendChild(chip);
    }
    function bip(freq, dur, delai) {
        if (!audioCtx || audioCtx.state !== 'running') return;
        const t0 = audioCtx.currentTime + (delai || 0);
        const o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.connect(g); g.connect(audioCtx.destination);
        o.type = 'sine'; o.frequency.value = freq;
        g.gain.setValueAtTime(0.25, t0);
        g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
        o.start(t0); o.stop(t0 + dur);
    }
    function jouerSonDanger() {
        if (!afficherAlertes) return;
        const now = Date.now();
        if (!jouerSonDanger._last || now - jouerSonDanger._last > 10000) {
            bip(880, 0.35); bip(660, 0.35, 0.4);
            jouerSonDanger._last = now;
        }
    }

    // Voix off (synthese vocale du navigateur, fr). Le navigateur ne parle
    // qu'apres un premier clic dans la page (comme pour le son d'alerte).
    function parler(texte) {
        if (!afficherAlertes || !('speechSynthesis' in window)) return;
        try {
            const u = new SpeechSynthesisUtterance(texte);
            u.lang = 'fr-FR'; u.rate = 0.95;
            const v = speechSynthesis.getVoices().find((x) => /^fr/i.test(x.lang));
            if (v) u.voice = v;
            speechSynthesis.cancel();
            speechSynthesis.speak(u);
        } catch (e) { /* synthese indisponible */ }
    }
    const dit = (code) => String(code).replace(/^DT-0*(\d+)/i, 'D T $1');
    let dernierDit = 0;
    function annoncerOffroad(nouveau) {
        const dts = data.offroad.dts || [];
        if (!dts.length) { dernierDit = 0; return; }
        const now = Date.now();
        if (!nouveau && now - dernierDit < 30000) return;   // rappel toutes les 30 s
        dernierDit = now;
        parler('Alerte. ' + dts.map((d) => dit(d.code)).join(', ') +
               (dts.length > 1 ? ' sont hors piste.' : ' est hors piste.'));
    }

    function poserPastille(texte, type) {
        const pill = $('alertBanner');
        pill.className = 'alert-pill ' + (type || '');
        const span = document.createElement('span');
        span.className = 'alert-text';
        span.textContent = texte;            // jamais d'HTML venant du serveur
        $('alertTrack').replaceChildren(span);
    }

    // Sortie de route en cours : pastille rouge fixe, prioritaire sur tout.
    function afficherAlerte() {
        if (!afficherAlertes) return;
        const dts = data.offroad.dts || [];
        if (dts.length) {
            const d = dts[0];
            const age = duree(secondesDepuis(d.depuis));   // heure locale (now_iso)
            poserPastille(dts.length > 1
                ? dts.map((x) => x.code).join(', ') + ' · sorties de route en cours'
                : d.code + ' · sortie de route' + (age ? ' depuis ' + age : '') +
                  (d.distance_m ? ' · ' + Math.round(d.distance_m) + ' m de la piste' : ''), 'offroad');
            return;
        }
        if (!alertesReelles.length) return;
        const a = alertesReelles[alertIndex % alertesReelles.length];
        alertIndex++;
        poserPastille(a.text, a.type);
        if (a.type === 'danger') jouerSonDanger();
    }

    function refreshAlertes() {
        const p = lirePeriode();
        getJSON(PASSERELLE + '/api/alertes?' + paramsPeriode(p))
            .then((d) => {
                const toutes = d.alertes || [];
                const nonVues = toutes.filter((a) => !Number(a.acquittee) && !TYPES_INFO[a.type]);
                // d'abord les alertes non vues (avec leur anciennete), puis les dernieres infos
                const liste = nonVues.slice(0, 8).map((a) => {
                    const age = duree(secondesDepuis(a.created_at, true));
                    return {
                        text: (a.dt_code && a.dt_code !== '-' && String(a.message || '').indexOf(a.dt_code) < 0
                                  ? a.dt_code + ' · ' : '') + (a.message || a.type) +
                              (age ? ' · alerte non vue depuis ' + age : ''),
                        type: 'danger'
                    };
                });
                if (!liste.length) {
                    const info = toutes.find((a) => TYPES_INFO[a.type]);
                    liste.push(info
                        ? { text: (info.dt_code && info.dt_code !== '-' ? info.dt_code + ' · ' : '') + (info.message || info.type), type: 'success' }
                        : { text: 'Aucune alerte sur cette période', type: 'success' });
                }
                alertesReelles = liste;

                const bell = $('notificationBell');
                $('bellCount').textContent = deux(nonVues.length);
                bell.classList.toggle('has', nonVues.length > 0);
                const plusRecent = toutes.length ? toutes[0].id : 0;
                if (dernierIdAlerte !== null && plusRecent > dernierIdAlerte) {
                    bell.classList.add('ring');
                    setTimeout(() => bell.classList.remove('ring'), 2000);
                }
                dernierIdAlerte = plusRecent;
            })
            .catch(() => { alertesReelles = [{ text: 'Passerelle injoignable', type: 'warn' }]; });
    }

    // ===== LIAISONS (pied de page) =====
    function refreshLiens() {
        const t0 = performance.now();
        getJSON(PASSERELLE + '/api/health')
            .then(() => { data.liens.passerelle = performance.now() - t0 > 3000 ? 'warn' : 'ok'; })
            .catch(() => { data.liens.passerelle = 'ko'; })
            .finally(renderPied);

        if (typeof API !== 'undefined') {
            API.health()
                .then(() => { data.liens.serveur = 'ok'; })
                .catch(() => { data.liens.serveur = 'ko'; })
                .finally(renderPied);
        }

        // Office : file d'envoi de la passerelle. Des echecs ou une file qui
        // s'allonge = liaison degradee.
        getJSON(PASSERELLE + '/api/office/sync')
            .then((d) => {
                const rows = d.office_sync || [];
                // seuls les echecs des dernieres 24 h comptent : au-dela, la
                // passerelle ne les rejoue plus (vieux envois d'essai d'octobre)
                const echecs = rows.filter((r) => r.statut === 'echec'
                    && secondesDepuis(r.created_at, true) < 86400).length;
                const attente = rows.filter((r) => r.statut === 'en_attente').length;
                data.liens.office = echecs ? 'ko' : attente > 5 ? 'warn' : 'ok';
                data.liens.officeTxt = echecs ? echecs + ' échec(s)' : attente ? attente + ' en attente' : '';
            })
            .catch(() => { data.liens.office = 'ko'; data.liens.officeTxt = ''; })
            .finally(renderPied);
    }

    function renderPied() {
        const poser = (id, st, titre) => {
            const el = $(id);
            el.className = 'lnk ' + (st || '');
            el.title = titre;
        };
        const lib = { ok: 'connecté', warn: 'lent', ko: 'injoignable' };
        poser('lnkServeur', data.liens.serveur, 'Serveur : ' + (lib[data.liens.serveur] || '…'));
        poser('lnkPasserelle', data.liens.passerelle, 'Passerelle : ' + (lib[data.liens.passerelle] || '…'));
        poser('lnkOffice', data.liens.office, 'Office : ' + (data.liens.officeTxt || lib[data.liens.office] || '…'));
        poser('lnkCloud', data.liens.cloud, 'Base en ligne (Serveur_SG) : ' + (data.liens.cloudTxt || lib[data.liens.cloud] || '…'));
        const h = (dt) => dt.toLocaleTimeString('fr-FR');
        const maj = $('derniereMaj');
        if (!data.derniereMaj) maj.textContent = 'En attente de la production du jour';
        else if (data.source === 'sg') maj.textContent = 'Production · base en ligne · lue à ' + h(data.luLe || data.derniereMaj);
        else if (data.source === 'direct') maj.textContent = 'Production · secours direct (passerelle injoignable) · lue à ' + h(data.luLe);
        else if (data.source === 'ancienne') maj.textContent = 'Production · table locale (passerelle à mettre à jour) · ' + h(data.derniereMaj);
        else maj.textContent = 'Production · table locale (base en ligne non configurée) · ' + h(data.derniereMaj);
        if (data.derniereMaj && data.ignores) {
            maj.textContent += ' · ' + data.ignores + ' chargement' + (data.ignores > 1 ? 's' : '') + ' non compté' + (data.ignores > 1 ? 's' : '') + ' (bloc ou tonnage manquant)';
        }
        maj.classList.toggle('secours', data.source === 'direct');
    }

    // ===== HORLOGE + DATE =====
    function tic() {
        const d = new Date();
        const clock = $('clock');
        clock.firstChild.nodeValue = deux(d.getHours()) + ':' + deux(d.getMinutes());
        clock.lastChild.textContent = ':' + deux(d.getSeconds());
        $('dateDuJour').textContent = d.toLocaleDateString('fr-FR', { day: 'numeric', month: 'long', year: 'numeric' });
    }

    // Cloche : ouvre la page Alertes (acquittement) dans sa propre fenetre / onglet.
    $('notificationBell').addEventListener('click', () => { window.open('alerts.html', 'oreTrackingAlertes'); });

    // ← Live : la salle de controle est ouverte depuis Live dans une fenetre a part
    $('backLive').addEventListener('click', (e) => {
        if (window.opener && !window.opener.closed) {
            e.preventDefault();
            window.opener.focus();
        }
    });

    // ===== BLOCS AFFICHES (pilotes par la page Live) =====
    // localStorage.oreTrackingKpi = { minerais: true, hg: false, ... }.
    const KPI_ELEMENTS = {
        minerais: 'kpiMineraisCard', ore: 'kpiOreCard', waste: 'kpiWasteCard',
        offroad: 'offroadCard', hg: 'gradeHG', mg: 'gradeMG', lg: 'gradeLG', mo: 'gradeMO',
        env: 'envBlock', tablets: 'tabletBlock'
    };
    function appliquerAffichageKpi() {
        let vis = {};
        try { vis = JSON.parse(localStorage.getItem('oreTrackingKpi')) || {}; } catch (e) {}
        // filtre « Teneurs » du Perimetre de suivi (Live) : masque aussi le bloc grade
        let flt = {};
        try { flt = (JSON.parse(localStorage.getItem('oreTrackingFiltres')) || {}).teneurs || {}; } catch (e) {}
        const TENEUR_KPI = { high: 'hg', medium: 'mg', low: 'lg', marginal: 'mo' };
        Object.entries(TENEUR_KPI).forEach(([t, k]) => { if (flt[t] === false) vis[k] = false; });
        Object.entries(KPI_ELEMENTS).forEach(([cle, id]) => { $(id).hidden = vis[cle] === false; });
        const visible = (el) => !el.hidden;
        const ligne = document.querySelector('.kpis');
        const kpis = [...ligne.children].filter(visible);
        ligne.hidden = !kpis.length;
        ligne.style.gridTemplateColumns = 'repeat(' + Math.max(kpis.length, 1) + ', 1fr)';
        const grades = $('gradesBlock');
        grades.hidden = ![...grades.querySelectorAll('.grade')].some(visible);
        const side = document.querySelector('.side');
        const sideVide = ![...side.children].some(visible);
        side.hidden = sideVide;
        document.querySelector('.main').classList.toggle('no-side', sideVide);
        window.dispatchEvent(new Event('resize'));   // Leaflet reprend la place libre
    }

    window.addEventListener('storage', (e) => {
        if (e.key === 'oreTrackingFiltres') { dernierIdAlerte = null; lirePeriode(); refreshKpi(); refreshAlertes(); renderKpis(); }
        if (e.key === 'oreTrackingKpi' || e.key === 'oreTrackingFiltres') { appliquerAffichageKpi(); return; }
        if (e.key === 'oreTrackingKpiPlan') { appliquerObjectifs(); renderKpis(); return; }
        if (e.key === 'oreTrackingConfig' || e.key === 'oreTrackingTrails') {
            setTimeout(() => location.reload(), 300);
        }
    });

    // ===== CARTE + SUIVI DES DT =====
    // Carte et suivi partages avec Live (js/carte-suivi.js, 19/11). Les
    // filtres du « Perimetre de suivi » de Live et les options de diffusion
    // (trajets, animations) s'appliquent ici en direct.
    async function initMap() {
        let config = null;
        try { config = JSON.parse(localStorage.getItem('oreTrackingConfig')); } catch (e) {}
        const cfg = config || {};
        const carte = await CarteSuivi.construire($('map'), cfg, {
            zones: cfg.showZones !== false, pistes: cfg.showTrails !== false, labels: cfg.showLabels !== false,
        });
        $('mapZones').textContent = CarteSuivi.resumeZones(carte.zones);
        $('basemapName').textContent = carte.fond;
        $('segmentsPiste').textContent = fmt(carte.segments);
        $('zoomIn').addEventListener('click', () => carte.map.zoomIn());
        $('zoomOut').addEventListener('click', () => carte.map.zoomOut());
        $('zoomCenter').addEventListener('click', () => carte.recentrer());
        // bouton REC : filme la carte telle qu'affichee (module js/enregistreur-carte.js)
        if (window.EnregistreurCarte) {
            let effacer = null;
            EnregistreurCarte.attacher({
                bouton: $('btnRec'), element: () => $('map'), passerelle: () => PASSERELLE,
                message: (t, ok) => {
                    $('recMsg').textContent = t; $('recMsg').style.color = ok ? '#bfe3b4' : '#ffb4a6';
                    clearTimeout(effacer); effacer = setTimeout(() => { $('recMsg').textContent = ''; }, 9000);
                },
            });
        }
        // chip de filtre : un clic remet tous les filtres a zero (type, matiere, statut, zone, teneurs)
        $('filtreActif').addEventListener('click', () => {
            CarteSuivi.ecrireFiltres(Object.assign({}, CarteSuivi.FILTRES_DEFAUT));
            location.reload();
        });

        const diffusion = CarteSuivi.lireDiffusion();
        CarteSuivi.suivre(carte, {
            labels: cfg.showLabels !== false,
            trajets: diffusion.trajets, animations: diffusion.animations,
            suivreDiffusion: true,
            onMaj: (r) => {
                $('enginsSuivis').textContent = r.affiches === r.total ? r.total : r.affiches + ' / ' + r.total;
                const actifs = CarteSuivi.filtresActifs(r.filtres);
                const el = $('filtreActif');
                el.hidden = !actifs.length;
                const vide = actifs.length && r.total > 0 && r.affiches === 0;
                el.classList.toggle('vide', !!vide);
                el.textContent = actifs.length
                    ? (vide ? 'Aucun engin ne passe le filtre : ' : 'Filtre : ') + actifs.join(' · ') + '  ✕ réinitialiser'
                    : '';
                el.title = 'Cliquer pour afficher tous les engins (réinitialise les filtres du dashboard et de Live)';
            },
        });
        return carte.map;
    }

    // ===== DEMARRAGE =====
    lirePeriode();
    appliquerObjectifs();
    appliquerAffichageKpi();
    renderKpis();
    renderOffroad();
    renderEnvironnement();
    renderPied();
    tic();
    setInterval(tic, 1000);

    applyServerParams();   setInterval(applyServerParams, 60000);
    refreshKpi();          setInterval(refreshKpi, 5000);
    refreshOffroad();      setInterval(() => unAUnSeul('offroad', refreshOffroad), 1000);
    refreshAlertes();      setInterval(refreshAlertes, 8000);
    refreshTablettes();    setInterval(() => unAUnSeul('tablettes', refreshTablettes), 2000);
    refreshMeteo();        setInterval(refreshMeteo, 600000);
    refreshLiens();        setInterval(refreshLiens, 15000);
    setTimeout(afficherAlerte, 1500);
    setInterval(afficherAlerte, 6000);

    initMap().catch((e) => console.error('[Dashboard] carte :', e));
});
