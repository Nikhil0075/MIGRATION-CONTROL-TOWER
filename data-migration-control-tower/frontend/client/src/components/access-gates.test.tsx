import { cleanup, render, screen } from "@testing-library/preact";
import { h } from "preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthenticationGate, NoAccessGate } from "./access-gates";

afterEach(cleanup);

const authProps = {
  configured: true,
  onGoogleSignIn: vi.fn(async () => undefined),
  onPasswordSignIn: vi.fn(async () => undefined),
};

describe("brand mark accessibility", () => {
  it("names the logo on the sign-in screen", () => {
    // On the command bar the logo is decorative — the product name sits
    // beside it in text, so naming it there would make a screen reader
    // announce the same thing twice. Here the logo IS the identity, above
    // the heading, so it carries a real accessible name. That asymmetry is
    // deliberate and easy to "tidy" into being wrong.
    render(<AuthenticationGate {...authProps} />);
    expect(screen.getByAltText("Migration Control Tower")).toBeTruthy();
  });

  it("names the logo on the no-access screen too", () => {
    render(<NoAccessGate email="someone@example.internal" onSignOut={vi.fn()} />);
    expect(screen.getByAltText("Migration Control Tower")).toBeTruthy();
  });

  it("points the logo at a local asset, never a remote one", () => {
    // img-src is 'self': a remote logo renders as nothing at all.
    render(<AuthenticationGate {...authProps} />);
    const logo = screen.getByAltText("Migration Control Tower") as HTMLImageElement;
    expect(logo.getAttribute("src")).toMatch(/^\/assets\/brand\//);
  });
});

describe("sign-in hero", () => {
  it("is decorative, so a screen reader is not told about the artwork", () => {
    render(<AuthenticationGate {...authProps} />);
    const art = document.querySelector(".auth-art");
    expect(art?.getAttribute("aria-hidden")).toBe("true");
    expect(art?.querySelector("img")?.getAttribute("alt")).toBe("");
  });

  it("loads the hero from our own origin and defers it", () => {
    // img-src is 'self'. And nothing about the sign-in FORM depends on the
    // artwork, so it must not block the thing the page exists to do.
    render(<AuthenticationGate {...authProps} />);
    const img = document.querySelector(".auth-art img") as HTMLImageElement;
    expect(img.getAttribute("src")).toMatch(/^\/assets\/brand\//);
    expect(img.getAttribute("loading")).toBe("lazy");
  });

  it("keeps the artwork out of the no-access screen", () => {
    // That screen is an error state. Celebrating it would be the wrong
    // register for someone who has just been told they have no access.
    render(<NoAccessGate email="someone@example.internal" onSignOut={vi.fn()} />);
    expect(document.querySelector(".auth-art")).toBeNull();
  });
});
