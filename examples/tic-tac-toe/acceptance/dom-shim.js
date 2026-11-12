// Minimal DOM SHIM for Node — NOT a browser. It parses a single HTML file, runs its
// inline <script>s in a vm context and supports the DOM surface a small game uses
// (selectors, events with bubbling, inline on* handlers, textContent/innerHTML,
// classList, dataset, disabled buttons). Real-browser checks live in browser_check.py.
"use strict";
const vm = require("node:vm");

const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
  "source", "track", "wbr"]);
const RAW = new Set(["script", "style", "textarea", "title"]);

function decode(s) {
  return s.replace(/&(#x?[0-9a-f]+|amp|lt|gt|quot|apos|nbsp|times);/gi, (m, e) => {
    const named = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", times: "×" };
    if (e[0] === "#") return String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10));
    return named[e.toLowerCase()] ?? m;
  });
}

class Node_ {
  constructor(doc) { this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() { const p = this.parentNode; if (!p) return null; const i = p.childNodes.indexOf(this); return p.childNodes[i + 1] || null; }
  appendChild(n) {
    if (n.nodeType === 11) { for (const c of [...n.childNodes]) this.appendChild(c); return n; }
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this; this.childNodes.push(n); return n;
  }
  append(...ns) { for (const n of ns) this.appendChild(typeof n === "string" ? this.ownerDocument.createTextNode(n) : n); }
  prepend(...ns) { for (const n of ns.reverse()) this.insertBefore(typeof n === "string" ? this.ownerDocument.createTextNode(n) : n, this.firstChild); }
  insertBefore(n, ref) {
    if (!ref) return this.appendChild(n);
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, n); return n;
  }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  replaceChildren(...ns) { for (const c of [...this.childNodes]) this.removeChild(c); this.append(...ns); }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.replaceChildren(); if (v !== "" && v != null) this.appendChild(this.ownerDocument.createTextNode(String(v))); }
  contains(n) { for (; n; n = n.parentNode) if (n === this) return true; return false; }
}

class Text_ extends Node_ {
  constructor(doc, text) { super(doc); this.nodeType = 3; this.data = text; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
  get nodeValue() { return this.data; }
}

class Fragment_ extends Node_ { constructor(doc) { super(doc); this.nodeType = 11; } }

class ClassList {
  constructor(el) { this.el = el; }
  _get() { return (this.el.getAttribute("class") || "").split(/\s+/).filter(Boolean); }
  _set(a) { this.el.setAttribute("class", a.join(" ")); }
  add(...c) { const a = this._get(); for (const x of c) if (!a.includes(x)) a.push(x); this._set(a); }
  remove(...c) { this._set(this._get().filter((x) => !c.includes(x))); }
  contains(c) { return this._get().includes(c); }
  toggle(c, force) { const on = force === undefined ? !this.contains(c) : !!force; on ? this.add(c) : this.remove(c); return on; }
  get length() { return this._get().length; }
}

class Element_ extends Node_ {
  constructor(doc, tag) {
    super(doc); this.nodeType = 1; this.tagName = tag.toUpperCase(); this.localName = tag.toLowerCase();
    this.attributes_ = new Map(); this.listeners = {}; this.style = {}; this.classList = new ClassList(this);
    const self = this;
    this.dataset = new Proxy({}, {
      get: (_, k) => typeof k === "string" ? self.getAttribute("data-" + k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())) ?? undefined : undefined,
      set: (_, k, v) => { self.setAttribute("data-" + String(k).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()), v); return true; },
      has: (_, k) => self.hasAttribute("data-" + String(k)),
    });
  }
  get nodeName() { return this.tagName; }
  getAttribute(k) { return this.attributes_.has(k.toLowerCase()) ? this.attributes_.get(k.toLowerCase()) : null; }
  setAttribute(k, v) { this.attributes_.set(k.toLowerCase(), String(v)); }
  hasAttribute(k) { return this.attributes_.has(k.toLowerCase()); }
  removeAttribute(k) { this.attributes_.delete(k.toLowerCase()); }
  toggleAttribute(k, f) { const on = f === undefined ? !this.hasAttribute(k) : !!f; on ? this.setAttribute(k, "") : this.removeAttribute(k); return on; }
  get id() { return this.getAttribute("id") || ""; } set id(v) { this.setAttribute("id", v); }
  get className() { return this.getAttribute("class") || ""; } set className(v) { this.setAttribute("class", v); }
  get disabled() { return this.hasAttribute("disabled"); } set disabled(v) { v ? this.setAttribute("disabled", "") : this.removeAttribute("disabled"); }
  get value() { return this.getAttribute("value") || ""; } set value(v) { this.setAttribute("value", v); }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  get innerText() { return this.textContent; } set innerText(v) { this.textContent = v; }
  get innerHTML() { return this.childNodes.map(serialize).join(""); }
  set innerHTML(html) { this.replaceChildren(); parseInto(this.ownerDocument, String(html), this); }
  get outerHTML() { return serialize(this); }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) { for (let e = this; e && e.nodeType === 1; e = e.parentNode) if (e.matches(sel)) return e; return null; }
  querySelectorAll(sel) { const out = []; walk(this, (e) => { if (e !== this && e.matches(sel)) out.push(e); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getElementsByTagName(t) { return this.querySelectorAll(t); }
  getElementsByClassName(c) { return this.querySelectorAll("." + c); }
  addEventListener(type, fn, opts) { (this.listeners[type] ||= []).push({ fn, once: !!(opts && opts.once) }); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((l) => l.fn !== fn); }
  dispatchEvent(ev) { return this.ownerDocument._dispatch(this, ev); }
  click() {
    if (this.disabled && /^(BUTTON|INPUT)$/.test(this.tagName)) return; // browsers don't fire click on disabled controls
    this.dispatchEvent(this.ownerDocument._event("click", { bubbles: true }));
  }
  focus() { this.ownerDocument.activeElement = this; }
  blur() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 100, top: 0, left: 0, right: 100, bottom: 100 }; }
  scrollIntoView() {}
}

function walk(root, fn) { for (const c of root.childNodes) if (c.nodeType === 1) { fn(c); walk(c, fn); } }

function serialize(n) {
  if (n.nodeType === 3) return n.data.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  if (n.nodeType !== 1) return n.childNodes.map(serialize).join("");
  const attrs = [...n.attributes_].map(([k, v]) => ` ${k}="${v.replace(/"/g, "&quot;")}"`).join("");
  return VOID.has(n.localName) ? `<${n.localName}${attrs}>` : `<${n.localName}${attrs}>${n.childNodes.map(serialize).join("")}</${n.localName}>`;
}

// ---- selectors: compound (tag#id.class[attr="v"]:not(...)), descendant ' ', child '>', lists ','
function splitTop(sel, ch) {
  const out = []; let depth = 0, cur = "";
  for (const c of sel) {
    if (c === "(" || c === "[") depth++; else if (c === ")" || c === "]") depth--;
    if (c === ch && depth === 0) { out.push(cur); cur = ""; } else cur += c;
  }
  out.push(cur); return out.map((s) => s.trim()).filter(Boolean);
}
function matchCompound(el, comp) {
  let rest = comp;
  const tag = rest.match(/^[a-zA-Z][\w-]*|^\*/);
  if (tag) { if (tag[0] !== "*" && el.localName !== tag[0].toLowerCase()) return false; rest = rest.slice(tag[0].length); }
  while (rest) {
    let m;
    if ((m = rest.match(/^#([\w-]+)/))) { if (el.id !== m[1]) return false; }
    else if ((m = rest.match(/^\.([\w-]+)/))) { if (!el.classList.contains(m[1])) return false; }
    else if ((m = rest.match(/^\[\s*([\w-]+)\s*(?:([~^$*|]?=)\s*["']?([^"'\]]*)["']?)?\s*\]/))) {
      const v = el.getAttribute(m[1]);
      if (v === null) return false;
      if (m[2] === "=" && v !== m[3]) return false;
      if (m[2] === "^=" && !v.startsWith(m[3])) return false;
      if (m[2] === "$=" && !v.endsWith(m[3])) return false;
      if (m[2] === "*=" && !v.includes(m[3])) return false;
      if (m[2] === "~=" && !v.split(/\s+/).includes(m[3])) return false;
    } else if ((m = rest.match(/^:not\((.*?)\)/))) { if (matchesSelector(el, m[1])) return false; }
    else if ((m = rest.match(/^:(first-child|last-child|disabled|enabled|empty)/))) {
      const sib = el.parentNode ? el.parentNode.children : [el];
      if (m[1] === "first-child" && sib[0] !== el) return false;
      if (m[1] === "last-child" && sib[sib.length - 1] !== el) return false;
      if (m[1] === "disabled" && !el.disabled) return false;
      if (m[1] === "enabled" && el.disabled) return false;
      if (m[1] === "empty" && el.childNodes.length) return false;
    } else if ((m = rest.match(/^:nth-child\((\d+)\)/))) {
      const sib = el.parentNode ? el.parentNode.children : [el];
      if (sib.indexOf(el) + 1 !== Number(m[1])) return false;
    } else throw new Error(`dom-shim: unsupported selector part ${JSON.stringify(rest)}`);
    rest = rest.slice(m[0].length);
  }
  return true;
}
function matchesSelector(el, sel) {
  return splitTop(sel, ",").some((one) => {
    const parts = one.replace(/\s*>\s*/g, " > ").split(/\s+/);
    let i = parts.length - 1;
    if (!matchCompound(el, parts[i])) return false;
    let cur = el;
    for (i -= 1; i >= 0; i--) {
      if (parts[i] === ">") { i--; cur = cur.parentNode; if (!cur || cur.nodeType !== 1 || !matchCompound(cur, parts[i])) return false; continue; }
      let p = cur.parentNode;
      while (p && p.nodeType === 1 && !matchCompound(p, parts[i])) p = p.parentNode;
      if (!p || p.nodeType !== 1) return false;
      cur = p;
    }
    return true;
  });
}

// ---- tolerant HTML parser (enough for one hand-written page)
function parseInto(doc, html, root) {
  const stack = [root];
  const top = () => stack[stack.length - 1];
  const re = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s=>\/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/gi;
  let last = 0, m;
  while ((m = re.exec(html))) {
    if (m.index > last) { const t = html.slice(last, m.index); if (t) top().appendChild(doc.createTextNode(decode(t))); }
    last = re.lastIndex;
    if (m[0].startsWith("<!")) continue;
    if (m[1]) { // close tag
      const name = m[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].localName === name) { stack.length = i; break; }
      continue;
    }
    const el = doc.createElement(m[2]);
    for (const a of m[3].matchAll(/([^\s=>\/]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?/g))
      el.setAttribute(a[1], decode(a[3] ?? a[4] ?? a[5] ?? ""));
    top().appendChild(el);
    const name = el.localName;
    if (RAW.has(name)) {
      const end = html.toLowerCase().indexOf(`</${name}`, last);
      const body = html.slice(last, end < 0 ? html.length : end);
      if (body) el.appendChild(doc.createTextNode(name === "title" || name === "textarea" ? decode(body) : body));
      last = end < 0 ? html.length : html.indexOf(">", end) + 1;
      re.lastIndex = last;
    } else if (!VOID.has(name) && !m[4]) stack.push(el);
  }
  if (last < html.length) top().appendChild(doc.createTextNode(decode(html.slice(last))));
}

class Document_ extends Node_ {
  constructor() {
    super(null); this.ownerDocument = this; this.nodeType = 9; this.listeners = {}; this.readyState = "loading";
    this.activeElement = null; this.errors = [];
  }
  createElement(t) { return new Element_(this, t); }
  createTextNode(t) { return new Text_(this, t); }
  createDocumentFragment() { return new Fragment_(this); }
  get documentElement() { return this.childNodes.find((c) => c.nodeType === 1) || null; }
  get body() { return this.querySelector("body"); }
  get head() { return this.querySelector("head"); }
  get title() { const t = this.querySelector("title"); return t ? t.textContent : ""; }
  getElementById(id) { return this.querySelector("#" + id.replace(/([^\w-])/g, "\\$1")); }
  querySelectorAll(sel) { const out = []; walk(this, (e) => { if (e.matches(sel)) out.push(e); }); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  getElementsByTagName(t) { return this.querySelectorAll(t); }
  getElementsByClassName(c) { return this.querySelectorAll("." + c); }
  addEventListener(type, fn, opts) { (this.listeners[type] ||= []).push({ fn, once: !!(opts && opts.once) }); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((l) => l.fn !== fn); }
  _event(type, init = {}) {
    return { type, bubbles: !!init.bubbles, cancelable: true, defaultPrevented: false, target: null, currentTarget: null,
      key: init.key, _stop: false, preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this._stop = true; }, stopImmediatePropagation() { this._stop = true; } };
  }
  _dispatch(target, ev) {
    ev.target = target;
    const path = []; for (let n = target; n; n = n.parentNode) path.push(n);
    if (path[path.length - 1] === this) path.push(this.defaultView);
    for (const node of ev.bubbles ? path : [target]) {
      if (!node) continue;
      ev.currentTarget = node;
      for (const l of [...((node.listeners || {})[ev.type] || [])]) {
        try { l.fn.call(node, ev); } catch (e) { this.errors.push(e); }
        if (l.once) node.removeEventListener(ev.type, l.fn);
      }
      const prop = node["on" + ev.type];
      if (typeof prop === "function") { try { prop.call(node, ev); } catch (e) { this.errors.push(e); } }
      else if (node.getAttribute && node.getAttribute("on" + ev.type)) {
        try { vm.runInContext(`(function(event){${node.getAttribute("on" + ev.type)}})`, this._ctx).call(node, ev); }
        catch (e) { this.errors.push(e); }
      }
      if (ev._stop) break;
    }
    return !ev.defaultPrevented;
  }
}

// Load an HTML file: parse, run inline scripts in order, fire DOMContentLoaded + load.
function load(html) {
  const doc = new Document_();
  const storage = new Map();
  const win = {
    document: doc, console, setTimeout, clearTimeout, setInterval, clearInterval,
    queueMicrotask, structuredClone, listeners: {},
    requestAnimationFrame: (f) => setTimeout(() => f(Date.now()), 0), cancelAnimationFrame: clearTimeout,
    alert() {}, confirm: () => true, prompt: () => null,
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {} }),
    getComputedStyle: () => new Proxy({}, { get: () => "" }),
    localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k), clear: () => storage.clear() },
    navigator: { userAgent: "dom-shim" }, location: { href: "http://localhost/", search: "", hash: "" },
    Event: function Event(type, init) { return doc._event(type, init); },
    CustomEvent: function CustomEvent(type, init) { const e = doc._event(type, init); e.detail = init && init.detail; return e; },
  };
  win.addEventListener = (t, fn, o) => (win.listeners[t] ||= []).push({ fn, once: !!(o && o.once) });
  win.removeEventListener = (t, fn) => { win.listeners[t] = (win.listeners[t] || []).filter((l) => l.fn !== fn); };
  win.window = win; win.self = win; win.globalThis = win;
  doc.defaultView = win;
  const ctx = vm.createContext(win);
  doc._ctx = ctx;
  parseInto(doc, html, doc);
  for (const s of doc.querySelectorAll("script")) {
    if (s.getAttribute("src")) throw new Error("external <script src> is not allowed in a self-contained page");
    const type = (s.getAttribute("type") || "").toLowerCase();
    if (type && !/javascript|module/.test(type)) continue;
    vm.runInContext(s.textContent, ctx, { filename: `inline-script${s.id ? "#" + s.id : ""}` });
  }
  doc.readyState = "interactive";
  doc._dispatch(doc, doc._event("DOMContentLoaded", { bubbles: true }));
  doc.readyState = "complete";
  for (const l of win.listeners.load || []) l.fn.call(win, doc._event("load"));
  if (typeof win.onload === "function") win.onload(doc._event("load"));
  return { document: doc, window: win, context: ctx };
}

module.exports = { load };
