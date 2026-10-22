import type { PublishDraft } from './store';

export const FLOW_URL = 'https://labs.google/fx/tools/flow';

/** Only visible assistant text enters this parser. Unknown text stays editable. */
export function parsePublishReply(text: string, platform: string, sourceId: string): PublishDraft {
  const sections: Record<string, string[]> = {};
  let section = 'unstructured';
  for (const line of text.split(/\r?\n/)) {
    const heading = line.replace(/^\s*(?:#{1,6}\s*|\d+[.、)]\s*)/, '').replace(/\*\*/g, '').trim();
    const match = heading.match(/^(小红书标题|公众号标题|标题|发布正文|正文|发布文案|话题标签|标签|Nano Banana 生图提示词|配图提示词(?:（[^）]*）)?|Flow 配图提示词)\s*(?:[：:]\s*(.*)|$)/i);
    if (match) {
      section = /提示词/i.test(match[1]) ? 'flow' : /标题/.test(match[1]) ? 'title' : /标签/.test(match[1]) ? 'tags' : 'body';
      sections[section] ??= [];
      if (match[2]) sections[section].push(match[2]);
    } else (sections[section] ??= []).push(line);
  }
  const value = (key: string) => (sections[key] || []).join('\n').trim();
  // Do not guess a title from the first sentence or synthesize hashtags.
  const body = sections.body ? value('body') : value('unstructured');
  const media = [...text.matchAll(/(?:\/api\/media\/|outputs\/)([^\s)\]<>"']+\.(?:png|jpe?g|webp|gif|mp4|mov))/gi)]
    .map(m => m[1]).filter(p => !p.split('/').some(part => part.startsWith('_') || part.startsWith('.') || part.includes('\\')));
  return { version: 2, id: `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,8)}`,
    sourceId, originalResponse: text, title: value('title'), body,
    tags: value('tags').split(/[\s,，]+/).map(t => t.replace(/^#/, '')).filter(Boolean).join(','),
    platforms: [platform], overrides: {}, flowPrompt: value('flow'),
    aspectRatio: value('flow').match(/(?:比例|画幅)\s*[：:]?\s*(\d+\s*[:：]\s*\d+)/)?.[1].replace(/\s/g, '').replace('：', ':') || '3:4', media: [...new Set(media)],
  };
}
