const { test } = require("node:test");
const assert = require("node:assert");
const {
  decideLinuxFrame,
  normalizeFramelessOverride,
  prefersClientSideDecorations,
  desktopTokens,
} = require("../linux-frame");

// ── desktopTokens ──

test("splits XDG_CURRENT_DESKTOP on colons and lowercases", () => {
  assert.deepStrictEqual(
    desktopTokens({ XDG_CURRENT_DESKTOP: "ubuntu:GNOME" }),
    ["ubuntu", "gnome"],
  );
});

test("folds all three XDG variables", () => {
  assert.deepStrictEqual(
    desktopTokens({
      XDG_CURRENT_DESKTOP: "KDE",
      XDG_SESSION_DESKTOP: "plasma",
      DESKTOP_SESSION: "plasmax11",
    }),
    ["kde", "plasma", "plasmax11"],
  );
});

test("empty env yields no tokens", () => {
  assert.deepStrictEqual(desktopTokens({}), []);
});

// ── prefersClientSideDecorations ──

test("GNOME-family desktops prefer CSD", () => {
  for (const desk of ["GNOME", "ubuntu:GNOME", "Unity", "Pantheon", "Budgie:GNOME"]) {
    assert.strictEqual(
      prefersClientSideDecorations({ XDG_CURRENT_DESKTOP: desk }),
      true,
      desk,
    );
  }
});

test("SSD desktops and tiling WMs do not prefer CSD", () => {
  for (const desk of ["KDE", "XFCE", "LXQt", "i3", "sway", "Hyprland", "MATE", "X-Cinnamon"]) {
    assert.strictEqual(
      prefersClientSideDecorations({ XDG_CURRENT_DESKTOP: desk }),
      false,
      desk,
    );
  }
});

test("headless (no desktop vars) does not prefer CSD", () => {
  assert.strictEqual(prefersClientSideDecorations({}), false);
});

// ── normalizeFramelessOverride: operator-editable JSON is untrusted ──

test("only literal booleans pass through; everything else is auto", () => {
  assert.strictEqual(normalizeFramelessOverride(true), true);
  assert.strictEqual(normalizeFramelessOverride(false), false);
  for (const junk of [null, undefined, "true", "false", 1, 0, "", {}, []]) {
    assert.strictEqual(normalizeFramelessOverride(junk), null, JSON.stringify(junk));
  }
});

// ── decideLinuxFrame ──

test("GNOME session goes frameless (kills the doubled title bar)", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "ubuntu:GNOME" } });
  assert.strictEqual(d.frameless, true);
  assert.strictEqual(d.reason, "csd-desktop");
});

test("tiling WM keeps the native frame (fail-safe)", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "i3" } });
  assert.strictEqual(d.frameless, false);
  assert.strictEqual(d.reason, "ssd-or-unknown-desktop");
});

test("unknown/headless environment keeps the native frame (fail-safe)", () => {
  const d = decideLinuxFrame({ env: {} });
  assert.strictEqual(d.frameless, false);
});

test("override=true forces frameless even on KDE", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "KDE" }, override: true });
  assert.strictEqual(d.frameless, true);
  assert.strictEqual(d.reason, "override-frameless");
});

test("override=false forces native frame even on GNOME", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "GNOME" }, override: false });
  assert.strictEqual(d.frameless, false);
  assert.strictEqual(d.reason, "override-native-frame");
});

test("string override is ignored (auto)", () => {
  const d = decideLinuxFrame({ env: { XDG_CURRENT_DESKTOP: "GNOME" }, override: "false" });
  assert.strictEqual(d.frameless, true, "malformed override must not disable the heuristic");
});

test("no arguments at all keeps the native frame", () => {
  assert.strictEqual(decideLinuxFrame().frameless, false);
});
