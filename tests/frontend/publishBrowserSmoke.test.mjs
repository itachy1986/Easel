import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = readFileSync(new URL('../publish_browser_smoke.js', import.meta.url), 'utf8');

test('browser smoke has mutually exclusive stages and failure is terminal', () => {
  assert.match(source, /switch\s*\(stage\)/);
  for (const stage of ['initial', 'refresh-manual', 'automated', 'done', 'failed']) {
    assert.match(source, new RegExp(`case ['"]${stage}['"]`));
  }
  assert.match(source, /sessionStorage\.setItem\(['"]smoke-stage['"],\s*['"]failed['"]\)/);
  const failureHandler = source.slice(source.lastIndexOf('} catch (error)'));
  assert.match(failureHandler, /await report\(['"]failure['"]/);
  assert.match(failureHandler, /sessionStorage\.setItem\(['"]smoke-stage['"],\s*['"]failed['"]\)/);
  assert.match(failureHandler, /return;/);
});

test('browser smoke uses real navigation and verifies automatic mode before pass', () => {
  assert.match(source, /button\(['"]去发布['"]\)/);
  assert.match(source, /fixture\/policy\/automated/);
  assert.match(source, /policyResponse\.ok/);
  assert.match(source, /button\(['"]自动发布['"]\)/);
  assert.match(source, /aria-pressed/);
  assert.match(source, /fixture\/result/);
});

test('copy verification waits for a human browser click instead of scripting the copy button', () => {
  assert.doesNotMatch(source, /button\(['"]复制 Flow 提示词['"]\)\)\.click\(\)/);
  assert.match(source, /请人工点击.*复制 Flow 提示词/);
  assert.match(source, /提示词已复制/);
});
