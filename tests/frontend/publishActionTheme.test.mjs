import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const base = new URL('../../web/frontend/src/', import.meta.url);
const theme = readFileSync(new URL('styles/theme.css', base), 'utf8');
const css = readFileSync(new URL('index.css', base), 'utf8');

// Specificity check for these class/attribute/pseudo-class-only selectors, not
// a browser/computed-style substitute. :is() contributes its strongest argument.
function weight(selector) {
  assert.doesNotMatch(selector, /[#>+~]|::/);
  const alternatives = [...selector.matchAll(/:is\(([^()]*)\)/g)];
  const remainder = selector.replace(/:is\([^()]*\)/g, '');
  return (remainder.match(/\.[\w-]+|\[[^\]]+\]|:[\w-]+/g) || []).length
    + alternatives.reduce((sum, match) => sum + Math.max(...match[1].split(',').map(weight)), 0);
}

test('dark publishing primary color outranks the imported theme even without hover or focus', () => {
  const themeRule = theme.match(/(:root\[data-theme='dark'\]\s+:is\(\.btn-primary,[^)]*\))\s*\{([^}]+)\}/);
  assert.ok(themeRule, 'Check the current theme competitor, including the strongest :is() argument');
  assert.match(themeRule[2], /color:\s*var\(--bg\)/);
  assert.equal(weight(themeRule[1]), 4);
  const override = css.match(/(:root\[data-theme='dark'\]\s+\.publish-draft-actions\s+[^,{]+)[,\s]*\{([^}]+)\}/);
  assert.ok(override, 'Missing unconditional dark publishing override: theme (0,4,0) beats default (0,2,0)');
  assert.match(override[1], /\.btn-primary/);
  assert.doesNotMatch(override[1], /:hover|:focus|:active/,
    'The same override must apply to default, hover and keyboard-only focus');
  assert.ok(weight(override[1]) > weight(themeRule[1]), 'Override must beat the actual theme specificity');
  assert.match(override[2], /color:\s*#fff\s*;/);
  assert.match(override[2], /background:\s*#2855b7\s*;/);
});
