"""测试 OC Renderer 与 Delivery。"""
import sys; sys.path.insert(0, r"C:\Users\王\dududa20-prototype")
import pytest
from dududa.core.renderer import (
    DraftResponse, FactAnchor, Persona, OCRenderer, RenderValidator, FinalResponse,
    ResponseKind, extract_atomic_facts, referenced_facts,
    unsupported_numeric_claims,
)
from dududa.core.delivery import (
    RuntimeResult, DeliveryReceipt, DeliveryStatus, RunOutcome,
    NoOpOutputAdapter, DeliveryManager, CompletionReceipt,
)
from dududa.core.envelope import Platform
from dududa.core.trace_recorder import trace_recorder


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

    def test_number_and_date_format_variants_are_preserved(self):
        validator = RenderValidator()
        draft = DraftResponse(
            text="温度 35.2℃，日期 2026-08-10",
            fact_anchors=(
                FactAnchor("temp", "35.2", kind="number", canonical="35.2"),
                FactAnchor("date", "2026-08-10", kind="date",
                           canonical="2026-08-10"),
            ),
        )
        final = FinalResponse(text="温度是 35.2 度，日期是 2026年8月10日")
        passed, errors = validator.validate(
            draft, final, Persona(persona_id="test", version="1.0"))
        assert passed, errors

    def test_text_anchor_rejects_negated_contradiction(self):
        validator = RenderValidator()
        draft = DraftResponse(
            text="今天有雨",
            fact_anchors=(FactAnchor("weather", "有雨"),),
        )
        final = FinalResponse(text="今天没有雨")
        passed, _ = validator.validate(
            draft, final, Persona(persona_id="test", version="1.0"))
        assert not passed

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


class TestStructuredFactGrounding:
    def test_structured_tool_data_becomes_typed_atomic_facts(self):
        facts = extract_atomic_facts({
            "query_city": "兰州",
            "temp_c": 35.2,
            "updated": "2026-08-30",
            "forecast": [{"desc": "小雨", "humidity": 72}],
        }, source="weather", field="mcp.weather")
        assert any(f.field.endswith("temp_c") and f.kind == "number"
                   and f.canonical == "35.2" for f in facts)
        assert any(f.kind == "date" and f.canonical == "2026-08-30"
                   for f in facts)
        selected = referenced_facts(
            "兰州 35.2 度，2026年8月30日更新，有小雨。", facts)
        assert {f.canonical for f in selected} >= {
            "兰州", "35.2", "2026-08-30", "小雨"}

    def test_unsupported_quantified_claim_is_rejected(self):
        facts = extract_atomic_facts({"score": 8.6, "reviews": 40})
        errors = unsupported_numeric_claims(
            "这门课评分 9.9 分，有40人评价。", facts)
        assert any("9.9" in item for item in errors)
        assert not any("40" in item for item in errors)

    def test_numeric_claims_cannot_cross_match_other_fields(self):
        facts = extract_atomic_facts({"score": 8.6, "review_count": 40})
        assert unsupported_numeric_claims(
            "这门课评分 8.6 分，有 40 人评价。", facts) == ()

        errors = unsupported_numeric_claims("这门课评分 40 分。", facts)
        assert any("评分 40" in item for item in errors)

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


class TestHybridRenderer:
    """2.5.8 hybrid Renderer：LLM 风格转换 + 事实锚点保持。"""

    def _persona(self):
        # 表情计数按"连续 CJK/符号段"统计，按生产 persona 上限 2 校验真实 emoji 计数
        return Persona(persona_id="t", version="1.0", name="测试",
                       max_emojis_per_message=2)

    def _draft(self):
        return DraftResponse(
            text="课程《数据结构》评分是 4.5 分，考试日期是 2026-08-10，"
                 "来源是评课社区。",
            fact_anchors=(
                FactAnchor(field="score", value="4.5"),
                FactAnchor(field="date", value="2026-08-10"),
                FactAnchor(field="source", value="评课社区"),
            ),
            citations=("评课社区",),
        )

    @pytest.mark.asyncio
    async def test_no_llm_equals_deterministic(self):
        r = OCRenderer(persona=self._persona())
        draft = self._draft()
        final = await r.render_hybrid(draft)
        assert final.text == draft.text
        assert final.fact_check_passed

    @pytest.mark.asyncio
    async def test_style_conversion_keeps_anchors(self):
        async def llm(prompt, **kw):
            assert "4.5" in prompt and "2026-08-10" in prompt
            return "嘿～《数据结构》评分 4.5 分哦，考试在 2026-08-10，" \
                   "来源评课社区～(≧▽≦)"
        r = OCRenderer(persona=self._persona(), llm=llm)
        final = await r.render_hybrid(self._draft())
        assert final.fact_check_passed
        assert final.text != self._draft().text
        assert "4.5" in final.text and "2026-08-10" in final.text

    @pytest.mark.asyncio
    async def test_anchor_dropped_repair_then_fallback(self):
        calls = []
        async def llm(prompt, **kw):
            calls.append(prompt)
            return "嘿～这门课评分被改成 5.0 分啦！"
        r = OCRenderer(persona=self._persona(), llm=llm, max_repairs=1)
        final = await r.render_hybrid(self._draft())
        assert final.text == self._draft().text   # 回退原文，事实安全
        assert final.fact_check_passed
        assert len(calls) == 2                     # 初始 + 1 次修复

    @pytest.mark.asyncio
    async def test_repair_recovers_and_uses_repaired(self):
        async def llm(prompt, **kw):
            if "上一次转换不符合要求" in prompt:
                return "嘿～评分 4.5，日期 2026-08-10，来源评课社区～"
            return "把评分改成 5.0！"
        r = OCRenderer(persona=self._persona(), llm=llm, max_repairs=1)
        final = await r.render_hybrid(self._draft())
        assert final.fact_check_passed
        assert "5.0" not in final.text
        assert "2026-08-10" in final.text

    @pytest.mark.asyncio
    async def test_llm_raises_falls_back(self):
        async def llm(prompt, **kw):
            raise RuntimeError("boom")
        r = OCRenderer(persona=self._persona(), llm=llm)
        final = await r.render_hybrid(self._draft())
        assert final.text == self._draft().text

    @pytest.mark.asyncio
    async def test_empty_output_falls_back(self):
        async def llm(prompt, **kw):
            return ""
        r = OCRenderer(persona=self._persona(), llm=llm)
        final = await r.render_hybrid(self._draft())
        assert final.text == self._draft().text

    @pytest.mark.asyncio
    async def test_emoji_limit_falls_back(self):
        async def llm(prompt, **kw):
            return "评分 4.5 分 😀😀😀😀😀"
        r = OCRenderer()   # 默认 persona 最多 2 段表情
        draft = DraftResponse(text="评分 4.5 分")
        final = await r.render_hybrid(draft)
        assert final.text == draft.text

    @pytest.mark.asyncio
    async def test_prompt_contains_anchors_and_constraints(self):
        captured = {}
        async def llm(prompt, **kw):
            captured["p"] = prompt
            return "ok"
        r = OCRenderer(persona=self._persona(), llm=llm)
        await r.render_hybrid(self._draft())
        assert "不可修改的事实锚点" in captured["p"]
        assert "4.5" in captured["p"] and "2026-08-10" in captured["p"]
        assert "不能修改数字" in captured["p"]

    @pytest.mark.asyncio
    async def test_llm_without_kwargs_supported(self):
        async def llm(prompt):
            return "评分4.5 日期2026-08-10 来源评课社区～"
        r = OCRenderer(persona=self._persona(), llm=llm)
        final = await r.render_hybrid(self._draft())
        assert final.fact_check_passed

    @pytest.mark.asyncio
    async def test_hybrid_records_trace(self):
        async def llm(prompt, **kw):
            return "评分4.5 日期2026-08-10 来源评课社区"
        r = OCRenderer(persona=self._persona(), llm=llm)
        await r.render_hybrid(self._draft(),
                              run_id="ren-run-1", trace_id="ren-tr-1")
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "render_result"
                 and x.get("run_id") == "ren-run-1"]
        assert len(lines) == 1
        assert lines[0]["passed"] is True and lines[0]["fallback"] is False

    @pytest.mark.asyncio
    async def test_hybrid_fallback_records_trace(self):
        async def llm(prompt, **kw):
            return "评分被改成 9.9"
        r = OCRenderer(persona=self._persona(), llm=llm, max_repairs=0)
        final = await r.render_hybrid(self._draft(),
                                      run_id="ren-run-2", trace_id="ren-tr-2")
        assert final.text == self._draft().text
        lines = [x for x in trace_recorder.lines_for()
                 if x.get("event") == "render_result"
                 and x.get("run_id") == "ren-run-2"]
        assert len(lines) == 1
        assert lines[0]["fallback"] is True
        assert lines[0]["errors"]

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
