import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
function load(file, initial = {}) {
  const source = readFileSync(new URL(`../../web/frontend/src/lib/${file}.ts`, import.meta.url), 'utf8');
  const entries = new Map(Object.entries(initial));
  const localStorage = { getItem: k => entries.get(k) ?? null, setItem: (k,v) => entries.set(k,v), removeItem: k => entries.delete(k) };
  const exports = {};
  runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, localStorage, Date, Math });
  return { ...exports, entries };
}
test('visible structured response separates Flow prompt and retains original without inventing fields', () => {
  const h = load('publishHandoff');
  const raw = '## 标题\n春日茶园\n## 发布正文\n在茶园散步。\n\n享受春天。\n## 话题标签\n#春天 #茶园\n## Nano Banana 生图提示词\n茶园主体，晨光，绿色，竖版留白。';
  const d = h.parsePublishReply(raw, 'xiaohongshu', 's:1');
  assert.equal(d.title, '春日茶园');
  assert.equal(d.body, '在茶园散步。\n\n享受春天。');
  assert.equal(d.tags, '春天,茶园');
  assert.equal(d.flowPrompt, '茶园主体，晨光，绿色，竖版留白。');
  assert.equal(d.originalResponse, raw);
  assert.equal(d.aspectRatio, '3:4');
  assert.equal(h.parsePublishReply('普通回复', 'wechat-oa', 's:2').title, '');
  assert.equal(h.parsePublishReply('普通回复', 'wechat-oa', 's:2').tags, '');
});
test('numbered Markdown and the actual Nano Banana / Google Flow heading stay out of body', () => {
  const h = load('publishHandoff');
  const raw = '## 1. 标题\n技术茶园\n## 2. 正文\n只保留正文。\n## 3. 话题标签\n#技术\n## 4. Nano Banana / Google Flow 生图提示词\n主体、构图与留白，比例 16:9。';
  const d = h.parsePublishReply(raw, 'xiaohongshu', 's:3');
  assert.equal(d.title, '技术茶园');
  assert.equal(d.body, '只保留正文。');
  assert.equal(d.tags, '技术');
  assert.equal(d.flowPrompt, '主体、构图与留白，比例 16:9。');
  assert.equal(d.aspectRatio, '16:9');
});

test('plain Flow heading is isolated from publish body', () => {
  const h = load('publishHandoff');
  const raw = '标题\n茶园\n正文\n只有正文。\nNano Banana 生图提示词\n不要进入正文的生图说明。';
  const d = h.parsePublishReply(raw, 'xiaohongshu', 's:plain-flow');
  assert.equal(d.title, '茶园');
  assert.equal(d.body, '只有正文。');
  assert.equal(d.flowPrompt, '不要进入正文的生图说明。');
  assert.deepEqual(Array.from(d.parseWarnings), []);
});

test('multi-platform response selects only the requested platform block', () => {
  const h = load('publishHandoff');
  const raw = '## 小红书\n### 标题\n小红书标题\n### 正文\n小红书正文\n## 公众号\n### 标题\n公众号标题\n### 正文\n公众号正文';
  const xhs = h.parsePublishReply(raw, 'xiaohongshu', 's:4');
  const wechat = h.parsePublishReply(raw, 'wechat-oa', 's:4');
  assert.equal(xhs.title, '小红书标题');
  assert.equal(xhs.body, '小红书正文');
  assert.equal(wechat.title, '公众号标题');
  assert.equal(wechat.body, '公众号正文');
});

test('an explicit block for another platform is left empty with a warning', () => {
  const h = load('publishHandoff');
  const d = h.parsePublishReply('## 小红书\n### 标题\n仅小红书\n### 正文\n不要串到公众号', 'wechat-oa', 's:4b');
  assert.equal(d.title, '');
  assert.equal(d.body, '');
  assert.ok(d.parseWarnings.some(w => w.includes('没有当前平台')));
});

test('unknown prompt block is flagged and never silently appended to publish body', () => {
  const h = load('publishHandoff');
  const d = h.parsePublishReply('## 正文\n安全正文\n## Midjourney 提示词\n不要进入正文', 'xiaohongshu', 's:5');
  assert.equal(d.body, '安全正文');
  assert.ok(d.parseWarnings.some(w => w.includes('提示词')));
});

test('local media references decode once and remain protected', () => {
  const h = load('publishHandoff');
  const d = h.parsePublishReply('![封面](outputs/茶园/封面%201.png)\n![Win](C:\\Easel\\outputs\\茶园\\封面%202.png)\n![拒绝](outputs/../secret.png)\n![私有](outputs/_inbox/a.png)', 'xiaohongshu', 's:6');
  assert.deepEqual(Array.from(d.media), ['茶园/封面 1.png', '茶园/封面 2.png']);
});

test('media follows the selected platform block and retains only global shared media', () => {
  const h = load('publishHandoff');
  const raw = '![共用](outputs/shared/logo.png)\n## 小红书\n### 正文\n小红书正文\n![小红书图](outputs/xhs/cover.png)\n## 公众号\n### 正文\n公众号正文\n![公众号图](outputs/wechat/cover.png)';
  const d = h.parsePublishReply(raw, 'wechat-oa', 's:scoped-media');
  assert.deepEqual(Array.from(d.media), ['shared/logo.png', 'wechat/cover.png']);
});

test('external URLs containing outputs are never rescanned as local media', () => {
  const h = load('publishHandoff');
  const d = h.parsePublishReply('![外部](https://example.com/outputs/private-project/cover.png)', 'xiaohongshu', 's:external-media');
  assert.deepEqual(Array.from(d.media), []);
});
test('legacy draft migrates; cancel is nonmutating; save-new preserves previous; stale writes rejected', () => {
  const s = load('store', { easel_publish_draft: JSON.stringify({ title:'旧稿', body:'原文', tags:'茶', platforms:['wechat-oa'] }) });
  const old = s.loadPublishDraft();
  const candidate = { ...old, id:'new', title:'新稿', sourceId:'s:1', flowPrompt:'茶园', media:['draft-media/a/cover.png'] };
  const before = s.entries.get('easel_publish_draft');
  s.importPublishDraft(candidate, 'cancel');
  assert.equal(s.entries.get('easel_publish_draft'), before);
  s.importPublishDraft(candidate, 'new');
  assert.equal(s.loadPublishDraft().title, '新稿');
  assert.equal(s.listPublishDrafts().find(d => d.id === old.id).body, '原文');
  assert.equal(s.savePublishDraft({...old, title:'迟到的异步写入'}), false);
  assert.equal(s.loadPublishDraft().title, '新稿');
  assert.equal(s.loadPublishDraft().media[0], 'draft-media/a/cover.png');
  assert.equal(s.loadPublishDraft().flowPrompt, '茶园');
  s.selectPublishDraft(old.id);
  assert.equal(s.loadPublishDraft().body, '原文');
});
