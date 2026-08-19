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
