"""Webhook pipeline stage ordering."""

from __future__ import annotations

from core.alert_concurrency import alert_processing_gate
from services.webhooks import pipeline_runtime, pipeline_stages
from services.webhooks.types import WebhookProcessContext


async def run_processing_pipeline(
    ctx: WebhookProcessContext, dependencies: pipeline_runtime.WebhookPipelineDependencies
) -> pipeline_runtime.PipelineProcessingResult:
    async with alert_processing_gate(ctx.alert_hash) as gate_res:
        suppressed = await pipeline_stages.validate_backpressure(ctx, gate_res)
        if suppressed is not None:
            return suppressed

        analysis, noise, analysis_res = await pipeline_stages.resolve_noise_context(ctx, dependencies)
        return await pipeline_stages.persist_and_schedule(ctx, analysis, noise, analysis_res, dependencies)
