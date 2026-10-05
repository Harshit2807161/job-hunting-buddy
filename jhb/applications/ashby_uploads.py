"""Bind verified upload bytes to Ashby's exact server-saved attachment identity.

Saved filenames are display metadata, not document verification. This module
only creates durable proof while both the native File bytes and a newly saved
server ID are observed in the same owned application control.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import time

from . import boards
from .booklet import normalize

FILE_ID = re.compile(r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}")


def document_key(field):
    label = normalize(field.get("label", ""))
    if field.get("type") != "file":
        return None
    return ("documents.resume" if label in {"resume", "resume/cv"} else
            "documents.cover_letter" if label == "cover letter" else None)


def saved_file(helpers, field):
    """Read only the exact owned input's observed React savedFile component."""
    from .manual_runtime import ASHBY_GROUPS
    if (field.get("type") != "file" or type(field.get("owner_index")) is not int
            or type(field.get("control_index")) is not int):
        return None
    return helpers["js"]("""(()=>{
      const field=FIELD,groups=GROUPS,g=groups[field.owner_index];
      if(!g)return null;
      const owned=[...g.querySelectorAll('input,textarea,select')].filter(e=>e.closest('[data-field-path]')===g);
      const e=owned[field.control_index],path=g.getAttribute('data-field-path');
      if(!e||e.type!=='file'||!path||(e.id||'ashby:'+path+':control:'+field.control_index)!==field.ref)return null;
      const owners=[];let f=e[Object.keys(e).find(k=>k.startsWith('__reactFiber'))];
      function rootOf(node){for(let i=0;node?.return&&i<64;i++)node=node.return;return node}
      const root=rootOf(f);
      if(root?.stateNode?.current&&root.stateNode.current!==root){
        f=f?.alternate;if(!f||rootOf(f)!==root.stateNode.current)return null;
      }
      for(let i=0;f&&i<16;i++,f=f.return){
        const p=f.memoizedProps;
        if(p&&Object.prototype.hasOwnProperty.call(p,'savedFile')&&p.field?.path===path)owners.push(p.savedFile);
        if(f.stateNode===g)break;
      }
      if(owners.length!==1)return null;
      const s=owners[0],names=[...g.querySelectorAll('.ashby-application-form-input-file-item-name')].filter(n=>n.closest('[data-field-path]')===g);
      const invalid=e.getAttribute('aria-invalid')==='true'||g.getAttribute('aria-invalid')==='true'||
        ['badInput','customError','patternMismatch','rangeOverflow','rangeUnderflow','stepMismatch','tooLong','tooShort','typeMismatch'].some(k=>e.validity[k]);
      return {field_path:path,saved_file:s?{id:s.id,filename:s.filename,typename:s.__typename}:null,
        displayed_filename:names.length===1?names[0].innerText.trim():null,
        native_file_count:e.files?.length||0,other_invalid:invalid};
    })()""".replace("FIELD", json.dumps(field)).replace("GROUPS", "("+ASHBY_GROUPS+")"))


def native_bytes(helpers, expression):
    result = helpers["cdp"]("Runtime.evaluate", expression="("+expression+")?.files?.[0]", returnByValue=False)
    obj = result.get("result", {}).get("objectId")
    if not obj:
        return None
    try:
        result = helpers["cdp"]("Runtime.callFunctionOn", objectId=obj,
            functionDeclaration="async function(){const d=await crypto.subtle.digest('SHA-256',await this.arrayBuffer());return {name:this.name,size:this.size,sha256:[...new Uint8Array(d)].map(b=>b.toString(16).padStart(2,'0')).join('')}}",
            awaitPromise=True, returnByValue=True)
        return result.get("result", {}).get("value")
    finally:
        helpers["cdp"]("Runtime.releaseObject", objectId=obj)


def proof_for_upload(helpers, field, url, path, receipt, before, expression):
    """Wait for the server-save acknowledgement after trusted native upload."""
    if before is None or document_key(field) is None:
        return None  # Older/native-only widgets keep their existing byte receipt.
    identity = boards.job_identity(url)
    if not identity or identity[0] != "ashby":
        raise ValueError("Ashby upload identity changed")
    raw = path.read_bytes()
    expected = {"name": path.name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if native_bytes(helpers, expression) != expected:
        raise ValueError("Ashby uploaded bytes differ from the approved document")
    previous_id = (before.get("saved_file") or {}).get("id")
    for _ in range(40):
        observed = saved_file(helpers, field)
        server = (observed or {}).get("saved_file") or {}
        if (observed and observed["field_path"] == before["field_path"] and not observed["other_invalid"]
                and server.get("typename") == "File" and FILE_ID.fullmatch(str(server.get("id", "")))
                and server["id"] != previous_id and server.get("filename") == path.name
                and observed.get("displayed_filename") == path.name):
            if (boards.job_identity(helpers["js"]("location.href")) != identity
                    or helpers["js"]("window.__jhbGuard===true") is not True):
                raise ValueError("Ashby upload identity changed")
            if native_bytes(helpers, expression) != expected or path.read_bytes() != raw:
                raise ValueError("Ashby uploaded bytes changed before server-save verification")
            return {"schema_version": 1, "kind": "ashby_saved_file", "job_identity": list(identity),
                    "field_ref": field["ref"], "field_path": observed["field_path"],
                    "document_key": document_key(field), "saved_file_id": server["id"],
                    "filename": path.name, "size": len(raw), "sha256": expected["sha256"],
                    "upload_receipt": receipt, "verified_at": time.time()}
        helpers["wait"](0.2)
    raise ValueError("Ashby server did not acknowledge the uploaded document")


def valid_proof(proof, *, url, field, key, sha256, receipt):
    identity = boards.job_identity(url)
    return (isinstance(proof, dict) and proof.get("schema_version") == 1 and proof.get("kind") == "ashby_saved_file"
            and identity and identity[0] == "ashby" and proof.get("job_identity") == list(identity)
            and proof.get("field_ref") == field.get("ref") and bool(proof.get("field_path"))
            and proof.get("document_key") == key == document_key(field)
            and proof.get("sha256") == sha256 and bool(re.fullmatch(r"[a-f0-9]{64}", str(sha256)))
            and proof.get("upload_receipt") == receipt and bool(receipt)
            and bool(FILE_ID.fullmatch(str(proof.get("saved_file_id", ""))))
            and type(proof.get("size")) is int and 0 < proof["size"] <= 10_000_000)


def restored_state(packet, field, state, url):
    """Hydrate a restored upload only from its previously verified byte proof.

    Does not modify the DOM, packet, or observation. A changed server attachment,
    field/job binding, local document, or explicit validation error fails closed.
    """
    observed = state.get("ashby_saved_file")
    if not isinstance(observed, dict) or observed.get("native_file_count") not in {0, 1}:
        return state
    key = document_key(field)
    records = [row for row in packet.get("filled", []) if row.get("key") == key and row.get("ref") == field.get("ref")]
    if key is None or len(records) != 1:
        return state
    row = records[0]
    proof = row.get("ashby_upload_proof")
    if not valid_proof(proof, url=url, field=field, key=key,
                       sha256=row.get("document_sha256"), receipt=row.get("upload_receipt")):
        return state
    server = observed.get("saved_file") or {}
    path = Path(row.get("value", ""))
    if (observed.get("other_invalid") is not False or observed.get("field_path") != proof["field_path"]
            or server.get("typename") != "File" or server.get("id") != proof["saved_file_id"]
            or server.get("filename") != proof["filename"] or observed.get("displayed_filename") != proof["filename"]
            or not path.is_absolute() or path.is_symlink() or not path.is_file() or path.suffix.lower() != ".pdf"
            or path.name != proof["filename"] or path.stat().st_size != proof["size"]):
        return state
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-") or hashlib.sha256(raw).hexdigest() != proof["sha256"]:
        return state
    if observed["native_file_count"]:
        native = state.get("document") or {}
        if native.get("sha256") != proof["sha256"] or native.get("size") != proof["size"]:
            return state
        return {**state, "ashby_upload_proof": copy.deepcopy(proof)}
    return {**state, "value": proof["filename"], "receipt": proof["upload_receipt"], "invalid": False,
            "ashby_upload_proof": copy.deepcopy(proof)}
