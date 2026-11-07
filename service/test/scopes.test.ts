import { describe, expect, it } from "vitest";
import { allows, matches } from "../src/scopes";

describe("topic scopes", () => {
  it.each([
    ["verify/linux", "verify/linux", true],
    ["verify/linux", "verify/linux/error", false],
    ["verify/*", "verify/linux", true],
    ["verify/*", "verify", false],
    ["verify/*", "verify/linux/error", false],
    ["verify/**", "verify", false],
    ["verify/**", "verify/linux", true],
    ["verify/**", "verify/linux/error", true],
    ["**", "verify", true],
    ["**", "verify/linux/error", true],
    ["*", "verify/linux", false],
    ["mac-*", "mac-a1b2", true],
    ["mac-*", "mac-", true],
    ["mac-*", "mac", false],
    ["a*b*c", "abc", true],
    ["a*b*c", "a-one-b-two-c", true],
    ["a*b*c", "a-one-b-two-d", false],
    ["a*b*c", "ac", false],
    ["a.b", "axb", false],
    ["a.b", "a.b", true],
    ["**/error", "error", false],
    ["**/error", "work/error", true],
    ["**/error", "work/linux/error", true],
    ["**/work/**", "a/work/b", true],
    ["**/work/**", "work/b", false],
    ["**/work/**", "a/work", false],
    ["a/**/b/**/c", "a/x/b/y/c", true],
    ["a/**/b/**/c", "a/b/y/c", false],
    ["**/**", "one", false],
    ["**/**", "one/two", true],
    ["work/**/error", "work/error", false],
    ["work/**/error", "work/linux/error", true],
    ["work/**/error", "work/linux/mac/error", true],
    ["work", "other/work", false],
    ["work", "work/other", false],
    ["*", "", false],
    ["**", "", false],
    ["**", "a//b", false],
    ["a**", "abc", false],
    ["***", "abc", false],
    ["", "abc", false],
  ])("matches %s against %s as %s", (glob, topic, expected) => {
    expect(matches(glob, topic)).toBe(expected);
  });

  it("keeps scope kinds distinct", () => {
    const scopes = {
      create: ["reply/*"],
      own: ["verify/**"],
      read: ["visible/*"],
    };
    expect(allows(scopes, "own", "verify/linux")).toBe(true);
    expect(allows(scopes, "create", "verify/linux")).toBe(false);
    expect(allows(scopes, "read", "verify/linux")).toBe(false);
    expect(allows(scopes, "read", "visible/linux")).toBe(true);
    expect(allows({ create: [], own: [], read: [] }, "read", "work")).toBe(
      false,
    );
  });

  it("handles many wildcard segments without recursive backtracking", () => {
    expect(
      matches(Array(100).fill("**").join("/"), Array(100).fill("a").join("/")),
    ).toBe(true);
    expect(
      matches(Array(100).fill("**").join("/"), Array(99).fill("a").join("/")),
    ).toBe(false);
  });
});
