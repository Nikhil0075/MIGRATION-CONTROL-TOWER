import { DrawerLayout } from "oj-c/drawer-layout";
import { registerCustomElement } from "ojs/ojvcomponent";
import Context = require("ojs/ojcontext");
import { h } from "preact";
import { useEffect, useMemo, useState } from "preact/hooks";
import { User } from "firebase/auth";
import { publicApi, api } from "../api";
import { initializeAuthentication, signIn, signOutUser } from "../auth";
import { NavItem, RuntimeConfig, Session } from "../models";
import { Icon } from "./icons";
import { PageRouter } from "./pages";
import "../styles/app.css";

declare const __MCT_E2E_BYPASS__: boolean;

const E2E_SESSION: Session = {
  uid: "playwright-operator",
  email: "operator@example.test",
  roles: ["viewer", "operator", "approver"],
};

const NAVIGATION: NavItem[] = [
  {
    route: "overview",
    label: "Overview",
    icon: "overview",
    description: "Estate posture and migration outcomes",
  },
  {
    route: "estates",
    label: "Estates",
    icon: "estates",
    description: "Inventory, connections, ownership, and drift",
  },
  {
    route: "assessments",
    label: "Assessments",
    icon: "assessments",
    description: "Findings, packs, and proposed plans",
  },
  {
    route: "waves",
    label: "Waves",
    icon: "waves",
    description: "Capacity, reservations, backlog, and overrides",
  },
  {
    route: "runs",
    label: "Runs",
    icon: "runs",
    description: "Stage timelines, evidence, and operations",
  },
  {
    route: "lineage",
    label: "Lineage",
    icon: "lineage",
    description: "Asset dependencies and migration impact",
  },
  {
    route: "reconciliation",
    label: "Reconciliation",
    icon: "reconciliation",
    description: "Source/target deltas and tolerances",
  },
  {
    route: "policies",
    label: "Policies & Approvals",
    icon: "policies",
    description: "Denials, evidence, and cutover inbox",
  },
  {
    route: "agents",
    label: "Agents",
    icon: "agents",
    description: "Registry, capabilities, and pinned versions",
  },
  {
    route: "evaluations",
    label: "Evaluations",
    icon: "evaluations",
    description: "Scenario quality, scale, and latency",
  },
  {
    route: "system-health",
    label: "System Health",
    icon: "health",
    description: "Services, telemetry, and build status",
  },
];

function routeFromLocation(): string {
  const candidate =
    window.location.pathname.replace(/^\/+|\/+$/g, "") || "overview";
  return NAVIGATION.some((item) => item.route === candidate.split("/")[0])
    ? candidate
    : "overview";
}

function useViewportWidth(): number {
  const [width, setWidth] = useState(window.innerWidth);
  useEffect(() => {
    const update = () => setWidth(window.innerWidth);
    window.addEventListener("resize", update, { passive: true });
    return () => window.removeEventListener("resize", update);
  }, []);
  return width;
}

function AuthenticationGate({
  configured,
  error,
  onSignIn,
}: {
  configured: boolean;
  error?: string | null;
  onSignIn: () => void;
}) {
  return (
    <main class="auth-page">
      <section class="auth-card" aria-labelledby="auth-title">
        <div class="brand-mark" aria-hidden="true">
          M
        </div>
        <p class="eyebrow">Autonomous data migration</p>
        <h1 id="auth-title">Migration Control Tower</h1>
        <p>
          Secure operational access to migration estates, evidence, approvals,
          and system health.
        </p>
        {error && (
          <div class="inline-alert danger" role="alert">
            {error}
          </div>
        )}
        {configured ? (
          <button
            class="button button-primary auth-action"
            type="button"
            onClick={onSignIn}
          >
            <Icon name="user" /> Sign in with Google
          </button>
        ) : (
          <div class="inline-alert warning" role="status">
            Firebase authentication is not configured. Set{" "}
            <code>FIREBASE_WEB_CONFIG_JSON</code> on this service.
          </div>
        )}
        <p class="auth-footnote">
          Access is controlled by Firebase custom claims and audited for every
          operator action.
        </p>
      </section>
    </main>
  );
}

function Navigation({
  route,
  compact,
  onNavigate,
}: {
  route: string;
  compact: boolean;
  onNavigate: (route: string) => void;
}) {
  return (
    <nav
      class={`side-navigation ${compact ? "compact" : ""}`}
      aria-label="Primary navigation"
    >
      <div class="nav-context">
        <span class="nav-context-label">Workspace</span>
        {!compact && <strong>Operations</strong>}
      </div>
      <ul>
        {NAVIGATION.map((item) => (
          <li key={item.route}>
            <button
              type="button"
              class={route.split("/")[0] === item.route ? "active" : ""}
              aria-current={
                route.split("/")[0] === item.route ? "page" : undefined
              }
              aria-label={compact ? item.label : undefined}
              title={compact ? item.label : undefined}
              onClick={() => onNavigate(item.route)}
            >
              <Icon name={item.icon} />
              {!compact && <span>{item.label}</span>}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Inspector({
  title,
  value,
  onClose,
}: {
  title: string;
  value: unknown;
  onClose: () => void;
}) {
  const entries: [string, unknown][] =
    value && typeof value === "object"
      ? Object.entries(value as Record<string, unknown>)
      : [["Value", value]];
  return (
    <aside class="inspector" aria-label="Context inspector">
      <div class="inspector-header">
        <div>
          <p class="eyebrow">Inspector</p>
          <h2>{title}</h2>
        </div>
        <button
          class="icon-button"
          type="button"
          onClick={onClose}
          aria-label="Close inspector"
        >
          <Icon name="close" />
        </button>
      </div>
      <dl class="inspector-details">
        {entries.map(([key, entry]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd>
              {entry === null || entry === undefined
                ? "Not available"
                : typeof entry === "object"
                  ? JSON.stringify(entry, null, 2)
                  : String(entry)}
            </dd>
          </div>
        ))}
      </dl>
    </aside>
  );
}

type Props = Readonly<Record<string, never>>;

export const App = registerCustomElement("app-root", (_props: Props) => {
  const width = useViewportWidth();
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [firebaseUser, setFirebaseUser] = useState<User | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [route, setRoute] = useState(routeFromLocation());
  const [navigationOpen, setNavigationOpen] = useState(width >= 600);
  const [inspector, setInspector] = useState<{
    title: string;
    value: unknown;
  } | null>(null);

  const large = width >= 1440;
  const mediumDesktop = width >= 1024 && width < 1440;
  const mobile = width < 600;
  const compactNavigation = width >= 1024 && !large;
  const currentPage = useMemo(
    () =>
      NAVIGATION.find((item) => item.route === route.split("/")[0]) ||
      NAVIGATION[0],
    [route],
  );

  useEffect(() => {
    let unsubscribe: () => void = () => undefined;
    let active = true;
    Context.getPageContext().getBusyContext().applicationBootstrapComplete();
    publicApi<RuntimeConfig>("/api/v1/config")
      .then((result) => {
        if (!active) return;
        setConfig(result.data);
        if (__MCT_E2E_BYPASS__) {
          setSession(E2E_SESSION);
          setAuthReady(true);
          return;
        }
        unsubscribe = initializeAuthentication(result.data.firebase, (user) => {
          setFirebaseUser(user);
          setAuthReady(true);
        });
      })
      .catch((reason) => {
        setAuthError(reason.message || String(reason));
        setAuthReady(true);
      });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!firebaseUser) {
      setSession(null);
      return;
    }
    api<Session>("/api/v1/session")
      .then((result) => setSession(result.data))
      .catch((reason) => setAuthError(reason.message || String(reason)));
  }, [firebaseUser]);

  useEffect(() => {
    const pop = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);

  useEffect(() => {
    if (width >= 1024) setNavigationOpen(true);
  }, [width]);

  const navigate = (nextRoute: string) => {
    const normalized = nextRoute.replace(/^\/+|\/+$/g, "") || "overview";
    window.history.pushState({}, "", `/${normalized}`);
    setRoute(normalized);
    if (width < 1024) setNavigationOpen(false);
    document.querySelector<HTMLElement>("#main-content")?.focus();
  };

  if (!config || !authReady) {
    return (
      <main class="boot-page" aria-live="polite">
        <span class="spinner" /> Loading Migration Control Tower…
      </main>
    );
  }
  if ((!firebaseUser && !__MCT_E2E_BYPASS__) || !session) {
    return (
      <AuthenticationGate
        configured={config.authentication_configured}
        error={authError}
        onSignIn={() => {
          setAuthError(null);
          void signIn().catch((reason) => setAuthError(reason.message || String(reason)));
        }}
      />
    );
  }

  const navigation = (
    <Navigation
      route={route}
      compact={compactNavigation}
      onNavigate={navigate}
    />
  );
  const inspectorPanel = inspector ? (
    <Inspector
      title={inspector.title}
      value={inspector.value}
      onClose={() => setInspector(null)}
    />
  ) : (
    <div />
  );
  const endOpened = Boolean(inspector) && !mobile;
  const bottomOpened = Boolean(inspector) && mobile;

  return (
    <div class="mct-app">
      <a class="skip-link" href="#main-content">
        Skip to operational workspace
      </a>
      <header class="command-bar">
        <button
          class="icon-button menu-button"
          type="button"
          onClick={() => setNavigationOpen(!navigationOpen)}
          aria-label="Toggle navigation"
          aria-expanded={navigationOpen}
        >
          <Icon name="menu" />
        </button>
        <button
          class="product-identity"
          type="button"
          onClick={() => navigate("overview")}
          aria-label="Migration Control Tower overview"
        >
          <span class="brand-mark small" aria-hidden="true">
            M
          </span>
          <span>Migration Control Tower</span>
        </button>
        <div class="command-context" aria-label="Current operating context">
          <span>
            <small>Environment</small>
            <strong>Production</strong>
          </span>
          <span>
            <small>Workspace</small>
            <strong>Migration estate</strong>
          </span>
        </div>
        <label class="global-search">
          <Icon name="search" />
          <span class="sr-only">Search current workspace</span>
          <input type="search" placeholder="Search" />
        </label>
        <button class="icon-button" type="button" aria-label="Notifications">
          <Icon name="alert" />
        </button>
        <button
          class="user-menu"
          type="button"
          onClick={() => signOutUser()}
          title="Sign out"
        >
          <Icon name="user" />
          <span>{session.email || session.uid}</span>
        </button>
      </header>
      <DrawerLayout
        start={navigation}
        end={inspectorPanel}
        bottom={inspectorPanel}
        startOpened={navigationOpen}
        endOpened={endOpened}
        bottomOpened={bottomOpened}
        startDisplay={width >= 1024 ? "reflow" : "overlay"}
        endDisplay={large ? "reflow" : "overlay"}
        bottomDisplay="overlay"
        onStartOpenedChanged={(value) => setNavigationOpen(value)}
        onEndOpenedChanged={(value) => !value && setInspector(null)}
        onBottomOpenedChanged={(value) => !value && setInspector(null)}
      >
        <main id="main-content" class="workspace" tabIndex={-1}>
          <div class="workspace-context">
            <div>
              <span>{currentPage.label}</span>
              <small>{currentPage.description}</small>
            </div>
            <span class="build-version">Build {config.build_version}</span>
          </div>
          <PageRouter
            route={route}
            session={session}
            onInspect={(title, value) => setInspector({ title, value })}
            navigate={navigate}
          />
        </main>
      </DrawerLayout>
      {mediumDesktop && inspector && (
        <span class="sr-only" aria-live="polite">
          Inspector opened as an overlay.
        </span>
      )}
    </div>
  );
});
