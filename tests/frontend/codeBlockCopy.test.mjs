import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/lib/codeBlockCopy.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;

// Minimal DOM adapter for the enhancement's event lifecycle, not browser evidence.
class Element {
  constructor(tag, doc) { this.tag = tag; this.ownerDocument = doc; this.children = []; this.events = {}; this.attributes = {}; this.textContent = ''; }
  append(...children) { for (const child of children) { child.remove(); child.parentElement = this; this.children.push(child); } }
  remove() { if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); this.parentElement = null; }
  before(node) { node.remove(); const parent = this.parentElement; parent.children.splice(parent.children.indexOf(this), 0, node); node.parentElement = parent; }
  replaceWith(node) { node.remove(); const parent = this.parentElement; parent.children.splice(parent.children.indexOf(this), 1, node); node.parentElement = parent; this.parentElement = null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  removeEventListener(name, fn) { if (this.events[name] === fn) delete this.events[name]; }
  querySelectorAll() { return this.children.flatMap(child => child.tag === 'pre' ? child.children.filter(node => node.tag === 'code') : child.querySelectorAll()); }
}
function setup(writeText) {
  let selected;
  const doc = { createElement: tag => new Element(tag, doc), getSelection: () => ({ removeAllRanges() {}, addRange: range => { selected = range.node; } }), createRange: () => ({ selectNodeContents(node) { this.node = node; } }) };
  const root = doc.createElement('div');
  const texts = ['茶园「第一张」："晨光"，' + '长行'.repeat(200) + '\n第二行，保留标点。\n', '配图二：只复制这一块。\n夜景 & <灯>\n'];
  const blocks = texts.map(text => { const pre = doc.createElement('pre'); const node = doc.createElement('code'); node.textContent = text; pre.append(node); root.append(pre); return node; });
  const exports = {};
  runInNewContext(code, { exports, navigator: { clipboard: { writeText } }, setTimeout: () => 1, clearTimeout() {} });
  const cleanup = exports.enhanceCodeBlocks(root);
  const controls = () => root.children.map(wrapper => wrapper.children[0].children);
  return { root, blocks, texts, controls, cleanup, selected: () => selected, enhance: () => exports.enhanceCodeBlocks(root) };
}
test('each block copies only its complete text including Unicode, quotes, newlines and long lines', async () => {
  const writes = []; const h = setup(async text => writes.push(text));
  for (let i = 0; i < 2; i++) {
    const [button, , status] = h.controls()[i];
    assert.equal(button.attributes['aria-label'], `复制第 ${i + 1} 个文本块`);
    await button.events.click(); assert.equal(status.textContent, '已复制');
  }
  assert.deepEqual(writes, h.texts);
  assert.equal(h.blocks[0].textContent, h.texts[0]);
});
test('success is shown only after clipboard resolves; rejection exposes block-only manual selection', async () => {
  let resolve, reject;
  const h = setup(() => new Promise((yes, no) => { resolve = yes; reject = no; }));
  const [button, manual, status] = h.controls()[0];
  let pending = button.events.click();
  assert.equal(status.textContent, ''); assert.equal(button.disabled, true);
  resolve(); await pending; assert.equal(status.textContent, '已复制');
  pending = button.events.click(); reject(new Error('clipboard denied')); await pending;
  assert.match(status.textContent, /复制失败/); assert.doesNotMatch(status.textContent, /已复制/);
  assert.equal(manual.hidden, false); manual.events.click(); assert.equal(h.selected(), h.blocks[0]);
  assert.equal(button.disabled, false);
});
test('cleanup restores code and pending clipboard cannot update a removed toolbar', async () => {
  let resolve; const h = setup(() => new Promise(yes => { resolve = yes; }));
  const [button, , status] = h.controls()[0]; const pending = button.events.click();
  h.cleanup(); resolve(); await pending;
  assert.equal(status.textContent, ''); assert.equal(button.events.click, undefined);
  assert.equal(h.root.children[0].tag, 'pre'); assert.equal(h.blocks[0].textContent, h.texts[0]);
  const cleanup = h.enhance(); assert.equal(h.controls().length, 2); cleanup();
});
test('publishing controls use scoped visible styles and keep primary contrast on hover', () => {
  const base = new URL('../../web/frontend/src/', import.meta.url);
  for (const name of ['PublishHandoff', 'FlowDraftPanel']) {
    const text = readFileSync(new URL(`components/${name}.tsx`, base), 'utf8');
    assert.match(text, /className="publish-draft-actions"/); assert.doesNotMatch(text, /className="msg-actions"/);
  }
  const css = readFileSync(new URL('index.css', base), 'utf8');
  assert.match(css, /\.publish-draft-actions, \.code-block-actions\s*\{[^}]*opacity: 1/s);
  assert.match(css, /\.publish-draft-actions \.btn-primary:hover:not\(:disabled\)\s*\{[^}]*background: #2855b7;[^}]*color: #fff/s);
});
