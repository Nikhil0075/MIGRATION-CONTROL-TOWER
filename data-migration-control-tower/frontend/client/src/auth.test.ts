import { describe, expect, it } from "vitest";
import { authenticationErrorMessage } from "./auth";

describe("authentication errors", () => {
  it.each(["auth/invalid-credential", "auth/user-not-found", "auth/wrong-password"])(
    "uses the same enumeration-safe message for %s",
    (code) => {
      expect(authenticationErrorMessage({ code })).toBe(
        "The email address or password is incorrect.",
      );
    },
  );

  it("explains when the Firebase password provider is disabled", () => {
    expect(authenticationErrorMessage({ code: "auth/operation-not-allowed" })).toMatch(
      /not enabled/,
    );
  });

  it("does not render an unknown provider error verbatim", () => {
    expect(
      authenticationErrorMessage({
        code: "auth/internal-error",
        message: "sensitive provider detail",
      }),
    ).toBe("Unable to sign in. Try again or contact an administrator.");
  });
});
