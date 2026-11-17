/* OreTracking V2 - GNSS provisioning via Web Serial API
 * Wizard "Unlock and Initialize" (panneau Tablette).
 *
 * Methode 2 (primaire) : interrogation UBX-SEC-UNIQID -> chip ID unique
 *                         -> match dans le registre GNSS -> binding auto.
 * Methode 1 (repli)     : timeout 3000 ms -> modal de selection manuelle.
 *
 * La lecture serie est un flux continu : l'objet SerialPort reste ouvert
 * apres le pairing et sert ensuite a parser les trames $GNGGA.
 */
(function () {
  'use strict';

  var UBlox_UNIQID_CMD = [0xB5, 0x62, 0x27, 0x03, 0x00, 0x00, 0x2A, 0xA5];
  var UNIQID_TIMEOUT_MS = 3000;
  var GNSS_BAUD = 115200;

  // Registre local : chip ID (hex) -> libelle GNSS (etiquette physique)
  var GNSS_REGISTRY = {
    '4A8B9C12': 'GNSS-01',
    '7F3E21D0': 'GNSS-02',
    '91C4B7A5': 'GNSS-03'
  };

  var activePort = null;      // SerialPort maintenu ouvert (stream $GNGGA)
  var activeReader = null;    // actuellement locke (release avant pause/close)
  var streamAbort = null;     // AbortController du flux de geoloc
  var currentBinding = null;  // { gnss, terminal, method }

  function $(sel) { return document.querySelector(sel); }

  // ---------- Catalogue GNSS (inventaire enregistre) ----------
  // Les GNSS du site vivent dans otGnssCatalog (localStorage) : [{id, addedAt}]
  // Le pairing (serie ou manuel) les LIE ensuite a une tablette. Le chip ID
  // resolu en auto (GNSS_REGISTRY ci-dessus) peut n'exister que dans le
  // regitre UBX : on l'ajoute au catalogue au moment du binding.
  function loadGnssCatalog() {
    try { return JSON.parse(localStorage.getItem('otGnssCatalog')) || []; }
    catch (err) { return []; }
  }
  function saveGnssCatalog(list) {
    try { localStorage.setItem('otGnssCatalog', JSON.stringify(list)); } catch (err) {}
  }
  function ensureCatalogEntry(gnssLabel) {
    var list = loadGnssCatalog();
    if (!list.some(function (r) { return r.id === gnssLabel; })) {
      list.push({ id: gnssLabel, addedAt: Date.now() });
      saveGnssCatalog(list);
    }
  }

  function setStatus(msg, ok) {
    var el = $('#wizardStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' ok' : ' err') : '');
  }

  function renderBindResult(binding) {
    var el = $('#gnssBindResult');
    if (!el) return;
    if (!binding) { el.innerHTML = ''; return; }
    el.innerHTML =
      '<div class="account-item gnss-bound">' +
        '<i class="fas fa-satellite-dish"></i>' +
        '<span class="account-nom">' + binding.gnss + '</span>' +
        '<span class="account-email">→ ' + binding.terminal + ' · ' +
          (binding.method === 'auto' ? 'AUTO DETECT' : 'MANUAL FALLBACK') + '</span>' +
      '</div>';
  }

  // ---------- Web Serial : ouverture du port ----------

  function openSerialPort() {
    if (!navigator.serial || !navigator.serial.requestPort) {
      return Promise.reject(new Error('Web Serial non disponible (HTTPS/Chrome requis)'));
    }
    return navigator.serial.requestPort().then(function (port) {
      return port.open({ baudRate: GNSS_BAUD }).then(function () {
        activePort = port;
        return port;
      });
    });
  }

  function readChunk(port) {
    if (!port.readable) return Promise.resolve(null);
    var reader = port.readable.getReader();
    activeReader = reader;
    return reader.read().then(function (res) {
      try { reader.releaseLock(); } catch (e) {}
      if (activeReader === reader) activeReader = null;
      if (res.done) return null;
      return res.value; // Uint8Array
    }, function (err) {
      try { reader.releaseLock(); } catch (e) {}
      if (activeReader === reader) activeReader = null;
      throw err;
    });
  }

  // ---------- Parsing UBX-SEC-UNIQID ----------

  function findUniqId(buf) {
    // header B5 62 27 03
    for (var i = 0; i + 7 < buf.length; i++) {
      if (buf[i] === 0xB5 && buf[i + 1] === 0x62 &&
          buf[i + 2] === 0x27 && buf[i + 3] === 0x03) {
        var len = buf[i + 4] | (buf[i + 5] << 8);
        // payload commence a i+6 ; UBX-SEC-UNIQID : version(1) reserved(3) uniqueId(5 ou 6)
        if (i + 6 + len > buf.length) return null;
        var idBytes = [];
        for (var j = i + 10; j < i + 6 + len && idBytes.length < 6; j++) {
          idBytes.push(buf[j]);
        }
        if (idBytes.length < 5) return null;
        var hex = '';
        for (var k = 0; k < idBytes.length; k++) {
          hex += ('0' + idBytes[k].toString(16)).slice(-2).toUpperCase();
        }
        return hex;
      }
    }
    return null;
  }

  // Methode 2 : auto-detection du chip ID (fenetre 3000 ms)
  function tryAutoDetect(port) {
    return port.write(UBlox_UNIQID_CMD).then(function () {
      return new Promise(function (resolve, reject) {
        var acc = [];
        var timer = setTimeout(function () {
          cleanup();
          reject(new Error('timeout'));
        }, UNIQID_TIMEOUT_MS);

        function cleanup() {
          clearTimeout(timer);
          if (poll) { poll.active = false; }
        }

        var poll = { active: true };
        (function loop() {
          if (!poll.active) return;
          readChunk(port).then(function (chunk) {
            if (!poll.active) return;
            if (chunk && chunk.length) {
              acc = acc.concat(Array.prototype.slice.call(chunk));
              var id = findUniqId(new Uint8Array(acc));
              if (id) { cleanup(); resolve(id); return; }
            }
            loop();
          }, function (err) { cleanup(); reject(err); });
        })();
      });
    });
  }

  // ---------- Fallback manuel (modal) ----------

  // listener du clic hors cadre de la derniere modale ouverte ( cleanup)
  var manualBackdrop = null;

  function askManualGnss() {
    return new Promise(function (resolve) {
      var overlay = $('#gnssModalOverlay');
      var select = $('#gnssManualSelect');
      var btn = $('#gnssModalConfirm');
      if (!overlay || !select || !btn) { resolve(null); return; }

      // nettoie le listener residuel d'un appel precedent
      if (manualBackdrop) overlay.removeEventListener('click', manualBackdrop);
      manualBackdrop = null;

      // le menu propose d'abord le CATALOGUE : les GNSS enregistres pas encore
      // lies, puis les deja lies (re-pairing vers une autre tablette). Si le
      // catalogue est vide : les trois etiquettes physiques par defaut.
      var cat = loadGnssCatalog();
      var regSrc = [];
      try { regSrc = JSON.parse(localStorage.getItem('otGnssRegistry') || '[]'); } catch (err) {}
      var lie = {};
      regSrc.forEach(function (r) { lie[r.id] = r.terminal || ''; });

      var ids = cat.map(function (r) { return r.id; }).slice();
      if (!ids.length) ids = Object.keys(GNSS_REGISTRY).map(function (k) { return GNSS_REGISTRY[k]; });
      var opts = ids.filter(function (id) { return !lie[id]; })
        .concat(ids.filter(function (id) { return lie[id]; }));
      if (!opts.length) opts = ['GNSS-01', 'GNSS-02', 'GNSS-03'];
      select.innerHTML = opts.map(function (id) {
        var l = lie[id] ? ' (le : ' + lie[id] + ')' : '';
        return '<option value="' + id + '">' + id + l + '</option>';
      }).join('');

      function close() {
        overlay.hidden = true;
        btn.removeEventListener('click', onConfirm);
      }
      // fermeture par clic hors cadre = annulation du pairing
      function onBackdrop(e) {
        if (e.target === overlay) { close(); resolve(null); }
      }
      overlay.addEventListener('click', onBackdrop);
      manualBackdrop = onBackdrop;
      function onConfirm() {
        if (manualBackdrop) overlay.removeEventListener('click', manualBackdrop);
        manualBackdrop = null;
        close();
        resolve(select.value);
      }

      btn.addEventListener('click', onConfirm);
      overlay.hidden = false;
    });
  }

  // ---------- Binding + flux $GNGGA ----------

  function bindGnss(gnssLabel, terminalId, method) {
    var binding = { gnss: gnssLabel, terminal: terminalId, method: method };

    // le GNSS pairé entre au catalogue s'il n'y etait pas enregistre
    try { ensureCatalogEntry(gnssLabel); } catch (e) {}

    // persiste le lien GNSS <-> terminal (meme cle que le registre d'affectation)
    try {
      var a = JSON.parse(localStorage.getItem('otAssignments') || '{"gnss":{},"tabTeam":{}}');
      a.gnss = a.gnss || {};
      a.gnss[gnssLabel] = { type: 'gnss', target: terminalId, boundAt: Date.now() };
      localStorage.setItem('otAssignments', JSON.stringify(a));
    } catch (e) {}

    // met a jour la liste "GNSS actifs"
    try {
      var reg = JSON.parse(localStorage.getItem('otGnssRegistry') || '[]');
      if (!reg.some(function (r) { return r.id === gnssLabel; })) {
        reg.push({ id: gnssLabel, terminal: terminalId, boundAt: Date.now() });
      } else {
        reg.forEach(function (r) { if (r.id === gnssLabel) r.terminal = terminalId; });
      }
      localStorage.setItem('otGnssRegistry', JSON.stringify(reg));
    } catch (e) {}

    currentBinding = binding;
    renderBindResult(binding);
    if (method === 'auto') {
      console.info('%c[GNSS] AUTO DETECT - chip ID lie: ' + gnssLabel + ' -> ' + terminalId,
        'color:#22c55e;font-weight:700');
      setStatus('AUTO DETECT: ' + gnssLabel + ' lie a ' + terminalId, true);
    } else {
      console.warn('%c[GNSS] MANUAL FALLBACK - selection manuelle: ' + gnssLabel + ' -> ' + terminalId,
        'color:#f59e0b;font-weight:700');
      setStatus('MANUAL FALLBACK: ' + gnssLabel + ' lie a ' + terminalId, true);
    }

    // rafraichit les listes d'affectation existantes
    if (window.OreTrackingServerUi && window.OreTrackingServerUi.refreshAssignments) {
      window.OreTrackingServerUi.refreshAssignments();
    }

    // lance le flux continu de geoloc
    startGnssStream();
    return binding;
  }

  // Checksum NMEA : XOR de tous les octets entre $ et *
  function nmeaChecksum(body, given) {
    var x = 0;
    for (var i = 0; i < body.length; i++) x ^= body.charCodeAt(i);
    var hex = (x < 16 ? '0' : '') + x.toString(16).toUpperCase();
    return hex === String(given).toUpperCase();
  }

  // Decodage d'une trame NMEA $GNGGA -> {lat, lon, fix, sats}
  function parseGngga(sentence) {
    var m = /^\$GNGGA,([^*]*)\*([0-9A-Fa-f]{2})/.exec(String(sentence).trim());
    if (!m) return null;
    // les donnees corrompues (bruit serie) sont ecartees avant conversion
    if (!nmeaChecksum('GNGGA,' + m[1], m[2])) return null;
    var f = m[1].split(',');
    // hhmmss.ss,lat,N/S,lon,E/W,qualite,nbSats,HDOP,alt,M,...
    var fix = parseInt(f[5], 10) || 0;
    var sats = parseInt(f[6], 10) || 0;
    function toDeg(v, hemi) {
      if (!v) return null;
      var d = parseFloat(v.slice(0, 2));
      var mm = parseFloat(v.slice(2));
      if (isNaN(d) || isNaN(mm)) return null;
      var deg = d + mm / 60;
      if (hemi === 'S' || hemi === 'W') deg = -deg;
      return deg;
    }
    return {
      lat: toDeg(f[1], f[2]),
      lon: toDeg(f[3], f[4]),
      fix: fix,
      sats: sats
    };
  }

  function startGnssStream() {
    if (!activePort || !activePort.readable) return;
    if (streamAbort) streamAbort.abort();
    streamAbort = new AbortController();
    var signal = streamAbort.signal;

    (function loop() {
      if (signal.aborted || !activePort || !activePort.readable) return;
      readChunk(activePort).then(function (chunk) {
        if (signal.aborted) return;
        if (chunk && chunk.length) {
          var text = '';
          for (var i = 0; i < chunk.length; i++) text += String.fromCharCode(chunk[i]);
          var lines = text.split(/\r?\n/);
          for (var j = 0; j < lines.length; j++) {
            var gga = parseGngga(lines[j]);
            if (gga && gga.fix > 0) {
              window.dispatchEvent(new CustomEvent('gnss:gngga', { detail: gga }));
            }
          }
        }
        loop();
      }, function () { /* port ferme ou lecture interrompue */ });
    })();
  }

  // ---------- Wiring de l'UI ----------

  function onUnlock(e) {
    e.preventDefault();
    // la tablette cible = celle enregistree dans otThisDeviceId (auto-detect)
    var terminalId = '';
    try { terminalId = localStorage.getItem('otThisDeviceId') || ''; } catch (err) {}
    if (!terminalId) {
      setStatus('Enregistrez d\'abord cette tablette (section Tablette et GNSS)', false);
      return;
    }

    var btn = $('#btnUnlockGnss');
    if (btn) btn.disabled = true;
    setStatus('Ouverture du port serie a ' + GNSS_BAUD + ' bauds...', true);

    openSerialPort().then(function (port) {
      setStatus('Port ouvert. Interrogation UBX-SEC-UNIQID...', true);
      return tryAutoDetect(port).then(function (chipId) {
        var label = GNSS_REGISTRY[chipId];
        if (label) {
          bindGnss(label, terminalId, 'auto');
        } else {
          console.warn('[GNSS] Chip ID inconnu du registre: ' + chipId + ' -> modal manuel');
          return askManualGnss().then(function (manual) {
            if (manual) bindGnss(manual, terminalId, 'manual');
            else setStatus('Pairing annule', false);
          });
        }
      }, function () {
        // Methode 1 : timeout ou parsing KO -> selection manuelle
        return askManualGnss().then(function (manual) {
          if (manual) bindGnss(manual, terminalId, 'manual');
          else setStatus('Pairing annule', false);
        });
      });
    }, function (err) {
      setStatus('Erreur serie: ' + err.message, false);
      console.error('[GNSS] Erreur ouverture port:', err);
    }).then(function () {
      if (btn) btn.disabled = false;
    }, function () {
      if (btn) btn.disabled = false;
    });
  }

  function init() {
    var form = $('#wizardForm');
    if (form) form.addEventListener('submit', onUnlock);

    var overlay = $('#gnssModalOverlay');
    if (overlay) {
      overlay.addEventListener('click', function (e) {
        if (e.target === overlay) overlay.hidden = true; // clic hors cadre = fermer
      });
    }

    renderBindResult(currentBinding);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.OreTrackingGnss = {
    openSerialPort: openSerialPort,
    tryAutoDetect: tryAutoDetect,
    askManualGnss: askManualGnss,
    parseGngga: parseGngga,
    startGnssStream: startGnssStream,
    GNSS_REGISTRY: GNSS_REGISTRY,
    loadGnssCatalog: loadGnssCatalog,
    saveGnssCatalog: saveGnssCatalog,
    ensureCatalogEntry: ensureCatalogEntry
  };
})();
