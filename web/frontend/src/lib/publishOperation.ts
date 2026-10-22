import type { PublishDraft } from './store';

export interface PublishSnapshot {
  draftId: string;
  title: string;
  body: string;
  tags: string;
  media: string[];
  platforms: string[];
  overrides: Record<string, string>;
  persona: string;
}

export function capturePublishSnapshot(draft: PublishDraft, persona: string): PublishSnapshot {
  return {
    draftId: draft.id || '', title: draft.title, body: draft.body, tags: draft.tags,
    media: [...(draft.media || [])], platforms: [...draft.platforms],
    overrides: { ...draft.overrides }, persona,
  };
}

export function publishSnapshotKey(snapshot: PublishSnapshot): string {
  return JSON.stringify(snapshot);
}

export function publishConfirmationMessage(snapshot: Pick<PublishSnapshot, 'draftId' | 'title'>, platforms: string[]): string {
  return `发布前预检已执行，结果已显示在页面中。人设评分只做提醒，不会阻止发布。\n\n`
    + `当前草稿：${snapshot.title.trim() || '未命名草稿'}（${snapshot.draftId || '无标识'}）\n`
    + `即将【真实发布】到：${platforms.join('、')}。\n`
    + '这会公开发布到你的账号，确定继续？';
}

export async function runGuardedPublish(options: {
  isCurrent: () => boolean;
  precheck: () => Promise<unknown>;
  confirm: () => boolean;
  submit: (isCurrent: () => boolean) => Promise<void>;
}): Promise<'stale' | 'cancelled' | 'submitted'> {
  if (!options.isCurrent()) return 'stale';
  await options.precheck();
  if (!options.isCurrent()) return 'stale';
  if (!options.confirm()) return 'cancelled';
  if (!options.isCurrent()) return 'stale';
  await options.submit(options.isCurrent);
  return options.isCurrent() ? 'submitted' : 'stale';
}
