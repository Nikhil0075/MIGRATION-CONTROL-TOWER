import { DrawerLayout } from "oj-c/drawer-layout";
import { RootEnvironmentProvider } from "@oracle/oraclejet-preact/UNSAFE_Environment";
import loadPreactTranslations from "@oracle/oraclejet-preact/translationBundle";
import { registerCustomElement } from "ojs/ojvcomponent";
import Context = require("ojs/ojcontext");
import { h } from "preact";
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { User } from "firebase/auth";
import { publicApi, api } from "../api";
import {
  authenticationErrorMessage,
  initializeAuthentication,
  signInWithGoogle,
  signInWithPassword,
  signOutUser,
} from "../auth";
import { EstateSummary, NavItem, Role, RuntimeConfig, Session } from "../models";
import { Icon } from "./icons";
import { AuthenticationGate, NoAccessGate } from "./access-gates";
import { PageRouter } from "./pages";
import "../styles/app.css";

declare const __MCT_E2E_BYPASS__: boolean;

const E2E_SESSION: Session = {
  uid: "playwright-operator",
  email: "operator@example.test",
  roles: ["viewer", "operator", "approver"],
  estate_roles: { "*": ["viewer", "operator", "approver"] },
  wildcard_roles: ["viewer", "operator", "approver"],
  scoped_estates: [],
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

function rolesFor(session: Session, estateId: string | null): Role[] {
  return Array.from(
    new Set([
      ...(session.wildcard_roles || session.estate_roles?.["*"] || []),
      ...((estateId && session.estate_roles?.[estateId]) || []),
    ]),
  );
}

function estateFromLocation(estates: EstateSummary[]): string | null {
  const requested = new URLSearchParams(window.location.search).get("estate_id");
  const active = estates.filter((estate) => estate.status !== "DISABLED");
  const ids = new Set(active.map((estate) => estate.estate_id));
  if (requested && ids.has(requested)) return requested;
  const remembered = localStorage.getItem("mct.activeEstate");
  if (remembered && ids.has(remembered)) return remembered;
  if (ids.has("wwi-demo-estate")) return "wwi-demo-estate";
  return active[0]?.estate_id || null;
}

type Props = Readonly<Record<string, never>>;

export const App = registerCustomElement("app-root", (_props: Props) => {
  const width = useViewportWidth();
  const [config, setConfig] = useState<RuntimeConfig | null>(null);
  const [firebaseUser, setFirebaseUser] = useState<User | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [preactTranslations, setPreactTranslations] = useState<Record<string, (...args: any[]) => string> | null>(null);
  const [route, setRoute] = useState(routeFromLocation());
  const [estates, setEstates] = useState<EstateSummary[]>([]);
  const [activeEstateId, setActiveEstateId] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<any[]>([]);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const [navigationOpen, setNavigationOpen] = useState(width >= 1024);
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
          if (user) setAuthError(null);
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
    let active = true;
    loadPreactTranslations(null)
      .then((bundle) => active && setPreactTranslations(bundle))
      .catch((reason) => active && setAuthError(reason.message || String(reason)));
    return () => { active = false; };
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
    if (!session) return;
    let active = true;
    api<EstateSummary[]>("/api/v1/estates")
      .then((result) => {
        if (!active) return;
        setEstates(result.data.filter((estate) => estate.status !== "DISABLED"));
        setActiveEstateId((current) => current || estateFromLocation(result.data));
      })
      .catch((reason) => setAuthError(reason.message || String(reason)));
    return () => { active = false; };
  }, [session]);

  useEffect(() => {
    if (!activeEstateId) return;
    localStorage.setItem("mct.activeEstate", activeEstateId);
    const url = new URL(window.location.href);
    url.searchParams.set("estate_id", activeEstateId);
    window.history.replaceState({}, "", `${url.pathname}${url.search}`);
  }, [activeEstateId]);

  useEffect(() => {
    if (!searchOpen || !activeEstateId || searchQuery.trim().length < 1) {
      setSearchResults([]);
      return;
    }
    const timer = window.setTimeout(() => {
      api<any[]>(`/api/v1/search?q=${encodeURIComponent(searchQuery)}&estate_id=${encodeURIComponent(activeEstateId)}`)
        .then((result) => setSearchResults(result.data))
        .catch(() => setSearchResults([]));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [searchOpen, searchQuery, activeEstateId]);

  useEffect(() => {
    const pop = () => {
      setRoute(routeFromLocation());
      const requested = new URLSearchParams(window.location.search).get("estate_id");
      if (requested && estates.some((estate) => estate.estate_id === requested)) {
        setInspector(null);
        setActiveEstateId(requested);
      }
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [estates]);

  useEffect(() => {
    if (!searchOpen && !userMenuOpen) return;
    const dismiss = (event: KeyboardEvent | MouseEvent) => {
      if (event instanceof KeyboardEvent && event.key !== "Escape") return;
      if (event instanceof MouseEvent && (event.target as HTMLElement).closest(".command-popover,.global-search,.user-menu")) return;
      setSearchOpen(false);
      setUserMenuOpen(false);
      if (event instanceof KeyboardEvent) searchRef.current?.focus();
    };
    document.addEventListener("keydown", dismiss);
    document.addEventListener("mousedown", dismiss);
    return () => { document.removeEventListener("keydown", dismiss); document.removeEventListener("mousedown", dismiss); };
  }, [searchOpen, userMenuOpen]);

  useEffect(() => {
    if (width >= 1024) setNavigationOpen(true);
  }, [width]);

  const navigate = (nextRoute: string) => {
    const normalized = nextRoute.replace(/^\/+|\/+$/g, "") || "overview";
    const query = activeEstateId ? `?estate_id=${encodeURIComponent(activeEstateId)}` : "";
    window.history.pushState({}, "", `/${normalized}${query}`);
    setRoute(normalized);
    if (width < 1024) setNavigationOpen(false);
    document.querySelector<HTMLElement>("#main-content")?.focus();
  };

  const switchEstate = (estateId: string) => {
    setInspector(null);
    setActiveEstateId(estateId);
    setSearchOpen(false);
  };

  if (!config || !authReady || !preactTranslations) {
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
        onGoogleSignIn={async () => {
          setAuthError(null);
          try {
            await signInWithGoogle();
          } catch (reason) {
            setAuthError(authenticationErrorMessage(reason));
            throw reason;
          }
        }}
        onPasswordSignIn={async (email, password) => {
          setAuthError(null);
          try {
            await signInWithPassword(email, password);
          } catch (reason) {
            setAuthError(authenticationErrorMessage(reason));
            throw reason;
          }
        }}
      />
    );
  }

  // Authenticated but unauthorized. This is the normal first-run state for
  // a fresh Google account, and it used to drop the user into a console
  // where every request 403'd with no explanation of why.
  if (!__MCT_E2E_BYPASS__ && (session.roles || []).length === 0) {
    return (
      <NoAccessGate
        email={session.email}
        onSignOut={() => void signOutUser()}
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
    <RootEnvironmentProvider environment={{ translations: { "@oracle/oraclejet-preact": preactTranslations } }}>
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
            <strong>{config.environment}</strong>
          </span>
          <span>
            <small>Estate</small>
            <select
              class="estate-selector"
              aria-label="Active estate"
              value={activeEstateId || ""}
              onChange={(event) => switchEstate(event.currentTarget.value)}
            >
              {estates.map((estate) => (
                <option key={estate.estate_id} value={estate.estate_id}>{estate.display_name}</option>
              ))}
            </select>
          </span>
        </div>
        <label class="global-search" onClick={() => { setSearchOpen(true); setUserMenuOpen(false); }}>
          <Icon name="search" />
          <span class="sr-only">Search current workspace</span>
          <input
            ref={searchRef}
            type="search"
            role="combobox"
            aria-autocomplete="list"
            placeholder="Search"
            value={searchQuery}
            aria-expanded={searchOpen}
            aria-controls="command-palette"
            onFocus={() => { setSearchOpen(true); setUserMenuOpen(false); }}
            onInput={(event) => setSearchQuery(event.currentTarget.value)}
            onKeyDown={(event) => event.key === "Escape" && setSearchOpen(false)}
          />
        </label>
        <button class="icon-button mobile-search-button" type="button" aria-label="Open search" onClick={() => { setSearchOpen(true); setUserMenuOpen(false); }}>
          <Icon name="search" />
        </button>
        <button
          class="icon-button"
          type="button"
          aria-label="Notifications"
          onClick={() => {
            setSearchOpen(false);
            setUserMenuOpen(false);
            if (!activeEstateId) return;
            api<any[]>(`/api/v1/notifications?estate_id=${encodeURIComponent(activeEstateId)}`)
              .then((result) => setInspector({ title: "Notifications", value: result.data }))
              .catch((reason) => setInspector({ title: "Notifications", value: { error: reason.message } }));
          }}
        >
          <Icon name="alert" />
        </button>
        <button
          class="user-menu"
          type="button"
          onClick={() => { setUserMenuOpen(!userMenuOpen); setSearchOpen(false); }}
          aria-expanded={userMenuOpen}
          aria-controls="user-account-menu"
        >
          <Icon name="user" />
          <span>{session.email || session.uid}</span>
        </button>
        {searchOpen && (
          <div class="command-popover command-palette" id="command-palette" role="dialog" aria-label="Search commands">
            <div class="popover-heading"><strong>Search this estate</strong><button class="icon-button" onClick={() => setSearchOpen(false)} aria-label="Close search"><Icon name="close" /></button></div>
            <input class="palette-query" type="search" aria-label="Search navigation, estates and runs" placeholder="Search navigation, estates and runs" value={searchQuery} onInput={(event) => setSearchQuery(event.currentTarget.value)} />
            <div class="command-results">
              {NAVIGATION.filter((item) => item.label.toLowerCase().includes(searchQuery.toLowerCase())).map((item) => (
                <button key={item.route} onClick={() => { navigate(item.route); setSearchOpen(false); }}><Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.description}</small></span></button>
              ))}
              {searchResults.map((result) => (
                <button key={`${result.kind}:${result.id}`} onClick={() => { navigate(String(result.route).replace(/^\//, "")); setSearchOpen(false); }}><Icon name={result.kind === "run" ? "runs" : "estates"} /><span><strong>{result.title}</strong><small>{result.subtitle}</small></span></button>
              ))}
              {!searchResults.length && searchQuery && <p class="empty-state">No estate records matched.</p>}
            </div>
          </div>
        )}
        {userMenuOpen && (
          <div class="command-popover user-account-popover" id="user-account-menu" role="menu">
            <strong>{session.email || session.uid}</strong>
            <small>Selected-estate roles: {rolesFor(session, activeEstateId).join(", ") || "none"}</small>
            <button role="menuitem" onClick={() => void signOutUser()}><Icon name="user" /> Sign out</button>
          </div>
        )}
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
            activeEstateId={activeEstateId}
            activeEstate={estates.find((estate) => estate.estate_id === activeEstateId) || null}
            estateRoles={rolesFor(session, activeEstateId)}
            onEstateCreated={(estate) => {
              setEstates((current) => [...current.filter((item) => item.estate_id !== estate.estate_id), estate]);
              switchEstate(estate.estate_id);
            }}
            progressPollIntervalMs={config.progress_poll_interval_ms || 2000}
          />
        </main>
      </DrawerLayout>
      {mediumDesktop && inspector && (
        <span class="sr-only" aria-live="polite">
          Inspector opened as an overlay.
        </span>
      )}
    </div>
    </RootEnvironmentProvider>
  );
});
