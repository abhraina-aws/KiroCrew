"use strict";

// Decides whether the Linux window should drop the native frame.
//
// On Linux the dashboard's own 42px header would otherwise stack under the
// window manager's native title bar -- two title bars, one duplicating the
// other's controls. Modern Electron honors `frame: false` on Linux, so the
// header can double as the title bar the same way it does on macOS and
// Windows.
//
// The decision is NOT unconditional. Whether a window should draw its own
// decorations is the desktop environment's call, not ours:
//   - GNOME-family desktops expect client-side decorations (GNOME's Wayland
//     compositor does not offer server-side decorations), so a frameless
//     window with an in-app header is the platform-native shape there.
//   - KDE, XFCE, LXQt, and tiling window managers (i3, sway, hyprland, ...)
//     expect server-side decorations. Going frameless there strips the
//     window of WM-provided dragging and edge-resize affordances -- a worse
//     regression than the doubled title bar.
//   - Unknown or headless environments keep the native frame (fail-safe:
//     today's behavior).
//
// The operator can force either shape via the `linuxFrameless` store key
// (true = always frameless, false = always native frame, anything else =
// decide from the desktop environment).

// Desktop-environment names (lowercased XDG tokens) that prefer client-side
// decorations. "ubuntu" appears alone in XDG_CURRENT_DESKTOP on some Ubuntu
// GNOME sessions ("ubuntu:GNOME" splits into both tokens on others).
const CSD_DESKTOPS = new Set([
  "gnome",
  "gnome-classic",
  "gnome-flashback",
  "ubuntu",
  "unity",
  "pantheon",
  "budgie",
  "budgie-desktop",
]);

/**
 * Lowercased desktop-identity tokens from the standard XDG variables.
 * XDG_CURRENT_DESKTOP is colon-separated by spec ("ubuntu:GNOME"); the other
 * two are single values but are folded through the same splitter for
 * uniformity.
 *
 * @param {Record<string, string|undefined>} env
 * @returns {string[]}
 */
function desktopTokens(env) {
  const raw = [env.XDG_CURRENT_DESKTOP, env.XDG_SESSION_DESKTOP, env.DESKTOP_SESSION]
    .filter(Boolean)
    .join(":");
  return raw
    .split(":")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

/**
 * True when the session's desktop environment expects windows to draw their
 * own decorations (see CSD_DESKTOPS).
 *
 * @param {Record<string, string|undefined>} env
 */
function prefersClientSideDecorations(env) {
  return desktopTokens(env).some((t) => CSD_DESKTOPS.has(t));
}

/**
 * Normalize the persisted override to strict boolean-or-null. electron-store
 * data is operator-editable JSON, so anything that is not literally true or
 * false means "auto".
 *
 * @param {*} value
 * @returns {boolean|null}
 */
function normalizeFramelessOverride(value) {
  return value === true || value === false ? value : null;
}

/**
 * The frame decision for a Linux window.
 *
 * @param {{env?: Record<string, string|undefined>, override?: *}} [opts]
 * @returns {{frameless: boolean, reason: string}}
 */
function decideLinuxFrame({ env = {}, override = null } = {}) {
  const forced = normalizeFramelessOverride(override);
  if (forced === true) return { frameless: true, reason: "override-frameless" };
  if (forced === false) return { frameless: false, reason: "override-native-frame" };
  if (prefersClientSideDecorations(env)) return { frameless: true, reason: "csd-desktop" };
  return { frameless: false, reason: "ssd-or-unknown-desktop" };
}

module.exports = {
  decideLinuxFrame,
  normalizeFramelessOverride,
  prefersClientSideDecorations,
  desktopTokens,
};
