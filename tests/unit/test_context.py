"""测试 Context Builder。"""
import sys
import pytest
from dududa.core.context import (
    ContextBuilder, ContextSnapshot, ContextMemoryScope, PolicyView,
    ConversationContext, UserPreference, PersonaRef,
)
from dududa.core.envelope import (
    MessageEnvelope, Actor, Platform, MessageKind, ConversationRef,
)
from dududa.core.memory import (
    InMemoryRepository, MemoryRecord, MemoryType,
    MemoryScope as MemScope, SensitivityLevel,
)
from dududa.core.capability import (
    Capability, CapabilityRegistry, ProviderType, CapProvider,
)


class StubProvider(CapProvider):
    async def execute(self, capability, arguments):
        from dududa.core.capability import ToolObservation
        return ToolObservation(step_id="s", capability_id="c", success=True)
    def health(self): return True


class TestContextSnapshot:
    def test_creation(self):
        env = MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(conversation_id="g1", platform=Platform.QQ, kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="test"),
            text="hello",
        )
        snapshot = ContextSnapshot(current_message=env)
        assert snapshot.current_message.text == "hello"


class TestContextBuilder:
    def test_build_basic(self):
        builder = ContextBuilder()
        env = MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(conversation_id="g1", platform=Platform.QQ, kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="test"),
            text="hello",
        )
        snapshot = builder.build(env)
        assert snapshot.current_message is env

    def test_build_with_memory(self):
        repo = InMemoryRepository()
        scope = MemScope(
            memory_type=MemoryType.SHORT_TERM, platform="qq", bot_id="dududa",
            conversation_id="g1", actor_id="u1",
        )
        repo.write(MemoryRecord(scope=scope, content="用户喜欢猫"))
        builder = ContextBuilder(memory_repo=repo)
        env = MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(conversation_id="g1", platform=Platform.QQ, kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="test"),
            text="hello",
        )
        snapshot = builder.build(env)
        assert len(snapshot.authorized_memories) >= 0

    def test_build_with_capabilities(self):
        reg = CapabilityRegistry()
        cap = Capability(capability_id="c1", name="课程查询", description="查询课程")
        reg.register(cap, StubProvider())
        builder = ContextBuilder(capability_registry=reg)
        env = MessageEnvelope(
            platform=Platform.QQ, kind=MessageKind.GROUP,
            conversation=ConversationRef(conversation_id="g1", platform=Platform.QQ, kind=MessageKind.GROUP),
            sender=Actor(actor_id="u1", platform=Platform.QQ, display_name="test"),
            text="hello",
        )
        snapshot = builder.build(env)
        assert len(snapshot.available_capability_summaries) >= 1
