// Runtime interaction tests for config_ui.html without a browser dependency.
// The shipped script is evaluated with a deliberately small DOM facade so the
// dirty-state and rendering contracts stay testable with plain Node.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import { scriptSource } from "./extract.mjs";

const ROOT = path.dirname(path.dirname(path.dirname(fileURLToPath(import.meta.url))));
const HTML = readFileSync(path.join(ROOT, "shellui", "config_ui.html"), "utf8");
// 脚本提取走 extract.mjs 单一归宿（HTML 仍本地读取——CSS 断言用）
const SCRIPT = scriptSource().replace(/\nload\(\);\s*$/, "\n");

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
  const html = rt.run("S=normalizeState({mp:{servers:[]}});activeTunnel=0;tunnelHTML()");
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
    S=normalizeState({mp:{servers:[{name:'t1',has_password:true,
      ssh:{user:'',host:'h',port:22,auth_type:'password',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
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
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);

  rt.run("window.__pw.value='secret';collectAndRecompute()");
  assert.equal(rt.run("dirty"), true);

  rt.run("window.__pw.value='';collectAndRecompute()");
  assert.equal(rt.run("dirty"), false, "cleared password must clear dirty");
  assert.equal(rt.run("S.mp.servers[0].password"), null,
    "cleared password must not leave a phantom value for the keychain write");
});

test("collectTunnel reads forward rows into the active tunnel", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'u',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'u@h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    const row=vals=>({querySelector:function(sel){
      const m=sel.match(/data-fwf="(\\w+)"/);
      return m?vals[m[1]]:null;}});
    const rows=[row({local_port:{value:'9000'},remote_host:{value:' 10.0.0.5 '},
      remote_port:{value:'8000'}}),
      row({local_port:{value:''},remote_host:{value:''},remote_port:{value:''}})];
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    },querySelectorAll:function(sel){
      return sel.includes('data-fwr')?rows:[];
    }};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);
  rt.run("collectTunnel();recomputeDirty()");
  // vm 跨 realm 对象不走 deepEqual（原型不同）——JSON 字符串钉形状
  assert.equal(rt.run("JSON.stringify(S.mp.servers[0].services.ssh.forwards)"),
    JSON.stringify([
      { local_port: 9000, remote_host: "10.0.0.5", remote_port: 8000,
        enabled: true },
      { local_port: 0, remote_host: "127.0.0.1", remote_port: 0,
        enabled: true },
    ]), "行序即数组序；空白地址 trim 后缺省 127.0.0.1，空端口为 0，"
    + "无开关（缺省）行为启用");
  assert.equal(rt.run("dirty"), true, "新增转发行必须点亮保存按钮");
});

test("collectTunnel reads the forward_autostart switch", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''},
      fw_autostart:{getAttribute:()=> 'true'}};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      if(m&&m[1]==='fw_autostart')return fields.fw_autostart;
      if(m&&fields[m[1].replace('fw_','')])return fields[m[1]];
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);
  rt.run("collectTunnel();recomputeDirty()");
  assert.equal(rt.run("S.mp.servers[0].services.ssh.autostart"), true,
    "autostart 开关经 collect 读回（services.ssh.autostart）");
  assert.equal(rt.run("dirty"), true, "开关翻转点亮保存按钮");
});

// ── proxy role：查看 ≠ 切换——隐式写已删，角色只经 setProxyTunnel 显式变更 ──
function setupTunnelForm(rt, { role = "t-a", active = 0 } = {}) {
  const name = active === 0 ? "A" : "B", addr = active === 0 ? "a" : "b";
  rt.run(`
    S=normalizeState({mp:{proxy_server_id:'${role}',servers:[
      {id:'t-a',name:'A',ssh:{user:'',host:'a',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-b',name:'B',ssh:{user:'',host:'b',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=${active};recomputeDirty();
    const fields={name:{value:'${name}'},addr:{value:'${addr}'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      if(m&&m[1]==='fw_autostart')return{getAttribute:()=>'false'};
      if(m&&fields[m[1]])return fields[m[1]];
      return null;
    },querySelectorAll:function(){return[];}};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
    document.querySelectorAll=function(){return[];};
    document.getElementById('viewport').firstElementChild={classList:{add(){}}};
  `);
}

test("viewing another tunnel must not silently switch the proxy role", () => {
  const rt = makeRuntime();
  setupTunnelForm(rt, { role: "t-a", active: 1 });
  // 保存路径的精确复现：用户停留在隧道页查看 B（activeTunnel=1）时按下保存
  rt.run("collect(true);recomputeDirty()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-a",
    "collect 只读表单——正在查看的服务器绝不能被隐式写成代理服务器");
  assert.equal(rt.run("dirty"), false, "单纯查看另一条隧道不得伪造待保存项");
});

test("setProxyTunnel marks the role switch as one tracked, reversible change", () => {
  const rt = makeRuntime();
  setupTunnelForm(rt, { role: "t-a", active: 1 });
  assert.match(rt.run("tunnelHTML()"), /设为代理隧道/,
    "非代理隧道的详情栏必须暴露显式角色动作");
  rt.run("setProxyTunnel()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-b");
  assert.equal(rt.run("dirty"), true);
  assert.equal(rt.run("totalDirtyCount()"), 1, "只有角色一个叶子计入待保存");
  assert.match(rt.run("tunnelHTML()"), /fw-badge[^>]*>代理隧道</,
    "当前代理服务器渲染徽标而非按钮");
  rt.run("discardAll()");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-a", "放弃更改恢复已保存的角色");
});

test("deleting a tunnel keeps the proxy role on the same tunnel", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{proxy_server_id:'t-c',servers:[
      {id:'t-a',name:'A',ssh:{user:'',host:'a',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-b',name:'B',ssh:{user:'',host:'b',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}},
      {id:'t-c',name:'C',ssh:{user:'',host:'c',port:22,auth_type:'key',ssh_key:'',compression:true},services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=2;recomputeDirty();
    document.querySelector=function(){return null;};
    document.querySelectorAll=function(){return[];};
    document.getElementById('viewport').firstElementChild={classList:{add(){}}};
  `);
  rt.run("removeTunnel(0)");
  assert.equal(rt.run("S.mp.proxy_server_id"), "t-c",
    "删掉代理前面的服务器后，角色 id 纹丝不动——不再依赖下标");
  assert.equal(rt.run("proxyIndexOf(S)"), 1, "解析下标指向同一条服务器");
  assert.equal(rt.run("S.mp.servers[proxyIndexOf(S)].id"), "t-c");
  rt.run("removeTunnel(1)");
  assert.equal(rt.run("S.mp.servers.length"), 1);
  assert.equal(rt.run("S.mp.proxy_server_id"), "",
    "删掉代理自身后清空 id 真相，交由首条兜底回落");
  assert.equal(rt.run("proxyIndexOf(S)"), 0);
});

// ── NFS 远程挂载视图（ADR-007 master-detail 重构）──────────
test("nfs view renders master-detail mirroring the tunnel view", () => {
  const rt = makeRuntime();
  const html = rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{user:'u',host:'a.example',port:22},
       services:{nfs:{enabled:true,local_port:12049,squash_to_ssh_user:false,
            mounts:[{name:'data',remote_path:'/data',local_dir:'',auto_mount:true}]}},
       nfs_states:{data:'mounted'}},
      {id:'t-2',name:'srv-b',ssh:{user:'u',host:'b.example',port:22},
       services:{nfs:{enabled:false,local_port:12049,squash_to_ssh_user:false,mounts:[]}}},
    ]}});
    activeTunnel=0;nfsHTML();
  `);
  // master：服务器列表 + 启用态圆点 + 挂载数徽标
  assert.match(html, /class="md-master"/);
  assert.match(html, /selectNfsTunnel\(1\)/);
  assert.match(html, /1 挂载中/);
  // detail：操作在 detail-bar，启用开关/端口/squash 在挂载选项区
  assert.match(html, /class="md-detail"/);
  assert.match(html, /nfsCheckRemote\(this\)/);
  assert.match(html, /data-nf="enabled"/);
  assert.match(html, /data-nf="port"/);
  assert.match(html, /data-nf="squash"/);
  // 挂载行：状态徽标 + 即时操作
  assert.match(html, /已挂载/);
  assert.match(html, /nfsMountAction\(this,0,'mount'\)/);
});

test("nfs view follows the shared activeTunnel selection", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[
      {id:'t-1',name:'srv-a',ssh:{host:'a.example'},services:{nfs:{enabled:true,local_port:12049,mounts:[]}}},
      {id:'t-2',name:'srv-b',ssh:{host:'b.example'},services:{nfs:{enabled:false,local_port:12049,mounts:[]}}},
    ]}});
    activeTunnel=0;
  `);
  assert.match(rt.run("nfsHTML()"),
    /is-selected" onclick="selectNfsTunnel\(0\)"/);
  rt.run("renderView=()=>undefined;selectNfsTunnel(1)");
  assert.equal(rt.run("activeTunnel"), 1);
  assert.match(rt.run("nfsHTML()"),
    /is-selected" onclick="selectNfsTunnel\(1\)"/);
  assert.doesNotMatch(rt.run("nfsHTML()"),
    /is-selected" onclick="selectNfsTunnel\(0\)"/);
});

test("nfs view empty state without tunnels", () => {
  const rt = makeRuntime();
  const html = rt.run("S=normalizeState({mp:{servers:[]}});nfsHTML()");
  assert.match(html, /还没有配置隧道/);
});


test("collectTunnel reads per-row enabled switches", () => {
  const rt = makeRuntime();
  rt.run(`
    S=normalizeState({mp:{servers:[{name:'t1',
      ssh:{user:'u',host:'h',port:22,auth_type:'key',ssh_key:'',compression:true},
      services:{ssh:{forwards:[],autostart:false}}}]}});
    baselineState=cloneData(S);baselineRoles={};ccRoles={};
    activeView='tunnel';activeTunnel=0;recomputeDirty();
    const fields={name:{value:'t1'},addr:{value:'u@h'},ssh_port:{value:'22'},
      auth:{value:'key'},key:{value:''}};
    const row=(vals,sw)=>({querySelector:function(sel){
      const m=sel.match(/data-fwf="enabled"/);
      if(m)return sw||null;
      const k=sel.match(/data-fwf="(\\w+)"/);
      return k?vals[k[1]]:null;}});
    const rows=[row({local_port:{value:'9000'},remote_host:{value:'x'},
      remote_port:{value:'80'}},{getAttribute:()=>'true'}),
      row({local_port:{value:'9001'},remote_host:{value:'x'},
      remote_port:{value:'81'}},{getAttribute:()=>'false'})];
    window.__detail={querySelector:function(sel){
      const m=sel.match(/data-tf="(\\w+)"/);
      if(m&&fields[m[1]])return fields[m[1]];
      if(m&&m[1]==='compress')return{getAttribute:()=>'true'};
      return null;
    },querySelectorAll:function(sel){
      return sel.includes('data-fwr')?rows:[];
    }};
    document.querySelector=function(sel){
      return sel.includes('detail-body')?window.__detail:null;
    };
  `);
  rt.run("collectTunnel();recomputeDirty()");
  assert.equal(rt.run("JSON.stringify(S.mp.servers[0].services.ssh.forwards)"),
    JSON.stringify([
      { local_port: 9000, remote_host: "x", remote_port: 80, enabled: true },
      { local_port: 9001, remote_host: "x", remote_port: 81, enabled: false },
    ]), "行内启用开关经 aria-checked 读回（逐条启停的配置面）");
  assert.equal(rt.run("dirty"), true);
});
