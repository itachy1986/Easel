import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const entry = process.argv[2];
if (!entry) throw new Error("usage: node harness.mjs /path/to/openclaw.mjs");

const root = path.dirname(path.resolve(entry));
const pkg = JSON.parse(await readFile(path.join(root, "package.json"), "utf8"));
if (pkg.version !== "2026.9.7") {
  process.stdout.write(JSON.stringify({ skipped: true, version: pkg.version }));
  process.exit(77);
}

const dist = path.join(root, "dist");
const names = await readdir(dist);
const one = async (prefix, marker) => {
  const matches = names.filter((name) => name.startsWith(prefix) && name.endsWith(".mjs"));
  const selected = [];
  for (const name of matches) {
    const candidate = path.join(dist, name);
    if (!marker || (await readFile(candidate, "utf8")).includes(marker)) selected.push(candidate);
  }
  assert.equal(selected.length, 1, `expected one ${prefix} module with ${marker}, got ${selected.length}`);
  return selected[0];
};
const load = async (prefix, marker) => import(pathToFileURL(await one(prefix, marker)).href);

const profileModule = await load("model-ref-profile-", "function splitTrailingAuthProfile");
const normalizeModule = await load("model-selection-normalize-", "function parseModelRef");
const visibilityModule = await load("model-visibility-policy-", "function createModelVisibilityPolicy");
const scopeModule = await load("agent-scope-", "function resolveEffectiveModelFallbacks");
const splitTrailingAuthProfile = profileModule.t;
const parseModelRef = normalizeModule.n;
const createModelVisibilityPolicy = visibilityModule.n;
const resolveEffectiveModelFallbacks = scopeModule.p;

assert.deepEqual(
  splitTrailingAuthProfile("openai/gpt-6-astra@openai:work"),
  { model: "openai/gpt-6-astra", profile: "openai:work" },
);
const gatewayParsed = parseModelRef(
  "openai/gpt-6-astra@openai:work",
  "openai",
  { allowManifestNormalization: false, allowPluginNormalization: false },
);
assert.deepEqual(gatewayParsed, {
  provider: "openai",
  model: "gpt-6-astra@openai:work",
});

const catalog = [
  { provider: "openai", id: "gpt-6-astra", name: "GPT-6 Astra" },
  { provider: "openai", id: "gpt-fallback", name: "GPT fallback" },
];
const exactCfg = {
  agents: {
    defaults: {
      model: {
        primary: "openai/gpt-6-astra",
        fallbacks: ["openai/gpt-fallback"],
      },
      modelPolicy: { allow: ["openai/gpt-6-astra"] },
      models: { "openai/gpt-6-astra": {} },
    },
  },
};
const exactPolicy = createModelVisibilityPolicy({
  cfg: exactCfg,
  catalog,
  defaultProvider: "openai",
  agentId: "main",
});
assert.equal(exactPolicy.allows({ provider: "openai", model: "gpt-6-astra" }), true);
assert.equal(exactPolicy.allows(gatewayParsed), false);

const unrestrictedCfg = {
  agents: { defaults: { model: { primary: "openai/gpt-6-astra" } } },
};
const unrestrictedPolicy = createModelVisibilityPolicy({
  cfg: unrestrictedCfg,
  catalog,
  defaultProvider: "openai",
  agentId: "main",
});
assert.equal(unrestrictedPolicy.allows(gatewayParsed), true);

assert.deepEqual(
  resolveEffectiveModelFallbacks({
    cfg: exactCfg,
    agentId: "main",
    hasSessionModelOverride: true,
    modelOverrideSource: "user",
    hasAutoFallbackProvenance: false,
  }),
  [],
);

const agentExecSource = await readFile(await one("agent-exec-", "function agentExecCommand"), "utf8");
assert.match(agentExecSource, /modelFallbacksOverride:\s*fallbacks\.length/);
assert.doesNotMatch(agentExecSource, /authProfileId/);
assert.doesNotMatch(agentExecSource, /credentialSource/);

const agentCommandSource = await readFile(await one("agent-command-", "function resolveEmbeddedModelSelection"), "utf8");
assert.match(
  agentCommandSource,
  /configuredDefaultAuthProfileId = splitTrailingAuthProfile\(resolveNativeModelPrimary\(params\.cfg, params\.sessionAgentId\) \?\? ""\)\.profile/,
);
assert.match(
  agentCommandSource,
  /providerOverride === defaultProvider && modelOverride === defaultModel \? configuredDefaultAuthProfileId/,
);
assert.match(agentCommandSource, /configuredAuthProfileId,/);
assert.match(
  agentCommandSource,
  /hasExplicitRunOverride = Boolean\(explicitProviderOverride \|\| explicitModelOverride\)/,
);
assert.match(
  agentCommandSource,
  /modelOverrideSource: hasExplicitRunOverride \? "user" : storedModelOverrideSource/,
);

const attemptSource = await readFile(await one("attempt-execution.runtime-", "function runAgentAttempt"), "utf8");
assert.match(
  attemptSource,
  /params\.configuredAuthProfileId\?\.trim\(\) \? \{[\s\S]*?source: "user"/,
);
assert.match(
  attemptSource,
  /sessionAuthProfileId: selectedAuthProfile\?\.id/,
);
assert.match(
  attemptSource,
  /: runtimeAuthPlan\.forwardedAuthProfileId;/,
);
assert.match(attemptSource, /authProfileId,\s*\n\s*authProfileIdSource:/);

const activationSource = await readFile(await one("auth-activate-", "function modelsAuthActivateCommand"), "utf8");
assert.match(activationSource, /activateSetupInference\(\{/);
assert.match(activationSource, /activationConfirmed: true/);
const setupSelectionSource = await readFile(
  await one("setup-model-selection-", "function createSystemAgentModelSelectionUpdater"),
  "utf8",
);
assert.match(
  setupSelectionSource,
  /params\.authProfileId \? `\$\{key\}@\$\{params\.authProfileId\}` : key/,
);
assert.match(
  setupSelectionSource,
  /if \(params\.profileId\) delete selectedProvider\.apiKey/,
);
const configRunnerSource = await readFile(
  await one("config-cli-runner-", "function assertConfigSetCurrentExpectation"),
  "utf8",
);
assert.match(configRunnerSource, /conditional config set expectation did not match/);
assert.match(configRunnerSource, /baseHash: snapshot\.hash/);
assert.match(configRunnerSource, /setAtPath\(next, operation\.setPath, operation\.value/);

const httpSource = await readFile(await one("http-utils-", "function resolveOpenAiCompatModelOverride"), "utf8");
assert.match(httpSource, /const parsed = parseModelRef\(raw,/);
assert.match(httpSource, /if \(!policy\.allows\(parsed\)\)/);
assert.match(httpSource, /return \{ modelOverride: raw \}/);

process.stdout.write(JSON.stringify({
  ok: true,
  version: pkg.version,
  exactAllowlistRejectsPinnedHeader: true,
  unrestrictedAcceptsPinnedHeader: true,
  explicitModelDisablesConfiguredFallbacks: true,
  persistedMainModelSuppliesCredentialPin: true,
  explicitModelOverridesSessionSelection: true,
  agentExecEnvelopeHasCredentialIdentity: false,
  authActivateDeletesProviderApiKeyWhenProfileSelected: true,
  conditionalConfigSetUsesRevisionGuardedSinglePathMutation: true,
}));
