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
// ADR-012 M2：分层求值时注入 t()——LAYER 1 的文案经 t() 取词，zh catalog
// 是唯一中文真相源（与页面运行时同一份 shared/locales/*.json）。
export function i18nPrelude() {
  const zh = JSON.parse(readFileSync(path.join(ROOT, "shared", "locales", "zh-CN.json"), "utf8"));
  const en = JSON.parse(readFileSync(path.join(ROOT, "shared", "locales", "en.json"), "utf8"));
  return (
    "const __I18N_MESSAGES={'zh-CN':" + JSON.stringify(zh) + ",en:" + JSON.stringify(en) + "};\n" +
    "function tt(key,params){let s=(__I18N_MESSAGES['zh-CN']||{})[key];" +
    "if(s==null)s=(__I18N_MESSAGES.en||{})[key];if(s==null)return key;" +
    "if(params)s=s.replace(/\\{(\\w+)\\}/g,(_,k)=>params[k]!=null?String(params[k]):'{'+'k'+'}');return s;}\n"
  );
}

export function loadLayer(n) {
  const src = extractLayer(n);
  // Collect top-level const/let/function names and return them.
  const names = [...src.matchAll(/^(?:async\s+)?(?:const|let|function)\s+([A-Za-z_$][\w$]*)/gm)]
    .map((x) => x[1]);
  const factory = new Function(i18nPrelude() + `${src}\nreturn {${names.join(",")}};`);
  return factory();
}
