import { FirebaseApp, initializeApp } from "firebase/app";
import {
  Auth,
  GoogleAuthProvider,
  User,
  getAuth,
  onAuthStateChanged,
  sendPasswordResetEmail,
  signInWithEmailAndPassword,
  signInWithPopup,
  signInWithRedirect,
  signOut,
} from "firebase/auth";

let app: FirebaseApp | null = null;
let auth: Auth | null = null;
let currentUser: User | null = null;

export function initializeAuthentication(
  config: Record<string, string>,
  onChange: (user: User | null) => void,
): () => void {
  if (!Object.keys(config).length) {
    currentUser = null;
    onChange(null);
    return () => undefined;
  }
  app = app || initializeApp(config);
  auth = auth || getAuth(app);
  return onAuthStateChanged(auth, (user) => {
    currentUser = user;
    onChange(user);
  });
}

export async function signInWithGoogle(): Promise<void> {
  if (!auth) throw new Error("Firebase authentication is not configured.");
  const provider = new GoogleAuthProvider();
  const mobile = window.matchMedia("(max-width: 599px)").matches;
  if (mobile) {
    await signInWithRedirect(auth, provider);
    return;
  }
  try {
    await signInWithPopup(auth, provider);
  } catch (reason) {
    const code = String((reason as { code?: unknown } | null)?.code || "");
    if ([
      "auth/popup-blocked",
      "auth/cancelled-popup-request",
      "auth/web-storage-unsupported",
      "auth/internal-error",
    ].includes(code)) {
      await signInWithRedirect(auth, provider);
      return;
    }
    throw reason;
  }
}

export async function signInWithPassword(email: string, password: string): Promise<void> {
  if (!auth) throw new Error("Firebase authentication is not configured.");
  await signInWithEmailAndPassword(auth, email.trim(), password);
}

export async function resetPassword(email: string): Promise<void> {
  if (!auth) throw new Error("Firebase authentication is not configured.");
  // Firebase email-enumeration protection should also be enabled in the
  // project. The UI always returns the same confirmation either way.
  await sendPasswordResetEmail(auth, email.trim());
}

/**
 * Authentication failures are deliberately translated at the UI boundary.
 * Raw Firebase messages expose implementation detail and, for password
 * accounts, can accidentally make email enumeration easier. The API still
 * performs the real role check after Firebase verifies the resulting token.
 */
export function authenticationErrorMessage(reason: unknown): string {
  const code = String((reason as { code?: unknown } | null)?.code || "");
  if (["auth/invalid-credential", "auth/user-not-found", "auth/wrong-password"].includes(code)) {
    return "The email address or password is incorrect.";
  }
  if (code === "auth/invalid-email") return "Enter a valid domain email address.";
  if (code === "auth/user-disabled") return "This account has been disabled. Contact an administrator.";
  if (code === "auth/too-many-requests") return "Too many sign-in attempts. Wait a few minutes and try again.";
  if (code === "auth/operation-not-allowed") {
    return "Email and password sign-in is not enabled for this Firebase project.";
  }
  if (code === "auth/network-request-failed") return "The sign-in service could not be reached. Check the network and try again.";
  return "Unable to sign in. Try again or contact an administrator.";
}

export async function signOutUser(): Promise<void> {
  if (auth) await signOut(auth);
}

export async function idToken(): Promise<string | null> {
  return currentUser ? currentUser.getIdToken() : null;
}
