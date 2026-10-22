import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';

const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/lib/publishOperation.ts', import.meta.url), 'utf8');
const exports = {};
runInNewContext(ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports });

const deferred = () => {
  let resolve;
  return { promise: new Promise(done => { resolve = done; }), resolve };
};

test('a draft switch while preflight is pending causes zero confirmation and zero submission', async () => {
  const preflight = deferred();
  let current = true, confirmations = 0, submissions = 0;
  const pending = exports.runGuardedPublish({
    isCurrent: () => current,
    precheck: () => preflight.promise,
    confirm: () => { confirmations += 1; return true; },
    submit: async () => { submissions += 1; },
  });
  current = false;
  preflight.resolve('checked');
  assert.equal(await pending, 'stale');
  assert.equal(confirmations, 0);
  assert.equal(submissions, 0);
});

test('an edit made during confirmation invalidates the operation before submission', async () => {
  let current = true, submissions = 0;
  const result = await exports.runGuardedPublish({
    isCurrent: () => current,
    precheck: async () => 'checked',
    confirm: () => { current = false; return true; },
    submit: async () => { submissions += 1; },
  });
  assert.equal(result, 'stale');
  assert.equal(submissions, 0);
});

test('confirmation identifies the exact draft and title', () => {
  const message = exports.publishConfirmationMessage({ draftId: 'draft-a', title: '春日茶园' }, ['小红书']);
  assert.match(message, /春日茶园/);
  assert.match(message, /draft-a/);
  assert.match(message, /小红书/);
});
