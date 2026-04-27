import re

with open('services/openclaw_poller.py', 'r') as f:
    content = f.read()

# Make the notification functions async
content = content.replace('def _notify_feishu_deep_analysis(record, source: str = \'\'):', 'async def _notify_feishu_deep_analysis(record, source: str = \'\'):')
content = content.replace('def _notify_feishu_deep_analysis_failed(record, reason: str = \'\'):', 'async def _notify_feishu_deep_analysis_failed(record, reason: str = \'\'):')

# Replace asyncio.run with await
content = content.replace('asyncio.run(send_feishu_deep_analysis(', 'await send_feishu_deep_analysis(')

# Update callers
content = content.replace('_notify_feishu_deep_analysis(record, original_event.source if original_event else \'\')', 'await _notify_feishu_deep_analysis(record, original_event.source if original_event else \'\')')
content = content.replace('_notify_feishu_deep_analysis_failed(record, f"请求失败: {e}")', 'await _notify_feishu_deep_analysis_failed(record, f"请求失败: {e}")')
content = content.replace('_notify_feishu_deep_analysis_failed(record, result.get(\'error\', \'未知错误\'))', 'await _notify_feishu_deep_analysis_failed(record, result.get(\'error\', \'未知错误\'))')

# What about the TCPTransport error?
# This error typically happens when sharing a connection or using an already closed asyncio loop/session.
# `services/openclaw_poller.py` -> `poll_pending_analyses` is called via `asyncio.run()` in a while loop from a Thread.
# Wait, `asyncio.run()` closes the loop when it returns.
# BUT, `_get_poll_stability` and others use `redis_client` which was created on another event loop or globally!
# The redis pool was initialized globally and is now being used in the temporary `asyncio.run` loop.

# We need to run the while loop INSIDE an asyncio event loop, instead of recreating it every iteration.
