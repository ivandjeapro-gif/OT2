/* =====================================================================
 * ot-config.js - point unique de configuration des serveurs.
 *
 * Avant, chaque page codait son endpoint en dur (":8100" dans api.js,
 * ":8101" dans senor.js et digplan.js). Toute bascule imposait d'editer
 * plusieurs fichiers. Ici tout est regroupe et persiste en localStorage.
 *
 *   OTCFG.saas()      -> SaaS local (pages, API SQLite)        :8100
 *   OTCFG.passerelle()-> Senor Geology / passerelle terrain    :8101
 *   OTCFG.ot()        -> { url, token } du Serveur_OT (Apps Script)
 *
 * A charger AVANT les scripts de page.
 * ===================================================================== */
(function (global) {
  'use strict';

  const CLES = {
    saas: 'otSaasBase',
    passerelle: 'sgApiBase',   // nom historique, conserve pour ne rien casser
    otUrl: 'dpOtUrl',
    otToken: 'dpOtToken',
    backend: 'dpBackend',
  };

  function lire(cle, defaut) {
    try { return localStorage.getItem(cle) || defaut; } catch (e) { return defaut; }
  }
  function ecrire(cle, valeur) {
    try {
      if (valeur) localStorage.setItem(cle, valeur);
      else localStorage.removeItem(cle);
    } catch (e) { /* storage indisponible */ }
  }
  function sansSlash(u) { return String(u || '').replace(/\/+$/, ''); }

  const OTCFG = {
    /** SaaS local : sert les pages et l'API SQLite principale. */
    saas() {
      const saved = lire(CLES.saas, '');
      if (saved) return sansSlash(saved);
      return global.location ? global.location.origin : 'http://localhost:8100';
    },

    /** Passerelle Senor Geology (terrain) : positions, missions, oreblocs. */
    passerelle() {
      const saved = lire(CLES.passerelle, '');
      if (saved) return sansSlash(saved);
      const host = (global.location && global.location.hostname) || 'localhost';
      const proto = (global.location && global.location.protocol) || 'http:';
      return proto + '//' + host + ':8101';
    },

    /** Serveur_OT (Apps Script). Jette si non configure. */
    ot() {
      const url = sansSlash(lire(CLES.otUrl, ''));
      const token = lire(CLES.otToken, '');
      if (!url) throw new Error('URL du Serveur_OT non configuree (page Settings)');
      if (!token) throw new Error('Jeton du Serveur_OT non configure (page Settings)');
      return { url: url, token: token };
    },

    otConfigure() {
      return !!(lire(CLES.otUrl, '') && lire(CLES.otToken, ''));
    },

    /** 'sg' (passerelle locale) ou 'ot' (Apps Script). */
    backend() {
      return String(lire(CLES.backend, 'sg')).toLowerCase() === 'ot' ? 'ot' : 'sg';
    },

    definir(conf) {
      if (conf.saas !== undefined) ecrire(CLES.saas, sansSlash(conf.saas));
      if (conf.passerelle !== undefined) ecrire(CLES.passerelle, sansSlash(conf.passerelle));
      if (conf.otUrl !== undefined) ecrire(CLES.otUrl, sansSlash(conf.otUrl));
      if (conf.otToken !== undefined) ecrire(CLES.otToken, conf.otToken);
      if (conf.backend !== undefined) ecrire(CLES.backend, conf.backend);
    },

    /**
     * Appel au Serveur_OT. Apps Script ne rend ni en-tetes ni codes HTTP :
     * tout sort en 200 avec un marqueur {ok:...}, et le jeton voyage en
     * parametre de requete. Content-Type text/plain volontaire : evite le
     * preflight CORS, qu'Apps Script ne sait pas traiter.
     */
    async appelOt(methode, action, params, corps) {
      const { url, token } = OTCFG.ot();
      let q = url + '?action=' + encodeURIComponent(action) +
              '&token=' + encodeURIComponent(token);
      for (const k in (params || {})) {
        if (params[k] !== undefined && params[k] !== null) {
          q += '&' + k + '=' + encodeURIComponent(params[k]);
        }
      }
      const opts = { method: methode, redirect: 'follow' };
      if (corps !== undefined) {
        opts.headers = { 'Content-Type': 'text/plain;charset=utf-8' };
        opts.body = JSON.stringify(corps);
      }
      const r = await fetch(q, opts);
      let j = {};
      try { j = await r.json(); } catch (e) {
        throw new Error('reponse illisible du Serveur_OT (HTTP ' + r.status + ')');
      }
      if (j.ok === false) throw new Error(j.erreur || 'Serveur_OT a refuse la requete');
      return j;
    },

    /**
     * Pousse le referentiel minier (fosses + familles de teneur) du SaaS
     * vers Serveur_OT, qui l'ecrit dans les onglets dig_sources/dig_teneurs.
     *
     * Le classeur n'est JAMAIS saisi a la main : la source de verite reste
     * la configuration du SaaS (page Senor Geology), le serveur se contente
     * de la recopier. Remplacement integral a chaque envoi.
     */
    async pousserReferentiel() {
      const base = OTCFG.passerelle();
      const rs = await fetch(base + '/api/sources');
      if (!rs.ok) throw new Error('passerelle injoignable sur ' + base);
      const ds = await rs.json();
      const brutes = ds.sources || [];
      if (!brutes.length) throw new Error('aucune source definie dans le SaaS');

      // Zones de deversement : indexees par id pour pouvoir traduire le
      // dumping_area_id d'une teneur en NOM. Sans cette traduction, la
      // consigne arrivait vide chez Serveur_SG et les camions recevaient
      // une destination sans lieu de deversement.
      const rz = await fetch(base + '/api/dumping_areas');
      const dz = await rz.json();
      const zonesBrutes = dz.dumping_areas || [];
      const parId = {};
      for (const z of zonesBrutes) parId[z.id] = z.nom;

      const dumpingAreas = zonesBrutes.map(function (z) {
        let sommets = [];
        try { sommets = JSON.parse(z.sommets || '[]'); } catch (e) { sommets = []; }
        return { nom: z.nom, x: z.x, y: z.y, sommets: sommets };
      });

      // Checkpoints (17/11) : lignes de passage, deux extremites en UTM.
      let checkpoints = [];
      try {
        const rc = await fetch(base + '/api/checkpoints');
        const dc = await rc.json();
        checkpoints = (dc.checkpoints || []).map(function (c) {
          return { nom: c.nom, points: [[c.x1, c.y1], [c.x2, c.y2]] };
        });
      } catch (e) { checkpoints = null; }   // passerelle ancienne : onglet garde

      const sources = brutes.map(function (s) { return { nom: s.nom }; });
      const teneurs = [];
      for (const s of brutes) {
        const rt = await fetch(base + '/api/teneurs?source_id=' + encodeURIComponent(s.id));
        const dt = await rt.json();
        for (const t of (dt.teneurs || [])) {
          teneurs.push({
            source: s.nom, nom: t.nom,
            valeur_min: t.valeur_min, valeur_max: t.valeur_max,
            dumping_area: parId[t.dumping_area_id] || '',
          });
        }
      }
      // Repere geographique du site. Un classeur plein de coordonnees qui
      // ne dit pas dans quel systeme elles sont exprimees est un piege :
      // on l'envoie avec le reste du referentiel.
      let geo = {};
      try {
        const rg = await fetch(base + '/api/geographic_system');
        const dg = await rg.json();
        geo = {
          systeme: dg.systeme || '', zone: dg.zone == null ? '' : String(dg.zone),
          hemisphere: dg.hemisphere || '', datum: dg.datum || '',
        };
      } catch (e) { geo = {}; }

      return OTCFG.appelOt('POST', 'dig_referentiel', null, {
        sources: sources, teneurs: teneurs, dumping_areas: dumpingAreas,
        geo: geo, checkpoints: checkpoints || undefined,
      });
    },

    /**
     * Pousse le GeoJSON des pistes du SaaS vers Drive. Le serveur le range
     * dans Pistes/ et enregistre lui-meme FICHIER_PISTES_ID : aucun
     * identifiant a recopier a la main.
     */
    async pousserPistes(chemin) {
      const src = chemin || '../data/Trails.geojson.geojson';
      const rep = await fetch(src);
      if (!rep.ok) throw new Error('GeoJSON introuvable dans le SaaS : ' + src);
      const contenu = await rep.text();
      const nom = src.split('/').pop().replace(/\.geojson\.geojson$/, '.geojson');
      return OTCFG.appelOt('POST', 'pistes_upload', null, { nom: nom, contenu: contenu });
    },
  };

  global.OTCFG = OTCFG;
})(window);
