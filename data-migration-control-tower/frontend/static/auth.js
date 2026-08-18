// Firebase Auth (Day 10 hardening, §5.2): real Google Sign-In so the
// approval action has a verified human identity instead of a client-
// supplied string. frontend/app.py's approve endpoint verifies the ID
// token server-side (firebase_admin.auth.verify_id_token) and derives
// approver_identity from the token's own email claim — this file never
// tells the backend who signed in, it only proves a real sign-in
// happened and hands over the token for the backend to check itself.
//
// Requires frontend/static/firebase-config.js (gitignored — copy
// firebase-config.example.js and fill in your project's Firebase web
// config from the Firebase console: Project settings > General > Your
// apps > Web app). If that file is missing, sign-in is disabled and
// approveRun() in app.js will tell the user so rather than silently
// failing.

let _authUser = null;

function _firebaseReady() {
  if (typeof window.FIREBASE_CONFIG === "undefined") {
    console.warn(
      "firebase-config.js not found (or FIREBASE_CONFIG unset) — sign-in is disabled. " +
        "Copy frontend/static/firebase-config.example.js to firebase-config.js and fill in " +
        "your Firebase web config."
    );
    return false;
  }
  if (!firebase.apps.length) {
    firebase.initializeApp(window.FIREBASE_CONFIG);
  }
  return true;
}

function _renderAuthState() {
  const status = document.getElementById("auth-status");
  const signinBtn = document.getElementById("signin-btn");
  const signoutBtn = document.getElementById("signout-btn");
  if (!status || !signinBtn || !signoutBtn) return;
  if (_authUser) {
    status.textContent = `Signed in as ${_authUser.email}`;
    signinBtn.style.display = "none";
    signoutBtn.style.display = "inline-block";
  } else {
    status.textContent = "Not signed in";
    signinBtn.style.display = "inline-block";
    signoutBtn.style.display = "none";
  }
}

async function signIn() {
  if (!_firebaseReady()) {
    alert("Sign-in is not configured on this deployment — see frontend/static/firebase-config.example.js.");
    return;
  }
  try {
    const provider = new firebase.auth.GoogleAuthProvider();
    await firebase.auth().signInWithPopup(provider);
  } catch (err) {
    alert("Sign-in failed: " + err.message);
  }
}

async function signOutUser() {
  if (!_firebaseReady()) return;
  await firebase.auth().signOut();
}

// Used by app.js::approveRun(). Returns null if nobody is signed in —
// the caller is responsible for telling the user sign-in is required,
// never for approving on their behalf.
async function getIdToken() {
  if (!_authUser) return null;
  return _authUser.getIdToken();
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("signin-btn")?.addEventListener("click", signIn);
  document.getElementById("signout-btn")?.addEventListener("click", signOutUser);
  if (!_firebaseReady()) {
    _renderAuthState();
    return;
  }
  firebase.auth().onAuthStateChanged((user) => {
    _authUser = user;
    _renderAuthState();
  });
});
