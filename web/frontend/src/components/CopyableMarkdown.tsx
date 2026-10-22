import { useEffect, useRef } from 'react';
import { enhanceCodeBlocks } from '../lib/codeBlockCopy';

/** The caller supplies HTML from the existing sanitization pipeline. */
export default function CopyableMarkdown({ html, completed }: { html: string; completed: boolean }) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    element.innerHTML = html;
    return completed ? enhanceCodeBlocks(element) : undefined;
  }, [html, completed]);
  return <div ref={root} />;
}
