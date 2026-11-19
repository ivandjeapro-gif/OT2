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
    const PASSERELLE = 'http://' + (window.location.hostname || 'localhost') + ':8101';
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

    // ===== FORMATS =====
    const fmt = (v) => (v === null || v === undefined || isNaN(v))
        ? '--' : Math.round(v).toLocaleString('fr-FR');
    const fmtSigne = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + fmt(Math.abs(v));
    const deux = (n) => String(n).padStart(2, '0');

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
        rouler($('kpi' + suffixe), k.current);
        $('plan' + suffixe).textContent = k.planned === null ? '--' : fmt(k.planned) + ' ' + unite;

        const pct = (k.current !== null && k.planned) ? k.current / k.planned * 100 : null;
        const st = etat(k.current, k.planned, k.alerte || 10);
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

    function renderGrade(prefix, g) {
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
        ligne.classList.toggle('bad', etat(g.actuel, g.planned, thresholds.gradeVariance) === 'bad');
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
        data.source = source;
        data.luLe = luLe || null;
        data.derniereMaj = new Date();
        renderKpis();
        renderPied();
    }

    const STATUT_CLOUD = { ok: 'ok', lent: 'warn', ko: 'ko' };

    function refreshKpi() {
        getJSON(PASSERELLE + '/api/kpi')
            .then((k) => {
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
        const d = new Date();
        const jour = d.getFullYear() + '-' + deux(d.getMonth() + 1) + '-' + deux(d.getDate());
        getJSON(cfg.url + '?action=chargements&jour=' + jour + '&token=' + encodeURIComponent(cfg.token))
            .then((r) => {
                if (r.ok === false || !Array.isArray(r.lignes)) throw new Error(r.erreur || 'réponse inattendue');
                data.liens.cloud = 'ok';
                data.liens.cloudTxt = 'secours direct, lu à ' + new Date().toLocaleTimeString('fr-FR');
                appliquerKpi(kpiDepuisChargements(r.lignes), 'direct', new Date());
            })
            .catch((e) => {
                data.liens.cloud = 'ko';
                data.liens.cloudTxt = 'secours direct en échec : ' + e.message;
                renderPied();
            })
            .finally(() => { directEnCours = false; });
    }

    // ===== OFFROAD =====
    let majCamionsMaintenant = null;   // branche par initMap()
    let codesHorsPiste = '';

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
        getJSON(PASSERELLE + '/api/offroad')
            .then((d) => {
                data.offroad.count = d.count || 0;
                data.offroad.dts = d.dts || [];
                renderOffroad();
                const codes = data.offroad.dts.map((x) => x.code).sort().join(',');
                if (codes !== codesHorsPiste) {
                    const nouveau = data.offroad.dts.some((x) => codesHorsPiste.split(',').indexOf(x.code) < 0);
                    codesHorsPiste = codes;
                    if (nouveau) jouerSonDanger();
                    afficherAlerte();
                    if (majCamionsMaintenant) majCamionsMaintenant();
                }
            })
            .catch(() => { /* on garde la derniere valeur */ });
    }

    // ===== ENVIRONNEMENT =====
    // Aucune sonde meteo n'est branchee : on affiche l'absence de mesure.
    function renderEnvironnement() {
        $('envTemp').textContent = '-- °C';
        $('envHumid').textContent = '-- %';
        $('envRain').textContent = '-- mm';
        $('envNoise').textContent = '-- dB';
    }

    // ===== TABLETTES =====
    // Connectee = vue par le serveur dans les 10 dernieres minutes.
    const TABLETTE_VUE_S = 600;
    function refreshTablettes() {
        if (typeof API === 'undefined' || !API.getTablettes) return;
        API.getTablettes().then((d) => {
            const vues = (d.tablettes || []).filter((t) =>
                Number(t.actif) !== 0 && secondesDepuis(t.dernier_seen) < TABLETTE_VUE_S);
            $('tabletCount').textContent = deux(vues.length);
            $('tabletList').textContent = vues.length
                ? vues.map((t) => t.nom || t.serial || t.device_id).join(' · ')
                : 'Aucune tablette vue depuis 10 min';
        }).catch((e) => {
            $('tabletCount').textContent = '--';
            // la liste des tablettes est privee : sans session ouverte, 401
            $('tabletList').textContent = e && e.status === 401
                ? 'Connexion requise (session OreTracking)'
                : 'Serveur injoignable';
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
    const afficherAlertes = configLive.showAlerts !== false;
    if (!afficherAlertes) $('alertBanner').style.visibility = 'hidden';

    const TYPES_INFO = { fin_mission: 1, arrivee: 1, retour_piste: 1, retour_trajet: 1, mission: 1 };
    let alertesReelles = [];
    let alertIndex = 0;
    let dernierIdAlerte = null;

    function jouerSonDanger() {
        if (!afficherAlertes || typeof PREFS === 'undefined' || !PREFS.alertSound) return;
        const now = Date.now();
        if (!jouerSonDanger._last || now - jouerSonDanger._last > 10000) {
            PREFS.alertSound();
            jouerSonDanger._last = now;
        }
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
        getJSON(PASSERELLE + '/api/alertes')
            .then((d) => {
                const toutes = d.alertes || [];
                const nonVues = toutes.filter((a) => !Number(a.acquittee) && !TYPES_INFO[a.type]);
                // d'abord les alertes non vues (avec leur anciennete), puis les dernieres infos
                const liste = nonVues.slice(0, 8).map((a) => {
                    const age = duree(secondesDepuis(a.created_at, true));
                    return {
                        text: (a.dt_code && a.dt_code !== '-' ? a.dt_code + ' · ' : '') + (a.message || a.type) +
                              (age ? ' · alerte non vue depuis ' + age : ''),
                        type: 'danger'
                    };
                });
                if (!liste.length) {
                    const info = toutes.find((a) => TYPES_INFO[a.type]);
                    liste.push(info
                        ? { text: (info.dt_code && info.dt_code !== '-' ? info.dt_code + ' · ' : '') + (info.message || info.type), type: 'success' }
                        : { text: 'Aucune alerte active', type: 'success' });
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
                const echecs = rows.filter((r) => r.statut === 'echec').length;
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
        if (e.key === 'oreTrackingKpi') { appliquerAffichageKpi(); return; }
        if (e.key === 'oreTrackingKpiPlan') { appliquerObjectifs(); renderKpis(); return; }
        if (e.key === 'oreTrackingConfig' || e.key === 'oreTrackingTrails') {
            setTimeout(() => location.reload(), 300);
        }
    });

    // ===== CARTE =====
    const BASEMAPS = {
        'osm':       { nom: 'Plan · OpenStreetMap', url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png', maxZoom: 19 },
        'esri-sat':  { nom: 'Vue satellite', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', maxZoom: 19 },
        'esri-topo': { nom: 'Vue topographique', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}', maxZoom: 19 },
        'esri-dark': { nom: 'Vue sombre', url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', maxZoom: 16 }
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
        } catch (e) { console.warn('[Dashboard] zones CSV illisibles :', e); }
        return [];
    }

    async function chargerTrails(config) {
        try {
            const saved = JSON.parse(localStorage.getItem('oreTrackingTrails') || 'null');
            if (saved && saved.length) return saved;
        } catch (e) {}
        if (config && config.trails && config.trails.length) return config.trails;
        try {
            const resp = await fetch('../data/Trails.geojson.geojson');
            if (resp.ok) {
                const geo = await resp.json();
                return (geo.features || [])
                    .filter((f) => f.geometry && f.geometry.coordinates && f.geometry.coordinates.length >= 2)
                    .map((f) => ({
                        type: 'imported', name: (f.properties && f.properties.name) || 'Trail',
                        color: '#EE7A2A', useColor: true,
                        waypoints: f.geometry.coordinates.map((c) => [c[1], c[0]])
                    }));
            }
        } catch (e) { console.warn('[Dashboard] pistes GeoJSON illisibles :', e); }
        return [];
    }

    // Resume des zones pour le sous-titre : « West Pit · Main Pit · ROM Pads 1, 2 et 3 »
    function resumeZones(zones) {
        const noms = zones.map((z) => z.name);
        const roms = noms.filter((n) => /^rom\s*pad\s*\d+$/i.test(n)).map((n) => n.match(/\d+/)[0]);
        const autres = noms.filter((n) => !/^rom\s*pad\s*\d+$/i.test(n));
        if (roms.length > 1) {
            autres.push('ROM Pads ' + roms.slice(0, -1).join(', ') + ' et ' + roms[roms.length - 1]);
        } else if (roms.length) {
            autres.push('ROM Pad ' + roms[0]);
        }
        return autres.join(' · ') || 'Aucune zone configurée';
    }

    const PIN_SVG = '<svg class="pin" viewBox="0 0 28 36"><path d="M14 1C6.8 1 1 6.7 1 13.8 1 23 14 35 14 35s13-12 13-21.2C27 6.7 21.2 1 14 1z" fill="#0E74B2" stroke="#FCF9EA" stroke-width="1.5"/><circle cx="14" cy="13.5" r="5.2" fill="#FCF9EA"/></svg>';

    function iconeZone(z, showLabels) {
        const role = /pit|fosse/i.test(z.name) ? ['load', 'Chargement DT']
                   : /rom|pad|dump|stock/i.test(z.name) ? ['unload', 'Déchargement DT'] : null;
        const tags = showLabels
            ? '<span class="tags"><span class="name"></span>' +
              (role ? '<span class="role ' + role[0] + '">' + role[1] + '</span>' : '') + '</span>'
            : '';
        const html = document.createElement('div');
        html.className = 'zone-pin';
        html.innerHTML = PIN_SVG + tags;
        const nom = html.querySelector('.name');
        if (nom) nom.textContent = z.name;     // nom de zone saisi : texte, jamais HTML
        return L.divIcon({ className: 'zone-marker', html, iconSize: [0, 0], iconAnchor: [0, 0] });
    }

    async function initMap() {
        let config = null;
        try { config = JSON.parse(localStorage.getItem('oreTrackingConfig')); } catch (e) {}

        const centerLat = (config && config.center && config.center.lat) || 8.2626;
        const centerLng = (config && config.center && config.center.lng) || -4.6301;
        const zoomVal = (config && config.zoom) || 14;
        const showZones = !config || config.showZones !== false;
        const showTrails = !config || config.showTrails !== false;
        const showLabels = !config || config.showLabels !== false;

        const zones = await chargerZones(config);
        const trailsDef = await chargerTrails(config);
        $('mapZones').textContent = resumeZones(zones);

        const map = L.map('map', {
            center: [centerLat, centerLng], zoom: zoomVal,
            zoomControl: false, attributionControl: false
        });

        // ---- fond ----
        const basemapId = config && config.basemap;
        if (basemapId === 'mbtiles' && config.mbtilesUrl) {
            L.tileLayer(config.mbtilesUrl.replace(/\/$/, '') + '/{z}/{x}/{y}.png', { maxZoom: 19, errorTileUrl: '' }).addTo(map);
            $('basemapName').textContent = 'Fond local · MBTiles';
        } else {
            const bm = BASEMAPS[basemapId] || BASEMAPS['esri-sat'];
            L.tileLayer(bm.url, { crossOrigin: true, maxZoom: bm.maxZoom }).addTo(map);
            $('basemapName').textContent = bm.nom;
        }

        // ---- couches importees sur Live (TIFF / GeoJSON / MBTiles) ----
        const mbtilesBase = (config && config.mbtilesUrl ? config.mbtilesUrl : 'http://localhost:8080').replace(/\/$/, '');
        const couches = [].concat((config && config.layers) || [], (config && config.tiffLayers) || []);
        couches.forEach((layer) => {
            if (!layer.visible) return;
            const op = layer.opacity || 0.8;
            if ((layer.type === 'tiff' || layer.type === 'georef' || !layer.type) && layer.dataUrl && layer.bounds) {
                L.imageOverlay(layer.dataUrl, layer.bounds, { opacity: op, interactive: false }).addTo(map);
            } else if (layer.type === 'geojson' && layer.geoJSON) {
                L.geoJSON(layer.geoJSON, {
                    style: () => ({ color: '#EE7A2A', weight: 2, opacity: op, fillColor: '#EE7A2A', fillOpacity: op * 0.35 }),
                    pointToLayer: (f, ll) => L.circleMarker(ll, { radius: 5, color: '#EE7A2A', weight: 2, fillOpacity: op * 0.6 })
                }).addTo(map);
            } else if (layer.type === 'mbtiles' && layer.mbtilesName) {
                L.tileLayer(mbtilesBase + '/' + encodeURIComponent(layer.mbtilesName) + '/{z}/{x}/{y}.png',
                    { maxZoom: 19, errorTileUrl: '', opacity: op }).addTo(map);
            }
        });

        // ---- pistes : trait plein sous un tirete clair = « piste roulee » ----
        let segments = 0;
        trailsDef.forEach((t) => {
            let wp = (t.waypoints && t.waypoints.length >= 2) ? t.waypoints : null;
            if (!wp) {
                const a = zones.find((z) => z.name === t.from), b = zones.find((z) => z.name === t.to);
                if (!a || !b) return;
                wp = [[a.lat, a.lng], [b.lat, b.lng]];
            }
            segments += wp.length - 1;
            if (!showTrails) return;
            const couleur = t.useColor === false ? '#B7B3A3' : (t.color || '#EE7A2A');
            L.polyline(wp, { color: '#000', weight: 7, opacity: 0.35, interactive: false }).addTo(map);
            L.polyline(wp, { color: couleur, weight: 4, opacity: 0.9, interactive: false }).addTo(map);
            L.polyline(wp, { color: '#FCF9EA', weight: 1.2, opacity: 0.7, dashArray: '2 10', interactive: false }).addTo(map);
        });
        $('segmentsPiste').textContent = fmt(segments);

        // ---- zones ----
        if (showZones) {
            zones.forEach((z) => {
                if (!z.lat || !z.lng) return;
                L.marker([z.lat, z.lng], { icon: iconeZone(z, showLabels), zIndexOffset: 200 }).addTo(map);
            });
        }

        $('zoomIn').addEventListener('click', () => map.zoomIn());
        $('zoomOut').addEventListener('click', () => map.zoomOut());
        $('zoomCenter').addEventListener('click', () => map.setView([centerLat, centerLng], zoomVal));

        // ---- DT reels (/api/engins de la passerelle) ----
        const dtMarkers = {};
        const POSITION_PERIMEE_S = 120;   // au-dela, DT grise (signal perdu)

        function iconeDT(code, offroad, perime) {
            return L.divIcon({
                className: 'zone-marker dt-marker' + (offroad ? ' offroad' : '') + (perime ? ' perime' : ''),
                html: (offroad ? '<span class="dt-halo"></span>' : '') + '<img src="../assets/icon DT.png" class="dt-icon" alt="">',
                iconSize: [28, 28], iconAnchor: [14, 14]
            });
        }

        function majCamions() {
            getJSON(PASSERELLE + '/api/engins')
                .then((d) => {
                    const horsPiste = new Set((data.offroad.dts || []).map((x) => x.code));
                    const vus = new Set();
                    (d.engins || [])
                        .filter((e) => e.type === 'DT' && Number(e.actif) !== 0)
                        .forEach((e) => {
                            const ll = convertCRS(Number(e.x), Number(e.y));
                            if (!ll.lat && !ll.lng) return;
                            vus.add(e.code);
                            const offroad = horsPiste.has(e.code) || Number(e.offroad) === 1;
                            const perime = secondesDepuis(e.derniere_maj) > POSITION_PERIMEE_S;
                            const etatDT = (offroad ? 'o' : '') + (perime ? 'p' : '');
                            let m = dtMarkers[e.code];
                            if (!m) {
                                m = L.marker([ll.lat, ll.lng], { zIndexOffset: 500 }).addTo(map);
                                m._etat = null;
                                dtMarkers[e.code] = m;
                            } else {
                                m.setLatLng([ll.lat, ll.lng]);
                            }
                            if (m._etat !== etatDT) {
                                m.setIcon(iconeDT(e.code, offroad, perime));
                                m.setZIndexOffset(offroad ? 1000 : 500);
                                m.unbindTooltip();
                                m.bindTooltip('', {
                                    permanent: showLabels || offroad, direction: 'top', offset: [0, -14],
                                    className: 'vehicle-tooltip' + (offroad ? ' offroad' : '') + (perime ? ' perime' : '')
                                });
                                m._etat = etatDT;
                            }
                            const dist = offroad && e.offroad_distance ? ' · ' + Math.round(e.offroad_distance) + ' m' : '';
                            m.setTooltipContent(e.code + dist);
                        });
                    Object.keys(dtMarkers).forEach((code) => {
                        if (!vus.has(code)) { map.removeLayer(dtMarkers[code]); delete dtMarkers[code]; }
                    });
                    $('enginsSuivis').textContent = vus.size;
                })
                .catch(() => { /* passerelle hors ligne : dernieres positions conservees */ });
        }
        majCamions();
        setInterval(majCamions, 3000);
        majCamionsMaintenant = majCamions;

        // la carte suit la taille de son cadre (blocs masques, polices chargees...)
        new ResizeObserver(() => map.invalidateSize()).observe($('map'));
        // a faible zoom, les roles (Chargement / Dechargement) se chevauchent : on les masque
        const majZoom = () => $('map').classList.toggle('z-low', map.getZoom() < 15);
        map.on('zoomend', majZoom); majZoom();
        return map;
    }

    // ===== DEMARRAGE =====
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
    refreshOffroad();      setInterval(refreshOffroad, 3000);
    refreshAlertes();      setInterval(refreshAlertes, 8000);
    refreshTablettes();    setInterval(refreshTablettes, 30000);
    refreshLiens();        setInterval(refreshLiens, 15000);
    setTimeout(afficherAlerte, 1500);
    setInterval(afficherAlerte, 6000);

    initMap().catch((e) => console.error('[Dashboard] carte :', e));
});
