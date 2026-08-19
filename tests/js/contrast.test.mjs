// WCAG AA contrast guard for the theme tokens (#3): the light palette once
// shipped warn 2.19:1 / ok 3.38:1 / danger 3.55:1 / accent 4.10:1 against
// white. This test parses the actual CSS variables from config_ui.html and
// fails if any small-text status color drops below 4.5:1 on either card
// background (--bg or --surface), in both light and dark themes.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const HTML = readFileSync(path.join(ROOT, "config_ui.html"), "utf8");

function tokens(css) {
  const out = {};
  // Accept 6-hex and 3-hex shorthand (the light theme uses #fff), normalize to 6.
  for (const m of css.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b/g)) {
    const hex = m[2];
    out[m[1]] = hex.length === 4
      ? "#" + [...hex.slice(1)].map((c) => c + c).join("") : hex;
  }
  return out;
}
const lightBlock = HTML.match(/:root\{([^}]*)\}/)[1];
const darkBlock = HTML.match(/@media\(prefers-color-scheme:dark\)\{:root\{([^}]*)\}/)[1];
const light = tokens(lightBlock);
const dark = tokens(darkBlock);

function luminance(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function contrast(fg, bg) {
  const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
  return (hi + 0.05) / (lo + 0.05);
}

// Small-text status colors must clear AA on both backgrounds the cards use.
const STATUS = ["ok", "warn", "danger", "accent", "muted"];
for (const theme of [
  { name: "light", vars: light, bgs: [light.bg, light.surface] },
  { name: "dark", vars: dark, bgs: [dark.bg, dark.surface] },
]) {
  test(`${theme.name} theme status colors meet WCAG AA 4.5:1`, () => {
    for (const name of STATUS) {
      const fg = theme.vars[name];
      assert.ok(fg, `--${name} missing in ${theme.name} theme`);
      for (const bg of theme.bgs) {
        const r = contrast(fg, bg);
        assert.ok(r >= 4.5,
          `--${name} ${fg} on ${bg}: ${r.toFixed(2)}:1 < 4.5:1 (${theme.name})`);
      }
    }
  });
}

test("light and dark themes define the full token set", () => {
  for (const name of [...STATUS, "bg", "surface", "fg", "border"]) {
    assert.ok(light[name], `light --${name} missing`);
    assert.ok(dark[name], `dark --${name} missing`);
  }
});
