/**
 * Bouton REC de la carte (dashboard / Live). L'enregistrement est fait PAR LA PASSERELLE, en arriere-plan :
 * elle ouvre le dashboard dans un navigateur sans ecran, photographie la carte reelle (DT en mouvement,
 * zones, pistes) et en tire une video WebM acceleree. Ce bouton ne fait que demarrer / arreter et afficher
 * le chrono ; il transmet les reglages de carte de ce navigateur (filtres, pistes, fond) pour que la video
 * montre la meme carte que celle que tu regardes.
 *
 *   EnregistreurCarte.attacher({ bouton, chrono, vitesse: () => 1, passerelle: () => 'http://hote:8101', message: (texte, ok) => {} })
 */
(function (global) {
    'use strict';

    const CLES_PAGE = ['oreTrackingConfig', 'oreTrackingTrails', 'oreTrackingFiltres', 'oreTrackingDiffusion'];
    const deux = (n) => String(n).padStart(2, '0');

    function attacher(opts) {
        const bouton = opts.bouton, chrono = opts.chrono || null;
        const dire = opts.message || function () {};
        let debut = null, minuteur = null, occupe = false;
        const base = () => String(opts.passerelle()).replace(/\/+$/, '');

        async function appel(action) {
            const etat = {};
            if (action === 'start') {
                CLES_PAGE.forEach((c) => { try { const v = localStorage.getItem(c); if (v) etat[c] = v; } catch (e) { /* stockage indisponible */ } });
            }
            const r = await fetch(base() + '/api/enregistrements/record', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: action, etat: etat, vitesse: opts.vitesse ? Number(opts.vitesse()) || 1 : 1,
                    qualite: opts.qualite ? opts.qualite() : 'haute' }),
            });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) {
                const e = new Error(r.status === 401 || r.status === 403
                    ? 'Connexion requise pour enregistrer (' + (d.error || r.status) + ') : utilise la page Live.'
                    : (d.error || 'Échec (' + r.status + ')'));
                e.status = r.status;
                throw e;
            }
            return d;
        }

        function afficher() {
            const actif = !!debut;
            bouton.classList.toggle('en-cours', actif);
            bouton.textContent = actif ? '■ Arrêter' : '● REC';
            bouton.title = actif ? 'Arrêter et créer la vidéo de la carte' : 'Enregistrer la carte en vidéo';
            if (chrono) chrono.hidden = !actif;
            (opts.selecteurs || []).forEach((s) => { s.disabled = actif; });      // vitesse et qualite se choisissent avant de demarrer
            clearInterval(minuteur); minuteur = null;
            if (actif) {
                const tic = () => {
                    const s = Math.max(0, Math.floor((Date.now() - debut.getTime()) / 1000));
                    const t = deux(Math.floor(s / 3600)) + ':' + deux(Math.floor(s / 60) % 60) + ':' + deux(s % 60);
                    if (chrono) chrono.textContent = t; else bouton.textContent = '■ ' + t;
                };
                tic(); minuteur = setInterval(tic, 1000);
            }
        }

        // l'etat vient de la passerelle : le chrono est le meme sur toutes les pages et tous les postes
        async function lireEtat() {
            try {
                const d = await fetch(base() + '/api/enregistrements', { cache: 'no-store' }).then((r) => r.json());
                const nouveau = d.record && d.record.debut ? new Date(d.record.debut.replace(' ', 'T')) : null;
                if ((nouveau && nouveau.getTime()) !== (debut && debut.getTime())) { debut = nouveau; afficher(); }
                if (d.record && d.record.vitesse && opts.selVitesse) opts.selVitesse.value = String(d.record.vitesse);
                if (d.record && d.record.qualite && opts.selQualite) opts.selQualite.value = d.record.qualite;
                if (d.record && d.record.erreur) dire('Enregistrement : ' + d.record.erreur, false);
            } catch (e) { /* passerelle injoignable : on garde l'affichage */ }
        }

        bouton.addEventListener('click', async () => {
            if (occupe) return;
            occupe = true; bouton.disabled = true;
            try {
                if (debut) {
                    dire('Arrêt… la vidéo se termine.', true);
                    const d = await appel('stop');
                    debut = null; afficher();
                    dire('Vidéo enregistrée : ' + d.fichier + ' — ' + d.message, true);
                    document.dispatchEvent(new CustomEvent('enregistrements:maj'));
                } else {
                    const d = await appel('start');
                    debut = new Date(d.record.debut.replace(' ', 'T')); afficher();
                    dire('Enregistrement démarré (en arrière-plan sur la passerelle).', true);
                }
            } catch (e) {
                dire(e.message || String(e), false);
                await lireEtat();
            } finally { occupe = false; bouton.disabled = false; }
        });

        lireEtat();
        setInterval(lireEtat, 8000);
        return { lireEtat };
    }

    global.EnregistreurCarte = { attacher };
})(window);
