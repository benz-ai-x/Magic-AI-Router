// Extracts a JS layer from config_ui.html for node testing.
// Layer boundaries are the "// LAYER N" banner comments inside the single
// <script data-layer="model"> block. Layer 1 ("model") is pure data/logic —
// no DOM — so it evaluates standalone in node.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));

// 提取契约的单一归宿：runtime.test.mjs 也从这里取整段脚本——
// 双份正则曾是 drift 温床（一处改 data-layer 属性另一处静默失配）。
export function scriptSource() {
  const html = readFileSync(path.join(ROOT, "shellui", "config_ui.html"), "utf8");
  const m = html.match(/<script data-layer="model">([\s\S]*?)<\/script>/);
  if (!m) throw new Error("script block not found");
  return m[1];
}

export function extractLayer(n) {
  const body = scriptSource();
  const start = body.indexOf(`// LAYER ${n} `);
  if (start < 0) throw new Error(`LAYER ${n} marker not found`);
  const next = body.indexOf(`// LAYER ${n + 1} `, start);
  return body.slice(start, next < 0 ? undefined : next);
}

// Evaluate layer source and return its bindings as an object.
export function loadLayer(n) {
  const src = extractLayer(n);
  // Collect top-level const/let/function names and return them.
  const names = [...src.matchAll(/^(?:async\s+)?(?:const|let|function)\s+([A-Za-z_$][\w$]*)/gm)]
    .map((x) => x[1]);
  const factory = new Function(`${src}\nreturn {${names.join(",")}};`);
  return factory();
}
