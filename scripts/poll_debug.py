#!/usr/bin/env python3
"""Debug script: poll chat.history for a session_key"""
import websocket
import json
import uuid
import sys
import os
import platform
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(env_path)

gateway_url = os.getenv('OPENOCTA_GATEWAY_URL', 'http://120.25.176.44:18900').replace('http://', 'ws://').replace('https://', 'wss://') + '/ws'
gateway_token = os.getenv('OPENOCTA_GATEWAY_TOKEN', '')
session_key = sys.argv[1] if len(sys.argv) > 1 else 'hook:deep-analysis:unknown:9b46430b-bf32-485e-9b2b-a8ebe91bd0ca'

print(f"Polling session_key: {session_key}")
ws = websocket.create_connection(gateway_url, timeout=15)

# Connect (v3 protocol)
connect_msg = {
    'type': 'req', 'id': str(uuid.uuid4()), 'method': 'connect',
    'params': {
        'minProtocol': 3, 'maxProtocol': 3,
        'client': {'id': 'debug-client', 'version': '1.0.0', 'platform': platform.system().lower(), 'mode': 'backend'},
        'auth': {'token': gateway_token}
    }
}
ws.send(json.dumps(connect_msg))
resp = ws.recv()
print(f"=== CONNECT === {resp[:200]}")

# chat.history
hist_msg = {'type': 'req', 'id': str(uuid.uuid4()), 'method': 'chat.history', 'params': {'sessionKey': session_key}}
ws.send(json.dumps(hist_msg))
resp = ws.recv()

data = json.loads(resp)
# chat.history response can be in 'result' or 'payload'
result = data.get('result') or data.get('payload', {})
if isinstance(result, dict):
    messages = result.get('messages', result.get('items', []))
elif isinstance(result, list):
    messages = result
else:
    messages = []

if messages:
    print(f"\n=== MESSAGES: {len(messages)} ===")
    for i, m in enumerate(messages):
        msg = m.get('message', m)
        role = msg.get('role', '?')
        content = msg.get('content', [])
        dur = msg.get('durationMs', 'N/A')
        if isinstance(content, list):
            types = [c.get('type', '?') for c in content if isinstance(c, dict)]
            tlen = sum(len(c.get('text', '')) for c in content if isinstance(c, dict) and c.get('type') == 'text')
        else:
            types = ['str']
            tlen = len(str(content))
        print(f"  [{i}] role={role}, types={types}, text_len={tlen}, durationMs={dur}")
        
        # Print text preview for assistant messages
        if role == 'assistant' and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get('type') == 'text':
                    text = c.get('text', '')
                    if i == len(messages) - 1:
                        # Last message: print full text
                        print(f"      === FULL TEXT ({len(text)} chars) ===")
                        print(text)
                        print(f"      === END ===")
                    else:
                        preview = text[:300] + '...' if len(text) > 300 else text
                        print(f"      TEXT: {preview}")
else:
    print("No messages found. Response keys:", list(data.keys()))
    if 'payload' in data:
        print("Payload keys:", list(data['payload'].keys()) if isinstance(data['payload'], dict) else type(data['payload']))
    print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])

ws.close()
