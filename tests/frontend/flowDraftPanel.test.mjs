import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { runInNewContext } from 'node:vm';
import { test } from 'node:test';
const require = createRequire(new URL('../../web/frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = readFileSync(new URL('../../web/frontend/src/components/FlowDraftPanel.tsx', import.meta.url), 'utf8');
const code = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const settle = () => new Promise(resolve=>setImmediate(resolve));
function setup() {
  const state=[], refs=[], cleanup=[]; let cursor=0, refCursor=0, current='a', resolve, reject, signal, writes=0;
  const props={draft:{id:'a',body:'保留文案',tags:'茶',media:[]},onMedia:fn=>{props.draft.media=fn(props.draft.media);writes++;},onPick(){},onPrompt(){},onRatio(){}};
  const modules={react:{
    useState(initial){const i=cursor++; if(!(i in state))state[i]=initial;return[state[i],v=>state[i]=v];},
    useRef(initial){const i=refCursor++; return refs[i]??=( {current:initial} );},
    useEffect(fn){if(!cleanup.length)cleanup.push(fn());},
  },'react/jsx-runtime':{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})},
  '../lib/store':{loadPublishDraft:()=>({id:current})},'../lib/publishHandoff':{FLOW_URL:'https://labs.google/fx/tools/flow'},
  '../lib/api':{mediaUrl:p=>p,importDraftImages:(_files,id,s)=>{assert.equal(id,'a');signal=s;return new Promise((yes,no)=>{resolve=yes;reject=no;});}}};
  const exports={};runInNewContext(code,{exports,require:n=>modules[n],AbortController,Set,Promise,fetch:async()=>({ok:true})});
  const render=()=>{cursor=refCursor=0;return exports.default(props);};
  const nodes=t=>!t||typeof t!=='object'?[]:Array.isArray(t)?t.flatMap(nodes):[t,...nodes(t.props?.children)];
  const input=()=>nodes(render()).find(n=>n.props?.id==='flow-upload');
  return {props,start:()=>input().props.onChange({target:{files:[{}],value:'x'}}),
    resolve:()=>resolve(['draft-media/a/image.png']),fail:()=>reject(new Error('failed')),
    switch:()=>{current='b';},unmount:()=>cleanup.forEach(fn=>fn?.()),
    cancel:()=>nodes(render()).find(n=>n.type==='button'&&n.props.children==='取消上传').props.onClick(),
    aborted:()=>signal.aborted,writes:()=>writes,render,nodes};
}
test('image return attaches to same draft and preserves edits',async()=>{const h=setup();h.start();h.resolve();await settle();assert.equal(h.writes(),1);assert.equal(h.props.draft.body,'保留文案');assert.equal(h.props.draft.tags,'茶');});
for(const action of ['switch','cancel','unmount'])test(`late image after ${action} never mutates a draft`,async()=>{const h=setup();h.start();h[action]();h.resolve();await settle();assert.equal(h.writes(),0);if(action!=='switch')assert.equal(h.aborted(),true);});
test('failed upload leaves current draft unchanged',async()=>{const h=setup();h.start();h.fail();await settle();assert.equal(h.writes(),0);assert.deepEqual(h.props.draft.media,[]);});
test('selected image is immediately rendered and a decode failure gives an actionable error',()=>{
  const h=setup(); h.props.draft.media=['draft-media/a/image.png'];
  let tree=h.render();
  const image=h.nodes(tree).find(n=>n.type==='img'&&n.props['data-draft-media']);
  assert.ok(image); assert.equal(image.props.src,'draft-media/a/image.png');
  image.props.onError();
  tree=h.render();
  assert.ok(h.nodes(tree).some(n=>n.props?.role==='alert'&&String(n.props.children).includes('重新上传')));
});
