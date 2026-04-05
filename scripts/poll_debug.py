#!/usr/bin/env python3
"""Debug script: poll chat.history for a session_key"""
import websocket
import json
import uuid
import sys
import os
import time
import base64
import platform
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env
env_path = Path(__file__).resolve().parent.parent / '.env'
load_dotenv(env_path)

gateway_url = os.getenv('OPENOCTA_GATEWAY_URL', 'http://127.0.0.1:18900').replace('http://', 'ws://').replace('https://', 'wss://') + '/ws'
gateway_token = os.getenv('OPENOCTA_GATEWAY_TOKEN', '')
session_key = sys.argv[1] if len(sys.argv) > 1 else 'hook:deep-analysis:unknown:10d9ce63-31cf-48c1-98fa-c5c175d3cc33'

print(f"Polling session_key: {session_key}")
ws = websocket.create_connection(gateway_url, timeout=30)

# --- OpenClaw Device Auth: 尝试接收 connect.challenge 并构造设备认证 ---
device_auth = None
nonce = None
try:
    ws.settimeout(3.0)
    raw = ws.recv()
    frame = json.loads(raw)
    if frame.get('type') == 'event' and frame.get('event') == 'connect.challenge':
        nonce = frame.get('payload', {}).get('nonce', '')
        print(f"Received connect.challenge, nonce={nonce[:32]}...")
        
        device_id = os.getenv('OPENCLAW_DEVICE_ID', '')
        private_key_b64 = os.getenv('OPENCLAW_DEVICE_PRIVATE_KEY_PEM', '')
        device_token_val = os.getenv('OPENCLAW_DEVICE_TOKEN', '')
        
        if device_id and private_key_b64 and nonce:
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                
                pem = f"-----BEGIN PRIVATE KEY-----\n{private_key_b64}\n-----END PRIVATE KEY-----\n"
                private_key = serialization.load_pem_private_key(pem.encode(), password=None)
                pub_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                pub_b64url = base64.urlsafe_b64encode(pub_bytes).decode().rstrip('=')
                
                signed_at = int(time.time() * 1000)
                scopes_str = 'operator.read'
                payload_str = f"v2|{device_id}|gateway-client|cli|operator|{scopes_str}|{signed_at}|{gateway_token}|{nonce}"
                signature = private_key.sign(payload_str.encode())
                sig_b64url = base64.urlsafe_b64encode(signature).decode().rstrip('=')
                
                device_auth = {
                    'role': 'operator',
                    'scopes': ['operator.read'],
                    'device_token': device_token_val,
                    'device': {
                        'id': device_id,
                        'publicKey': pub_b64url,
                        'signature': sig_b64url,
                        'signedAt': signed_at,
                        'nonce': nonce
                    }
                }
                print("Device auth constructed successfully")
            except ImportError:
                print("WARNING: cryptography not installed, skipping device auth")
            except Exception as e:
                print(f"WARNING: Failed to build device auth: {e}")
    else:
        print(f"First frame is not connect.challenge: type={frame.get('type')}, event={frame.get('event', '')}")
except Exception:
    print("No connect.challenge received (likely OpenOcta, not OpenClaw)")

# Connect (v3 protocol)
client_platform = 'linux' if device_auth else platform.system().lower()
client_mode = 'cli' if device_auth else 'backend'
connect_msg = {
    'type': 'req', 'id': str(uuid.uuid4()), 'method': 'connect',
    'params': {
        'minProtocol': 3, 'maxProtocol': 3,
        'client': {'id': 'gateway-client', 'version': '1.0.0', 'platform': client_platform, 'mode': client_mode},
        'auth': {'token': gateway_token}
    }
}
if device_auth:
    connect_msg['params']['role'] = device_auth['role']
    connect_msg['params']['scopes'] = device_auth['scopes']
    connect_msg['params']['auth']['deviceToken'] = device_auth['device_token']
    connect_msg['params']['device'] = device_auth['device']

ws.settimeout(60)
ws.send(json.dumps(connect_msg))
resp = ws.recv()
print(f"=== CONNECT === {resp[:200]}")

# chat.history
hist_msg = {'type': 'req', 'id': str(uuid.uuid4()), 'method': 'chat.history', 'params': {'sessionKey': session_key}}
ws.send(json.dumps(hist_msg))
ws.settimeout(60)  # chat.history 需要更长时间

# 可能收到多个帧（event帧等），需要找到匹配id的res帧
request_id = hist_msg['id']
max_frames = 100
for frame_num in range(max_frames):
    resp = ws.recv()
    data = json.loads(resp)
    
    # 检查是否是chat.history的响应
    if data.get('type') == 'res' and data.get('id') == request_id:
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
        break
    else:
        # 跳过中间的事件帧
        event_name = data.get('event', '')
        print(f"[skip frame #{frame_num+1}] type={data.get('type')}, event={event_name}")
else:
    print(f"ERROR: No matching response after {max_frames} frames")

ws.close()
