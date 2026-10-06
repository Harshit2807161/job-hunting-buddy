"""Native Workday upload proof survives FileList clearing, never filename-only reuse."""
from __future__ import annotations

import json
import uuid


PREPARE = r'''(args=>{
 const e=[...document.querySelectorAll('input[type=file]')][args.index];
 if(!e||e.disabled)return false;
 const owner=e.closest('[data-automation-id="attachments-FileUpload"]');
 if(owner&&(owner.querySelectorAll('input[type=file]').length!==1||owner.querySelectorAll('[data-automation-id="file-upload-item"]').length))return false;
 if(e.files.length)return false;
 const p={...args,input:e,owner,href:location.href,invalid:false,captured:null,item:null,settled:false};
 window.__jhbWorkdayFileProofs||={};window.__jhbWorkdayFileProofs[args.nonce]=p;
 let events=0;e.addEventListener('change',()=>{if(++events>1)p.invalid=true;},{capture:true});
 e.addEventListener('change',async()=>{
  const files=[...e.files];if(files.length!==1){p.invalid=true;return;}
  const f=files[0];p.file=f;
  if(f.name!==p.filename||f.size!==p.size){p.invalid=true;return;}
  try{p.captured={filename:f.name,size:f.size,sha256:[...new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('')};}
  catch(_){p.invalid=true;}
 },{capture:true,once:true});
 return true;
})'''

VERIFY = r'''(args=>{
 const p=window.__jhbWorkdayFileProofs?.[args.nonce];
 if(!p||p.invalid||p.href!==location.href||p.index!==args.index||p.filename!==args.filename||p.sha256!==args.sha256||p.size!==args.size)return null;
 if(!p.captured||p.captured.sha256!==p.sha256||p.captured.filename!==p.filename||p.captured.size!==p.size)return null;
 let method='retained_native_file';
 const retained=p.input.isConnected&&p.input.files.length===1&&p.input.files[0]===p.file;
 if(p.owner&&!retained){
  if(!p.owner.isConnected||p.owner.querySelectorAll('input[type=file]').length!==1)return null;
  const items=[...p.owner.querySelectorAll('[data-automation-id="file-upload-item"]')];
  if(items.length!==1)return null;const item=items[0];
  const names=[...item.querySelectorAll('[data-automation-id="file-upload-item-name"]')];
  const success=[...item.querySelectorAll('[data-automation-id="file-upload-successful"]')];
  if(names.length!==1||names[0].innerText.trim()!==p.filename||success.length!==1||success[0].innerText.trim()!=='Successfully Uploaded!')return null;
  const size=item.innerText.split('\n').map(s=>s.trim()).find(s=>/^\d+(?:\.\d+)?\s+(?:KB|MB|B)$/.test(s));
  if(!size)return null;const match=size.match(/^(\d+(?:\.\d+)?)\s+(KB|MB|B)$/),factor={B:1,KB:1024,MB:1048576}[match[2]];
  if(Number(match[1])!==Number((p.size/factor).toFixed(2)))return null;
  const currentInput=p.owner.querySelector('input[type=file]');
  if(p.settled&&(p.item!==item||p.liveInput!==currentInput))return null;
  if(!p.settled){
   p.item=item;p.liveInput=currentInput;p.settled=true;
   p.observer=new MutationObserver(records=>{if(records.some(r=>r.target===item||item.contains(r.target)||[...r.removedNodes].some(n=>n===item||n===p.liveInput||n.contains?.(item)||n.contains?.(p.liveInput))))p.invalid=true;});
   p.observer.observe(p.owner,{subtree:true,childList:true,characterData:true});
  }
  method='captured_native_file_and_owned_server_success';
 }else if(!retained)return null;
 return {method,filename:p.filename,size:p.size,sha256:p.sha256,nonce:p.nonce};
})'''


def prepare(js, field, path, sha256):
    args = {"index": field["file_index"], "filename": path.name, "size": path.stat().st_size,
            "sha256": sha256, "nonce": uuid.uuid4().hex}
    if js(PREPARE+"("+json.dumps(args)+")") is not True:
        raise ValueError("Workday uploaded document did not retain its approved bytes")
    return args


def verify(js, args):
    return js(VERIFY+"("+json.dumps(args)+")")
