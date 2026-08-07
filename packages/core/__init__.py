"""嘟嘟哒 2.0 Agent Runtime Core —— 核心领域类型包。"""

from .attachment_repo import (
    AttachmentRef, AttachmentRecord, AttachmentRepository,
)
from .envelope import (
    Platform, MessageKind, AttachmentKind, Attachment,
    Actor, ConversationRef, MessageEnvelope, PreprocessedEnvelope,
)
from .state import (
    RuntimePhase, RunOutcome, SocialAction, ToolPlanStatus,
    WriteGateDecision, RuntimeBudget, ToolStep, ToolPlan, RuntimeState,
)
from .context import (
    ContextMemoryScope, PolicyView, UserPreference, PersonaRef,
    ConversationContext, ContextSnapshot, ContextBuilder,
)
from .perception import (
    EntityRef, SpeechAct, PerceptionResult,
)
from .decision import (
    DecisionReason, SocialDecision, SocialDecisionEngine,
)
from .structured_output import (
    StructuredOutputValidator, PerceptionMerger,
    merge_perception_with_model, decision_from_signal,
    KNOWN_SPEECH_ACTS,
)
from .capability import (
    CapabilityRisk, ProviderType, CapabilitySchema, Capability,
    CapabilityQuery, CapabilityCandidate, ToolObservation, ValidatorAction,
    ValidationResult, CapProvider, CapabilityRegistry, ToolPlanValidator,
)
from .memory import (
    MemoryType, SensitivityLevel, MemoryScope, MemoryRecord,
    MemoryCandidate, WriteGate, MemoryRepository, InMemoryRepository,
    ScopeSelector, JSONMemoryRepository,
)
from .profile import (
    UserProfile, SessionState, ProfileStore, extract_profile_signals,
)
from .protocols import MemoryPort, ModelPort, CapabilityPort, DeliveryPort
from .renderer import (
    FactAnchor, DraftResponse, Persona, FinalResponse,
    RenderValidator, OCRenderer,
)
from .delivery import (
    DeliveryStatus, RuntimeResult, DeliveryReceipt, CompletionReceipt,
    OutputAdapter, NoOpOutputAdapter, DeliveryManager,
)
from .persona.templates import (
    FormalityLevel, PlayfulnessLevel, EmojiStyle,
    PersonaTraits, ToneConfig, PersonaTemplate, PRESETS,
)
from .persona.registry import PersonaRegistry
from .persona.emoji_strategy import EmojiStrategy
from .persona.expressions import ExpressionLibrary
from .persona.persona_renderer import PersonaRenderer

__all__ = [
    # attachment repo
    "AttachmentRef", "AttachmentRecord", "AttachmentRepository",
    # envelope
    "Platform", "MessageKind", "AttachmentKind", "Attachment",
    "Actor", "ConversationRef", "MessageEnvelope", "PreprocessedEnvelope",
    # state
    "RuntimePhase", "RunOutcome", "SocialAction", "ToolPlanStatus",
    "WriteGateDecision", "RuntimeBudget", "ToolStep", "ToolPlan", "RuntimeState",
    # context
    "ContextMemoryScope", "PolicyView", "UserPreference", "PersonaRef",
    "ConversationContext", "ContextSnapshot", "ContextBuilder",
    # perception
    "EntityRef", "SpeechAct", "PerceptionResult",
    # decision
    "DecisionReason", "SocialDecision", "SocialDecisionEngine",
    # structured output
    "StructuredOutputValidator", "PerceptionMerger",
    "merge_perception_with_model", "decision_from_signal",
    "KNOWN_SPEECH_ACTS",
    # capability
    "CapabilityRisk", "ProviderType", "CapabilitySchema", "Capability",
    "CapabilityQuery", "CapabilityCandidate", "ToolObservation", "ValidatorAction",
    "ValidationResult", "CapProvider", "CapabilityRegistry", "ToolPlanValidator",
    # memory
    "MemoryType", "SensitivityLevel", "MemoryScope", "MemoryRecord",
    "MemoryCandidate", "WriteGate", "MemoryRepository", "InMemoryRepository",
    "ScopeSelector", "JSONMemoryRepository",
    "UserProfile", "SessionState", "ProfileStore", "extract_profile_signals",
    # ports
    "MemoryPort", "ModelPort", "CapabilityPort", "DeliveryPort",
    # renderer
    "FactAnchor", "DraftResponse", "Persona", "FinalResponse",
    "RenderValidator", "OCRenderer",
    # delivery
    "DeliveryStatus", "RuntimeResult", "DeliveryReceipt", "CompletionReceipt",
    "OutputAdapter", "NoOpOutputAdapter", "DeliveryManager",
    # persona
    "FormalityLevel", "PlayfulnessLevel", "EmojiStyle",
    "PersonaTraits", "ToneConfig", "PersonaTemplate", "PRESETS",
    "PersonaRegistry", "EmojiStrategy", "ExpressionLibrary", "PersonaRenderer",
]
