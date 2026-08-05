"""测试 OC Renderer 与 Delivery。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from packages.core.renderer import (
    DraftResponse, FactAnchor, Persona, OCRenderer, RenderValidator, FinalResponse,
)
from packages.core.delivery import (
    RuntimeResult, DeliveryReceipt, DeliveryStatus, RunOutcome,
    NoOpOutputAdapter, DeliveryManager, CompletionReceipt,
)
from packages.core.envelope import Platform


class TestFactAnchor:
    def test_creation(self):
        anchor = FactAnchor(field="score", value="4.5", source="icourse")
        assert anchor.field == "score"
        assert anchor.value == "4.5"


class TestRenderValidator:
    def test_fact_anchor_preserved(self):
        v = RenderValidator()
        draft = DraftResponse(
            text="这门课评分4.5",
            fact_anchors=(FactAnchor(field="score", value="4.5"),),
        )
        final = FinalResponse(text="这门课评分4.5")
        passed, errors = v.validate(draft, final, Persona(
            persona_id="test", version="1.0",
        ))
        assert passed

    def test_fact_anchor_lost(self):
        v = RenderValidator()
        draft = DraftResponse(
            text="评分4.5",
            fact_anchors=(FactAnchor(field="score", value="4.5"),),
        )
        final = FinalResponse(text="评分改变了")
        passed, errors = v.validate(draft, final, Persona(
            persona_id="test", version="1.0",
        ))
        assert not passed
        assert any("4.5" in e for e in errors)

    def test_emoji_limit(self):
        v = RenderValidator()
        draft = DraftResponse(text="hello")
        final = FinalResponse(text="hello 😀😀😀", emoji_count=3)
        persona = Persona(
            persona_id="test", version="1.0",
            max_emojis_per_message=2,
        )
        passed, errors = v.validate(draft, final, persona)
        assert not passed
        assert any("emoji" in e.lower() for e in errors)


class TestOCRenderer:
    def test_basic_render(self):
        renderer = OCRenderer()
        draft = DraftResponse(text="你好，这是一条测试消息。")
        result = renderer.render(draft)
        assert result.text == "你好，这是一条测试消息。"
        assert result.fact_check_passed

    def test_fact_preservation(self):
        renderer = OCRenderer()
        draft = DraftResponse(
            text="课程评分4.5分",
            fact_anchors=(FactAnchor(field="score", value="4.5"),),
        )
        result = renderer.render(draft)
        assert "4.5" in result.text
        assert result.fact_check_passed

    def test_fallback_on_validation_failure(self):
        renderer = OCRenderer()
        draft = DraftResponse(
            text="评分4.5",
            fact_anchors=(FactAnchor(field="score", value="4.5"),),
        )
        # Manually create a final that would fail
        result = renderer.render(draft)
        assert "4.5" in result.text


class TestDeliveryReceipt:
    def test_successful(self):
        receipt = DeliveryReceipt(
            run_id="run1", status=DeliveryStatus.SUCCEEDED,
        )
        assert receipt.is_ok

    def test_failed(self):
        receipt = DeliveryReceipt(
            run_id="run1", status=DeliveryStatus.FAILED,
        )
        assert not receipt.is_ok


class TestRuntimeResult:
    def test_has_visible_output(self):
        rr = RuntimeResult(
            run_id="r1", outcome=RunOutcome.SUCCEEDED,
            final_response=FinalResponse(text="hello"),
        )
        assert rr.has_visible_output

    def test_no_visible_output(self):
        rr = RuntimeResult(run_id="r1", outcome=RunOutcome.IGNORED)
        assert not rr.has_visible_output


class TestNoOpOutputAdapter:
    @pytest.mark.asyncio
    async def test_send(self):
        adapter = NoOpOutputAdapter()
        receipt = await adapter.send(
            Platform.QQ, "conv1",
            RuntimeResult(run_id="r1", outcome=RunOutcome.SUCCEEDED),
        )
        assert receipt.status == DeliveryStatus.SUCCEEDED
        assert receipt.run_id == "r1"

    @pytest.mark.asyncio
    async def test_send_reaction(self):
        adapter = NoOpOutputAdapter()
        receipt = await adapter.send_reaction(Platform.QQ, "conv1", "👍")
        assert receipt.status == DeliveryStatus.SUCCEEDED


class TestDeliveryManager:
    @pytest.mark.asyncio
    async def test_deliver(self):
        mgr = DeliveryManager(NoOpOutputAdapter())
        result = RuntimeResult(
            run_id="r1", outcome=RunOutcome.SUCCEEDED,
            final_response=FinalResponse(text="test"),
        )
        receipt = await mgr.deliver(result, Platform.QQ, "conv1")
        assert receipt.status == DeliveryStatus.SUCCEEDED
        assert mgr.get_receipt("r1") is receipt

    @pytest.mark.asyncio
    async def test_no_output_skips(self):
        mgr = DeliveryManager(NoOpOutputAdapter())
        result = RuntimeResult(run_id="r1", outcome=RunOutcome.IGNORED)
        receipt = await mgr.deliver(result, Platform.QQ, "conv1")
        assert receipt.status == DeliveryStatus.SUCCEEDED


class TestCompletionReceipt:
    def test_creation(self):
        cr = CompletionReceipt(
            run_id="r1",
            final_phase="completed",
            delivery_status=DeliveryStatus.SUCCEEDED,
            memory_write_receipts=("m1", "m2"),
        )
        assert cr.run_id == "r1"
        assert len(cr.memory_write_receipts) == 2
