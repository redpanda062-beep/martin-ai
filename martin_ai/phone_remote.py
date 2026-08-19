from __future__ import annotations

import ctypes
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from collections import defaultdict, deque
from ctypes import wintypes
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from .mobile_companion import MobileCompanionService, MAX_TRANSFER_BYTES, PROTOCOL_VERSION


LOGGER = logging.getLogger(__name__)
STATE_FILE = Path.home() / "Documents" / "MartinAI" / "phone_remote_devices.json"
PWA_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "pwa"
REMOTE_UI_VERSION = "2.0.0"
PWA_MANIFEST = {"name": "Martin AI", "short_name": "Martin AI", "id": "/", "start_url": "/", "scope": "/",
                "display": "standalone", "orientation": "any", "theme_color": "#090909", "background_color": "#050505",
                "icons": [{"src": "/pwa/martin-ai-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                          {"src": "/pwa/martin-ai-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}
SERVICE_WORKER_JS = b"""const CACHE='martin-ai-shell-v2';const STATIC=['/offline','/manifest.webmanifest','/pwa/martin-ai-180.png','/pwa/martin-ai-192.png','/pwa/martin-ai-512.png'];self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(STATIC)).then(()=>self.skipWaiting())));self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.origin!==location.origin||u.pathname.startsWith('/api/'))return;if(e.request.mode==='navigate'){e.respondWith(fetch(e.request,{cache:'no-store'}).catch(()=>caches.match('/offline')));return}if(STATIC.includes(u.pathname))e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request,{cache:'no-cache'})))})"""
OFFLINE_HTML = b"""<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'><meta name=theme-color content='#090909'><style>body{margin:0;padding:env(safe-area-inset-top) 18px env(safe-area-inset-bottom);background:#050505;color:#eee;font:16px Segoe UI,Arial}h1{color:#f33;margin-top:36px}.card{border:1px solid #711;background:#111;border-radius:12px;padding:18px}button{display:block;width:100%;padding:14px;margin-top:18px;background:#a11;color:white;border:1px solid #f44;border-radius:9px;font-weight:bold}</style></head><body><h1>MARTIN AI</h1><div class=card><b>MARTIN PC: OFFLINE</b><p id=wakeText>Martin PC is unreachable.</p><button id=wake onclick=wakePC()>WAKE MARTIN PC</button><button onclick=location.reload()>TRY AGAIN</button></div><script>let config;try{config=JSON.parse(localStorage.getItem('martinWake')||'null')}catch(e){}if(!config?.mac){wake.disabled=true;wakeText.textContent='Wake unavailable. Configure Remote Wake while Martin PC is online.'}function wakePC(){if(!config?.mac)return;wakeText.textContent='Opening the local Wake-on-LAN provider...';location.href='martin-wol://wake?mac='+encodeURIComponent(config.mac)+'&name='+encodeURIComponent(config.pc);setTimeout(()=>{wakeText.textContent='Wake signal requested. Waiting for Martin Remote...';let end=Date.now()+90000,t=setInterval(()=>{fetch('/api/status',{cache:'no-store'}).then(r=>{if(r.status===200||r.status===401){clearInterval(t);location.reload()}}).catch(()=>{});if(Date.now()>end){clearInterval(t);wakeText.textContent='Martin PC has not appeared yet. Try again or view diagnostics when it reconnects.'}},3000)},1000)}</script></body></html>"""
SERVICE_WORKER_JS = SERVICE_WORKER_JS.replace(b"martin-ai-shell-v2", b"martin-ai-shell-v3")
DEFAULT_REMOTE_SETTINGS = {"enabled": False, "port": 8765, "allow_chat": True, "allow_pc_controls": True,
                           "allow_remote_input": False, "allow_game_controls": True,
                           "require_power_confirmation": True, "remote_quality": "Balanced",
                           "gaming_remote_quality": "Low", "control_timeout_minutes": 15}
DEFAULT_DEVICE_PERMISSIONS = {
    "remote_chat": True, "game_status": True, "game_controls": True,
    "media_controls": True, "volume_controls": True, "pc_lock": True,
    "remote_input": False, "sleep": False, "restart": False,
    "shutdown": False, "stop_ai": False,
    "view_screen": False, "clipboard_sync": False,
    "file_transfer": False, "terminal": False, "camera": False, "audio": False,
    "screenshots": False, "notifications": True,
    "start_martin_ai": True,
}
ACTION_PERMISSIONS = {"volume_up": "volume_controls", "volume_down": "volume_controls", "volume_mute": "volume_controls",
                      "media_play_pause": "media_controls", "media_next": "media_controls", "media_previous": "media_controls",
                      "lock": "pc_lock", "sleep": "sleep", "restart": "restart", "shutdown": "shutdown", "stop_ai": "stop_ai"}
CONFIRMED_ACTIONS = frozenset({"sleep", "restart", "shutdown", "stop_ai"})
PERMISSION_LABELS = {"remote_chat": "Remote chat", "game_status": "Game status", "game_controls": "Game controls",
                     "media_controls": "Media controls", "volume_controls": "Volume control", "pc_lock": "PC lock",
                     "remote_input": "Remote input", "sleep": "Sleep", "restart": "Restart", "shutdown": "Shutdown", "stop_ai": "Stop AI"}
PERMISSION_LABELS.update({"view_screen": "View Screen", "clipboard_sync": "Clipboard Sync"})
PERMISSION_LABELS.update({"file_transfer": "File Transfer", "terminal": "Terminal", "camera": "Camera",
                          "audio": "PC Audio", "screenshots": "Screenshots", "notifications": "Notifications",
                          "start_martin_ai": "Start Martin AI"})
PERMISSION_DENIALS = {"volume_controls": "Volume control is not enabled for this device.",
                      "media_controls": "Media controls are not enabled for this device.",
                      "pc_lock": "PC lock is not enabled for this device.", "sleep": "Sleep permission is disabled.",
                      "restart": "Restart permission is disabled.", "shutdown": "Shutdown permission is disabled.",
                      "stop_ai": "Stop AI permission is disabled.", "game_controls": "Game controls are not enabled for this device.",
                      "remote_chat": "Remote chat is not enabled for this device.",
                      "remote_input": "Remote input permission is disabled for this device."}
PERMISSION_DENIALS.update({"view_screen": "View Screen permission is disabled for this device.",
                           "clipboard_sync": "Clipboard Sync permission is disabled for this device."})
QUALITY_PROFILES = {"Low": {"fps": 8, "quality": 42, "scale": 0.55},
                    "Balanced": {"fps": 14, "quality": 62, "scale": 0.75},
                    "High": {"fps": 18, "quality": 78, "scale": 1.0}}


MOBILE_HTML = r"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Martin AI Remote</title><meta name=theme-color content="#090909"><meta name=apple-mobile-web-app-capable content=yes><meta name=apple-mobile-web-app-status-bar-style content=black-translucent><meta name=apple-mobile-web-app-title content="Martin AI"><link rel=manifest href=/manifest.webmanifest><link rel=apple-touch-icon sizes=180x180 href=/pwa/martin-ai-180.png><link rel=icon sizes=192x192 href=/pwa/martin-ai-192.png>
<style>*{box-sizing:border-box}html{background:#050505}body{margin:0;padding-top:env(safe-area-inset-top);padding-right:env(safe-area-inset-right);padding-bottom:env(safe-area-inset-bottom);padding-left:env(safe-area-inset-left);min-height:100vh;min-height:100dvh;background:#080808;color:#eee;font:15px Segoe UI,Arial}header{padding:18px;color:#f44;font-size:25px;font-weight:800}.wrap{padding:9px;max-width:720px;margin:auto}.card{background:#111;border:1px solid #711;border-radius:12px;padding:13px;margin:9px 0}h2{font-size:13px;color:#f44;letter-spacing:1px}button,input,select{background:#181818;color:#fff;border:1px solid #a22;border-radius:8px;padding:11px;margin:3px}button{color:#f66}.grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}.metric{padding:8px;background:#0b0b0b}.danger{background:#3b0808}.desktop{background:#000;min-height:210px;touch-action:none}.desktop img{display:block;width:100%;height:auto;max-height:72vh;object-fit:contain}.state{color:#f55;font-weight:700}#log{white-space:pre-wrap;max-height:220px;overflow:auto}@media(orientation:landscape){.wrap{max-width:none}.desktop img{max-height:78vh}}@media(display-mode:standalone){header{padding-top:12px}.wrap{padding-bottom:max(9px,env(safe-area-inset-bottom))}}</style></head>
<body><header>MARTIN AI <small id=appMode>BROWSER REMOTE</small></header><div class=wrap><div id=pair class=card><h2>PAIR PHONE</h2><input id=code placeholder="Pairing code"><input id=device placeholder="Device name"><button onclick=pair()>PAIR</button></div><div id=app hidden>
<div class=card><h2>HOME</h2><b>Martin PC — <span id=online>ONLINE</span></b><div class=grid><div class=metric>CPU <span id=cpu>--</span>%</div><div class=metric>GPU <span id=gpu>--</span>%</div><div class=metric>GPU TEMP <span id=temp>--</span> C</div><div class=metric>RAM <span id=ram>--</span>%</div></div></div>
<div class=card><h2>AI / GAME MODE</h2><div id=ai>--</div><div id=game>--</div><div id=opt>--</div><button onclick=cmd('optimization status')>STATUS</button><button onclick=cmd('reoptimize game')>REOPTIMIZE</button></div>
<div id=installGuide class=card><h2>INSTALL MARTIN AI</h2><div>On iPhone Safari, tap Share, then Add to Home Screen.</div></div>
<div class=card><h2>REMOTE DESKTOP</h2><div class=state id=rdstate>DISCONNECTED</div><div id=desktop class=desktop><img id=screen alt="Remote desktop"></div><div><button onclick=viewDesktop()>VIEW ONLY</button><button onclick=control()>CONTROL</button><button onclick=takeControl()>TAKE CONTROL</button><button onclick=full()>FULLSCREEN</button><button onclick=disconnectDesktop()>DISCONNECT</button></div><select id=touchmode><option value=touchpad>Touchpad</option><option value=direct>Direct Touch</option></select><label>Sensitivity <input id=sensitivity type=range min=.3 max=2 step=.1 value=1></label><div class=grid><button onclick=input('click',{button:'left'})>LEFT CLICK</button><button onclick=input('click',{button:'right'})>RIGHT CLICK</button><button onclick=input('click',{button:'middle'})>MIDDLE CLICK</button><button onclick=input('scroll',{amount:120})>SCROLL UP</button><button onclick=input('scroll',{amount:-120})>SCROLL DOWN</button></div><input id=keys placeholder="Keyboard text"><button onclick="keys.focus()">KEYBOARD</button><button onclick="input('text',{text:keys.value}).then(()=>keys.value='')">TYPE</button><div><button onclick=key('ESCAPE')>ESC</button><button onclick=key('TAB')>TAB</button><button onclick=key('ENTER')>ENTER</button><button onclick=key('BACKSPACE')>⌫</button><button onclick=key('DELETE')>DEL</button><button onclick=key('LEFT')>←</button><button onclick=key('UP')>↑</button><button onclick=key('DOWN')>↓</button><button onclick=key('RIGHT')>→</button><button onclick=toggleMod('CTRL') id=mCTRL>CTRL</button><button onclick=toggleMod('ALT') id=mALT>ALT</button><button onclick=toggleMod('SHIFT') id=mSHIFT>SHIFT</button><button onclick=key('WINDOWS')>WIN</button></div><small>Ctrl+Alt+Del is not supported.</small></div>
<div class=card><h2>QUICK CONTROLS</h2><div class=grid><button onclick=act('lock')>LOCK PC</button><button onclick=act('media_play_pause')>PLAY / PAUSE</button><button onclick=act('media_previous')>PREVIOUS</button><button onclick=act('media_next')>NEXT</button><button onclick=act('volume_down')>VOL −</button><button onclick=act('volume_up')>VOL +</button><button class=danger onclick=act('stop_ai',true)>STOP AI</button></div></div><div class=card><h2>CHAT</h2><div id=log></div><input id=message style="width:72%" placeholder="Message Martin AI..."><button onclick=send()>SEND</button></div></div></div>
<script>let token=localStorage.martinToken||'',mods=[],inputSequence=0;const H=()=>({'Authorization':'Bearer '+token,'Content-Type':'application/json'});if(token){pair.hidden=true;app.hidden=false;poll()}async function pair(){let r=await fetch('/api/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code.value,device_name:device.value||'Phone'})});let j=await r.json();if(j.token){token=j.token;localStorage.martinToken=token;location.reload()}else alert(j.error)}async function api(path,data){let r=await fetch(path,{method:data?'POST':'GET',headers:H(),body:data?JSON.stringify(data):null});if(r.status==401){localStorage.removeItem('martinToken');location.reload()}return r.json()}async function poll(){try{let s=await api('/api/status');cpu.textContent=s.cpu??'--';gpu.textContent=s.gpu??'--';temp.textContent=s.gpu_temperature??'--';ram.textContent=s.ram??'--';ai.textContent=`Ollama: ${s.ollama_status} | Autonomy: ${s.autonomy?'ON':'OFF'}`;game.textContent=`Game: ${s.game||'None'} | ${s.game_active?'ACTIVE':'INACTIVE'}`;opt.textContent=`Optimization: ${s.optimization.status}`;online.textContent='ONLINE'}catch(e){online.textContent='OFFLINE'}setTimeout(poll,3000)}async function cmd(text){let j=await api('/api/chat',{message:text});log.textContent+='\nMartin: '+(j.message||j.error)}async function send(){let t=message.value;message.value='';await cmd(t)}async function act(action,c=false){if(c&&!confirm(action+' Martin PC?'))return;let j=await api('/api/action',{action,confirmed:c});if(j.error)alert(j.error)}async function input(type,data){if(type==='move'||type==='direct')data.sequence=++inputSequence;let j=await api('/api/input',{type,...data});if(j.error)alert(j.error);return j}
function viewDesktop(){screen.src='/api/desktop/stream?t='+Date.now();rdstate.textContent='LIVE — VIEW ONLY'}async function control(){let j=await api('/api/desktop/control',{operation:'start'});rdstate.textContent=j.success?'CONTROL ACTIVE':j.error}async function takeControl(){if(confirm('Take control?')){let j=await api('/api/desktop/control',{operation:'take',confirmed:true});rdstate.textContent=j.success?'CONTROL ACTIVE':j.error}}async function disconnectDesktop(){await api('/api/desktop/control',{operation:'disconnect'});screen.src='';rdstate.textContent='DISCONNECTED'}function full(){desktop.requestFullscreen&&desktop.requestFullscreen()}function toggleMod(k){mods.includes(k)?mods=mods.filter(x=>x!=k):mods.push(k);document.querySelector('#m'+k).style.background=mods.includes(k)?'#711':'#181818'}function key(k){input('key',{key:k,modifiers:mods});mods=[];['CTRL','ALT','SHIFT'].forEach(x=>document.querySelector('#m'+x).style.background='#181818')}
let x,y,last=0,down=0,moved=false,dragging=false,timer;desktop.onpointerdown=e=>{x=e.clientX;y=e.clientY;down=Date.now();moved=false;dragging=false;desktop.setPointerCapture(e.pointerId);timer=setTimeout(()=>{if(!moved){input('click',{button:'right'});moved=true}},650)};desktop.onpointermove=e=>{if(x===undefined||Date.now()-last<25)return;last=Date.now();let dx=e.clientX-x,dy=e.clientY-y;if(Math.abs(dx)+Math.abs(dy)>2){moved=true;if(Date.now()-down>250&&!dragging){clearTimeout(timer);dragging=true;input('drag_start',{})}}if(touchmode.value==='direct'){let r=screen.getBoundingClientRect();input('direct',{x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))})}else input('move',{dx:Math.round(dx*sensitivity.value),dy:Math.round(dy*sensitivity.value)});x=e.clientX;y=e.clientY};desktop.onpointerup=e=>{clearTimeout(timer);if(dragging)input('drag_end',{});else if(!moved&&Date.now()-down<400)input('click',{button:'left',double:e.detail>=2});x=y=undefined};desktop.onwheel=e=>{e.preventDefault();input('scroll',{amount:e.deltaY<0?120:-120})};</script></body></html>"""

MOBILE_HTML = MOBILE_HTML.replace(
    "<div class=state id=rdstate>DISCONNECTED</div><div id=desktop",
    "<div class=state id=rdstate>DISCONNECTED</div><label>Display <select id=display onchange=selectDisplay()><option>Loading displays...</option></select></label><div id=desktop",
).replace(
    "function viewDesktop(){screen.src=",
    "async function loadDisplays(){let j=await api('/api/desktop/monitors');if(!j.monitors)return;display.innerHTML='';j.monitors.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.label+' — '+m.width+'x'+m.height;o.selected=m.id===j.selected_monitor;display.appendChild(o)})}async function selectDisplay(){let j=await api('/api/desktop/monitor',{monitor_id:display.value});if(!j.success)alert(j.error)}function viewDesktop(){loadDisplays();screen.src=",
)
MOBILE_HTML = MOBILE_HTML.replace("if(token){pair.hidden=true;app.hidden=false;poll()}",
                                  "if(token){pair.hidden=true;app.hidden=false;poll();loadDisplays()}")
MOBILE_HTML = MOBILE_HTML.replace(
    ".state{color:#f55;font-weight:700}",
    ".state{color:#f55;font-weight:700}.desktop{position:relative;border:2px solid #c33;min-width:240px}.desktop img{min-height:180px}.streamdiag{font:12px monospace;white-space:pre-wrap;background:#090909;border:1px solid #522;padding:7px;margin:6px 0;color:#ccc}",
).replace(
    "<div id=desktop class=desktop>",
    "<button onclick=testSingleFrame()>TEST SINGLE FRAME</button><div id=streamdiag class=streamdiag>Connection: Disconnected\\nStream Mode: Safari JPEG\\nFrames Received: 0\\nFrames Rendered: 0</div><div id=desktop class=desktop>",
).replace(
    "function viewDesktop(){loadDisplays();screen.src='/api/desktop/stream?t='+Date.now();rdstate.textContent='LIVE \u2014 VIEW ONLY'}",
    "let streaming=false,framesReceived=0,framesRendered=0,lastFrame=0,streamError='',streamMode='Safari JPEG',frameObjectUrl='';function showDiag(){let age=lastFrame?Math.round((Date.now()-lastFrame)/1000)+'s':'Never';streamdiag.textContent='Connection: '+(streaming?'Connected':'Disconnected')+'\\nStream URL: '+location.origin+'/api/desktop/frame\\nSelected Monitor: '+(display.options[display.selectedIndex]?.text||'Unknown')+'\\nStream Mode: '+streamMode+'\\nFrames Received: '+framesReceived+'\\nFrames Rendered: '+framesRendered+'\\nRendered Size: '+screen.clientWidth+'x'+screen.clientHeight+'\\nLast Frame Age: '+age+'\\nLast Error: '+(streamError||'None')}async function fetchFrame(test=false){let path=test?'/api/desktop/frame-test':'/api/desktop/frame';try{let r=await fetch(new URL(path+'?t='+Date.now(),location.origin),{headers:H(),cache:'no-store'});console.info('[Martin Remote] frame response',r.status);if(r.status===401)throw Error('Stream authorization failed');if(!r.ok){let j=await r.json().catch(()=>({}));throw Error(j.error||('Stream HTTP '+r.status))}let blob=await r.blob();framesReceived++;console.info('[Martin Remote] frame received',blob.size);let frameId=r.headers.get('X-Martin-Frame-Id')||'';let url=URL.createObjectURL(blob);await new Promise((ok,fail)=>{screen.onload=()=>{if(frameObjectUrl)URL.revokeObjectURL(frameObjectUrl);frameObjectUrl=url;framesRendered++;lastFrame=Date.now();rdstate.textContent=test?'FRAME RECEIVED':'LIVE â€” VIEW ONLY';console.info('[Martin Remote] image decode/render success',screen.naturalWidth,screen.naturalHeight);api('/api/desktop/frame-ack',{frame_id:frameId,rendered:true}).catch(()=>{});showDiag();ok()};screen.onerror=()=>{URL.revokeObjectURL(url);fail(Error('image decode failed'))};screen.src=url})}catch(e){streamError=e.message;rdstate.textContent='RENDER ERROR';console.error('[Martin Remote]',e);showDiag();throw e}}async function frameLoop(){while(streaming){try{await fetchFrame(false);await new Promise(r=>setTimeout(r,100))}catch(e){streamMode='MJPEG fallback';rdstate.textContent='Web request failed; trying MJPEG fallback';console.warn('[Martin Remote] trying MJPEG fallback',e.message);screen.src=new URL('/api/desktop/stream?t='+Date.now(),location.origin);screen.onload=()=>{lastFrame=Date.now();framesRendered++;rdstate.textContent='LIVE â€” MJPEG';showDiag()};screen.onerror=()=>{streamError='MJPEG stream failed';rdstate.textContent='No frames received';showDiag()};break}}}async function viewDesktop(){await loadDisplays();streaming=true;streamMode='Safari JPEG';streamError='';rdstate.textContent='CONNECTING';showDiag();rdstate.textContent='WAITING FOR FRAME';console.info('[Martin Remote] stream open',location.origin);frameLoop()}async function testSingleFrame(){streaming=false;rdstate.textContent='WAITING FOR FRAME';await fetchFrame(true).catch(()=>{})}",
).replace(
    "async function disconnectDesktop(){await api('/api/desktop/control',{operation:'disconnect'});screen.src='';rdstate.textContent='DISCONNECTED'}",
    "async function disconnectDesktop(){streaming=false;await api('/api/desktop/control',{operation:'disconnect'});screen.src='';rdstate.textContent='DISCONNECTED';console.info('[Martin Remote] stream closed');showDiag()}",
)
MOBILE_HTML = MOBILE_HTML.replace("LIVE \u00e2\u20ac\u201d", "LIVE \u2014")
MOBILE_HTML = MOBILE_HTML.replace("let streaming=false,", "let firstAckSent=false,streaming=false,").replace(
    "api('/api/desktop/frame-ack',{frame_id:frameId,rendered:true}).catch(()=>{});showDiag()",
    "if(!firstAckSent){firstAckSent=true;api('/api/desktop/frame-ack',{frame_id:frameId,rendered:true}).catch(()=>{})}showDiag()",
).replace(
    "screen.onerror=()=>{URL.revokeObjectURL(url);fail(Error('image decode failed'))}",
    "screen.onerror=()=>{URL.revokeObjectURL(url);if(!firstAckSent){firstAckSent=true;api('/api/desktop/frame-ack',{frame_id:frameId,rendered:false,error:'image decode failed'}).catch(()=>{})}fail(Error('image decode failed'))}",
)
MOBILE_HTML = MOBILE_HTML.replace("<img id=screen ", "<img id=remoteScreen ").replace("screen.", "remoteImage.")
MOBILE_HTML = MOBILE_HTML.replace(
    "let firstAckSent=false,streaming=false,framesReceived=0,framesRendered=0,lastFrame=0,streamError='',streamMode='Safari JPEG',frameObjectUrl='';",
    "const remoteImage=document.getElementById('remoteScreen');let firstAckSent=false,streaming=false,framesRequested=0,framesReceived=0,framesRendered=0,lastHttpStatus='None',lastFrameBytes=0,lastFrame=0,streamError='',streamMode='Safari JPEG',frameObjectUrl='';",
).replace(
    "'Connection: '+(streaming?'Connected':'Disconnected')+'\\nStream URL: '",
    "'Dashboard Connection: CONNECTED\\nDesktop Stream: '+(framesReceived?'CONNECTED':(streaming?'WAITING':'DISCONNECTED'))+'\\nStream URL: '",
).replace(
    "+'\\nFrames Received: '+framesReceived+'\\nFrames Rendered: '+framesRendered+'\\nRendered Size: '+remoteImage.clientWidth+'x'+remoteImage.clientHeight+",
    "+'\\nFrames Requested: '+framesRequested+'\\nFrames Received: '+framesReceived+'\\nFrames Rendered: '+framesRendered+'\\nLast Frame HTTP Status: '+lastHttpStatus+'\\nLast Frame Bytes: '+lastFrameBytes+'\\nRendered Size: '+(remoteImage.naturalWidth||0)+'x'+(remoteImage.naturalHeight||0)+",
).replace(
    "let path=test?'/api/desktop/frame-test':'/api/desktop/frame';try{let r=await fetch(new URL(path+'?t='+Date.now(),location.origin),",
    "let path='/api/desktop/frame';try{framesRequested++;showDiag();let query='?t='+Date.now()+'&monitor_id='+encodeURIComponent(display.value);let r=await fetch(new URL(path+query,location.origin),",
).replace(
    "console.info('[Martin Remote] frame response',r.status);if(r.status===401)",
    "lastHttpStatus=r.status;showDiag();console.info('[Martin Remote] frame response',r.status);if(r.status===401)",
).replace(
    "let blob=await r.blob();framesReceived++;",
    "let blob=await r.blob();lastFrameBytes=blob.size;if(!blob.size)throw Error('Frame request failed: empty response');framesReceived++;",
).replace("setTimeout(r,100)", "setTimeout(r,200)")
MOBILE_HTML = MOBILE_HTML.replace(
    "<input id=device placeholder=\"Device name\"><button onclick=pair()>PAIR</button>",
    "<input id=device placeholder=\"Device name\"><label><input id=trustDevice type=checkbox checked> Trust this device</label><small>Trusted devices reconnect automatically without another pairing code.</small><button onclick=pair()>PAIR</button>",
).replace(
    "<div id=app hidden>",
    "<div id=app hidden><div class=card><b id=trustedName>Martin phone</b><div class=state>TRUSTED DEVICE — CONNECTED</div><button onclick=disconnectSession()>DISCONNECT</button><button onclick=forgetDevice()>FORGET THIS DEVICE</button></div>",
).replace(
    "const H=()=>({'Authorization':'Bearer '+token,'Content-Type':'application/json'});if(token){pair.hidden=true;app.hidden=false;poll();loadDisplays()}",
    "const H=()=>token?{'Authorization':'Bearer '+token,'Content-Type':'application/json'}:{'Content-Type':'application/json'};boot();",
).replace(
    "async function pair(){let r=await fetch('/api/pair'",
    "async function boot(){let saved=localStorage.martinToken||'';if(!saved){pair.hidden=false;app.hidden=true;return}token=saved;try{let r=await fetch(new URL('/api/session',location.origin),{method:'POST',headers:H(),cache:'no-store'});let j=await r.json();if(!r.ok||!j.success)throw Error(j.error||'Authentication failed');token='';pair.hidden=true;app.hidden=false;trustedName.textContent=j.device_name||'Trusted phone';poll();loadDisplays();console.info('[Martin Remote] trusted device session restored')}catch(e){token='';localStorage.removeItem('martinToken');pair.hidden=false;app.hidden=true;console.warn('[Martin Remote] automatic authentication failed',e.message)}}async function disconnectSession(){await fetch(new URL('/api/session/disconnect',location.origin),{method:'POST',headers:H()});streaming=false;app.hidden=true;pair.hidden=false}async function forgetDevice(){if(!confirm('Forget this trusted device? Pairing will be required next time.'))return;await fetch(new URL('/api/session/forget',location.origin),{method:'POST',headers:H()});localStorage.removeItem('martinToken');token='';streaming=false;app.hidden=true;pair.hidden=false}async function pair(){let r=await fetch('/api/pair'",
).replace(
    "if(j.token){token=j.token;localStorage.martinToken=token;location.reload()}else alert(j.error)",
    "if(j.token){token=j.token;if(trustDevice.checked)localStorage.martinToken=token;else localStorage.removeItem('martinToken');pair.hidden=true;app.hidden=false;trustedName.textContent=j.device_name||device.value||'Trusted phone';token='';poll();loadDisplays()}else alert(j.error)",
)
MOBILE_HTML = MOBILE_HTML.replace(
    "let saved=localStorage.martinToken||'';if(!saved){pair.hidden=false;app.hidden=true;return}token=saved;try{",
    "let saved=localStorage.martinToken||'';token=saved;try{",
).replace(
    "token='';pair.hidden=true;app.hidden=false;trustedName.textContent=j.device_name",
    "token='';localStorage.removeItem('martinToken');pair.hidden=true;app.hidden=false;trustedName.textContent=j.device_name",
).replace(
    "device_name:device.value||'Phone'})",
    "device_name:device.value||'Phone',trust_device:trustDevice.checked})",
).replace(
    "if(j.token){token=j.token;if(trustDevice.checked)localStorage.martinToken=token;else localStorage.removeItem('martinToken');pair.hidden=true;",
    "if(j.token){token='';localStorage.removeItem('martinToken');pair.hidden=true;",
)

MOBILE_HTML = MOBILE_HTML.replace(
    "<script>let token=",
    "<script>const standalone=window.matchMedia('(display-mode: standalone)').matches||window.navigator.standalone===true;window.addEventListener('DOMContentLoaded',()=>{let mode=document.getElementById('appMode'),guide=document.getElementById('installGuide');if(mode)mode.textContent=standalone?'MARTIN AI APP':'BROWSER REMOTE';if(guide)guide.hidden=standalone;if('serviceWorker' in navigator&&window.isSecureContext)navigator.serviceWorker.register('/service-worker.js',{scope:'/'}).catch(e=>console.warn('[Martin Remote] service worker unavailable',e.message))});let token=",
)
MOBILE_HTML = MOBILE_HTML.replace("<button onclick=pair()>PAIR</button>",
    "<button id=pairButton type=button>PAIR</button><div id=pairStatus class=state role=status aria-live=polite></div><small>Remote UI v" + REMOTE_UI_VERSION + "</small>")
MOBILE_HTML = MOBILE_HTML.replace("async function pair(){", "async function legacyPair(){").replace(
    "pair.hidden", "document.getElementById('pair').hidden")
MOBILE_HTML = MOBILE_HTML.replace(
    "let firstAckSent=false,streaming=false,framesRequested=0,",
    "let firstAckSent=false,streaming=false,targetFps=8,frameRateDrops=0,framesDropped=0,framesRequested=0,",
).replace(
    "if(!j.monitors)return;display.innerHTML='';",
    "if(!j.monitors)return;targetFps=j.target_fps||8;display.innerHTML='';",
).replace(
    "+'\\nFrames Rendered: '+framesRendered+'\\nLast Frame HTTP Status: '",
    "+'\\nFrames Rendered: '+framesRendered+'\\nTarget FPS: '+targetFps+'\\nFrames Dropped: '+framesDropped+'\\nFrame Rate-Limit Drops: '+frameRateDrops+'\\nLast Frame HTTP Status: '",
).replace(
    "console.info('[Martin Remote] frame response',r.status);if(r.status===401)",
    "console.info('[Martin Remote] frame response',r.status);if(r.status===204){frameRateDrops++;framesDropped++;showDiag();return false}if(r.status===401)",
).replace(
    "showDiag();ok()};remoteImage.onerror",
    "showDiag();ok()};remoteImage.onerror",
).replace(
    "fail(Error('image decode failed'))};remoteImage.src=url})}catch(e)",
    "fail(Error('image decode failed'))};remoteImage.src=url});return true}catch(e)",
).replace(
    "async function frameLoop(){while(streaming){try{await fetchFrame(false);await new Promise(r=>setTimeout(r,200))}",
    "async function frameLoop(){while(streaming){if(document.hidden){await new Promise(r=>setTimeout(r,500));continue}let cycle=performance.now();try{await fetchFrame(false);let wait=Math.max(0,(1000/targetFps)-(performance.now()-cycle));if(wait)await new Promise(r=>setTimeout(r,wait))}",
).replace(
    "async function viewDesktop(){await loadDisplays();streaming=true;",
    "async function viewDesktop(){if(streaming)return;await loadDisplays();streaming=true;",
)
PAIRING_CLIENT_JS = r"""
const pairButton=document.getElementById('pairButton');
const pairStatus=document.getElementById('pairStatus');
async function submitPairing(){
  const pairingCode=document.getElementById('code').value.trim();
  const deviceName=document.getElementById('device').value.trim()||'iPhone';
  const trust=document.getElementById('trustDevice').checked;
  console.info('[Martin Remote] pair button pressed');
  pairButton.disabled=true;pairButton.textContent='PAIRING...';pairStatus.textContent='Contacting Martin PC...';
  try{
    console.info('[Martin Remote] pair request started',window.location.origin);
    const response=await fetch(new URL('/api/pair',window.location.origin),{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({code:pairingCode,device_name:deviceName,trust_device:trust})});
    console.info('[Martin Remote] pair server reached HTTP',response.status);
    const text=await response.text();let result;
    try{result=JSON.parse(text)}catch(_error){throw new Error('Server pairing error: invalid response (HTTP '+response.status+')')}
    console.info('[Martin Remote] pair response parsed',result.success?'success':'failure');
    if(!response.ok||!result.success)throw new Error('Pairing failed — HTTP '+response.status+': '+(result.error||'Unknown server error'));
    localStorage.removeItem('martinToken');token='';pairStatus.textContent='Device paired successfully.';
    document.getElementById('pair').hidden=true;document.getElementById('app').hidden=false;
    document.getElementById('trustedName').textContent=result.device_name||deviceName;poll();loadDisplays();
  }catch(error){pairStatus.textContent=error instanceof TypeError?'Unable to contact Martin PC.':error.message;console.error('[Martin Remote] pair failed',error.message)}
  finally{pairButton.disabled=false;pairButton.textContent='PAIR'}
}
pairButton.addEventListener('click',submitPairing);
"""
MOBILE_HTML = MOBILE_HTML.replace("</script></body></html>", PAIRING_CLIENT_JS + "</script></body></html>")

MOBILE_V2_CARDS = r"""
<div id=skillsPage class="card mobilePage"><h2>SKILLS</h2><div id=skillsList class=listState>Loading skills...</div></div>
<div id=crashesPage class="card mobilePage"><h2>CRASH HISTORY</h2><div id=crashList class=listState>No crashes recorded.</div><pre id=crashDetail></pre></div>
<div id=filesPage class="card mobilePage"><h2>FILES & CLIPBOARD</h2><select id=fileLocation onchange=loadFiles()><option>Martin Shared</option><option>Downloads</option><option>Desktop</option><option>Documents</option><option>Pictures</option><option>Videos</option></select><input id=fileUpload type=file multiple><button onclick=uploadFiles()>SEND TO MARTIN PC</button><div id=fileList class=listState>File transfer is disabled until enabled on Martin PC.</div><h3>CLIPBOARD</h3><textarea id=clipboardText placeholder="Clipboard text"></textarea><button onclick=sendClipboard()>SEND TO PC</button><button onclick=getClipboard()>GET FROM PC</button><h3>TRANSFERS</h3><div id=transferList></div></div>
<div id=toolsPage class="card mobilePage"><h2>COMPANION TOOLS</h2><h3>FAVORITES</h3><div id=favoriteList></div><input id=favoriteName placeholder="Application name"><button onclick=addFavorite()>ADD FAVORITE</button><h3>COMMAND HISTORY</h3><div id=commandList></div><h3>SAFE TERMINAL</h3><input id=terminalCommand placeholder="ipconfig"><button onclick=runTerminal()>RUN</button><pre id=terminalOutput>Disabled by default on Martin PC.</pre><h3>SCREENSHOT</h3><button onclick=takeScreenshot()>CAPTURE SELECTED DISPLAY</button></div>
<div id=activityPage class="card mobilePage"><h2>MARTIN ACTIVITY</h2><button onclick=markAllRead()>MARK ALL READ</button><button onclick=clearActivity()>CLEAR</button><div id=activityList class=listState>No activity yet.</div></div>
<div id=appsPage class="card mobilePage"><h2>RUNNING APPS</h2><button onclick=loadApps()>REFRESH</button><div id=appsList class=listState>Loading applications...</div></div>
<div id=settingsPage class="card mobilePage"><h2>SECURITY & CONNECTION</h2><label class=pref><input id=lowBandwidth type=checkbox onchange=saveCompanionSettings()> Low-bandwidth mode</label><select id=sessionTimeout onchange=saveCompanionSettings()><option value=5>5 minute timeout</option><option value=15 selected>15 minute timeout</option><option value=30>30 minute timeout</option><option value=60>1 hour timeout</option><option value=0>Never</option></select><button class=danger onclick=emergencyDisconnect()>DISCONNECT REMOTE SESSION</button><button onclick=testConnection()>TEST CONNECTION</button><pre id=diagnostics>Diagnostics not run.</pre><div id=versions></div><h2>TRUSTED DEVICES</h2><div id=devicesList class=listState>Loading devices...</div><h2>NOTIFICATION PREFERENCES</h2><div id=notificationPrefs></div><button onclick=savePreferences()>SAVE PREFERENCES</button></div>
<nav class=bottomNav><button onclick=go('homePage')>HOME</button><button onclick=go('remotePage')>REMOTE</button><button onclick=go('filesPage')>FILES</button><button onclick=go('activityPage')>ACTIVITY</button><button onclick=go('settingsPage')>SETTINGS</button></nav>
"""
MOBILE_V2_JS = r"""
let reconnectAttempt=0,reconnectTimer=0,lastStatusAt=0,notificationPreferences={};
function connection(state){let el=document.getElementById('connectionState');if(el){el.textContent=state;el.dataset.state=state.toLowerCase().replaceAll(' ','-')}}
function go(id){document.getElementById(id)?.scrollIntoView({behavior:'smooth',block:'start'})}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function duration(s){s=Number(s||0);return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m'}
async function mobileGet(name){return api('/api/mobile/'+name)}
async function reliablePoll(){clearTimeout(reconnectTimer);connection(reconnectAttempt?'Reconnecting':'Connecting');try{let s=await mobileGet('dashboard');if(!s.success)throw Error(s.error||'Status unavailable');lastStatusAt=Date.now();reconnectAttempt=0;connection(document.hidden?'Sleeping':'Connected');online.textContent='ONLINE';cpu.textContent=s.cpu??'--';gpu.textContent=s.gpu??'--';temp.textContent=s.gpu_temperature??'--';ram.textContent=s.ram??'--';document.getElementById('cpuTemp').textContent=s.cpu_temperature??'--';document.getElementById('ramAmount').textContent=s.ram_total_gb?((s.ram_total_gb-(s.ram_available_gb||0)).toFixed(1)+' / '+s.ram_total_gb+' GB'):'Unavailable';document.getElementById('storageFree').textContent=(s.storage_free_gb??'--')+' GB free';document.getElementById('networkRate').textContent='Down '+(s.network_download_mbps??0)+' Mbps / Up '+(s.network_upload_mbps??0)+' Mbps';ai.textContent='Ollama: '+s.ollama_status+' | Autonomy: '+(s.autonomy?'ON':'OFF');game.textContent='Game: '+(s.game||'None')+' | '+(s.game_active?'ACTIVE':'INACTIVE');opt.textContent='Optimization: '+(s.optimization?.status||'Unavailable');document.getElementById('sessionTime').textContent=s.game_active?duration(s.game_session_seconds):'--';if(!document.hidden)await loadActivity();reconnectTimer=setTimeout(reliablePoll,5000)}catch(e){online.textContent='OFFLINE';reconnectAttempt=Math.min(reconnectAttempt+1,6);connection(reconnectAttempt===1?'Reconnecting':'Offline');let delay=Math.min(30000,1000*Math.pow(2,reconnectAttempt-1))+Math.random()*500;reconnectTimer=setTimeout(reliablePoll,delay)}}
poll=reliablePoll;window.addEventListener('online',()=>{reconnectAttempt=0;reliablePoll()});window.addEventListener('offline',()=>connection('Offline'));document.addEventListener('visibilitychange',()=>{if(!document.hidden)reliablePoll();else connection('Sleeping')});
async function loadSkills(){let j=await mobileGet('skills');skillsList.innerHTML=(j.skills||[]).map(s=>'<div class=listRow><b>'+esc(s.name)+'</b><span>'+esc(s.status)+'</span><small>v'+esc(s.version)+' · Levels '+esc(s.permissions.join(', '))+'</small><button '+(['remote','security'].includes(s.id)?'disabled title="Change on Martin PC"':'')+' onclick="toggleSkill(\''+esc(s.id)+'\','+(!s.enabled)+')">'+(s.enabled?'DISABLE':'ENABLE')+'</button></div>').join('')||'No skills available.'}
async function toggleSkill(id,enabled){let j=await api('/api/mobile/skills',{skill_id:id,enabled});if(j.error)alert(j.error);loadSkills()}
async function loadCrashes(){let j=await mobileGet('crashes');let rows=j.incidents||[];crashList.innerHTML=rows.map((x,i)=>'<button class=listRow onclick="showCrash('+i+')"><b>'+esc(x.application)+'</b><span>'+esc(x.cause)+' · '+esc(x.confidence)+'</span><small>'+esc(x.timestamp||x.created_at)+'</small></button>').join('')||'No crashes recorded.';window.crashRows=rows}
function showCrash(i){let x=(window.crashRows||[])[i];if(!x)return;crashDetail.textContent=(x.explanation||'No explanation available.')+'\n\nConfidence: '+(x.confidence||'Low')+'\nExecutable: '+(x.executable||'Unavailable')+'\nFaulting module: '+(x.faulting_module||'Unavailable')+'\nException: '+(x.exception_code||'Unavailable')+'\nEvent IDs: '+((x.event_ids||[]).join(', ')||'None')}
async function loadActivity(){let j=await mobileGet('notifications');notificationPreferences=j.preferences||{};activityList.innerHTML=(j.notifications||[]).map(n=>'<div class="listRow '+(n.read?'':'unread')+'"><b>'+esc(n.title)+'</b><span>'+esc(n.message)+'</span><small>'+esc(n.timestamp)+' · '+esc(n.category)+'</small></div>').join('')||'No activity yet.';renderPrefs()}
function renderPrefs(){notificationPrefs.innerHTML=Object.entries(notificationPreferences).map(([k,v])=>'<label class=pref><input type=checkbox data-pref="'+esc(k)+'" '+(v?'checked':'')+'> '+esc(k)+'</label>').join('')}
async function savePreferences(){document.querySelectorAll('[data-pref]').forEach(x=>notificationPreferences[x.dataset.pref]=x.checked);await api('/api/mobile/notifications',{operation:'preferences',preferences:notificationPreferences});loadActivity()}
async function markAllRead(){await api('/api/mobile/notifications',{operation:'read_all'});loadActivity()}async function clearActivity(){if(confirm('Clear Martin activity?')){await api('/api/mobile/notifications',{operation:'clear'});loadActivity()}}
async function loadDevices(){let j=await mobileGet('devices');devicesList.innerHTML=(j.devices||[]).map(d=>'<div class=listRow><b>'+esc(d.name)+'</b><span>'+(d.connected?'Connected':'Offline')+'</span><small>Last seen: '+esc(d.last_seen||'Never')+'</small><button onclick="removeDevice(\''+esc(d.id)+'\',\''+esc(d.name)+'\')">REMOVE</button></div>').join('')||'No trusted devices.'}
async function removeDevice(id,name){if(confirm('Remove '+name+'? Its credential will stop working.')){let j=await api('/api/mobile/devices',{device_id:id});if(j.error)alert(j.error);loadDevices()}}
async function loadApps(){let j=await mobileGet('apps');appsList.innerHTML=(j.apps||[]).map(a=>'<div class=listRow><b>'+esc(a.name)+'</b><span>'+esc(a.instances)+' instance(s)</span><button onclick="closeApp(\''+esc(a.name)+'\')">CLOSE</button></div>').join('')||'No normal applications found.'}async function closeApp(name){let j=await api('/api/mobile/apps',{operation:'close',app_name:name});alert(j.pending?'Approval is required on Martin PC.':(j.message||j.error||'Request sent'))}
async function loadFiles(){let j=await mobileGet('files?location='+encodeURIComponent(fileLocation.value));fileList.innerHTML=j.success?(j.files||[]).map(f=>'<button class=listRow onclick="downloadFile(\''+esc(f.name)+'\')"><b>'+esc(f.name)+'</b><small>'+esc(f.directory?'Folder':f.size+' bytes')+'</small></button>').join('')||'This folder is empty.':esc(j.error);let t=await mobileGet('transfers');transferList.innerHTML=(t.transfers||[]).map(x=>'<div class=listRow><b>'+esc(x.filename)+'</b><span>'+esc(x.status)+'</span><small>'+esc(x.direction)+' · '+esc(x.size)+' bytes</small></div>').join('')||'No transfers.'}
async function uploadFiles(){for(let f of fileUpload.files){if(f.size>25*1024*1024){alert(f.name+' exceeds 25 MB');continue}let content=await new Promise(ok=>{let r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.readAsDataURL(f)});let j=await api('/api/mobile/files',{operation:'upload',filename:f.name,content});if(!j.success)alert(j.error)}loadFiles()}
async function downloadFile(name){let j=await api('/api/mobile/files',{operation:'download',location:fileLocation.value,filename:name});if(!j.success){alert(j.error);return}let a=document.createElement('a');a.href='data:application/octet-stream;base64,'+j.content;a.download=j.filename;a.click();loadFiles()}
async function sendClipboard(){let j=await api('/api/mobile/clipboard',{operation:'set',text:clipboardText.value});alert(j.message||j.error||'Clipboard sent.')}async function getClipboard(){let j=await mobileGet('clipboard');if(j.success)clipboardText.value=j.text;else alert(j.error)}
async function loadTools(){let [f,c,v,s]=await Promise.all([mobileGet('favorites'),mobileGet('commands'),mobileGet('versions'),mobileGet('settings')]);favoriteList.innerHTML=(f.favorites||[]).map(x=>'<button class=listRow onclick="openMobileApp(\''+esc(x.command)+'\')">'+esc(x.label)+'</button>').join('')||'No favorites.';commandList.innerHTML=(c.commands||[]).slice(0,20).map(x=>'<div class=listRow><b>'+esc(x.command)+'</b><span>'+(x.success?'Success':'Failed')+'</span><small>'+esc(x.timestamp)+' · Permission '+esc(x.permission_level)+'</small></div>').join('')||'No commands.';versions.textContent='Mobile '+v.mobile_version+' · Desktop '+v.desktop_version+' · Protocol '+v.remote_protocol+' · '+v.compatibility;lowBandwidth.checked=!!s.settings?.low_bandwidth;sessionTimeout.value=String(s.settings?.session_timeout_minutes??15)}
async function addFavorite(){let name=favoriteName.value.trim();if(!name)return;await api('/api/mobile/favorites',{operation:'add',favorite:{label:name,command:name}});favoriteName.value='';loadTools()}async function runTerminal(){let j=await api('/api/mobile/terminal',{command:terminalCommand.value});terminalOutput.textContent=j.output||j.error||'Completed'}
async function takeScreenshot(){let j=await mobileGet('screenshot');if(!j.success){alert(j.error);return}let a=document.createElement('a');a.href='data:image/jpeg;base64,'+j.content;a.download=j.filename;a.click()}
async function loadServices(){let j=await mobileGet('services');serviceStatus.textContent='Remote Service: '+j.remote_service+' | Session Agent: '+j.session_agent+' | Martin AI: '+j.martin_ai;startMartinButton.hidden=j.martin_ai==='Running';let adapters=j.wake?.adapters||[];let ready=adapters.find(x=>x.support==='Supported'&&x.mac);if(ready){localStorage.setItem('martinWake',JSON.stringify({pc:location.hostname,mac:ready.mac,adapter:ready.name}));wakeStatus.textContent=ready.name+' — Ethernet WoL Ready';wakeButton.disabled=false}else{wakeStatus.textContent='Wake unavailable or not verified. Configure it on Martin PC.';wakeButton.disabled=true}}
async function startMartinAI(){let j=await api('/api/mobile/services',{operation:'start_martin_ai'});alert(j.message||j.error);setTimeout(loadServices,3000)}
function wakeMartinPC(){let config;try{config=JSON.parse(localStorage.getItem('martinWake')||'null')}catch(e){}if(!config?.mac){alert('Martin PC has not provided a verified Wake-on-LAN adapter.');return}wakeStatus.textContent='Opening the phone Wake-on-LAN provider...';location.href='martin-wol://wake?mac='+encodeURIComponent(config.mac)+'&name='+encodeURIComponent(config.pc);setTimeout(()=>{wakeStatus.textContent='Waiting for Martin PC...';reconnectAttempt=0;reliablePoll()},1200)}
async function saveCompanionSettings(){await api('/api/mobile/settings',{settings:{low_bandwidth:lowBandwidth.checked,session_timeout_minutes:Number(sessionTimeout.value)}})}async function emergencyDisconnect(){if(confirm('Immediately stop remote control and all active remote features?')){await api('/api/mobile/emergency_disconnect',{});location.reload()}}async function testConnection(){let j=await api('/api/mobile/connection_test',{});diagnostics.textContent='Connection: '+j.connection+'\nAuthentication: '+j.authentication+'\nScreen capture: '+(j.screen_capture?'Passed':'Unavailable')+'\nInput: '+(j.input_service?'Available':'Disabled')+'\nFile transfer: '+(j.file_transfer?'Enabled':'Disabled')+'\nLatency: '+j.latency_ms+' ms\nProtocol: '+j.protocol}
async function shortcut(key,modifiers=[]){if(key==='CTRL_ALT_DEL'){alert('Windows blocks Ctrl+Alt+Del from normal applications. Use the PC security screen directly.');return}return input('key',{key,modifiers})}
let touchPoints=new Map(),lastPinch=0;function trackPinch(e){touchPoints.set(e.pointerId,{x:e.clientX,y:e.clientY});if(touchPoints.size!==2)return;let p=[...touchPoints.values()];let d=Math.hypot(p[0].x-p[1].x,p[0].y-p[1].y);if(lastPinch){let current=parseFloat(screen.dataset.zoom||'1');let zoom=Math.max(1,Math.min(4,current*d/lastPinch));screen.dataset.zoom=zoom;screen.style.transform='scale('+zoom+')'}lastPinch=d}desktop.addEventListener('pointerdown',trackPinch);desktop.addEventListener('pointermove',trackPinch);desktop.addEventListener('pointerup',e=>{touchPoints.delete(e.pointerId);if(touchPoints.size<2)lastPinch=0});desktop.addEventListener('pointercancel',()=>{touchPoints.clear();lastPinch=0});
loadSkills();loadCrashes();loadDevices();loadApps();loadActivity();loadFiles();loadTools();loadServices();
"""
MOBILE_HTML = MOBILE_HTML.replace("<div class=card><h2>HOME</h2>", "<div id=homePage class=card><h2>HOME</h2><div id=connectionState class=connection>Connecting</div>")
MOBILE_HTML = MOBILE_HTML.replace("<div id=settingsPage class=\"card mobilePage\"><h2>SECURITY & CONNECTION</h2>",
    "<div id=settingsPage class=\"card mobilePage\"><h2>MARTIN SERVICES</h2><div id=serviceStatus>Loading...</div><button id=startMartinButton onclick=startMartinAI()>START MARTIN AI</button><h3>REMOTE WAKE</h3><div id=wakeStatus>Wake configuration unavailable.</div><button id=wakeButton onclick=wakeMartinPC()>WAKE MARTIN PC</button><h2>SECURITY & CONNECTION</h2>")
MOBILE_HTML = MOBILE_HTML.replace("<div class=grid><div class=metric>CPU", "<div class=grid><div class=metric>CPU")
MOBILE_HTML = MOBILE_HTML.replace("</div></div></div>\n<div class=card><h2>AI / GAME MODE", "</div><div class=grid><div class=metric>CPU TEMP <span id=cpuTemp>--</span> C</div><div class=metric>RAM <span id=ramAmount>--</span></div><div class=metric>STORAGE <span id=storageFree>--</span></div><div class=metric>NETWORK <span id=networkRate>--</span></div></div></div>\n<div class=card><h2>AI / GAME MODE")
MOBILE_HTML = MOBILE_HTML.replace("<div class=card><h2>REMOTE DESKTOP</h2>", "<div id=remotePage class=card><h2>REMOTE DESKTOP</h2><div>Session <span id=sessionTime>--</span></div>")
MOBILE_HTML = MOBILE_HTML.replace("<small>Ctrl+Alt+Del is not supported.</small>", "<div class=shortcuts><button onclick=shortcut('TAB',['ALT'])>ALT+TAB</button><button onclick=shortcut('D',['WINDOWS'])>WIN+D</button><button onclick=shortcut('E',['WINDOWS'])>WIN+E</button><button onclick=shortcut('C',['CTRL'])>CTRL+C</button><button onclick=shortcut('V',['CTRL'])>CTRL+V</button><button onclick=shortcut('Z',['CTRL'])>CTRL+Z</button><button onclick=shortcut('CTRL_ALT_DEL')>CTRL+ALT+DEL</button></div><small>Ctrl+Alt+Del is restricted by Windows.</small>")
MOBILE_HTML = MOBILE_HTML.replace("<div class=card><h2>CHAT</h2>", MOBILE_V2_CARDS + "<div class=card><h2>CHAT</h2>")
MOBILE_HTML = MOBILE_HTML.replace("</style></head>", ".connection{display:inline-block;padding:5px 9px;border-radius:20px;background:#321;color:#f88;margin:6px 0}.connection[data-state=connected]{background:#15351f;color:#73e893}.mobilePage{scroll-margin-top:10px}.listRow{display:grid;grid-template-columns:1fr auto;gap:5px;width:100%;padding:10px;margin:6px 0;background:#0b0b0b;border:1px solid #522;border-radius:9px;text-align:left}.listRow small,.listRow span{color:#aaa}.listRow.unread{border-left:4px solid #f33}.pref{display:block;padding:8px}.bottomNav{position:sticky;bottom:0;z-index:20;display:grid;grid-template-columns:repeat(5,1fr);background:#090909dd;backdrop-filter:blur(8px);padding-bottom:env(safe-area-inset-bottom)}.bottomNav button{font-size:11px;padding:12px 3px;min-width:0}pre{white-space:pre-wrap;background:#090909;padding:9px;overflow:auto}.desktop:fullscreen{width:100vw;height:100vh}.desktop:fullscreen img{width:100%;height:100%;object-fit:contain;transform-origin:center}.shortcuts{display:flex;flex-wrap:wrap}@media(max-width:390px){.grid{grid-template-columns:1fr}.card{padding:10px}button{min-height:44px}} </style></head>")
MOBILE_HTML = MOBILE_HTML.replace(PAIRING_CLIENT_JS + "</script>", PAIRING_CLIENT_JS + MOBILE_V2_JS + "</script>")
MOBILE_HTML = MOBILE_HTML.replace(
    "if(r.status==401){localStorage.removeItem('martinToken');location.reload()}return r.json()",
    "if(r.status==401){localStorage.removeItem('martinToken');connection&&connection('Authentication Required');throw Error('Authentication Required')}return r.json()",
).replace(
    "}catch(e){online.textContent='OFFLINE';reconnectAttempt=",
    "}catch(e){if(e.message==='Authentication Required'){connection('Authentication Required');document.getElementById('pair').hidden=false;document.getElementById('app').hidden=true;return}online.textContent='OFFLINE';reconnectAttempt=",
).replace(
    "<button class=danger onclick=act('stop_ai',true)>STOP AI</button>",
    "<button class=danger onclick=act('sleep',true)>SLEEP</button><button class=danger onclick=act('restart',true)>RESTART</button><button class=danger onclick=act('shutdown',true)>SHUTDOWN</button><button onclick=openMobileApp('task manager')>TASK MANAGER</button><button onclick=openMobileApp('settings')>SETTINGS</button><button class=danger onclick=act('stop_ai',true)>STOP AI</button>",
).replace(
    "async function closeApp(name){",
    "async function openMobileApp(name){let j=await api('/api/mobile/apps',{operation:'open',app_name:name});if(j.error)alert(j.error)}async function closeApp(name){",
)

@dataclass
class RemoteCallbacks:
    status: Callable[[dict[str, Any]], dict[str, Any]]
    chat: Callable[[str, dict[str, Any]], dict[str, Any]]
    action: Callable[[str, bool, dict[str, Any]], dict[str, Any]]
    mobile: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None


class RateLimiter:
    def __init__(self) -> None:
        self.buckets: dict[tuple[str, str], dict[str, float]] = {}; self.denials: dict[str, int] = defaultdict(int); self.lock = threading.Lock()
    def allow(self, identity: str, bucket: str, rate_per_second: float, capacity: float) -> bool:
        now = time.monotonic(); key = (identity, bucket)
        with self.lock:
            state = self.buckets.setdefault(key, {"tokens": float(capacity), "updated": now, "accepted": 0.0})
            state["tokens"] = min(float(capacity), state["tokens"] + max(0.0, now - state["updated"]) * float(rate_per_second)); state["updated"] = now
            if state["tokens"] < 1:
                self.denials[bucket] += 1; return False
            state["tokens"] -= 1; state["accepted"] += 1; return True


class PhoneRemoteService:
    def __init__(self, callbacks: RemoteCallbacks, *, settings: dict[str, Any] | None = None,
                 state_file: Path = STATE_FILE, clock: Callable[[], float] = time.time) -> None:
        self.callbacks, self.settings = callbacks, {**DEFAULT_REMOTE_SETTINGS, **(settings or {})}
        self.state_file, self.clock = state_file, clock; self.server: ThreadingHTTPServer | None = None
        self.capture_provider: Callable[..., Any] = self._capture_screen
        self.thread: threading.Thread | None = None; self.address = ""; self.pairing: dict[str, Any] | None = None
        self.pairing_diagnostics = {"requests": 0, "last_request": None, "last_result": "NONE", "last_failure": "None", "code_state": "NONE"}
        self.limiter = RateLimiter(); self._lock = threading.RLock(); self.devices = self._load_devices()
        self.control_device_id: str | None = None; self.control_device_name = ""; self.control_last_input = 0.0
        self.input_sequences: dict[str, int] = {}
        self.monitors: list[dict[str, Any]] = []; self.monitor_selections: dict[str, str] = {}; self.monitor_capture_status: dict[str, dict[str, Any]] = {}
        self._monitors_checked_at = 0.0; self.refresh_monitors(force=True)
        self.viewers: dict[str, float] = {}; self.stream_stats = {"frames": 0, "bytes": 0, "started": 0.0,
            "capture_ms": 0.0, "encode_ms": 0.0, "last_frame_at": 0.0, "quality": self.settings.get("remote_quality", "Balanced"),
            "authenticated_requests": 0, "client_acks": 0, "last_client_ack": 0.0, "last_client_error": "",
            "frame_requests": 0, "frame_rate_drops": 0, "frames_dropped": 0, "mouse_events": 0,
            "mouse_coalesced": 0, "mouse_started": 0.0, "last_rate_limited_endpoint": "", "target_fps": 0}
        self.latest_frame: bytes | None = None; self.latest_frame_id = 0; self.latest_frame_monitor = ""
        self._capture_inflight = False; self.frame_request_log: deque[dict[str, Any]] = deque(maxlen=60)
        self.sessions: dict[str, dict[str, Any]] = {}
        self.companion = MobileCompanionService(root=self.state_file.parent, clock=clock)
        self.camera_device_id: str | None = None; self.audio_device_id: str | None = None; self.terminal_device_id: str | None = None
        self.notifications: deque[dict[str, Any]] = deque(maxlen=300)
        self.notification_cooldowns: dict[str, float] = {}
        migrated = False
        for device in self.devices:
            if "permissions" not in device:
                device["permissions"] = dict(DEFAULT_DEVICE_PERMISSIONS); migrated = True
            else:
                device["permissions"] = {**DEFAULT_DEVICE_PERMISSIONS, **device["permissions"]}
        if migrated: self._save()

    @staticmethod
    def local_ipv4() -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: sock.connect(("192.0.2.1", 9)); value = sock.getsockname()[0]
        except OSError:
            try: value = socket.gethostbyname(socket.gethostname())
            except OSError: return None
        finally: sock.close()
        try: return value if ipaddress.ip_address(value).is_private and not ipaddress.ip_address(value).is_loopback else None
        except ValueError: return None

    @staticmethod
    def _hash(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()
    def create_session(self, device_id: str) -> str:
        token = secrets.token_urlsafe(32); digest = self._hash(token)
        with self._lock:
            self.sessions[digest] = {"device_id": device_id, "created": self.clock(), "expires": self.clock() + 43200}
        return token
    def _touch_device(self, device: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            device["last_request_monotonic"] = time.monotonic()
            if self.clock() - float(device.get("last_seen_epoch", 0)) >= 30:
                device["last_connected"] = datetime_now(); device["last_seen_epoch"] = self.clock(); self._save()
        return device
    def _invalidate_device_sessions(self, device_id: str) -> None:
        with self._lock:
            for digest, session in list(self.sessions.items()):
                if session.get("device_id") == device_id: self.sessions.pop(digest, None)
    def _load_devices(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8")); return data.get("devices", []) if isinstance(data, dict) else []
        except (OSError, ValueError, json.JSONDecodeError): return []
    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True); temp = self.state_file.with_suffix(".tmp")
        temp.write_text(json.dumps({"devices": self.devices}, indent=2), encoding="utf-8"); temp.replace(self.state_file)

    def start(self) -> dict[str, Any]:
        if self.server: return self.get_status()
        host = self.local_ipv4()
        if not host: return {"success": False, "error": "No private local-network IPv4 address is available."}
        port = int(self.settings.get("port", 8765)); service = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None: return
            def _json(self, status: int, data: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
                body = json.dumps(data, default=str).encode(); self.send_response(status); self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
                if getattr(self, "_new_session", ""): self.send_header("Set-Cookie", f"martin_session={self._new_session}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000")
                if getattr(self, "_trusted_token", ""): self.send_header("Set-Cookie", f"martin_trusted={self._trusted_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
                if getattr(self, "_clear_trusted", False): self.send_header("Set-Cookie", "martin_trusted=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                for key, value in (extra_headers or {}).items(): self.send_header(key, value)
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            def _body(self) -> dict[str, Any]:
                try:
                    declared = int(self.headers.get("Content-Length", 0))
                    if declared < 0 or declared > MAX_TRANSFER_BYTES * 2: return {}
                    length = declared
                    return json.loads(self.rfile.read(length)) if length else {}
                except (ValueError, json.JSONDecodeError): return {}
            def _image(self, status: int, payload: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status); self.send_header("Content-Type", "image/jpeg"); self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0"); self.send_header("X-Content-Type-Options", "nosniff")
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
            def _static(self, payload: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
                self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "public, max-age=86400" if content_type == "image/png" else "no-cache")
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
            def _no_content(self, headers: dict[str, str] | None = None) -> None:
                self.send_response(204); self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items(): self.send_header(key, value)
                self.end_headers()
            def _device(self) -> dict[str, Any] | None:
                cookie = self.headers.get("Cookie", "")
                session_token = next((part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("martin_session=")), "")
                if session_token:
                    session_digest = service._hash(session_token); session = service.sessions.get(session_digest)
                    if session and service.clock() < float(session.get("expires", 0)):
                        self._session_digest = session_digest
                        device = next((row for row in service.devices if row.get("id") == session.get("device_id") and not row.get("revoked")), None)
                        if device:
                            session["expires"] = service.clock() + 43200
                            return service._touch_device(device)
                    service.sessions.pop(session_digest, None)
                value = self.headers.get("Authorization", ""); token = value[7:] if value.startswith("Bearer ") else ""
                if not token:
                    token = next((part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("martin_trusted=")), "")
                if not token:
                    token = next((part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("martin_device=")), "")
                if not token: return None
                digest = service._hash(token)
                with service._lock:
                    device = next((row for row in service.devices if hmac.compare_digest(row.get("token_hash", ""), digest) and not row.get("revoked")), None)
                if not device: return None
                self._new_session = service.create_session(str(device["id"]))
                self._trusted_token = token
                return service._touch_device(device)
            def do_GET(self) -> None:
                parsed = urlparse(self.path); path = parsed.path
                if path == "/":
                    body = MOBILE_HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
                if path == "/manifest.webmanifest":
                    self._static(json.dumps(PWA_MANIFEST, separators=(",", ":")).encode(), "application/manifest+json"); return
                if path == "/service-worker.js":
                    self._static(SERVICE_WORKER_JS, "text/javascript; charset=utf-8", {"Service-Worker-Allowed": "/"}); return
                if path == "/offline": self._static(OFFLINE_HTML, "text/html; charset=utf-8"); return
                icon_files = {"/pwa/martin-ai-180.png": "martin-ai-180.png", "/pwa/martin-ai-192.png": "martin-ai-192.png",
                              "/pwa/martin-ai-512.png": "martin-ai-512.png"}
                if path in icon_files:
                    try: payload = (PWA_ASSET_DIR / icon_files[path]).read_bytes()
                    except OSError: self._json(404, {"error": "PWA icon unavailable"}); return
                    self._static(payload, "image/png"); return
                device = self._device()
                if not device:
                    if not service.limiter.allow(self.client_address[0], "auth", 10 / 60, 10): self._json(429, {"error": "Authentication attempts temporarily limited.", "category": "AUTH"}); return
                    self._json(401, {"error": "Device authorization revoked. Pair this device again."}); return
                if path == "/api/desktop/stream":
                    if not service.has_permission(device, "view_screen"):
                        LOGGER.info("Remote Desktop view denied for device %s", device.get("name")); self._json(403, {"error": PERMISSION_DENIALS["view_screen"]}); return
                    service.stream_stats["authenticated_requests"] += 1
                    service.stream_desktop(self, device); return
                if path in {"/api/desktop/frame", "/api/desktop/frame-test"}:
                    if not service.has_permission(device, "view_screen"):
                        LOGGER.info("Desktop frame request device=%s auth=PASS permission=DENIED HTTP=403", device.get("name"))
                        self._json(403, {"error": PERMISSION_DENIALS["view_screen"]}); return
                    service.stream_stats["authenticated_requests"] += 1
                    quality_name, profile = service._quality(device); target_fps = int(profile["fps"])
                    with service._lock:
                        service.stream_stats["frame_requests"] += 1; service.stream_stats["target_fps"] = target_fps
                    frame_rate = min(30, max(12, target_fps * 2))
                    if not service.limiter.allow(str(device["id"]), "screen_frames", frame_rate, max(20, min(30, frame_rate * 1.5))):
                        with service._lock:
                            service.stream_stats["frame_rate_drops"] += 1; service.stream_stats["frames_dropped"] += 1
                            service.stream_stats["last_rate_limited_endpoint"] = path
                        self._no_content({"X-Martin-Frame-Dropped": "rate-limit"}); return
                    requested_monitor = parse_qs(parsed.query).get("monitor_id", [""])[0]
                    current_monitor = service.selected_monitor(device)
                    if requested_monitor and (not current_monitor or current_monitor.get("id") != requested_monitor):
                        selection = service.select_monitor(device, requested_monitor)
                        if not selection.get("success"):
                            LOGGER.info("Desktop frame request device=%s auth=PASS monitor=%s HTTP=400 error=%s", device.get("name"), requested_monitor, selection.get("error"))
                            self._json(400, {"error": selection.get("error")}); return
                    selected = service.selected_monitor(device)
                    LOGGER.info("Desktop frame request device=%s auth=PASS monitor=%s capture=ATTEMPT", device.get("name"), selected.get("label") if selected else "none")
                    try: result = service.capture_jpeg_with_timeout(device, diagnostic=path.endswith("frame-test"))
                    except Exception as exc:
                        LOGGER.exception("Desktop frame capture unhandled failure device=%s", device.get("name"))
                        self._json(500, {"error": f"Capture failed: {exc}"}); return
                    if not result.get("success"):
                        status_code = 504 if result.get("timeout") else 503
                        LOGGER.error("Desktop frame request device=%s auth=PASS monitor=%s capture=FAIL HTTP=%s error=%s", device.get("name"),
                                     selected.get("label") if selected else "none", status_code, result.get("error"))
                        service.record_frame_request(device, selected, result, status_code)
                        self._json(status_code, {"error": result.get("error", "Capture failed")}); return
                    with service._lock:
                        service.stream_stats["frames"] += 1; service.stream_stats["bytes"] += len(result["payload"])
                        if not service.stream_stats["started"]: service.stream_stats["started"] = time.monotonic()
                    LOGGER.info("Desktop frame request device=%s auth=PASS monitor=%s capture=PASS resolution=%sx%s JPEG=PASS bytes=%s HTTP=200",
                                device.get("name"), result["monitor_label"], result["width"], result["height"], len(result["payload"]))
                    service.record_frame_request(device, result["monitor"], result, 200)
                    self._image(200, result["payload"], {"X-Martin-Frame-Id": str(result["frame_id"]),
                                "X-Martin-Monitor-Id": str(result["monitor"]["id"]), "Access-Control-Expose-Headers": "X-Martin-Frame-Id, X-Martin-Monitor-Id"}); return
                if path == "/api/desktop/monitors": self._json(200, service.monitor_payload(device)); return
                if path == "/api/status":
                    result = service.callbacks.status(device)
                    if not service.has_permission(device, "game_status"):
                        result = {**result, "game": None, "game_active": False, "optimization": {"status": "Unavailable", "bottleneck": "Unavailable"}}
                    self._json(200, result); return
                if path.startswith("/api/mobile/"):
                    resource = path.removeprefix("/api/mobile/")
                    if resource == "notifications": self._json(200, service.notification_payload(device)); return
                    if resource == "devices": self._json(200, service.trusted_devices_payload()); return
                    internal = service.mobile_get(resource, parse_qs(parsed.query), device)
                    if internal is not None: self._json(200, internal); return
                    if service.callbacks.mobile:
                        self._json(200, service.callbacks.mobile(resource, {}, device)); return
                    self._json(404, {"error": "Mobile capability unavailable"}); return
                self._json(404, {"error": "Not found"})
            def do_POST(self) -> None:
                path, body, peer = urlparse(self.path).path, self._body(), self.client_address[0]
                if path == "/api/pair":
                    service.pairing_diagnostics["requests"] += 1; service.pairing_diagnostics["last_request"] = datetime_now()
                    LOGGER.info("Phone Remote pair request received from local network peer=%s", peer)
                    if not service.limiter.allow(peer, "pairing", 5 / 60, 5):
                        service.pairing_diagnostics.update({"last_result": "FAILURE", "last_failure": "Too many pairing attempts"})
                        LOGGER.warning("Phone Remote pair request rejected: rate limit peer=%s", peer); self._json(429, {"error": "Too many pairing attempts"}); return
                    result = service.complete_pairing(str(body.get("code", "")).strip(), str(body.get("device_name", "Phone")).strip()[:80], peer)
                    headers = None
                    if result.get("token"):
                        self._new_session = service.create_session(str(result["device_id"]))
                        if bool(body.get("trust_device", True)): self._trusted_token = result["token"]
                    status_code = 200 if result.get("success") else {"expired": 410, "used": 409, "inactive": 409, "invalid": 403}.get(str(result.get("error_kind")), 400)
                    LOGGER.info("Phone Remote pair request %s peer=%s reason=%s", "accepted" if result.get("success") else "rejected", peer,
                                "success" if result.get("success") else result.get("error"))
                    self._json(status_code, result, headers); return
                if path == "/api/session":
                    device = self._device()
                    if not device: self._json(401, {"success": False, "error": "Trusted-device credential is invalid or revoked"}); return
                    self._json(200, {"success": True, "device_id": device["id"], "device_name": device.get("name"),
                                     "permissions": device.get("permissions", {}), "trusted": True}); return
                device = self._device()
                if not device:
                    if not service.limiter.allow(peer, "auth", 10 / 60, 10): self._json(429, {"error": "Authentication attempts temporarily limited.", "category": "AUTH"}); return
                    LOGGER.warning("Phone Remote failed authentication from local peer %s", peer); self._json(401, {"error": "Device authorization revoked. Pair this device again."}); return
                identity = str(device["id"])
                if path == "/api/chat":
                    if not service.limiter.allow(identity, "chat", .2, 5): self._json(429, {"error": "Chat requests temporarily limited.", "category": "CHAT"}); return
                    if not service.settings.get("allow_chat"): self._json(403, {"error": "Remote chat is disabled on Martin PC."}); return
                    if not service.has_permission(device, "remote_chat"): self._json(403, {"error": "Remote chat is not enabled for this device."}); return
                    message = str(body.get("message", ""))[:2000].strip()
                    if not message: self._json(400, {"error": "Message is required"}); return
                    normalized = re.sub(r"[^a-z0-9 ]+", "", message.lower()).strip()
                    game_commands = {"optimization status", "game performance", "what did you optimize", "optimize game",
                                     "reoptimize game", "stop game optimization", "restore game settings",
                                     "what game am i playing", "game status", "current game"}
                    if normalized in game_commands and not service.settings.get("allow_game_controls"):
                        self._json(403, {"error": "Remote game controls are disabled on Martin PC."}); return
                    if normalized in game_commands and not service.has_permission(device, "game_controls"):
                        self._json(403, {"error": "Game controls are not enabled for this device."}); return
                    started = time.perf_counter(); result = service.callbacks.chat(message, device)
                    service.companion.record_command(message, result, skill=str(result.get("intent", "")),
                                                     capability=str(result.get("intent", "")), duration_ms=round((time.perf_counter()-started)*1000))
                    LOGGER.info("Phone Remote command executed by device %s", identity)
                    self._json(200, result); return
                if path == "/api/action":
                    if not service.settings.get("allow_pc_controls"): self._json(403, {"error": "Remote PC controls are disabled"}); return
                    action = str(body.get("action", "")); permission = ACTION_PERMISSIONS.get(action)
                    if not permission: self._json(400, {"error": "Remote action is not allowlisted"}); return
                    if not service.has_permission(device, permission):
                        result = {"success": False, "error": PERMISSION_DENIALS[permission]}
                    elif action in CONFIRMED_ACTIONS and service.settings.get("require_power_confirmation", True) and not bool(body.get("confirmed")):
                        result = {"success": False, "error": f"{PERMISSION_LABELS[permission]} requires confirmation on the phone."}
                    else:
                        category, rate, burst = ("power_actions", .1, 2) if action in CONFIRMED_ACTIONS or action == "lock" else ("normal_api", 2, 15)
                        if not service.limiter.allow(identity, category, rate, burst):
                            self._json(429, {"error": "Power actions temporarily limited." if category == "power_actions" else "Remote controls temporarily limited.",
                                             "category": "POWER ACTIONS" if category == "power_actions" else "NORMAL API"}); return
                        result = service.callbacks.action(action, bool(body.get("confirmed")), device)
                    LOGGER.info("Phone Remote device=%s action=%s permission=%s result=%s", device.get("name"), action,
                                "allowed" if service.has_permission(device, permission) else "denied",
                                "success" if result.get("success") else "failed")
                    self._json(200, result); return
                if path == "/api/input": self._json(200, service.handle_input(body, device)); return
                if path == "/api/desktop/control":
                    if not service.limiter.allow(identity, "normal_api", 2, 15): self._json(429, {"error": "Remote controls temporarily limited.", "category": "NORMAL API"}); return
                    operation = str(body.get("operation", "start"))
                    self._json(200, service.control_session(device, operation, bool(body.get("confirmed")))); return
                if path == "/api/desktop/monitor":
                    if not service.limiter.allow(identity, "normal_api", 2, 15): self._json(429, {"error": "Remote controls temporarily limited.", "category": "NORMAL API"}); return
                    self._json(200, service.select_monitor(device, str(body.get("monitor_id", "")))); return
                if path == "/api/desktop/frame-ack":
                    service.record_frame_ack(device, body); self._json(200, {"success": True}); return
                if path.startswith("/api/mobile/"):
                    resource = path.removeprefix("/api/mobile/")
                    if resource == "notifications": self._json(200, service.update_notifications(device, body)); return
                    if resource == "devices":
                        target = str(body.get("device_id", ""))
                        if target == str(device.get("id")): self._json(400, {"success": False, "error": "Use Forget This Device to remove the current phone."}); return
                        self._json(200, {"success": service.delete_device(target)}); return
                    internal = service.mobile_post(resource, body, device)
                    if internal is not None: self._json(200, internal); return
                    if service.callbacks.mobile:
                        self._json(200, service.callbacks.mobile(resource, body, device)); return
                    self._json(404, {"error": "Mobile capability unavailable"}); return
                if path in {"/api/session/disconnect", "/api/session/forget"}:
                    digest = getattr(self, "_session_digest", "")
                    if digest: service.sessions.pop(digest, None)
                    if path.endswith("forget"): self._clear_trusted = True
                    self._json(200, {"success": True}, {"Set-Cookie": "martin_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"}); return
                if not service.limiter.allow(identity, "normal_api", 2, 15):
                    self._json(429, {"error": "Normal API requests temporarily limited.", "category": "NORMAL API"}); return
                self._json(404, {"error": "Not found"})
        try:
            self.server = ThreadingHTTPServer((host, port), Handler); self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, name="MartinPhoneRemote", daemon=True); self.thread.start()
            self.address = f"http://{host}:{self.server.server_port}"; LOGGER.info("Phone Remote started on private local address %s", self.address)
            return {"success": True, **self.get_status()}
        except OSError as exc: self.server = None; return {"success": False, "error": str(exc)}

    def stop(self) -> None:
        server, self.server = self.server, None
        if server: server.shutdown(); server.server_close()
        self.address = ""; self.settings["allow_remote_input"] = False; self.disconnect_control("remote server stopped")

    def begin_pairing(self) -> dict[str, Any]:
        code = f"{secrets.randbelow(1000000):06d}"; self.pairing = {"hash": self._hash(code), "expires": self.clock() + 300}
        self.pairing_diagnostics.update({"code_state": "ACTIVE", "last_result": "NONE", "last_failure": "None"})
        return {"address": self.address, "code": code, "expires_seconds": 300}
    def complete_pairing(self, code: str, name: str, peer: str) -> dict[str, Any]:
        pairing = self.pairing
        if not pairing:
            used = self.pairing_diagnostics.get("code_state") == "USED"; reason = "Pairing code has already been used. Generate a new code on Martin PC." if used else "No active pairing code. Generate a new code on Martin PC."
            self.pairing_diagnostics.update({"last_result": "FAILURE", "last_failure": reason})
            return {"success": False, "error": reason, "error_kind": "used" if used else "inactive"}
        if self.clock() > pairing["expires"]:
            self.pairing = None; reason = "Pairing code expired. Generate a new code on Martin PC."
            self.pairing_diagnostics.update({"code_state": "EXPIRED", "last_result": "FAILURE", "last_failure": reason})
            return {"success": False, "error": reason, "error_kind": "expired"}
        if not hmac.compare_digest(pairing["hash"], self._hash(code)):
            reason = "Invalid pairing code."
            self.pairing_diagnostics.update({"last_result": "FAILURE", "last_failure": reason})
            return {"success": False, "error": reason, "error_kind": "invalid"}
        token = secrets.token_urlsafe(32); row = {"id": secrets.token_hex(8), "name": name or "Phone", "token_hash": self._hash(token),
            "paired_at": datetime_now(), "last_connected": datetime_now(), "last_seen_epoch": self.clock(), "last_address": peer,
            "last_request_monotonic": time.monotonic(),
            "permissions": dict(DEFAULT_DEVICE_PERMISSIONS), "revoked": False}
        with self._lock: self.devices.append(row); self._save(); self.pairing = None
        self.pairing_diagnostics.update({"code_state": "USED", "last_result": "SUCCESS", "last_failure": "None"})
        LOGGER.info("Phone Remote device paired: %s", row["name"]); return {"success": True, "token": token, "device_id": row["id"], "device_name": row["name"]}
    def revoke(self, device_id: str) -> bool:
        with self._lock:
            row = next((item for item in self.devices if item["id"] == device_id), None)
            if not row: return False
            row["revoked"] = True; self._save()
            self._invalidate_device_sessions(device_id)
            if self.control_device_id == device_id: self.disconnect_control("device access revoked")
            LOGGER.info("Phone Remote access revoked for device %s", row.get("name")); return True
    def delete_device(self, device_id: str) -> bool:
        with self._lock:
            row = next((item for item in self.devices if item.get("id") == device_id), None)
            if not row: return False
            self._invalidate_device_sessions(device_id)
            if self.control_device_id == device_id: self.disconnect_control("device registration deleted")
            self.viewers.pop(device_id, None); self.monitor_selections.pop(device_id, None); self.input_sequences.pop(device_id, None)
            self.devices = [item for item in self.devices if item.get("id") != device_id]; self._save()
        LOGGER.info("Phone Remote device registration deleted device=%s id=%s", row.get("name"), device_id)
        return True
    def rename_device(self, device_id: str, name: str) -> bool:
        clean_name = re.sub(r"\s+", " ", str(name)).strip()[:80]
        if not clean_name: return False
        with self._lock:
            row = next((item for item in self.devices if item.get("id") == device_id), None)
            if not row: return False
            old_name = row.get("name"); row["name"] = clean_name
            if self.control_device_id == device_id: self.control_device_name = clean_name
            self._save()
        LOGGER.info("Phone Remote device renamed id=%s old_name=%s new_name=%s", device_id, old_name, clean_name)
        return True
    def duplicate_device_groups(self) -> list[list[dict[str, Any]]]:
        groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for position, row in enumerate(self.devices):
            key = re.sub(r"\s+", " ", str(row.get("name") or "").strip()).casefold()
            if key: groups[key].append((position, row))
        return [[row for _position, row in sorted(rows, key=lambda item: (str(item[1].get("paired_at", "")), item[0]), reverse=True)]
                for rows in groups.values() if len(rows) > 1]
    def remove_duplicate_devices(self) -> list[str]:
        removed: list[str] = []
        for group in self.duplicate_device_groups():
            for row in group[1:]:
                if self.delete_device(str(row["id"])): removed.append(str(row["id"]))
        return removed
    def clear_devices(self) -> int:
        with self._lock:
            count = len(self.devices); self.sessions.clear(); self.disconnect_control("all paired devices cleared")
            self.viewers.clear(); self.monitor_selections.clear(); self.input_sequences.clear(); self.devices = []; self._save()
        LOGGER.info("Phone Remote cleared all paired device registrations count=%s", count)
        return count
    def revoke_all(self) -> None:
        with self._lock:
            for row in self.devices: row["revoked"] = True
            self.sessions.clear()
            self._save()
            self.disconnect_control("all device access revoked")
        LOGGER.info("Phone Remote access revoked for all devices")
    def disconnect_control(self, reason: str = "disabled on Martin PC") -> None:
        with self._lock:
            if self.control_device_id: LOGGER.info("Remote Desktop control ended device=%s reason=%s", self.control_device_name, reason)
            self.control_device_id = None; self.control_device_name = ""; self.control_last_input = 0.0

    def emergency_disconnect(self, *, invalidate_sessions: bool = True) -> dict[str, Any]:
        self.disconnect_control("emergency remote disconnect"); self.viewers.clear()
        self.camera_device_id = None; self.audio_device_id = None; self.terminal_device_id = None
        if invalidate_sessions: self.sessions.clear()
        LOGGER.warning("Phone Remote emergency disconnect activated")
        return {"success": True, "message": "Remote access stopped. Trusted devices were preserved."}

    def mobile_get(self, resource: str, query: dict[str, list[str]], device: dict[str, Any]) -> dict[str, Any] | None:
        if resource == "dashboard" and self.callbacks.mobile is None: return self.callbacks.status(device)
        if resource == "clipboard": return self.companion.clipboard("get", device)
        if resource == "files": return self.companion.list_files(device, query.get("location", ["Martin Shared"])[0])
        if resource == "transfers": return {"success": True, "transfers": list(reversed(self.companion.state["transfers"][-200:]))}
        if resource == "commands": return {"success": True, "commands": list(reversed(self.companion.state["commands"][-200:]))}
        if resource == "favorites": return {"success": True, "favorites": self.companion.state["favorites"]}
        if resource == "queue": return {"success": True, "queue": self.companion.pending_queue()}
        if resource == "versions": return self.companion.versions()
        if resource == "services":
            status = self.callbacks.status(device)
            return {"success": True, "remote_service": "Running", "session_agent": status.get("session_agent", "Running"),
                    "martin_ai": status.get("martin_ai", "Running"), "wake": status.get("wake", {}),
                    "remote_access": "Available"}
        if resource == "diagnostics":
            desktop = self.remote_desktop_status()
            return {"success": True, "reachable": True, "authenticated": True, "remote_service": bool(self.server),
                "screen_capture": not bool(desktop.get("last_error")), "input_service": bool(self.settings.get("allow_remote_input")),
                "selected_display": desktop.get("monitor_label"), "notifications": True, "protocol": PROTOCOL_VERSION,
                "last_error": desktop.get("last_error", ""), "file_transfer": self.companion.settings["file_transfer_enabled"],
                "sanitized": True}
        if resource == "settings": return {"success": True, "settings": self.companion.settings}
        if resource == "screenshot":
            if not self.has_permission(device, "screenshots"): return {"success": False, "error": "Screenshot permission is denied."}
            result = self.capture_jpeg_with_timeout(device, diagnostic=True)
            return ({"success": True, "filename": f"martin-{int(self.clock())}.jpg", "content": base64.b64encode(result["payload"]).decode(),
                     "monitor": result.get("monitor_label")} if result.get("success") else result)
        if resource == "camera": return {"success": True, "active": self.camera_device_id == str(device.get("id")),
            "enabled": self.companion.settings["camera_enabled"], "note": "Live camera requires an available OpenCV camera provider."}
        if resource == "audio": return {"success": True, "active": self.audio_device_id == str(device.get("id")),
            "enabled": self.companion.settings["audio_enabled"], "available": False,
            "note": "System-audio loopback codec is unavailable in this build."}
        return None

    def mobile_post(self, resource: str, body: dict[str, Any], device: dict[str, Any]) -> dict[str, Any] | None:
        if resource == "clipboard": return self.companion.clipboard(str(body.get("operation", "")), device, str(body.get("text", "")))
        if resource == "files":
            operation = str(body.get("operation", ""))
            if operation == "upload": return self.companion.upload(device, str(body.get("filename", "")), str(body.get("content", "")))
            if operation == "download": return self.companion.download(device, str(body.get("location", "Martin Shared")), str(body.get("filename", "")))
            if operation == "cancel": return self.companion.cancel_transfer(str(body.get("transfer_id", "")))
            return {"success": False, "error": "Unsupported file operation"}
        if resource == "terminal":
            self.terminal_device_id = str(device.get("id"))
            try: return self.companion.terminal(device, str(body.get("command", "")))
            finally: self.terminal_device_id = None
        if resource == "queue": return self.companion.queue(device, str(body.get("command", "")), ttl_seconds=int(body.get("ttl_seconds", 600)))
        if resource == "favorites":
            operation, favorite = str(body.get("operation", "")), body.get("favorite", {})
            if operation == "add" and isinstance(favorite, dict): self.companion.state["favorites"].append({"id": secrets.token_hex(6), "label": str(favorite.get("label", ""))[:80], "command": str(favorite.get("command", ""))[:200]})
            elif operation == "remove": self.companion.state["favorites"] = [row for row in self.companion.state["favorites"] if row.get("id") != body.get("id")]
            elif operation == "reorder" and isinstance(body.get("ids"), list):
                order = {value: index for index, value in enumerate(body["ids"])}; self.companion.state["favorites"].sort(key=lambda row: order.get(row.get("id"), 9999))
            else: return {"success": False, "error": "Unsupported favorite operation"}
            self.companion._save(); return {"success": True, "favorites": self.companion.state["favorites"]}
        if resource == "settings":
            # A phone may tune its session, but may never enable privileged PC features.
            allowed = {"low_bandwidth", "session_timeout_minutes", "require_unlock_launch", "require_unlock_remote",
                       "require_unlock_terminal", "require_unlock_power"}
            supplied = body.get("settings", {}) if isinstance(body.get("settings"), dict) else {}
            self.companion.settings.update({key: value for key, value in supplied.items() if key in allowed}); self.companion._save()
            return {"success": True, "settings": self.companion.settings}
        if resource == "camera":
            if not self.companion.settings["camera_enabled"] or not self.has_permission(device, "camera"): return {"success": False, "error": "Camera is disabled or not permitted."}
            if body.get("operation") == "stop": self.camera_device_id = None; return {"success": True, "active": False, "message": "Camera stopped."}
            return {"success": False, "error": "No camera streaming provider is installed in this build."}
        if resource == "audio":
            if body.get("operation") == "stop": self.audio_device_id = None; return {"success": True, "active": False}
            return {"success": False, "error": "PC audio streaming is not available in this build."}
        if resource == "emergency_disconnect": return self.emergency_disconnect()
        if resource == "connection_test":
            start = time.perf_counter(); diagnostics = self.mobile_get("diagnostics", {}, device) or {}
            return {**diagnostics, "latency_ms": round((time.perf_counter()-start)*1000, 1), "connection": "Passed", "authentication": "Passed"}
        if resource == "services":
            if body.get("operation") != "start_martin_ai": return {"success": False, "error": "Unsupported service operation"}
            if not self.has_permission(device, "start_martin_ai"): return {"success": False, "error": "Start Martin AI permission is denied."}
            return self.callbacks.action("start_martin_ai", True, device)
        return None
    def disable_input(self) -> None:
        self.settings["allow_remote_input"] = False; self.disconnect_control("input disabled"); LOGGER.info("Phone Remote input disabled")
    @staticmethod
    def has_permission(device: dict[str, Any], permission: str) -> bool:
        return bool(device.get("permissions", {}).get(permission, DEFAULT_DEVICE_PERMISSIONS.get(permission, False)))
    def update_permissions(self, device_id: str, permissions: dict[str, bool]) -> bool:
        with self._lock:
            device = next((row for row in self.devices if row.get("id") == device_id and not row.get("revoked")), None)
            if not device: return False
            device["permissions"] = {key: bool(permissions.get(key, device.get("permissions", {}).get(key, default)))
                                     for key, default in DEFAULT_DEVICE_PERMISSIONS.items()}
            self._save()
            if self.control_device_id == device_id and not device["permissions"].get("remote_input"): self.disconnect_control("device permission revoked")
        LOGGER.info("Phone Remote permissions updated for device %s", device.get("name")); return True
    def get_status(self) -> dict[str, Any]:
        self._expire_control()
        connected = [row["name"] for row in self.devices if not row.get("revoked") and self.clock() - float(row.get("last_seen_epoch", 0)) < 15]
        return {"running": bool(self.server), "security": "CONNECTED" if self.server and connected else ("LOCAL ONLY" if self.server else "OFF"), "address": self.address,
                "port": self.server.server_port if self.server else int(self.settings.get("port", 8765)),
                "paired_devices": len([row for row in self.devices if not row.get("revoked")]),
                "connected_devices": connected,
                "devices": [{key: row.get(key) for key in ("id", "name", "paired_at", "last_connected", "permissions", "revoked")} for row in self.devices],
                "remote_input": bool(self.settings.get("allow_remote_input")),
                "active_features": {"control": bool(self.control_device_id), "camera": bool(self.camera_device_id),
                                    "audio": bool(self.audio_device_id), "terminal": bool(self.terminal_device_id)}}

    def trusted_devices_payload(self) -> dict[str, Any]:
        now = self.clock()
        return {"success": True, "devices": [{"id": row.get("id"), "name": row.get("name"),
            "paired_at": row.get("paired_at"), "last_seen": row.get("last_connected"),
            "connected": not row.get("revoked") and now - float(row.get("last_seen_epoch", 0)) < 15,
            "revoked": bool(row.get("revoked"))} for row in self.devices]}

    def add_notification(self, category: str, title: str, message: str, *, severity: str = "Informational",
                         key: str | None = None, cooldown: int = 300, details: dict[str, Any] | None = None) -> bool:
        identity = key or f"{category}:{title}:{message}"
        now = self.clock()
        if now - self.notification_cooldowns.get(identity, 0) < max(1, cooldown): return False
        self.notification_cooldowns[identity] = now
        self.notifications.appendleft({"id": secrets.token_hex(8), "timestamp": datetime_now(), "category": category,
            "title": title, "message": message, "severity": severity, "key": identity,
            "details": details or {}, "read_by": []})
        return True

    def notification_payload(self, device: dict[str, Any]) -> dict[str, Any]:
        device_id = str(device.get("id")); preferences = {**{
            "Crash": True, "High Temperature": True, "Low Disk Space": True, "Security Alert": True,
            "Repair Completed": True, "Device Disconnected": True, "Game Detected": False,
            "Optimization Started": False, "Optimization Finished": False, "Martin PC Offline": True,
            "Martin PC Online": True}, **device.get("notification_preferences", {})}
        rows = [{**row, "read": device_id in row.get("read_by", [])} for row in self.notifications if preferences.get(row["category"], True)]
        return {"success": True, "notifications": rows, "unread": sum(not row["read"] for row in rows), "preferences": preferences}

    def update_notifications(self, device: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        operation, device_id = str(body.get("operation", "")), str(device.get("id"))
        if operation == "preferences":
            supplied = body.get("preferences", {}) if isinstance(body.get("preferences"), dict) else {}
            device["notification_preferences"] = {str(key)[:60]: bool(value) for key, value in supplied.items()}; self._save()
        elif operation in {"read", "read_all"}:
            selected = str(body.get("id", ""))
            for row in self.notifications:
                if operation == "read_all" or row["id"] == selected:
                    if device_id not in row["read_by"]: row["read_by"].append(device_id)
        elif operation == "clear":
            self.notifications.clear()
        else: return {"success": False, "error": "Unsupported notification operation"}
        return self.notification_payload(device)
    def pairing_debug(self) -> str:
        if self.pairing and self.clock() > float(self.pairing.get("expires", 0)):
            self.pairing = None; self.pairing_diagnostics["code_state"] = "EXPIRED"
        state = "ACTIVE" if self.pairing else str(self.pairing_diagnostics.get("code_state", "NONE"))
        rows = self.pairing_diagnostics
        return ("PAIRING DEBUG\n\n"
                f"Phone Remote Server: {'RUNNING' if self.server else 'STOPPED'}\n"
                f"Pair Endpoint: {'READY' if self.server else 'UNAVAILABLE'}\n"
                f"Pairing Code: {state}\n"
                f"Pairing Requests Received: {rows.get('requests', 0)}\n"
                f"Last Pair Request: {rows.get('last_request') or 'None'}\n"
                f"Last Pair Result: {rows.get('last_result', 'NONE')}\n"
                f"Last Failure Reason: {rows.get('last_failure', 'None')}\n"
                f"Remote UI: v{REMOTE_UI_VERSION}")

    def _expire_control(self) -> None:
        configured = int(self.companion.settings.get("session_timeout_minutes", self.settings.get("control_timeout_minutes", 15)))
        timeout = max(1, configured) * 60
        if not self.control_device_id: return
        if configured == 0: return
        if time.monotonic() - self.control_last_input > timeout: self.disconnect_control("inactivity timeout"); return
        device = next((row for row in self.devices if row.get("id") == self.control_device_id), None)
        if device and time.monotonic() - float(device.get("last_request_monotonic", time.monotonic())) > 12:
            self.disconnect_control("device connection lost")

    @staticmethod
    def interactive_desktop() -> bool:
        if os.name != "nt": return False
        try:
            user32 = ctypes.windll.user32; handle = user32.OpenInputDesktop(0, False, 0x0100)
            if not handle: return False
            user32.CloseDesktop(handle); return True
        except (AttributeError, OSError): return False

    def control_session(self, device: dict[str, Any], operation: str, confirmed: bool = False) -> dict[str, Any]:
        self._expire_control(); device_id, name = str(device.get("id")), str(device.get("name") or "Phone")
        if operation in {"stop", "disconnect"}:
            if self.control_device_id == device_id: self.disconnect_control("device disconnected")
            return {"success": True, "mode": "VIEW ONLY"}
        if not self.settings.get("allow_remote_input"): return {"success": False, "error": "Remote input is disabled on Martin PC."}
        if not self.has_permission(device, "remote_input"): return {"success": False, "error": PERMISSION_DENIALS["remote_input"]}
        if not self.has_permission(device, "view_screen"): return {"success": False, "error": PERMISSION_DENIALS["view_screen"]}
        if not self.interactive_desktop(): return {"success": False, "error": "PC is locked. Remote input unavailable."}
        with self._lock:
            if self.control_device_id and self.control_device_id != device_id and not (operation == "take" and confirmed):
                return {"success": False, "error": "Another device currently controls Martin PC."}
            if self.control_device_id and self.control_device_id != device_id: self.disconnect_control("control taken by confirmed device")
            self.control_device_id, self.control_device_name = device_id, name; self.control_last_input = time.monotonic()
        LOGGER.info("Remote Desktop control started device=%s", name)
        return {"success": True, "mode": "CONTROL ACTIVE", "device": name}

    def refresh_monitors(self, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and time.monotonic() - self._monitors_checked_at < 5: return self.monitors
        self._monitors_checked_at = time.monotonic(); found: list[dict[str, Any]] = []
        if os.name == "nt":
            class MonitorInfo(ctypes.Structure):
                _fields_ = (("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD))
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                               ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
            @callback_type
            def callback(handle: int, _dc: int, _rect: Any, _data: int) -> bool:
                info = MonitorInfo(); info.cbSize = ctypes.sizeof(info)
                if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                    rect = info.rcMonitor; found.append({"handle": int(handle), "x": int(rect.left), "y": int(rect.top),
                        "width": int(rect.right - rect.left), "height": int(rect.bottom - rect.top),
                        "primary": bool(info.dwFlags & 1)})
                return True
            try: ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
            except (AttributeError, OSError): found = []
        found.sort(key=lambda row: (not row["primary"], row["x"], row["y"], row["handle"]))
        for index, row in enumerate(found, 1):
            row.update({"id": f"monitor-{row['handle']:x}", "number": index,
                        "label": f"Monitor {index}" + (" — Primary" if row["primary"] else "")})
        if found:
            left = min(row["x"] for row in found); top = min(row["y"] for row in found)
            right = max(row["x"] + row["width"] for row in found); bottom = max(row["y"] + row["height"] for row in found)
            found.append({"id": "all", "number": 0, "label": "All Displays", "x": left, "y": top,
                          "width": right - left, "height": bottom - top, "primary": False, "all_displays": True})
        previous_ids = {row["id"] for row in self.monitors}; current_ids = {row["id"] for row in found}
        self.monitors = found
        if previous_ids and previous_ids != current_ids:
            LOGGER.info("Remote Desktop monitor configuration changed: %s physical display(s)", len([row for row in found if row["id"] != "all"]))
        primary = self.primary_monitor()
        for device_id, selected in list(self.monitor_selections.items()):
            if selected not in current_ids and primary: self.monitor_selections[device_id] = primary["id"]
        return found

    def primary_monitor(self) -> dict[str, Any] | None:
        return next((row for row in self.monitors if row.get("primary")),
                    next((row for row in self.monitors if row.get("id") != "all"), None))

    def selected_monitor(self, device: dict[str, Any]) -> dict[str, Any] | None:
        self.refresh_monitors(); device_id = str(device.get("id")); selected = self.monitor_selections.get(device_id) or device.get("selected_monitor")
        monitor = next((row for row in self.monitors if row["id"] == selected), None) or self.primary_monitor()
        if monitor: self.monitor_selections[device_id] = monitor["id"]
        return monitor

    def select_monitor(self, device: dict[str, Any], monitor_id: str) -> dict[str, Any]:
        self.refresh_monitors(force=True); monitor = next((row for row in self.monitors if row["id"] == monitor_id), None)
        if not monitor: return {"success": False, "error": "The selected display is no longer connected."}
        self.monitor_selections[str(device.get("id"))] = monitor_id
        device["selected_monitor"] = monitor_id; self._save()
        LOGGER.info("Remote Desktop monitor changed device=%s monitor=%s", device.get("name"), monitor["label"])
        return {"success": True, "selected_monitor": monitor_id, "label": monitor["label"]}

    def monitor_payload(self, device: dict[str, Any]) -> dict[str, Any]:
        self.refresh_monitors(force=True); selected = self.selected_monitor(device)
        quality_name, profile = self._quality(device)
        return {"success": True, "selected_monitor": selected["id"] if selected else None,
                "target_fps": int(profile["fps"]), "quality": quality_name,
                "monitors": [{key: row.get(key) for key in ("id", "number", "label", "width", "height", "x", "y", "primary", "all_displays")}
                             for row in self.monitors]}

    @staticmethod
    def _monitor_bbox(monitor: dict[str, Any]) -> tuple[int, int, int, int]:
        return (int(monitor["x"]), int(monitor["y"]), int(monitor["x"] + monitor["width"]), int(monitor["y"] + monitor["height"]))

    def _quality(self, device: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if self.companion.settings.get("low_bandwidth"):
            return "Low bandwidth", QUALITY_PROFILES["Low"]
        try: gaming = bool(self.callbacks.status(device).get("game_active"))
        except Exception: gaming = False
        name = str(self.settings.get("gaming_remote_quality" if gaming else "remote_quality", "Low" if gaming else "Balanced"))
        if name not in QUALITY_PROFILES: name = "Balanced"
        return name, dict(QUALITY_PROFILES[name])

    @staticmethod
    def _capture_screen(bbox: tuple[int, int, int, int] | None = None) -> Any:
        from PIL import ImageGrab
        try: return ImageGrab.grab(bbox=bbox, all_screens=True)
        except OSError as pillow_error:
            try: return PhoneRemoteService._capture_screen_gdi(bbox)
            except OSError as gdi_error: raise OSError(f"Pillow capture failed: {pillow_error}; Win32 GDI capture failed: {gdi_error}") from gdi_error

    @staticmethod
    def _capture_screen_gdi(bbox: tuple[int, int, int, int] | None = None) -> Any:
        from PIL import Image
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        if bbox is None:
            left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        else:
            left, top, right, bottom = map(int, bbox); width, height = right - left, bottom - top
        if width <= 1 or height <= 1: raise OSError(f"invalid capture bounds {left},{top} {width}x{height}")
        screen_dc = user32.GetDC(None)
        if not screen_dc: raise OSError("GetDC returned no desktop device context")
        memory_dc = bitmap = old_bitmap = None
        try:
            memory_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
            if not memory_dc or not bitmap: raise OSError("unable to allocate GDI capture surface")
            old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
            if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, 0x00CC0020 | 0x40000000):
                raise OSError(f"BitBlt failed with Windows error {ctypes.get_last_error()}")
            class BitmapInfoHeader(ctypes.Structure):
                _fields_ = (("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD))
            class BitmapInfo(ctypes.Structure):
                _fields_ = (("bmiHeader", BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3))
            info = BitmapInfo(); info.bmiHeader.biSize = ctypes.sizeof(BitmapInfoHeader); info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height; info.bmiHeader.biPlanes = 1; info.bmiHeader.biBitCount = 32
            data = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(memory_dc, bitmap, 0, height, data, ctypes.byref(info), 0) != height:
                raise OSError(f"GetDIBits failed with Windows error {ctypes.get_last_error()}")
            return Image.frombuffer("RGB", (width, height), bytes(data), "raw", "BGRX", 0, 1)
        finally:
            if old_bitmap and memory_dc: gdi32.SelectObject(memory_dc, old_bitmap)
            if bitmap: gdi32.DeleteObject(bitmap)
            if memory_dc: gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)

    def _capture_monitor(self, monitor: dict[str, Any]) -> Any:
        bbox = self._monitor_bbox(monitor)
        try: return self.capture_provider(bbox)
        except TypeError: return self.capture_provider()

    @staticmethod
    def _near_black_ratio(image: Any) -> float:
        sample = image.convert("RGB"); sample.thumbnail((96, 96))
        pixels = list(sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata())
        if not pixels: return 1.0
        return sum(1 for red, green, blue in pixels if red <= 12 and green <= 12 and blue <= 12) / len(pixels)

    def capture_jpeg(self, device: dict[str, Any], *, diagnostic: bool = False) -> dict[str, Any]:
        self.refresh_monitors(force=diagnostic); selected = self.selected_monitor(device)
        candidates = ([selected] if selected else []) + [row for row in self.monitors if row.get("id") != "all" and row is not selected]
        failures: list[str] = []
        for monitor in candidates:
            started = time.perf_counter()
            try:
                image = self._capture_monitor(monitor); captured = time.perf_counter()
                if not image or image.width <= 1 or image.height <= 1: raise OSError("capture returned invalid dimensions")
                black_ratio = self._near_black_ratio(image)
                if black_ratio >= .99: raise OSError(f"frame is {black_ratio:.1%} near-black")
                quality_name, profile = self._quality(device)
                encoded_image = image
                if not diagnostic and float(profile["scale"]) < 1:
                    scale = float(profile["scale"]); encoded_image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                buffer = io.BytesIO(); encoded_image.convert("RGB").save(buffer, "JPEG", quality=72 if diagnostic else int(profile["quality"]), optimize=False)
                payload = buffer.getvalue(); encoded = time.perf_counter()
                if not payload: raise OSError("JPEG encoder returned zero bytes")
                self.latest_frame_id += 1; self.latest_frame = payload; self.latest_frame_monitor = monitor["id"]
                self.monitor_selections[str(device.get("id"))] = monitor["id"]
                self.monitor_capture_status[monitor["id"]] = {"ok": True, "error": "", "last_frame": datetime_now(),
                    "width": image.width, "height": image.height, "black_ratio": round(black_ratio, 4)}
                with self._lock:
                    self.stream_stats["capture_ms"] = round((captured - started) * 1000, 1)
                    self.stream_stats["encode_ms"] = round((encoded - captured) * 1000, 1)
                    self.stream_stats["last_frame_at"] = self.clock(); self.stream_stats["selected_monitor"] = monitor["id"]
                    self.stream_stats["selected_monitor_label"] = monitor["label"]
                return {"success": True, "payload": payload, "frame_id": self.latest_frame_id, "monitor": monitor,
                        "monitor_label": monitor["label"], "width": image.width, "height": image.height,
                        "black_ratio": black_ratio, "capture_ms": round((captured - started) * 1000, 1),
                        "encode_ms": round((encoded - captured) * 1000, 1)}
            except (ImportError, OSError, ValueError) as exc:
                error = str(exc); failures.append(f"{monitor.get('label')}: {error}")
                self.monitor_capture_status[monitor["id"]] = {"ok": False, "error": error}
        error = "; ".join(failures) or "No valid physical monitor is available"
        self.stream_stats["last_error"] = error
        return {"success": False, "error": error}

    def capture_jpeg_with_timeout(self, device: dict[str, Any], *, diagnostic: bool = False, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            if self._capture_inflight: return {"success": False, "timeout": True, "error": "A previous Windows screen capture is still pending"}
            self._capture_inflight = True
        completed = threading.Event(); result: dict[str, Any] = {}
        def worker() -> None:
            try: result.update(self.capture_jpeg(device, diagnostic=diagnostic))
            except Exception as exc: result.update({"success": False, "error": f"Capture failed: {exc}"})
            finally:
                with self._lock: self._capture_inflight = False
                completed.set()
        threading.Thread(target=worker, name="MartinDesktopCapture", daemon=True).start()
        if not completed.wait(timeout): return {"success": False, "timeout": True, "error": f"Windows screen capture timed out after {timeout:g} seconds"}
        return result

    def record_frame_request(self, device: dict[str, Any], monitor: dict[str, Any] | None,
                             result: dict[str, Any], http_status: int) -> None:
        self.frame_request_log.append({"timestamp": datetime_now(), "device": str(device.get("name") or "Unknown"),
            "authenticated": True, "monitor": monitor.get("label") if monitor else "Unavailable",
            "capture": "PASS" if result.get("success") else "FAIL", "width": result.get("width", 0),
            "height": result.get("height", 0), "jpeg_bytes": len(result.get("payload", b"")),
            "http_status": http_status, "error": str(result.get("error", ""))[:500]})

    def record_frame_ack(self, device: dict[str, Any], body: dict[str, Any]) -> None:
        with self._lock:
            self.stream_stats["client_acks"] += 1; self.stream_stats["last_client_ack"] = self.clock()
            self.stream_stats["last_client_error"] = str(body.get("error", ""))[:300]
        LOGGER.info("Remote Desktop first-frame ACK device=%s frame=%s rendered=%s", device.get("name"), body.get("frame_id"), body.get("rendered"))

    def remote_desktop_self_test(self) -> str:
        self.refresh_monitors(force=True); physical = [row for row in self.monitors if row.get("id") != "all"]
        primary = self.primary_monitor(); selected_id = self.stream_stats.get("selected_monitor")
        selected = next((row for row in self.monitors if row.get("id") == selected_id), None) or primary
        test_device = {"id": "self-test"}
        if selected: self.monitor_selections["self-test"] = selected["id"]
        lines = ["REMOTE DESKTOP SELF TEST", "",
            f"1. Physical monitors detected: {'PASS' if physical else 'FAIL'} ({len(physical)})",
            f"2. Primary monitor identified: {'PASS' if primary else 'FAIL'}" + (f" ({primary['label']})" if primary else ""),
            f"3. Selected monitor valid: {'PASS' if selected else 'FAIL'}" + (f" ({selected['label']})" if selected else "")]
        result = self.capture_jpeg_with_timeout(test_device, diagnostic=True)
        lines.append(f"4. One raw frame captured: {'PASS' if result.get('success') else 'FAIL'}" + ("" if result.get("success") else f" — {result.get('error')}"))
        valid_dimensions = bool(result.get("width", 0) > 1 and result.get("height", 0) > 1)
        lines.append(f"5. Frame dimensions valid: {'PASS' if valid_dimensions else 'FAIL'}" + (f" ({result.get('width')}x{result.get('height')})" if valid_dimensions else ""))
        not_black = bool(result.get("success") and result.get("black_ratio", 1) < .99)
        lines.append(f"6. Frame not all-black: {'PASS' if not_black else 'FAIL'}" + (f" ({result.get('black_ratio', 1):.2%} near-black)" if result.get("success") else ""))
        encoded = bool(result.get("payload")); lines.append(f"7. Frame successfully encoded: {'PASS' if encoded else 'FAIL'}")
        lines.append(f"8. Encoded size greater than zero: {'PASS' if encoded else 'FAIL'}" + (f" ({len(result['payload']) // 1024} KB)" if encoded else ""))
        if encoded:
            try:
                from PIL import Image
                path = Path(tempfile.gettempdir()) / "martin_remote_desktop_self_test.jpg"; path.write_bytes(result["payload"])
                with Image.open(path) as verify: verify.load(); saved = verify.width > 1 and verify.height > 1
                lines.append(f"   Saved frame verification: {'PASS' if saved else 'FAIL'} ({path})")
            except (OSError, ValueError) as exc: lines.append(f"   Saved frame verification: FAIL — {exc}")
        endpoint_reachable = bool(self.server and self.address)
        if endpoint_reachable:
            try: urlopen(Request(self.address + "/api/desktop/frame-test"), timeout=2)
            except HTTPError as exc: endpoint_reachable = exc.code == 401
            except URLError: endpoint_reachable = False
        lines.append(f"9. Local stream endpoint reachable: {'PASS' if endpoint_reachable else 'FAIL'}")
        authenticated = int(self.stream_stats.get("authenticated_requests", 0)) > 0
        lines.append(f"10. Authenticated stream accepted session: {'PASS' if authenticated else 'FAIL'}")
        pushed = int(self.stream_stats.get("frames", 0))
        lines.append(f"11. At least one frame pushed: {'PASS' if pushed else 'FAIL'} ({pushed})")
        acks = int(self.stream_stats.get("client_acks", 0))
        lines.append(f"12. Browser first-frame ACK: {'PASS' if acks else 'FAIL'} ({acks})")
        for monitor in physical:
            try:
                image = self._capture_monitor(monitor)
                if not image or image.width <= 1 or image.height <= 1: raise OSError("invalid frame dimensions")
                ratio = self._near_black_ratio(image)
                if ratio >= .99: raise OSError(f"frame is {ratio:.1%} near-black")
                buffer = io.BytesIO(); image.convert("RGB").save(buffer, "JPEG", quality=72)
                if not buffer.getvalue(): raise OSError("JPEG encoder returned zero bytes")
                self.monitor_capture_status[monitor["id"]] = {"ok": True, "error": "", "width": image.width,
                    "height": image.height, "black_ratio": round(ratio, 4), "last_frame": datetime_now()}
            except (ImportError, OSError, ValueError) as exc:
                self.monitor_capture_status[monitor["id"]] = {"ok": False, "error": str(exc)}
            status = self.monitor_capture_status.get(monitor["id"], {})
            state = "PASS" if status.get("ok") else ("BLACK" if "near-black" in str(status.get("error")) else "ERROR")
            lines.append(f"{monitor['label']}: {state}" + (f" — {status.get('error')}" if not status.get("ok") else ""))
        return "\n".join(lines)

    def stream_desktop(self, handler: BaseHTTPRequestHandler, device: dict[str, Any]) -> None:
        device_id, name = str(device.get("id")), str(device.get("name") or "Phone")
        self.refresh_monitors(force=True); monitor = self.selected_monitor(device); first_image = None; selected_error = ""
        candidates = ([monitor] if monitor else []) + [row for row in self.monitors if row.get("id") != "all" and row is not monitor]
        for candidate in candidates:
            try:
                image = self._capture_monitor(candidate)
                if not image or image.width <= 1 or image.height <= 1: raise OSError("capture returned an invalid frame")
                extrema = image.convert("RGB").getextrema()
                if all(high <= 2 for _low, high in extrema): raise OSError("capture returned a black frame")
                first_image, monitor = image, candidate; self.monitor_selections[device_id] = candidate["id"]
                self.monitor_capture_status[candidate["id"]] = {"ok": True, "error": "", "last_frame": datetime_now()}; break
            except (ImportError, OSError) as exc:
                selected_error = str(exc); self.monitor_capture_status[candidate["id"]] = {"ok": False, "error": selected_error}
        if first_image is None:
            exc = selected_error or "No valid display is available"
            self.stream_stats["last_error"] = str(exc)
            handler._json(503, {"error": "Screen capture support is unavailable"}); return  # type: ignore[attr-defined]
        handler.send_response(200); handler.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        handler.send_header("Cache-Control", "no-store, no-cache"); handler.send_header("X-Accel-Buffering", "no"); handler.end_headers()
        with self._lock:
            self.viewers[device_id] = time.monotonic()
            if not self.stream_stats["started"]: self.stream_stats["started"] = time.monotonic()
        LOGGER.info("Remote Desktop viewing started device=%s monitor=%s", name, monitor["label"])
        adaptive_low = False
        pending_image = first_image
        try:
            while self.server and self.has_permission(device, "view_screen") and not device.get("revoked"):
                self.refresh_monitors(); current = self.selected_monitor(device)
                if not current: raise OSError("No connected display is available")
                monitor = current
                quality_name, profile = self._quality(device); frame_started = time.perf_counter()
                if adaptive_low: quality_name, profile = "Low (adaptive)", dict(QUALITY_PROFILES["Low"])
                image = pending_image if pending_image is not None else self._capture_monitor(monitor); pending_image = None; captured = time.perf_counter()
                if image.width <= 1 or image.height <= 1: raise OSError("capture returned an invalid frame")
                scale = float(profile["scale"])
                if scale < 1: image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
                buffer = io.BytesIO(); image.convert("RGB").save(buffer, "JPEG", quality=int(profile["quality"]), optimize=False)
                payload = buffer.getvalue(); encoded = time.perf_counter()
                handler.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload + b"\r\n")
                handler.wfile.flush(); finished = time.perf_counter()
                with self._lock:
                    stats = self.stream_stats; stats["frames"] += 1; stats["bytes"] += len(payload); stats["last_frame_at"] = self.clock()
                    stats["capture_ms"] = round((captured - frame_started) * 1000, 1); stats["encode_ms"] = round((encoded - captured) * 1000, 1); stats["quality"] = quality_name
                    stats["selected_monitor"] = monitor["id"]; stats["selected_monitor_label"] = monitor["label"]
                    self.monitor_capture_status[monitor["id"]] = {"ok": True, "error": "", "last_frame": datetime_now()}
                    self.viewers[device_id] = time.monotonic()
                interval = 1 / int(profile["fps"]); elapsed = finished - frame_started
                with self._lock: self.stream_stats["capture_overhead_percent"] = round(min(100, elapsed / interval * 100), 1)
                if elapsed > interval * 1.25 and not quality_name.startswith("Low"): adaptive_low = True
                time.sleep(max(0, interval - elapsed))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        except OSError as exc:
            self.monitor_capture_status[monitor["id"]] = {"ok": False, "error": str(exc)}
            primary = self.primary_monitor()
            if primary: self.monitor_selections[device_id] = primary["id"]
        finally:
            with self._lock: self.viewers.pop(device_id, None)
            if self.control_device_id == device_id: self.disconnect_control("viewer connection lost")
            LOGGER.info("Remote Desktop viewing ended device=%s", name)

    def remote_desktop_status(self) -> dict[str, Any]:
        self._expire_control(); self.refresh_monitors(); stats = dict(self.stream_stats); elapsed = max(0.001, time.monotonic() - float(stats.get("started") or time.monotonic()))
        frames, transferred = int(stats.get("frames", 0)), int(stats.get("bytes", 0))
        mouse_elapsed = max(0.001, time.monotonic() - float(stats.get("mouse_started") or time.monotonic()))
        selected_id = stats.get("selected_monitor")
        selected = next((row for row in self.monitors if row.get("id") == selected_id), None) or self.primary_monitor()
        return {"active": bool(self.viewers or self.control_device_id), "device": self.control_device_name or next((row.get("name") for row in self.devices if row.get("id") in self.viewers), None),
                "mode": "CONTROL" if self.control_device_id else ("VIEW ONLY" if self.viewers else "INACTIVE"),
                "monitor": selected.get("number") if selected else None, "monitor_id": selected.get("id") if selected else None,
                "monitor_label": selected.get("label") if selected else "Unavailable",
                "connected_monitors": len([row for row in self.monitors if row.get("id") != "all"]),
                "stream_fps": round(frames / elapsed, 1) if frames else 0.0, "bandwidth_mbps": round(transferred * 8 / elapsed / 1_000_000, 2),
                "target_fps": stats.get("target_fps", 0), "frame_requests_per_second": round(int(stats.get("frame_requests", 0)) / elapsed, 1),
                "frames_received": frames, "frames_rendered_acknowledged": stats.get("client_acks", 0),
                "frames_dropped": stats.get("frames_dropped", 0), "frame_rate_limit_drops": stats.get("frame_rate_drops", 0),
                "mouse_events_per_second": round(int(stats.get("mouse_events", 0)) / mouse_elapsed, 1) if stats.get("mouse_events") else 0.0,
                "mouse_events_coalesced": stats.get("mouse_coalesced", 0),
                "last_rate_limited_endpoint": stats.get("last_rate_limited_endpoint", ""),
                "capture_latency_ms": stats.get("capture_ms", 0.0), "encode_latency_ms": stats.get("encode_ms", 0.0),
                "capture_overhead_percent": stats.get("capture_overhead_percent", 0.0),
                "quality": stats.get("quality", self.settings.get("remote_quality", "Balanced")),
                "last_error": stats.get("last_error", ""),
                "authenticated_stream_requests": stats.get("authenticated_requests", 0),
                "client_frame_acks": stats.get("client_acks", 0), "last_client_ack": stats.get("last_client_ack", 0.0),
                "last_client_error": stats.get("last_client_error", ""),
                "remote_input": bool(self.settings.get("allow_remote_input")), "control_active": bool(self.control_device_id),
                "viewers": len(self.viewers)}

    def remote_desktop_debug(self) -> str:
        self.refresh_monitors(force=True); status = self.remote_desktop_status()
        physical = [row for row in self.monitors if row.get("id") != "all"]
        lines = [f"Connected Monitors: {len(physical)}"]
        for monitor in physical:
            capture = self.monitor_capture_status.get(monitor["id"], {})
            if capture.get("ok") is True: capture_text = "OK"
            elif capture.get("ok") is False: capture_text = f"FAILED — {capture.get('error') or 'unknown error'}"
            else: capture_text = "NOT TESTED"
            lines.extend(["", f"Monitor {monitor['number']}:", f"Resolution: {monitor['width']}x{monitor['height']}",
                          f"Position: {monitor['x']},{monitor['y']}", f"Primary: {'Yes' if monitor['primary'] else 'No'}",
                          f"Capture: {capture_text}"])
        last_frame = "OK" if status.get("stream_fps", 0) > 0 and not status.get("last_error") else (status.get("last_error") or "NOT CAPTURED")
        lines.extend(["", f"Selected Monitor: {status.get('monitor_label', 'Unavailable')}", f"Last Frame: {last_frame}"])
        lines.extend([f"Target FPS: {status.get('target_fps', 0)}", f"Actual FPS: {status.get('stream_fps', 0)}",
                      f"Frame Requests/sec: {status.get('frame_requests_per_second', 0)}",
                      f"Frames Received: {status.get('frames_received', 0)}", f"Frames Rendered ACKs: {status.get('frames_rendered_acknowledged', 0)}",
                      f"Frames Dropped: {status.get('frames_dropped', 0)}", f"Frame Rate-Limit Drops: {status.get('frame_rate_limit_drops', 0)}",
                      f"Mouse Events/sec: {status.get('mouse_events_per_second', 0)}", f"Mouse Events Coalesced: {status.get('mouse_events_coalesced', 0)}",
                      f"Last Rate-Limited Endpoint: {status.get('last_rate_limited_endpoint') or 'None'}"])
        if self.frame_request_log:
            request = self.frame_request_log[-1]
            lines.extend(["", "Latest Frame Request:", f"Device: {request['device']}", "Auth: PASS",
                          f"Monitor: {request['monitor']}", f"Capture: {request['capture']}",
                          f"Resolution: {request['width']}x{request['height']}", f"JPEG Bytes: {request['jpeg_bytes']}",
                          f"HTTP: {request['http_status']}", f"Error: {request['error'] or 'None'}"])
        return "\n".join(lines)

    def handle_input(self, body: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.get("allow_remote_input"): return {"success": False, "error": "Remote input is disabled on Martin PC."}
        if not self.has_permission(device, "remote_input"): return {"success": False, "error": PERMISSION_DENIALS["remote_input"]}
        identity = str(device.get("id"))
        self._expire_control()
        if self.control_device_id != identity: return {"success": False, "error": "This device does not own the active remote-control session."}
        if not self.interactive_desktop(): self.disconnect_control("Windows desktop locked"); return {"success": False, "error": "PC is locked. Remote input unavailable."}
        kind = str(body.get("type", ""))
        if kind in {"move", "direct"} and "sequence" in body:
            sequence = int(body.get("sequence", 0))
            if sequence <= self.input_sequences.get(identity, -1):
                self.stream_stats["mouse_coalesced"] += 1; return {"success": True, "dropped": True, "reason": "stale movement"}
            self.input_sequences[identity] = sequence
        bucket, rate, burst, silent = {
            "move": ("mouse_movement", 60, 90, True), "direct": ("mouse_movement", 60, 90, True),
            "scroll": ("scroll", 30, 45, True), "click": ("mouse_buttons", 12, 20, False),
            "drag_start": ("mouse_buttons", 12, 20, False), "drag_end": ("mouse_buttons", 12, 20, False),
            "key": ("keyboard", 30, 60, False), "text": ("keyboard", 15, 30, False),
        }.get(kind, ("normal_api", 2, 15, False))
        if kind in {"move", "direct", "scroll"}:
            self.stream_stats["mouse_events"] += 1
            if not self.stream_stats["mouse_started"]: self.stream_stats["mouse_started"] = time.monotonic()
        if not self.limiter.allow(identity, bucket, rate, burst):
            self.stream_stats["last_rate_limited_endpoint"] = f"/api/input:{kind}"
            if silent:
                self.stream_stats["mouse_coalesced"] += 1; return {"success": True, "dropped": True, "reason": "coalesced"}
            return {"success": False, "error": f"{PERMISSION_LABELS.get('remote_input', 'Remote input')} {bucket.replace('_', ' ')} temporarily limited.", "category": bucket.upper()}
        self.control_last_input = time.monotonic()
        try:
            if kind == "move": ctypes.windll.user32.mouse_event(0x0001, max(-200, min(200, int(body.get("dx", 0)))), max(-200, min(200, int(body.get("dy", 0)))), 0, 0)
            elif kind == "direct":
                x, y = float(body.get("x", -1)), float(body.get("y", -1))
                if not (0 <= x <= 1 and 0 <= y <= 1): return {"success": False, "error": "Direct-touch coordinates must be normalized between 0 and 1"}
                monitor = self.selected_monitor(device)
                if not monitor: return {"success": False, "error": "The selected display is no longer connected."}
                virtual_x = int(monitor["x"]) + round(x * max(0, int(monitor["width"]) - 1))
                virtual_y = int(monitor["y"]) + round(y * max(0, int(monitor["height"]) - 1))
                ctypes.windll.user32.SetCursorPos(virtual_x, virtual_y)
            elif kind == "click":
                buttons = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
                flags = buttons.get(str(body.get("button", "left")))
                if not flags: return {"success": False, "error": "Unsupported mouse button"}
                for flag in flags: ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)
                if bool(body.get("double")):
                    for flag in flags: ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)
            elif kind in {"drag_start", "drag_end"}:
                ctypes.windll.user32.mouse_event(0x0002 if kind == "drag_start" else 0x0004, 0, 0, 0, 0)
            elif kind == "scroll": ctypes.windll.user32.mouse_event(0x0800, 0, 0, max(-1200, min(1200, int(body.get("amount", 0)))), 0)
            elif kind == "text":
                for char in str(body.get("text", ""))[:500]:
                    code = ctypes.windll.user32.VkKeyScanW(ord(char)); vk, shift = code & 0xff, (code >> 8) & 1
                    if shift: ctypes.windll.user32.keybd_event(0x10, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(vk, 0, 0, 0); ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
                    if shift: ctypes.windll.user32.keybd_event(0x10, 0, 2, 0)
            elif kind == "key":
                keys = {"ENTER": 0x0D, "BACKSPACE": 0x08, "TAB": 0x09, "DELETE": 0x2E, "LEFT": 0x25, "UP": 0x26,
                        "RIGHT": 0x27, "DOWN": 0x28, "ESCAPE": 0x1B, "CTRL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WINDOWS": 0x5B}
                keys.update({f"F{index}": 0x6F + index for index in range(1, 13)})
                key_name = str(body.get("key", "")).upper(); vk = keys.get(key_name)
                if not vk and len(key_name) == 1 and key_name.isalnum(): vk = ord(key_name)
                if not vk: return {"success": False, "error": "Unsupported key"}
                modifiers = [keys.get(str(value).upper()) for value in body.get("modifiers", [])]
                if any(value not in {0x11, 0x12, 0x10, 0x5B} for value in modifiers): return {"success": False, "error": "Unsupported modifier"}
                for modifier in modifiers: ctypes.windll.user32.keybd_event(modifier, 0, 0, 0)
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0); ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
                for modifier in reversed(modifiers): ctypes.windll.user32.keybd_event(modifier, 0, 2, 0)
            else: return {"success": False, "error": "Unsupported input action"}
            return {"success": True}
        except (AttributeError, OSError, ValueError, TypeError) as exc: return {"success": False, "error": str(exc)}


def datetime_now() -> str: return time.strftime("%Y-%m-%dT%H:%M:%S%z")


_PHONE_REMOTE_SERVICE: PhoneRemoteService | None = None
def configure_phone_remote(callbacks: RemoteCallbacks, **kwargs: Any) -> PhoneRemoteService:
    global _PHONE_REMOTE_SERVICE; _PHONE_REMOTE_SERVICE = PhoneRemoteService(callbacks, **kwargs); return _PHONE_REMOTE_SERVICE
def phone_remote_service() -> PhoneRemoteService | None: return _PHONE_REMOTE_SERVICE
