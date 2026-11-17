/* OreTracking V2 - Configuration Firebase
 * Module : expose l'instance Auth et Firestore de l'application.
 */
import { initializeApp } from "https://www.gstatic.com/firebasejs/13.0.0/firebase-app.js";
import { getAuth } from "https://www.gstatic.com/firebasejs/13.0.0/firebase-auth.js";
import { getFirestore } from "https://www.gstatic.com/firebasejs/13.0.0/firebase-firestore.js";

const firebaseConfig = {
  apiKey: "AIzaSyDJoPRZ0ZSWGga4fEF3btnQFj6CHEPqOc4",
  authDomain: "ot-test-3c6df.firebaseapp.com",
  projectId: "ot-test-3c6df",
  storageBucket: "ot-test-3c6df.firebasestorage.app",
  messagingSenderId: "330527215444",
  appId: "1:330527215444:web:234559f2fa1d14e02bc2c1",
  measurementId: "G-TGW8CPV0KV"
};

const app = initializeApp(firebaseConfig);

export const auth = getAuth(app);

// Instance secondaire : pour CREER des comptes depuis le panneau Securite
// sans deconnecter l'admin de la session principale.
const app2 = initializeApp(firebaseConfig, 'admin-secondary');
export const authAdmin = getAuth(app2);

// Base nommee "ot-test" (pas (default)) — cf. console Firebase > Firestore.
export const db = getFirestore(app, 'ot-test');
