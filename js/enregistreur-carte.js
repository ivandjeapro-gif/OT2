/**
 * Enregistreur de carte (dashboard / Live) : filme la carte TELLE QU'ELLE S'AFFICHE (DT en mouvement,
 * zones, pistes) et envoie le flux, par morceaux de 5 s, a la passerelle qui l'assemble en un fichier WebM.
 *
 * Fonctionnement (Chrome / Edge) : « Region Capture » de la page courante. Le navigateur demande une fois
 * l'autorisation de partager l'onglet ; seule la zone de la carte est enregistree. L'onglet doit rester
 * affiche (au premier plan d'une fenetre) pendant l'enregistrement.
 *
 *   EnregistreurCarte.attacher({ bouton, chrono, element, passerelle: () => 'http://hote:8101',
 *                                message: (texte, ok) => {} })
 */
(function (global) {
    'use strict';

    const DECOUPE_MS = 5000;

    function supporte() {
        return !!(global.MediaRecorder && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia && global.CropTarget);
    }

    const deux = (n) => String(n).padStart(2, '0');
    const attendre = (ms) => new Promise((r) => setTimeout(r, ms));

    function attacher(opts) {
        const bouton = opts.bouton, chrono = opts.chrono || null;
        const dire = opts.message || function () {};
        let flux = null, enregistreur = null, file = Promise.resolve(), local = false, debutServeur = null;
        let minuteur = null, sonde = null, occupe = false;

        const base = () => String(opts.passerelle()).replace(/\/+$/, '');

        async function appel(chemin, corps, type) {
            const r = await fetch(base() + chemin, {
                method: 'POST', headers: { 'Content-Type': type || 'application/json' },
                body: corps === undefined ? '{}' : corps,
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

        // ---- affichage : bouton + chrono, quel que soit le poste qui a lance l'enregistrement
        function afficher() {
            const actif = !!debutServeur;
            bouton.classList.toggle('en-cours', actif);
            bouton.textContent = actif ? '■ Arrêter' : '● REC';
            bouton.title = actif ? 'Arrêter et enregistrer la vidéo de la carte' : 'Enregistrer la carte en vidéo';
            if (chrono) chrono.hidden = !actif;
            clearInterval(minuteur); minuteur = null;
            if (actif) {
                const tic = () => {
                    const s = Math.max(0, Math.floor((Date.now() - debutServeur.getTime()) / 1000));
                    const t = deux(Math.floor(s / 3600)) + ':' + deux(Math.floor(s / 60) % 60) + ':' + deux(s % 60);
                    if (chrono) chrono.textContent = t; else bouton.textContent = '■ ' + t;
                };
                tic(); minuteur = setInterval(tic, 1000);
            }
        }

        async function lireEtat() {
            try {
                const d = await fetch(base() + '/api/enregistrements', { cache: 'no-store' }).then((r) => r.json());
                const debut = d.record && d.record.debut ? new Date(d.record.debut.replace(' ', 'T')) : null;
                if ((debut && debut.getTime()) !== (debutServeur && debutServeur.getTime())) { debutServeur = debut; afficher(); }
            } catch (e) { /* passerelle injoignable : on garde l'etat affiche */ }
        }

        // ---- envoi des morceaux (en file : l'ordre est conserve), 3 essais chacun
        function envoyer(blob) {
            file = file.then(async () => {
                for (let essai = 1; essai <= 3; essai++) {
                    try { await appel('/api/enregistrements/morceau', blob, 'application/octet-stream'); return; }
                    catch (e) { if (essai === 3) dire('Envoi du flux interrompu : ' + e.message, false); else await attendre(1000); }
                }
            });
        }

        async function demarrer() {
            if (!supporte()) throw new Error('Enregistrement de la carte : utilise Chrome ou Edge.');
            // 1) l'utilisateur autorise le partage de CET onglet (rien n'est cree cote passerelle s'il refuse)
            flux = await navigator.mediaDevices.getDisplayMedia({
                video: { frameRate: 15 }, audio: false, preferCurrentTab: true, selfBrowserSurface: 'include',
            });
            const piste = flux.getVideoTracks()[0];
            try {
                await piste.cropTo(await global.CropTarget.fromElement(opts.element()));   // seulement la carte
            } catch (e) {
                flux.getTracks().forEach((t) => t.stop()); flux = null;
                throw new Error('Choisis « cet onglet » dans la fenêtre de partage (' + (e.message || e) + ')');
            }
            // 2) debut cote passerelle, puis capture
            const d = await appel('/api/enregistrements/record', JSON.stringify({ action: 'start' }));
            debutServeur = new Date(d.record.debut.replace(' ', 'T'));
            const type = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'].find((t) => MediaRecorder.isTypeSupported(t));
            enregistreur = new MediaRecorder(flux, { mimeType: type, videoBitsPerSecond: 2500000 });
            enregistreur.ondataavailable = (ev) => { if (ev.data && ev.data.size) envoyer(ev.data); };
            piste.addEventListener('ended', () => { if (local) arreter().catch((e) => dire(e.message, false)); });   // « Arreter le partage »
            local = true;
            enregistreur.start(DECOUPE_MS);
            afficher();
            dire('Enregistrement de la carte démarré. Garde cet onglet affiché.', true);
        }

        async function arreter() {
            if (local && enregistreur) {
                local = false;
                await new Promise((resolve) => {
                    enregistreur.addEventListener('stop', resolve, { once: true });
                    if (enregistreur.state !== 'inactive') enregistreur.stop(); else resolve();
                });
                flux.getTracks().forEach((t) => t.stop());
                flux = null; enregistreur = null;
                await file;                                              // dernier morceau envoye
            }
            const d = await appel('/api/enregistrements/record', JSON.stringify({ action: 'stop' }));
            debutServeur = null; afficher();
            dire('Vidéo enregistrée : ' + d.fichier + ' (' + d.message + ')', true);
            document.dispatchEvent(new CustomEvent('enregistrements:maj'));
        }

        bouton.addEventListener('click', async () => {
            if (occupe) return;
            occupe = true; bouton.disabled = true;
            try {
                if (debutServeur) await arreter(); else await demarrer();
            } catch (e) {
                if (e && e.name === 'NotAllowedError') dire('Partage refusé : enregistrement annulé.', false);
                else dire(e.message || String(e), false);
                await lireEtat();
            } finally { occupe = false; bouton.disabled = false; }
        });

        window.addEventListener('beforeunload', (ev) => { if (local) { ev.preventDefault(); ev.returnValue = ''; } });
        lireEtat();
        sonde = setInterval(lireEtat, 10000);
        return { arreter, lireEtat, supporte: supporte() };
    }

    global.EnregistreurCarte = { attacher, supporte };
})(window);
