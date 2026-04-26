import pytest

from core.config import Config
from services import ai_analyzer


def test_parse_truncated_json_fallback_extracts_clean_lists():
    truncated = '''{
  "source": "cloud-monitor",
  "event_type": "资源告警-CPU使用率",
  "importance": "high",
  "summary": "杭州区域实例i-abc123的CPU使用率达95.5%，超过阈值80%，已触发critical级别告警",
  "actions": [
    "立即登录实例执行top/htop命令，识别占用CPU最高的进程和线程，确认是否为业务进程",
    "检查应用日志和监控面板，分析当前流量是否异常增长，是否有慢接口或错误率上升",
    "如确认为流量突增，立即执行水平扩容，增加实例数量分担负载；如为性能问题，重启异常进程或回滚最近变更"
  ],
  "risks": [
    "服务响应时间持续增加，可能导致大量请求超时和用户流失",
    "CPU持续高负载可能触发实例自动重启或宕机，造成服务中断"
  ],
  "monitoring_suggestions": [
    "设置CPU使用率的多级告警阈值（70%预警、85%警告、90%严重），实现分级响应",
    "增加进程级CPU监控，追踪Top 5消耗CPU的进程变化趋势",
    "建立CPU使用率与业务指标（QPS、并发数）的关联监控，识别异常
'''

    result = ai_analyzer._parse_ai_analysis_response(truncated, 'cloud-monitor')

    assert result['source'] == 'cloud-monitor'
    assert result['importance'] == 'high'
    assert 'CPU使用率' in result['event_type']
    assert len(result['actions']) == 3
    assert len(result['risks']) == 2
    assert all(item not in {'[', ']'} for item in result['actions'])
    assert all(item not in {'[', ']'} for item in result['risks'])


def test_extract_from_text_removes_junk_tokens():
    text = '''{
  "source": "cloud-monitor",
  "event_type": "cpu_alert",
  "importance": "high",
  "summary": "cpu high",
  "actions": ["检查进程", "["],
  "risks": ["[", "服务不可用"]
}'''

    result = ai_analyzer.extract_from_text(text, 'cloud-monitor')

    assert result['actions'] == ['检查进程']
    assert result['risks'] == ['服务不可用']


@pytest.mark.asyncio
async def test_analyze_with_openai_retries_when_finish_reason_is_length(monkeypatch):
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content, finish_reason):
            self.message = _Message(content)
            self.finish_reason = finish_reason

    class _Response:
        def __init__(self, content, finish_reason):
            self.choices = [_Choice(content, finish_reason)]

    calls = []

    async def fake_request(_client, _messages, max_tokens):
        calls.append(max_tokens)
        if len(calls) == 1:
            return _Response('{"source":"cloud-monitor","event_type":"x","importance":"high","summary":"a"}', 'length')
        return _Response('{"source":"cloud-monitor","event_type":"x","importance":"high","summary":"b"}', 'stop')

    monkeypatch.setattr(ai_analyzer, '_request_openai_completion', fake_request)
    monkeypatch.setattr(ai_analyzer, 'AsyncOpenAI', lambda **_kwargs: object())

    old_max = Config.OPENAI_MAX_TOKENS
    old_retry_max = Config.OPENAI_TRUNCATION_RETRY_MAX_TOKENS
    old_key = Config.OPENAI_API_KEY
    try:
        Config.OPENAI_API_KEY = "test"
        Config.OPENAI_MAX_TOKENS = 100
        Config.OPENAI_TRUNCATION_RETRY_MAX_TOKENS = 200
        result = await ai_analyzer.analyze_with_openai({'k': 'v'}, 'cloud-monitor')
    finally:
        Config.OPENAI_API_KEY = old_key
        Config.OPENAI_MAX_TOKENS = old_max
        Config.OPENAI_TRUNCATION_RETRY_MAX_TOKENS = old_retry_max

    assert calls == [100, 200]
    assert result['summary'] == 'b'
    assert result['importance'] == 'high'
