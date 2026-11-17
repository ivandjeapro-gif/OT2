/* OreTracking V2 - Montee des positions GNSS vers le serveur Senor Geology
 * par radio XBee (Web Serial API, second port serie distinct du GNSS).
 *
 * Chaine : Rover --USB-C--> gnss-serial.js (parse $GNGGA, evenement
 * 'gnss:gngga') --> xbee-serial.js (ce fichier, encapsule en JSON et ecrit
 * sur le port serie XBee) --> radio --> Base --> Server_Senor_Geology.py
 * (ecouteur_xbee / traiter_trame_xbee).
 */
(function () {
  'use strict';

  var XBEE_BAUD = 9600; // doit correspondre a la config radio faite via XCTU

  var xbeePort = null;    // SerialPort XBee, distinct du port GNSS
  var xbeeWriter = null;  // WritableStreamDefaultWriter garde ouvert

  function $(sel) { return document.querySelector(sel); }

  function setStatus(msg, ok) {
    var el = $('#xbeeStatus');
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' ok' : ' err') : '');
  }

  // ---------- Resolution du code DT affecte a ce terminal ----------
  // Reprend la chaine deja utilisee par server-ui.js (otThisDeviceId ->
  // otGnssRegistry -> otAssignments.gnss) pour trouver l'engin (DT) que
  // cette tablette equipe actuellement.
  function resoudreDtCode() {
    try {
      var thisId = localStorage.getItem('otThisDeviceId') || '';
      if (!thisId) return '';
      var reg = JSON.parse(localStorage.getItem('otGnssRegistry') || '[]');
      var entry = reg.filter(function (r) { return r.terminal === thisId; })[0];
      if (!entry) return '';
      var a = JSON.parse(localStorage.getItem('otAssignments') || '{"gnss":{},"tabTeam":{}}');
      var aff = a.gnss && a.gnss[entry.id];
      if (!aff || !aff.target) return '';
      return String(aff.target);
    } catch (e) {
      return '';
    }
  }

  // ---------- Cle secrete HMAC par DT (provisionnement manuel) ----------
  // otClesDt : { "DT-01": "<64 hex = 32 octets>", ... } dans localStorage.
  // Generee cote serveur (/api/cles_dt) puis copiee une fois sur la
  // tablette lors du provisionnement de ce camion.
  function lireCleDt(code) {
    try {
      var cles = JSON.parse(localStorage.getItem('otClesDt') || '{}');
      return cles[code] || '';
    } catch (e) {
      return '';
    }
  }
  function enregistrerCleDt(code, cleHex) {
    try {
      var cles = JSON.parse(localStorage.getItem('otClesDt') || '{}');
      cles[code] = cleHex;
      localStorage.setItem('otClesDt', JSON.stringify(cles));
      return true;
    } catch (e) {
      return false;
    }
  }

  // ---------- Signature HMAC-SHA256 (Web Crypto) ----------
  function hexVersBytes(hex) {
    var propre = String(hex).trim();
    var bytes = new Uint8Array(propre.length / 2);
    for (var i = 0; i < bytes.length; i++) bytes[i] = parseInt(propre.substr(i * 2, 2), 16);
    return bytes;
  }
  function bytesVersHex(bytes) {
    var hex = '';
    for (var i = 0; i < bytes.length; i++) hex += ('0' + bytes[i].toString(16)).slice(-2);
    return hex;
  }
  function signerHmac(cleHex, texte) {
    return crypto.subtle.importKey(
      'raw', hexVersBytes(cleHex), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']
    ).then(function (cryptoKey) {
      return crypto.subtle.sign('HMAC', cryptoKey, new TextEncoder().encode(texte));
    }).then(function (signature) {
      return bytesVersHex(new Uint8Array(signature));
    });
  }

  // Construit la trame signee : signe le JSON SANS le champ sig, puis
  // ajoute ,"sig":"..."} a la suite du meme texte (pas de re-serialisation)
  // pour que le serveur puisse verifier sur les octets exacts transmis
  // (voir extraire_corps_signe() cote Server_Senor_Geology.py).
  function construireTrameSignee(code, cleHex, lat, lon) {
    var corps = JSON.stringify({ id: code, lat: lat, lon: lon, ts: Date.now() / 1000 });
    return signerHmac(cleHex, corps).then(function (sig) {
      return corps.slice(0, -1) + ',"sig":"' + sig + '"}';
    });
  }

  // ---------- Ouverture / fermeture du port serie XBee ----------
  function openXbeePort() {
    if (!navigator.serial || !navigator.serial.requestPort) {
      return Promise.reject(new Error('Web Serial non disponible (HTTPS/Chrome requis)'));
    }
    return navigator.serial.requestPort().then(function (port) {
      return port.open({ baudRate: XBEE_BAUD }).then(function () {
        xbeePort = port;
        xbeeWriter = port.writable.getWriter();
        return port;
      });
    });
  }

  function closeXbeePort() {
    window.removeEventListener('gnss:gngga', onGngga);
    if (xbeeWriter) {
      try { xbeeWriter.releaseLock(); } catch (e) {}
      xbeeWriter = null;
    }
    if (xbeePort) {
      try { xbeePort.close(); } catch (e) {}
      xbeePort = null;
    }
  }

  // ---------- Envoi d'une trame JSON signee (une ligne par trame) ----------
  function envoyerTrame(gga) {
    if (!xbeeWriter) return Promise.resolve(false);
    var code = resoudreDtCode();
    if (!code) {
      console.warn('[XBEE] Aucun DT affecte a ce terminal, trame non envoyee');
      setStatus('Aucun DT affecte a cette tablette (voir Affectation)', false);
      return Promise.resolve(false);
    }
    var cleHex = lireCleDt(code);
    if (!cleHex) {
      console.warn('[XBEE] Aucune cle HMAC provisionnee pour ' + code + ', trame non envoyee');
      setStatus('Cle HMAC manquante pour ' + code + ' (voir provisionnement)', false);
      return Promise.resolve(false);
    }
    return construireTrameSignee(code, cleHex, gga.lat, gga.lon).then(function (trame) {
      var data = new TextEncoder().encode(trame + '\n');
      return xbeeWriter.write(data).then(function () {
        setStatus('Radio XBee connectee - position signee envoyee pour ' + code, true);
        return true;
      });
    }, function (err) {
      console.error('[XBEE] Erreur envoi:', err);
      setStatus('Erreur envoi radio: ' + err.message, false);
      return false;
    });
  }

  function onGngga(e) {
    var gga = e && e.detail;
    if (!gga || gga.lat == null || gga.lon == null) return;
    envoyerTrame(gga);
  }

  // ---------- Wiring UI ----------
  function onConnect(e) {
    if (e) e.preventDefault();
    var btn = $('#btnConnectXbee');
    if (btn) btn.disabled = true;
    setStatus('Ouverture du port radio XBee a ' + XBEE_BAUD + ' bauds...', true);
    openXbeePort().then(function () {
      setStatus('Radio XBee connectee (' + XBEE_BAUD + ' bauds), en attente de position GNSS...', true);
      window.addEventListener('gnss:gngga', onGngga);
    }, function (err) {
      setStatus('Erreur radio: ' + err.message, false);
      console.error('[XBEE] Erreur ouverture port:', err);
    }).then(function () {
      if (btn) btn.disabled = false;
    }, function () {
      if (btn) btn.disabled = false;
    });
  }

  function onEnregistrerCle(e) {
    if (e) e.preventDefault();
    var code = resoudreDtCode();
    var champCle = $('#xbeeCleHex');
    var cleHex = champCle ? champCle.value.trim() : '';
    var statusEl = $('#xbeeCleStatus');
    function dire(msg, ok) {
      if (!statusEl) return;
      statusEl.textContent = msg;
      statusEl.className = 'account-status' + (ok ? ' ok' : ' err');
    }
    if (!code) {
      dire('Aucun DT affecte a cette tablette (voir Affectation)', false);
      return;
    }
    if (!/^[0-9a-fA-F]{64}$/.test(cleHex)) {
      dire('Cle invalide (attendu 64 caracteres hexadecimaux)', false);
      return;
    }
    enregistrerCleDt(code, cleHex.toLowerCase());
    if (champCle) champCle.value = '';
    dire('Cle enregistree pour ' + code, true);
  }

  function init() {
    var form = $('#xbeeForm');
    if (form) form.addEventListener('submit', onConnect);
    var formCle = $('#xbeeCleForm');
    if (formCle) formCle.addEventListener('submit', onEnregistrerCle);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.OreTrackingXbee = {
    openXbeePort: openXbeePort,
    closeXbeePort: closeXbeePort,
    envoyerTrame: envoyerTrame,
    resoudreDtCode: resoudreDtCode,
    lireCleDt: lireCleDt,
    enregistrerCleDt: enregistrerCleDt
  };
})();
