/* =====================================================================
 * splash.js — retour visible pour TOUTE action.
 *
 * Regle du projet : aucun temps mort invisible. Toute operation qui
 * touche les donnees affiche immediatement ce qui se passe :
 *
 *   Splash.bloquer(titre, sousTitre)  -> overlay modal (ecritures)
 *   Splash.discret(texte)             -> bandeau haut-droite (lectures)
 *   Splash.etape(texte)               -> met a jour l'etape en cours
 *   Splash.succes(txt, details)       -> vert 2 s puis fondu
 *   Splash.erreur(txt)                -> rouge, persistant, fermable
 *   Splash.fermer()                   -> disparition immediate
 *
 * L'overlay bloque la page : fini les double-clics qui creent des
 * doublons (cf. blasts X7 du 07/11). Chaque splash renvoie un objet
 * `fermer()` pour les usages bout-en-bout.
 * ===================================================================== */
(function () {
  'use strict';

  let overlay = null;      // element modal (cree a la demande)
  let bandeau = null;      // bandeau discret
  let timerSucces = null;
  let operationsEnCours = 0;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
      (c) => ({ '&': '&', '<': '<', '>': '>', '"': '"', "'": '&#39;' }[c]));
  }

  // ------------------ overlay bloquant (ecritures) ------------------
  function bloquer(titre, sousTitre) {
    if (!document.body) return { fermer: function () { } };
    operationsEnCours++;
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'splash-overlay';
      overlay.innerHTML =
        '<div class="splash-carte" role="status" aria-live="polite">' +
        '<div class="splash-icones"><i class="fas fa-sync-alt splash-spin"></i></div>' +
        '<div class="splash-titre"></div>' +
        '<div class="splash-sous"></div>' +
        '<div class="splash-barre"><div class="splash-barre-fill"></div></div>' +
        '</div>';
      document.body.appendChild(overlay);
    }
    overlay.hidden = false;
    // force le reflow pour jouer l'animation d'apparition
    void overlay.offsetWidth;
    overlay.classList.add('visible');
    const ic = overlay.querySelector('.splash-icones');
    if (ic) ic.innerHTML = '<i class="fas fa-sync-alt splash-spin"></i>';
    const t = overlay.querySelector('.splash-titre');
    if (t) t.textContent = titre || 'Operation en cours...';
    const s = overlay.querySelector('.splash-sous');
    if (s) s.textContent = sousTitre || '';
    const barre = overlay.querySelector('.splash-barre');
    if (barre) barre.classList.remove('fini');
    return { fermer: fermer };
  }

  function etape(texte) {
    if (!overlay || overlay.hidden) return;
    const s = overlay.querySelector('.splash-sous');
    if (s) s.textContent = texte || '';
    const barre = overlay.querySelector('.splash-barre-fill');
    if (barre) {
      // progression indeterminee animee : elle avance par a-coups visuels
      barre.style.width = Math.min(90, (parseFloat(barre.style.width) || 15) + 12) + '%';
    }
  }

  function succes(titre, details, ms) {
    if (overlay && !overlay.hidden) {
      const ic = overlay.querySelector('.splash-icones');
      if (ic) ic.innerHTML = '<i class="fas fa-check-circle" style="color:var(--green,#22c55e)"></i>';
      const t = overlay.querySelector('.splash-titre');
      if (t) t.textContent = titre || 'Termine';
      const s = overlay.querySelector('.splash-sous');
      if (s) s.textContent = details || '';
      const barre = overlay.querySelector('.splash-barre');
      if (barre) barre.classList.add('fini');
    } else {
      // succes sans splash ouvert : petit bandeau vert
      discret('<i class="fas fa-check-circle"></i> ' + (titre || 'Termine'), 'ok', ms || 2500);
    }
    // laisse lire la confirmation puis fondu
    clearTimeout(timerSucces);
    timerSucces = setTimeout(fermer, ms || 1800);
  }

  function erreur(titre, details) {
    if (overlay && !overlay.hidden) {
      const ic = overlay.querySelector('.splash-icones');
      if (ic) ic.innerHTML = '<i class="fas fa-times-circle" style="color:#ef4444"></i>';
      const t = overlay.querySelector('.splash-titre');
      if (t) t.textContent = titre || 'Echec';
      const s = overlay.querySelector('.splash-sous');
      if (s) s.textContent = details || '';
      const barre = overlay.querySelector('.splash-barre');
      if (barre) barre.classList.add('fini', 'erreur');
    } else {
      discret('<i class="fas fa-times-circle"></i> ' + (titre || 'Echec') +
        (details ? ' — ' + details : ''), 'ko', 6000);
    }
    // une erreur se lit : fermeture plus longue
    clearTimeout(timerSucces);
    timerSucces = setTimeout(fermer, 3500);
  }

  function fermer() {
    operationsEnCours = Math.max(0, operationsEnCours - 1);
    if (overlay) {
      overlay.classList.remove('visible');
      // filet de securite : quelle que soit l'etat du compteur (double
      // fermer, succes+fermer...), l'overlay finit TOUJOURS masque.
      setTimeout(() => {
        if (overlay && !overlay.classList.contains('visible')) {
          overlay.hidden = true;
          operationsEnCours = 0;
        }
      }, 300);
    }
  }

  // ------------------ bandeau discret (lectures / sync) ------------------
  function discret(html, classe, ms) {
    if (!document.body) return;
    if (!bandeau) {
      bandeau = document.createElement('div');
      bandeau.className = 'splash-bandeau';
      document.body.appendChild(bandeau);
    }
    bandeau.innerHTML = html;
    bandeau.className = 'splash-bandeau visible ' + (classe || '');
    clearTimeout(bandeau._timer);
    if (ms !== 0) {
      bandeau._timer = setTimeout(() => bandeau.classList.remove('visible'), ms || 2500);
    }
  }

  /** Enrobe une promesse : splash bloquant automatique.
   *  Usage : Splash.autour('Ajout du blast...', 'Extraction...', () => api.enregistrer(...), 'Blast cree') */
  function autour(titre, sousTitre, promesseOuFn, succesMsg) {
    const sp = bloquer(titre, sousTitre);
    const p = (typeof promesseOuFn === 'function') ? promesseOuFn() : promesseOuFn;
    return Promise.resolve(p)
      .then((res) => {
        sp.fermer();
        if (succesMsg) succes(succesMsg);
        return res;
      })
      .catch((err) => {
        sp.fermer();
        erreur('Echec', err && err.message ? err.message : String(err));
        throw err; // l'appelant peut toujours attraper l'erreur
      });
  }

  /** Confirmation elegante (meme design que le splash), remplace les
   *  confirm() natifs du navigateur partout.
   *  Usage : if (!await Splash.confirmer('Supprimer X ?')) return;
   *  opts = { titre, libelleOk, danger, details } */
  function confirmer(message, opts) {
    const o = opts || {};
    return new Promise((resolve) => {
      const el = document.createElement('div');
      el.className = 'splash-confirm-overlay';
      el.innerHTML =
        '<div class="splash-confirm' + (o.danger === false ? '' : ' danger') + '" role="alertdialog" aria-modal="true">' +
          '<div class="splash-confirm-ico"><i class="fas fa-' + (o.danger === false ? 'question-circle' : 'exclamation-triangle') + '"></i></div>' +
          '<h3></h3>' +
          '<p></p>' +
          (o.details ? '<small></small>' : '') +
          '<div class="splash-confirm-actions">' +
            '<button type="button" class="splash-btn-annuler">Annuler</button>' +
            '<button type="button" class="splash-btn-ok">' + (o.libelleOk || 'Confirmer') + '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(el);
      void el.offsetWidth;
      el.classList.add('visible');
      const h = el.querySelector('h3');
      if (h) h.textContent = o.titre || 'Confirmer';
      const p = el.querySelector('p');
      if (p) p.textContent = message;
      const d = el.querySelector('small');
      if (d && o.details) d.textContent = o.details;
      const btnOk = el.querySelector('.splash-btn-ok');
      const btnNon = el.querySelector('.splash-btn-annuler');
      let repondu = false;
      function fermer(reponse) {
        if (repondu) return;
        repondu = true;
        el.classList.remove('visible');
        setTimeout(() => el.remove(), 200);
        resolve(reponse);
      }
      btnOk.addEventListener('click', () => fermer(true));
      btnNon.addEventListener('click', () => fermer(false));
      el.addEventListener('click', (e) => { if (e.target === el) fermer(false); });
      document.addEventListener('keydown', function onEsc(e) {
        if (e.key === 'Escape') { document.removeEventListener('keydown', onEsc); fermer(false); }
      });
      setTimeout(() => btnOk.focus(), 60);
    });
  }

  window.Splash = { bloquer, etape, succes, erreur, fermer, discret, autour, confirmer };
})();
