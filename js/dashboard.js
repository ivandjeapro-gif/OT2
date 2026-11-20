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
    // Tablettes connectees = DT dont la tablette est en ligne sur le broker
    // MQTT (presence + positions recentes), lu sur la passerelle : sans
    // session, contrairement a l'ancienne liste /api/tablettes (19/11).
    function refreshTablettes() {
        getJSON(PASSERELLE + '/api/mqtt/comptes').then((d) => {
            if (!d.configure) {
                $('tabletCount').textContent = '--';
                $('tabletList').textContent = 'Suivi temps réel non installé';
                return;
            }
            const enLigne = (d.comptes || []).filter((c) => c.en_ligne).map((c) => c.code);
            $('tabletCount').textContent = deux(enLigne.length);
            $('tabletList').textContent = enLigne.length
                ? enLigne.join(' · ')
                : 'Aucune tablette en ligne (' + (d.comptes || []).length + ' DT équipés)';
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
                el.textContent = actifs.length ? 'Filtre : ' + actifs.join(' · ') : '';
            },
        });
        return carte.map;
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
    refreshTablettes();    setInterval(refreshTablettes, 5000);
    refreshLiens();        setInterval(refreshLiens, 15000);
    setTimeout(afficherAlerte, 1500);
    setInterval(afficherAlerte, 6000);

    initMap().catch((e) => console.error('[Dashboard] carte :', e));
});
