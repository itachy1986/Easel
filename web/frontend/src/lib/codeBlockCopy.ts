/** Enhance already-sanitized Markdown; never reparse reply text or rewrite code. */
export function enhanceCodeBlocks(root: HTMLElement): () => void {
  const cleanups: (() => void)[] = [];
  root.querySelectorAll<HTMLElement>('pre > code').forEach((code, index) => {
    const pre = code.parentElement!;
    const doc = root.ownerDocument;
    const wrapper = doc.createElement('div');
    wrapper.className = 'copyable-code-block';
    const toolbar = doc.createElement('div');
    toolbar.className = 'code-block-actions';
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm';
    button.textContent = '复制此块';
    button.setAttribute('aria-label', `复制第 ${index + 1} 个文本块`);
    const status = doc.createElement('span');
    status.setAttribute('role', 'status');
    const manual = doc.createElement('button');
    manual.type = 'button';
    manual.className = 'btn btn-sm';
    manual.textContent = '选中此块，手动复制';
    manual.hidden = true;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const copy = async () => {
      clearTimeout(timer);
      status.textContent = '';
      manual.hidden = true;
      button.disabled = true;
      try {
        // textContent preserves newlines/quotes/long lines, without toolbar or fences.
        await navigator.clipboard.writeText(code.textContent || '');
        if (!active) return;
        status.textContent = '已复制';
        timer = setTimeout(() => { status.textContent = ''; }, 2000);
      } catch {
        if (!active) return;
        status.textContent = '复制失败，请选中此块后按 Ctrl+C（Mac：⌘C）。';
        manual.hidden = false;
      } finally {
        if (active) button.disabled = false;
      }
    };
    const select = () => {
      const selection = doc.getSelection();
      const range = doc.createRange();
      range.selectNodeContents(code);
      selection?.removeAllRanges();
      selection?.addRange(range);
    };
    button.addEventListener('click', copy);
    manual.addEventListener('click', select);
    toolbar.append(button, manual, status);
    pre.before(wrapper);
    wrapper.append(toolbar, pre);
    cleanups.push(() => {
      active = false;
      clearTimeout(timer);
      button.removeEventListener('click', copy);
      manual.removeEventListener('click', select);
      wrapper.replaceWith(pre);
    });
  });
  return () => cleanups.forEach(cleanup => cleanup());
}
