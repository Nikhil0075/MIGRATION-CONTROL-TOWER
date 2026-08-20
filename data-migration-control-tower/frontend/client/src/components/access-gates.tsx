/**
 * Pre-console screens: choose nothing, learn what access you need.
 *
 * Separate from app.tsx deliberately. These render before any workspace
 * exists, so they need none of the JET shell (drawer layout, environment
 * provider, translation bundles) — and keeping them free of it means they
 * can be tested directly rather than through a chain of module stubs.
 */

import { h } from "preact";
import { useState } from "preact/hooks";
import { Icon } from "./icons";

export function AccessLevels() {
  // Shown BEFORE sign-in so it is clear which access a task needs, and
  // deliberately not presented as a choice: Google proves who you are, the
  // role is granted to your account. Two "sign in as..." buttons would
  // imply you can self-select privilege, which is exactly what the
  // authorization model forbids.
  return (
    <ul class="access-levels" aria-label="Access levels">
      <li class="access-level">
        <span class="access-level-name">Operator</span>
        <span class="access-level-scope">Onboards estates</span>
        <p>
          Connect a new estate, validate its source connection, then start
          assessments and migration runs and manage wave capacity.
        </p>
      </li>
      <li class="access-level">
        <span class="access-level-name">SME &amp; approver</span>
        <span class="access-level-scope">Acts on the rest</span>
        <p>
          Review findings, lineage, reconciliation evidence and policy
          decisions, and give the human approval a cutover cannot proceed
          without.
        </p>
      </li>
    </ul>
  );
}

export function AuthenticationGate({
  configured,
  error,
  onGoogleSignIn,
  onPasswordSignIn,
  onPasswordReset,
}: {
  configured: boolean;
  error?: string | null;
  onGoogleSignIn: () => Promise<void>;
  onPasswordSignIn: (email: string, password: string) => Promise<void>;
  onPasswordReset?: (email: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"password" | "google" | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [emailOpen, setEmailOpen] = useState(false);
  const [resetNotice, setResetNotice] = useState<string | null>(null);

  const run = async (provider: "password" | "google", action: () => Promise<void>) => {
    setBusy(provider);
    try {
      await action();
    } catch {
      // The parent owns the user-facing, enumeration-safe error message.
    } finally {
      setBusy(null);
    }
  };

  return (
    <main class="auth-page auth-page-hero">
      <div class="auth-shell">
      <section class="auth-card auth-card-primary" aria-labelledby="auth-title">
        <img
          class="brand-mark"
          src="/assets/brand/v1/logo-symbol.png"
          alt="Migration Control Tower"
          width="56"
          height="56"
        />
        <p class="eyebrow">Autonomous data migration</p>
        <h1 id="auth-title">Migration Control Tower</h1>
        <p>Governed migration operations, evidence and human-approved cutover in one secure workspace.</p>

        {error && (
          <div class="inline-alert danger" role="alert">
            {error}
          </div>
        )}
        {configured ? (
          <div class="auth-methods">
            <button
              class="button button-primary auth-action auth-google"
              type="button"
              disabled={busy !== null}
              aria-busy={busy === "google"}
              onClick={() => void run("google", onGoogleSignIn)}
            >
              <Icon name="user" /> {busy === "google" ? "Opening secure sign-in…" : "Continue with Google"}
            </button>

            <button
              type="button"
              class="auth-email-toggle"
              aria-expanded={emailOpen}
              aria-controls="email-signin-panel"
              onClick={() => setEmailOpen(!emailOpen)}
            >
              Sign in with domain email
              <span aria-hidden="true">{emailOpen ? "−" : "+"}</span>
            </button>

            {emailOpen && (
            <form
              id="email-signin-panel"
              class="password-signin"
              aria-label="Domain email sign in"
              onSubmit={(event) => {
                event.preventDefault();
                void run("password", () => onPasswordSignIn(email, password));
              }}
            >
              <label class="auth-field">
                <span>Domain email address</span>
                <input
                  type="email"
                  name="email"
                  value={email}
                  autoComplete="username"
                  inputMode="email"
                  required
                  disabled={busy !== null}
                  onInput={(event) => setEmail(event.currentTarget.value)}
                />
              </label>
              <div class="auth-field">
                <label htmlFor="control-tower-password">Password</label>
                <span class="password-input-row">
                  <input
                    id="control-tower-password"
                    type={showPassword ? "text" : "password"}
                    name="password"
                    value={password}
                    autoComplete="current-password"
                    required
                    disabled={busy !== null}
                    onInput={(event) => setPassword(event.currentTarget.value)}
                  />
                  <button type="button" onClick={() => setShowPassword(!showPassword)} aria-label={showPassword ? "Hide password" : "Show password"}>
                    {showPassword ? "Hide" : "Show"}
                  </button>
                </span>
              </div>
              <button
                class="button button-primary auth-action"
                type="submit"
                disabled={busy !== null}
                aria-busy={busy === "password"}
              >
                <Icon name="user" /> {busy === "password" ? "Signing in…" : "Sign in with email"}
              </button>
              {onPasswordReset && (
                <button
                  class="auth-reset"
                  type="button"
                  disabled={!email || busy !== null}
                  onClick={() => {
                    setResetNotice(null);
                    void onPasswordReset(email)
                      .catch(() => undefined)
                      .finally(() => setResetNotice("If the account is eligible, password-reset instructions have been sent."));
                  }}
                >
                  Forgot password?
                </button>
              )}
              {resetNotice && <p class="auth-reset-notice" role="status">{resetNotice}</p>}
            </form>
            )}
          </div>
        ) : (
          <div class="inline-alert warning" role="status">
            Firebase authentication is not configured. Set{" "}
            <code>FIREBASE_WEB_CONFIG_JSON</code> on this service.
          </div>
        )}
        <p class="auth-footnote">
          Firebase verifies identity. Access is administrator-assigned and every operational action is audited.
        </p>
        <details class="auth-access-details"><summary>Access levels</summary><AccessLevels /></details>
      </section>
      {/* Decorative, and hidden below 900px rather than squashed: the
          artwork is 142KB that a phone signing in does not need, and a
          scaled-down isometric scene reads as noise. */}
      <aside class="auth-art" aria-hidden="true">
        <img src="/assets/brand/v1/architecture-hero.jpg" alt="" loading="lazy" />
      </aside>
      </div>
    </main>
  );
}

export function NoAccessGate({
  email,
  onSignOut,
}: {
  email: string;
  onSignOut: () => void;
}) {
  // Previously a user with no roles reached a console where every request
  // returned 403 and "Onboard estate" was disabled with no explanation.
  // Authenticated-but-unauthorized is a normal state on first run, so it
  // says what to do about it.
  return (
    <main class="auth-page">
      <section class="auth-card" aria-labelledby="no-access-title">
        <img
          class="brand-mark"
          src="/assets/brand/v1/logo-symbol.png"
          alt="Migration Control Tower"
          width="56"
          height="56"
        />
        <p class="eyebrow">Signed in</p>
        <h1 id="no-access-title">No access yet</h1>
        <p>
          <strong>{email}</strong> is signed in, but no access level has been
          granted to this account yet.
        </p>

        <AccessLevels />

        <div class="inline-alert warning" role="status">
          To grant access, add this address to <code>OPERATOR_ALLOWLIST</code>{" "}
          (onboarding and operations) or <code>APPROVER_ALLOWLIST</code>{" "}
          (cutover approval) in the service environment, then sign in again.
          Deployments should instead set per-estate <code>estate_roles</code>{" "}
          custom claims.
        </div>

        <button class="button auth-action" type="button" onClick={onSignOut}>
          Sign out
        </button>
      </section>
    </main>
  );
}
