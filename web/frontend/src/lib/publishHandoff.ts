import type { PublishDraft } from './store';

export const FLOW_URL = 'https://labs.google/fx/tools/flow';
type PlatformScope = 'global' | 'xiaohongshu' | 'wechat-oa';
type Section = 'unstructured' | 'title' | 'body' | 'tags' | 'flow' | 'unknownPrompt';

function cleanHeading(line: string): string {
  let value = line.trim();
  for (let i = 0; i < 4; i += 1) {
    const previous = value;
    value = value.replace(/^#{1,6}\s*/, '').replace(/^\d+[.、)]\s*/, '').trim();
    if (value.startsWith('**') && value.endsWith('**')) value = value.slice(2, -2).trim();
    value = value.replace(/\*\*/g, '').trim();
    if (value === previous) break;
  }
  return value;
}

function scopeFor(label: string): PlatformScope | null {
  if (/^小红书(?:版|版本)?$/i.test(label)) return 'xiaohongshu';
  if (/^(?:微信公众号|公众号)(?:版|版本)?$/i.test(label)) return 'wechat-oa';
  return null;
}

/** Decode exactly once, normalize Windows separators and keep all existing private-path guards. */
export function normalizeLocalMediaReference(input: string): string | null {
  let target = input.trim().replace(/^<|>$/g, '');
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(target) || target.startsWith('//') || target.startsWith('#')) return null;
  try { target = decodeURIComponent(target); } catch { return null; }
  target = target.replace(/\\/g, '/').split(/[?#]/, 1)[0];
  let relative = '';
  if (target.startsWith('/api/media/')) relative = target.slice('/api/media/'.length);
  else if (target.startsWith('outputs/')) relative = target.slice('outputs/'.length);
  else {
    const marker = target.toLowerCase().lastIndexOf('/outputs/');
    if (marker >= 0) relative = target.slice(marker + '/outputs/'.length);
  }
  if (!relative || !/\.(?:png|jpe?g|webp|gif|bmp|avif|mp4|mov|webm)$/i.test(relative)) return null;
  const parts = relative.split('/');
  if (parts.some(part => !part || part === '.' || part === '..' || part.startsWith('.') || part.startsWith('_') || /\p{Cc}/u.test(part))) return null;
  return parts.join('/');
}

function mediaReferences(text: string): string[] {
  const candidates: string[] = [];
  for (const match of text.matchAll(/!?\[[^\]]*\]\((?:<([^>]+)>|([^)\n]+))\)/g)) candidates.push(match[1] || match[2]);
  for (const match of text.matchAll(/(?:\/api\/media\/|outputs[\\/])[^\s)\]<>"']+\.(?:png|jpe?g|webp|gif|bmp|avif|mp4|mov|webm)/gi)) candidates.push(match[0]);
  return [...new Set(candidates.map(normalizeLocalMediaReference).filter((path): path is string => Boolean(path)))];
}

/** Only visible assistant text enters this conservative parser. */
export function parsePublishReply(text: string, platform: string, sourceId: string): PublishDraft {
  const emptySections = (): Record<Section, string[]> => ({ unstructured: [], title: [], body: [], tags: [], flow: [], unknownPrompt: [] });
  const buckets: Record<PlatformScope, Record<Section, string[]>> = {
    global: emptySections(), xiaohongshu: emptySections(), 'wechat-oa': emptySections(),
  };
  const warnings: string[] = [];
  const seenScopes = new Set<PlatformScope>();
  let scope: PlatformScope = 'global';
  let section: Section = 'unstructured';
  for (const line of text.split(/\r?\n/)) {
    const heading = cleanHeading(line);
    const platformHeading = scopeFor(heading);
    if (platformHeading) {
      scope = platformHeading; section = 'unstructured'; seenScopes.add(scope); continue;
    }
    const match = heading.match(/^(?:(小红书|微信公众号|公众号)\s*)?(标题|发布正文|正文|发布文案|话题标签|标签|(?:Nano Banana(?:\s*\/\s*Google Flow)?|Google Flow(?:\s*\/\s*Nano Banana)?|Google Flow|Flow)\s*(?:生图|配图)?提示词|配图提示词(?:（(?:[^）]*)）)?)\s*(?:[：:]\s*(.*)|$)/i);
    const visiblyAHeading = /^(?:#{1,6}\s*|\d+[.、)]\s*|\*\*)/.test(line.trim())
      || /[：:]/.test(heading)
      || /^(?:标题|发布正文|正文|发布文案|话题标签|标签)$/.test(heading);
    if (match && visiblyAHeading) {
      const targetScope = (match[1] ? scopeFor(match[1]) : null) || scope;
      seenScopes.add(targetScope);
      const label = match[2];
      section = /提示词/i.test(label) ? 'flow' : /标题/.test(label) ? 'title' : /标签/.test(label) ? 'tags' : 'body';
      scope = targetScope;
      if (match[3]) buckets[scope][section].push(match[3]);
      continue;
    }
    if (/提示词/.test(heading) && /^(?:#{1,6}|\s*\d+[.、)]|\*\*)/.test(line.trim())) {
      section = 'unknownPrompt';
      buckets[scope][section].push(line);
      warnings.push(`未能识别提示词标题“${heading}”，已从发布正文排除，请核对原文。`);
      continue;
    }
    buckets[scope][section].push(line);
  }
  const desired: PlatformScope = platform === 'wechat-oa' ? 'wechat-oa' : 'xiaohongshu';
  const selected = seenScopes.has(desired) ? buckets[desired] : buckets.global;
  if ((seenScopes.has('xiaohongshu') || seenScopes.has('wechat-oa')) && !seenScopes.has(desired)) {
    warnings.push('回复包含明确的平台区块，但没有当前平台的内容；字段已留空，请选择平台并核对。');
  }
  const value = (key: Section) => (selected[key].length ? selected[key] : buckets.global[key]).join('\n').trim();
  const body = selected.body.length || buckets.global.body.length ? value('body') : value('unstructured');
  const flowPrompt = value('flow');
  return {
    version: 2, id: `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
    sourceId, originalResponse: text, title: value('title'), body,
    tags: value('tags').split(/[\s,，]+/).map(tag => tag.replace(/^#/, '')).filter(Boolean).join(','),
    platforms: [platform], overrides: {}, flowPrompt,
    aspectRatio: flowPrompt.match(/(?:比例|画幅)\s*[：:]?\s*(\d+\s*[:：]\s*\d+)/)?.[1].replace(/\s/g, '').replace('：', ':') || '3:4',
    media: mediaReferences(text), parseWarnings: [...new Set(warnings)],
  };
}
