import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const settings = readFileSync('web/frontend/src/components/SettingsPanel.tsx', 'utf8');
const store = readFileSync('web/frontend/src/lib/store.ts', 'utf8');
const api = readFileSync('web/frontend/src/lib/api.ts', 'utf8');
const questions = readFileSync('web/frontend/src/components/QuestionCards.tsx', 'utf8');

test('settings exposes Native Codex as an explicit opt-in with OpenClaw default', () => {
  assert.match(settings, /Codex \(Native\)/);
  assert.match(settings, /OpenClaw.*默认/s);
  assert.match(settings, /不会自动安装或登录/);
  assert.match(api, /fetchHarnessSettings/);
  assert.match(api, /saveHarnessSettings/);
});

test('new chat sessions latch runtime while legacy sessions remain OpenClaw', () => {
  assert.match(store, /runtime\?: 'openclaw' \| 'codex'/);
  assert.match(store, /runtime: loadPreferredRuntime\(\)/);
  assert.match(store, /session\.runtime \?\? 'openclaw'/);
  assert.match(questions, /answerQuestion\(\{ sessionId, questionId:/);
});
