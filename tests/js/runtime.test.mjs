// Runtime interaction tests for config_ui.html without a browser dependency.
// The shipped script is evaluated with a deliberately small DOM facade so the
// dirty-state and rendering contracts stay testable with plain Node.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const HTML = readFileSync(path.join(ROOT, "shellui", "config_ui.html"), "utf8");
const SCRIPT = HTML.match(/<script data-layer="model">([\s\S]*?)<\/script>/)[1]
  .replace(/\nload\(\);\s*$/, "\n");

test("workbench regions stay pinned when the pending bar is hidden", () => {
  assert.match(
    HTML,
    /\.workbench\{[^}]*grid-template-areas:"header" "pending" "content" "status"/,
  );
  assert.match(HTML, /\.app-header\{grid-area:header;/);
  assert.match(HTML, /\.pending-bar\{grid-area:pending;/);
  assert.match(HTML, /\.content-viewport\{grid-area:content;/);
  assert.match(HTML, /\.statusbar\{grid-area:status;/);
});

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(name) { this.values.add(name); }
  remove(name) { this.values.delete(name); }
  toggle(name, force) {
    const on = force === undefined ? !this.values.has(name) : Boolean(force);
    if (on) this.values.add(name); else this.values.delete(name);
    return on;
  }
  contains(name) { return this.values.has(name); }
}

function fakeElement(extra = {}) {
  const attrs = new Map();
  return {
    textContent: "",
    innerHTML: "",
    value: "",
    checked: false,
    disabled: false,
    hidden: false,
    title: "",
    classList: new FakeClassList(),
    style: {},
    dataset: {},
    addEventListener() {},
    setAttribute(name, value) { attrs.set(name, String(value)); },
    getAttribute(name) { return attrs.has(name) ? attrs.get(name) : null; },
    querySelector() { return null; },
    ...extra,
  };
}

function makeRuntime() {
  const elements = new Map();
  for (const id of [
    "save-btn", "status-left", "shortcut-hint", "pending-bar",
    "pending-title", "pending-items", "pending-save-btn", "viewport",
    "nav-container", "page-title", "page-subtitle", "toast", "toast-msg",
  ]) elements.set(id, fakeElement());

  for (const id of ["cfg-sysproxy", "cfg-sleep", "cfg-login"]) {
    const label = fakeElement();
    const el = fakeElement({
      parentElement: { querySelector: () => label },
    });
    el.setAttribute("aria-checked", "false");
    elements.set(id, el);
  }

  const document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; },
    addEventListener() {},
  };
  const context = vm.createContext({
    console,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    structuredClone,
    location: { search: "", port: "9528" },
    navigator: { clipboard: { writeText: async () => {} } },
    window: {},
    document,
    fetch: async () => { throw new Error("unexpected fetch"); },
  });
  vm.runInContext(SCRIPT, context);
  return {
    elements,
    run(source) { return vm.runInContext(source, context); },
  };
}

test("a system switch reverted to its baseline clears dirty state", () => {
  const rt = makeRuntime();
  rt.run(`
    window.bridgeMessages=[];
    window.webkit={messageHandlers:{bridge:{postMessage(message){window.bridgeMessages.push(message);}}}};
    S=normalizeState({mp:{system_proxy_default:false,prevent_sleep:false,launch_at_login:false}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};activeView='system';
    recomputeDirty();
  `);

  rt.run("toggleSwitch(document.getElementById('cfg-sysproxy'))");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("totalDirtyCount()"), 1);
  assert.equal(rt.elements.get("pending-bar").hidden, false);

  rt.run("toggleSwitch(document.getElementById('cfg-sysproxy'))");
  assert.equal(rt.run("dirty"), false);
  assert.equal(rt.run("totalDirtyCount()"), 0);
  assert.equal(rt.elements.get("pending-bar").hidden, true);
  assert.equal(rt.elements.get("save-btn").disabled, true);
  assert.deepEqual(
    structuredClone(rt.run("window.bridgeMessages.at(-1)")),
    { type: "dirtyState", payload: { dirty: false } },
  );
});

test("read-only pages expose refresh while preserving visible cross-page changes", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{system_proxy_default:false,prevent_sleep:false,launch_at_login:false}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};activeView='system';
    recomputeDirty();toggleSwitch(document.getElementById('cfg-sysproxy'));
    activeView='usage';updateStatus();
  `);

  assert.equal(rt.elements.get("save-btn").textContent, "刷新");
  assert.equal(rt.elements.get("save-btn").disabled, false);
  assert.equal(rt.elements.get("pending-bar").hidden, false);
  assert.match(rt.elements.get("pending-items").innerHTML, /系统选项 · 1 项/);
  assert.equal(rt.elements.get("shortcut-hint").textContent, "⌘S 保存全部");
  assert.doesNotMatch(rt.elements.get("save-btn").textContent, /保存/);
});

test("a clean read-only page advertises refresh, not save", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({});baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='status';recomputeDirty();
  `);
  assert.equal(rt.elements.get("save-btn").textContent, "刷新");
  assert.equal(rt.elements.get("shortcut-hint").textContent, "⌘R 刷新");
});

test("zero tunnels render a real empty state without a fake Server 1 editor", () => {
  const rt = makeRuntime();
  const html = rt.run("S=normalizeState({mp:{tunnels:[]}});activeTunnel=0;tunnelHTML()");
  assert.match(html, /0 个隧道/);
  assert.match(html, /添加第一个隧道/);
  assert.doesNotMatch(html, /Server 1/);
  assert.doesNotMatch(html, /data-tf=/);
});

test("typing then clearing a provider API key restores the masked baseline", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({sp:{providers:{p1:{base_url:'',api_key:null,api_key_set:true,
      api_key_env:null,auth_header:null,models:[],anthropic_native:false,enabled:true}}}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeProvider='p1';activeView='providers';recomputeDirty();
    const fields={name:{value:'p1'},base_url:{value:''},api_key_env:{value:''},auth_header:{value:''}};
    window.__key={value:''};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-pf="(\\w+)"/);
      if(m&&m[1]==='api_key')return window.__key;
      if(m&&fields[m[1]])return fields[m[1]];
      return null;
    }};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);

  rt.run("window.__key.value='sk-typed';collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("S.sp.providers.p1.api_key"), "sk-typed");

  rt.run("window.__key.value='';collectAndRecompute()");
  assert.equal(rt.run("dirty"), false, "cleared key must clear dirty");
  assert.equal(rt.run("S.sp.providers.p1.api_key"), null,
    "cleared key must not leave a phantom value that would be silently saved");
  assert.equal(rt.run("S.sp.providers.p1.api_key_set"), true,
    "api_key_set is the server's masked truth and is never mutated by typing");
});

test("typing then clearing an SSH password restores the masked baseline", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{tunnels:[{name:'t1',ssh_user:'',ssh_host:'h',ssh_port:22,
      auth_type:'password',ssh_key:'',ssh_compression:true,has_password:true}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'h'},ssh_port:{value:'22'},
      auth:{value:'password'},key:{value:''}};
    window.__pw={value:''};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='pw')return window.__pw;
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    }};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);

  rt.run("window.__pw.value='secret';collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);

  rt.run("window.__pw.value='';collectAndRecompute()");
  assert.equal(rt.run("dirty"), false, "cleared password must clear dirty");
  assert.equal(rt.run("S.mp.tunnels[0].password"), null,
    "cleared password must not leave a phantom value for the keychain write");
});
