// Extracts a JS layer from config_ui.html for node testing.
// Layer boundaries are the "// LAYER N" banner comments inside the single
// <script data-layer="model"> block. Layer 1 ("model") is pure data/logic —
// no DOM — so it evaluates standalone in node.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));

export function extractLayer(n) {
  const html = readFileSync(path.join(ROOT, "shellui", "config_ui.html"), "utf8");
  const m = html.match(/<script data-layer="model">([\s\S]*?)<\/script>/);
  if (!m) throw new Error("script block not found");
  const body = m[1];
  const start = body.indexOf(`// LAYER ${n} `);
  if (start < 0) throw new Error(`LAYER ${n} marker not found`);
  const next = body.indexOf(`// LAYER ${n + 1} `, start);
  return body.slice(start, next < 0 ? undefined : next);
}

// Evaluate layer source and return its bindings as an object.
export function loadLayer(n) {
  const src = extractLayer(n);
  // Collect top-level const/let/function names and return them.
  const names = [...src.matchAll(/^(?:const|let|function)\s+([A-Za-z_$][\w$]*)/gm)]
    .map((x) => x[1]);
  const factory = new Function(`${src}\nreturn {${names.join(",")}};`);
  return factory();
}
