// Copy this file to firebase-config.js (gitignored) and fill in your
// project's Firebase web config: Firebase console > Project settings >
// General > Your apps > Web app > SDK setup and configuration > Config.
//
// This requires two one-time console steps this repo can't do for you
// (same pattern as Day 1's GCP project creation — console/ToS steps stay
// manual):
//   1. Enable Firebase for the autonomous-data-migration GCP project
//      (https://console.firebase.google.com/ > Add project > select the
//      existing GCP project rather than creating a new one).
//   2. Authentication > Sign-in method > enable "Google" as a provider.
//
// These values (apiKey included) are public by design — Firebase web
// config is not a secret, it identifies which project a client SDK talks
// to; the real security boundary is server-side ID-token verification in
// frontend/app.py, not keeping this file hidden.

window.FIREBASE_CONFIG = {
  apiKey: "YOUR_API_KEY",
  authDomain: "autonomous-data-migration.firebaseapp.com",
  projectId: "autonomous-data-migration",
  storageBucket: "autonomous-data-migration.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId: "YOUR_APP_ID",
};
