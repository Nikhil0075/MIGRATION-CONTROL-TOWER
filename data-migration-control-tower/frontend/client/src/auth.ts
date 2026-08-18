import { FirebaseApp, initializeApp } from "firebase/app";
import {
  Auth,
  GoogleAuthProvider,
  User,
  getAuth,
  onAuthStateChanged,
  signInWithPopup,
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

export async function signIn(): Promise<void> {
  if (!auth) throw new Error("Firebase authentication is not configured.");
  await signInWithPopup(auth, new GoogleAuthProvider());
}

export async function signOutUser(): Promise<void> {
  if (auth) await signOut(auth);
}

export async function idToken(): Promise<string | null> {
  return currentUser ? currentUser.getIdToken() : null;
}
