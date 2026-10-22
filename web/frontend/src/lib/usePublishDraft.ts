import { useRef, useState } from 'react';
import { loadPublishDraft, savePublishDraft } from './store';
import type { PublishDraft } from './store';

export function usePublishDraft() {
  const [draft, setDraft] = useState(loadPublishDraft);
  const current = useRef(draft);
  const [saveError, setSaveError] = useState('');
  const field = <K extends keyof PublishDraft>(key: K) => (value: PublishDraft[K] | ((old: PublishDraft[K]) => PublishDraft[K])) => {
    const next = { ...current.current, [key]: typeof value === 'function' ? value(current.current[key]) : value };
    try {
      if (!savePublishDraft(next)) { setSaveError('当前草稿已在其他页面切换，请重新打开发布中心。'); return; }
      current.current = next;
      setDraft(next);
      setSaveError('');
    } catch { setSaveError('保存失败，请检查浏览器存储空间。当前编辑仍保留在页面中。'); current.current = next; setDraft(next); }
  };
  return { draft, field, saveError };
}
