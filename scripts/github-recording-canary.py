#!/usr/bin/env python3
"""Bounded GitHub Actions canary for driving Recordly through Chromium CDP."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path

import websockets


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9333)
    parser.add_argument("--duration", type=float, default=8.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    deadline = time.time() + 40
    targets = []
    while time.time() < deadline:
        try:
            targets = get_json(f"{base}/json")
            if any(t.get("type") == "page" and "Recordly" in t.get("title", "") for t in targets):
                break
        except Exception:
            pass
        await asyncio.sleep(1)
    else:
        raise RuntimeError("Recordly CDP target did not become ready")

    target = next(t for t in targets if t.get("type") == "page" and "Recordly" in t.get("title", ""))
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=8_000_000) as ws:
        seq = 0

        async def evaluate(expression: str):
            nonlocal seq
            seq += 1
            msg_id = seq
            await ws.send(json.dumps({
                "id": msg_id,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
            }))
            while True:
                data = json.loads(await ws.recv())
                if data.get("id") != msg_id:
                    continue
                if "error" in data:
                    raise RuntimeError(data["error"])
                result = data.get("result", {})
                if result.get("exceptionDetails"):
                    raise RuntimeError(result["exceptionDetails"])
                return result.get("result", {}).get("value")

        sources = await evaluate("""
            (async()=>{
              const s=await window.electronAPI.getSources({types:['screen','window'],thumbnailSize:{width:1,height:1},fetchWindowIcons:false});
              return s.map(x=>({id:x.id,name:x.name,sourceType:x.sourceType,display_id:x.display_id}));
            })()
        """)
        print("SOURCES=" + json.dumps(sources, ensure_ascii=False))
        Path("canary-sources.json").write_text(json.dumps(sources, indent=2), encoding="utf-8")
        if not sources:
            raise RuntimeError("Recordly returned no capture sources")
        screen = next((x for x in sources if x.get("sourceType") == "screen"), None)
        if not screen:
            raise RuntimeError("No screen capture source was exposed on the GitHub runner")

        selected = await evaluate(f"""
            (async()=>{{
              const s=await window.electronAPI.getSources({{types:['screen','window'],thumbnailSize:{{width:1,height:1}},fetchWindowIcons:false}});
              const x=s.find(v=>v.id==={json.dumps(screen['id'])});
              if(!x) throw new Error('Selected screen disappeared');
              await window.electronAPI.selectSource(x);
              return await window.electronAPI.getSelectedSource();
            }})()
        """)
        print("SELECTED=" + json.dumps(selected, ensure_ascii=False))

        state = await evaluate("""
            (async()=>{
              const b=document.querySelector('button[title="Record"]');
              if(!b) throw new Error('Record button missing');
              b.click();
              await new Promise(r=>setTimeout(r,1800));
              return (document.body.innerText||'').slice(0,500);
            })()
        """)
        print("AFTER_START=" + repr(state))
        if "REC" not in str(state):
            raise RuntimeError("Recordly did not enter recording state")

        await asyncio.sleep(max(3.0, args.duration))
        try:
            await evaluate("""
                (()=>{
                  const b=document.querySelector('button[title="Stop"]');
                  if(!b) throw new Error('Stop button missing');
                  b.click();
                  return true;
                })()
            """)
        except Exception as exc:
            print(f"STOP_CHANNEL_CLOSED={type(exc).__name__}: {exc}")

    await asyncio.sleep(5)
    roots = [Path.home()/".config"/"Recordly"/"recordings", Path.home()/".config"/"recordly"/"recordings"]
    files = [p for root in roots if root.exists() for p in root.rglob("*.webm")]
    if not files:
        raise RuntimeError("No Recordly WebM was produced on the GitHub runner")
    newest = max(files, key=lambda p: p.stat().st_mtime)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(newest, args.output)
    print(f"RECORDING_SOURCE={newest}")
    print(f"RECORDING_OUTPUT={args.output}")
    print(f"RECORDING_SIZE={args.output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
