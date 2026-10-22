/* Runs only in the explicitly opened isolated fixture, never in product builds. */
(async () => {
  const report = (step, ok, detail = '') => fetch('/evidence', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({step,ok,detail})});
  const wait = async fn => { for(let i=0;i<150;i++){ const result=fn(); if(result)return result; await new Promise(r=>setTimeout(r,100)); } throw new Error('Timed out: '+fn); };
  const button = text => [...document.querySelectorAll('button')].find(b=>b.textContent.trim()===text);
  const draft = () => JSON.parse(localStorage.getItem('easel_publish_draft'));
  const assert = (v, message) => { if(!v)throw new Error(message); };
  try {
    if (!sessionStorage.getItem('smoke-stage')) {
      (await wait(()=>button('对话'))).click();
      (await wait(()=>button('发送到发布中心'))).click();
      await wait(()=>document.querySelector('dialog[open]'));
      assert(document.querySelector('#handoff-body').value==='在茶园散步。', 'body separated');
      (await wait(()=>button('保存草稿并前往') || button('保留旧稿，另存新稿'))).click();
      await wait(()=>document.querySelector('#flow-prompt'));
      assert(draft().flowPrompt.includes('晨光') && !draft().body.includes('INTERNAL'), 'visible text only');
      assert(document.querySelector('a[href="https://labs.google/fx/tools/flow"]'), 'Flow link');
      await report('chat-to-draft', true, 'title/body/tags/Flow separate; manual policy; original retained');
      const canvas=document.createElement('canvas'); canvas.width=24; canvas.height=32; canvas.getContext('2d').fillRect(0,0,24,32);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
      const transfer=new DataTransfer(); transfer.items.add(new File([blob],'synthetic-cover.png',{type:'image/png'}));
      const input=document.querySelector('#flow-upload'); input.files=transfer.files; input.dispatchEvent(new Event('change',{bubbles:true}));
      await wait(()=>draft().media?.length===1);
      assert(draft().body==='在茶园散步。' && draft().tags==='春天,茶园', 'upload preserves text');
      await report('image-upload', true, draft().media[0]);
      sessionStorage.setItem('smoke-stage','refresh'); location.reload(); return;
    }
    (await wait(()=>button('工作台'))).click();
    (await wait(()=>button('发布中心') || [...document.querySelectorAll('button')].find(b=>b.textContent.includes('发布中心')))).click();
    await wait(()=>document.querySelector('#flow-upload'));
    assert(draft().media.length===1 && draft().flowPrompt.includes('晨光'), 'refresh persisted');
    await wait(()=>[...document.images].some(i=>i.src.includes('draft-media') && i.complete && i.naturalWidth>0));
    await report('refresh-preview',true,'real image decoded; text/Flow/media persisted');
    const checks=[...document.querySelectorAll('input[type="checkbox"]')];
    const review=checks.find(c=>c.parentElement.textContent.includes('审核'));
    assert(review,'review gate found'); review.click();
    const exportButton=await wait(()=>[...document.querySelectorAll('button')].find(b=>b.textContent.includes('导出')&&!b.disabled));
    exportButton.click();
    await wait(()=>document.body.textContent.includes('已生成稿件包'));
    await report('manual-export',true,'reviewed package created, no social publish/model calls');
    const banner=document.createElement('p'); banner.textContent='ISOLATED BROWSER SMOKE PASS — chat → draft → image → refresh → preview → export'; banner.style='position:fixed;bottom:0;left:0;background:#165c35;color:white;padding:16px;z-index:99999';document.body.append(banner);
    sessionStorage.setItem('smoke-stage','done');
  } catch(error) { await report('failure',false,String(error)); }
})();
