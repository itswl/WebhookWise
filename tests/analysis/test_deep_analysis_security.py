import pytest


@pytest.mark.asyncio
async def test_forward_deep_analysis_validates_outbound_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import TARGET_URL_UNAVAILABLE_MESSAGE
    from api.v1 import deep_analysis
    from core.url_security import UnsafeTargetUrlError

    async def reject_url(url: str) -> str:
        raise UnsafeTargetUrlError("target host cannot be resolved: internal.example")

    monkeypatch.setattr("services.analysis.deep_analysis_workflow.validate_outbound_url", reject_url)

    response = await deep_analysis.forward_deep_analysis(
        1,
        {"target_url": "http://10.0.0.1/hook"},
        session=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 400
    assert TARGET_URL_UNAVAILABLE_MESSAGE.encode("utf-8") in response.body
    assert b"internal.example" not in response.body
