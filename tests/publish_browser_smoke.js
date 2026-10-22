/* Runs only in the explicitly opened isolated fixture, never in product builds. */
(async () => {
  const report = async (step, ok, detail = '') => {
    const response = await fetch('/evidence', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({step,ok,detail})});
    if (!response.ok) throw new Error(`Evidence write failed: ${response.status}`);
  };
  const wait = async (fn, attempts = 150) => {
    for (let i = 0; i < attempts; i += 1) {
      const value = fn();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Timed out waiting for expected UI state');
  };
  const button = text => [...document.querySelectorAll('button')].find(item => item.textContent.trim() === text);
  const draft = () => JSON.parse(localStorage.getItem('easel_publish_draft'));
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const imageDecoded = () => [...document.querySelectorAll('img[data-draft-media]')]
    .some(image => image.complete && image.naturalWidth > 0);
  const addBanner = (text, background) => {
    document.querySelector('[data-smoke-banner]')?.remove();
    const banner = document.createElement('p');
    banner.dataset.smokeBanner = 'true';
    banner.textContent = text;
    banner.style = `position:fixed;bottom:0;left:0;right:0;background:${background};color:white;padding:16px;z-index:99999`;
    document.body.append(banner);
    return banner;
  };
  const openPublishingFromDashboard = async () => {
    (await wait(() => button('工作台'))).click();
    (await wait(() => button('去发布'))).click();
  };

  const stage = sessionStorage.getItem('smoke-stage') || 'initial';
  try {
    switch (stage) {
      case 'initial': {
        const runResponse = await fetch('/fixture/run', {method:'POST'});
        assert(runResponse.ok, `run initialization failed: ${runResponse.status}`);
        const run = await runResponse.json();
        assert(run.ok && run.runId, 'run identity missing');
        sessionStorage.setItem('smoke-run-id', run.runId);

        (await wait(() => button('对话'))).click();
        (await wait(() => button('发送到发布中心'))).click();
        await wait(() => document.querySelector('dialog[open]'));
        assert(document.querySelector('#handoff-body').value === '在茶园散步。', 'body separated');
        (await wait(() => button('保留旧稿，另存新稿'))).click();
        await wait(() => document.querySelector('#flow-prompt'));
        assert(draft().flowPrompt.includes('晨光') && !draft().body.includes('INTERNAL'), 'visible text only');
        assert(document.querySelector('a[href="https://labs.google/fx/tools/flow"]'), 'Flow link');

        const instruction = addBanner('请人工点击页面中的“复制 Flow 提示词”；页面出现复制成功提示后，本轮会自动继续。', '#704900');
        await wait(() => document.body.textContent.includes('提示词已复制'), 750);
        instruction.remove();
        await report('chat-to-draft', true, 'human copy click observed; title/body/tags/Flow separate; old draft retained');

        const canvas = document.createElement('canvas');
        canvas.width = 24; canvas.height = 32;
        canvas.getContext('2d').fillRect(0, 0, 24, 32);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
        const transfer = new DataTransfer();
        transfer.items.add(new File([blob], 'synthetic-cover.png', {type:'image/png'}));
        const input = document.querySelector('#flow-upload');
        input.files = transfer.files;
        input.dispatchEvent(new Event('change', {bubbles:true}));
        await wait(() => draft().media?.length === 1);
        assert(draft().body === '在茶园散步。' && draft().tags === '春天,茶园', 'upload preserves text');
        await wait(imageDecoded);
        await report('image-upload', true, `selected image decoded immediately: ${draft().media[0]}`);
        sessionStorage.setItem('smoke-stage', 'refresh-manual');
        location.reload();
        return;
      }

      case 'refresh-manual': {
        await openPublishingFromDashboard();
        await wait(() => document.querySelector('#flow-upload'));
        assert(draft().media.length === 1 && draft().flowPrompt.includes('晨光'), 'refresh persisted');
        await wait(imageDecoded);
        await report('refresh-preview', true, 'real image decoded; text/Flow/media persisted');
        const review = document.querySelector('input[aria-label="小红书人工审核"]');
        assert(review, 'review gate found');
        review.click();
        const exportButton = await wait(() => [...document.querySelectorAll('button')]
          .find(item => item.textContent.includes('导出') && !item.disabled));
        exportButton.click();
        await wait(() => document.body.textContent.includes('已生成稿件包'));
        await report('manual-export', true, 'reviewed package created, no social publish/model calls');

        const policyResponse = await fetch('/fixture/policy/automated', {method:'POST'});
        assert(policyResponse.ok, `policy switch failed: ${policyResponse.status}`);
        const policy = await policyResponse.json();
        assert(policy.ok && policy.mode === 'automated', 'automatic fixture policy not applied');
        sessionStorage.setItem('smoke-stage', 'automated');
        location.reload();
        return;
      }

      case 'automated': {
        await openPublishingFromDashboard();
        const automaticButton = await wait(() => button('自动发布'));
        assert(!automaticButton.disabled, 'automatic mode is unexpectedly disabled');
        await wait(() => automaticButton.getAttribute('aria-pressed') === 'true');
        await wait(() => document.querySelector('#flow-upload'));
        assert(document.body.textContent.includes('发布中心') && draft().media.length === 1, 'automatic page or draft missing');
        await wait(imageDecoded);
        const boundary = await (await fetch('/fixture/assertions')).json();
        assert(boundary.ok, `forbidden network call: ${boundary.forbidden}`);
        await report('automatic-preview', true, 'automatic mode selected; same persisted draft image decoded');
        const result = await (await fetch('/fixture/result')).json();
        assert(result.ok && result.runId === sessionStorage.getItem('smoke-run-id'), `incomplete current run: ${JSON.stringify(result)}`);
        addBanner('ISOLATED BROWSER SMOKE PASS — chat → draft → image → refresh → manual export → automatic preview', '#165c35');
        sessionStorage.setItem('smoke-stage', 'done');
        return;
      }

      case 'done': {
        const completed = await (await fetch('/fixture/result')).json();
        assert(completed.ok && completed.runId === sessionStorage.getItem('smoke-run-id'), 'completed evidence is no longer available; start a new run');
        addBanner('ISOLATED BROWSER SMOKE PASS — current run already completed', '#165c35');
        return;
      }
      case 'failed':
        addBanner('ISOLATED BROWSER SMOKE FAILED — open a new ?run= value to retry', '#8b1e1e');
        return;
      default:
        throw new Error(`Unknown smoke stage: ${stage}`);
    }
  } catch (error) {
    try { await report('failure', false, String(error)); } catch { /* preserve the original failure */ }
    sessionStorage.setItem('smoke-stage', 'failed');
    addBanner(`ISOLATED BROWSER SMOKE FAILED — ${String(error)}`, '#8b1e1e');
    return;
  }
})();
