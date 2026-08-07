"""PersonaRegistry - manage multiple personas, switching, and lifecycle."""
from __future__ import annotations
from typing import Optional
from .templates import PersonaTemplate, PRESETS

class PersonaRegistry:
    def __init__(self):
        self._personas: dict[str, PersonaTemplate] = dict(PRESETS)
        self._active_id: str = "dududa_default"
        self._group_overrides: dict[str, str] = {}
        self._user_overrides: dict[str, str] = {}

    @property
    def active(self) -> PersonaTemplate:
        return self._personas[self._active_id]

    @property
    def active_id(self) -> str:
        return self._active_id

    def switch(self, persona_id: str) -> bool:
        if persona_id in self._personas:
            self._active_id = persona_id
            return True
        return False

    def get(self, persona_id: str) -> Optional[PersonaTemplate]:
        return self._personas.get(persona_id)

    def list_all(self) -> tuple[str, ...]:
        return tuple(self._personas.keys())

    def register(self, persona: PersonaTemplate):
        self._personas[persona.persona_id] = persona

    def unregister(self, persona_id: str) -> bool:
        if persona_id in ("dududa_default","dududa_serious","dududa_tsundere","dududa_mentor"):
            return False
        return self._personas.pop(persona_id, None) is not None

    def set_group_override(self, group_id: str, persona_id: Optional[str]):
        if persona_id is None:
            self._group_overrides.pop(group_id, None)
        elif persona_id in self._personas:
            self._group_overrides[group_id] = persona_id

    def set_user_override(self, user_id: str, persona_id: Optional[str]):
        if persona_id is None:
            self._user_overrides.pop(user_id, None)
        elif persona_id in self._personas:
            self._user_overrides[user_id] = persona_id

    def resolve(self, group_id: Optional[str] = None, user_id: Optional[str] = None) -> PersonaTemplate:
        if user_id and user_id in self._user_overrides:
            return self._personas[self._user_overrides[user_id]]
        if group_id and group_id in self._group_overrides:
            return self._personas[self._group_overrides[group_id]]
        return self.active

    def get_system_prompt(self, group_id: Optional[str] = None, user_id: Optional[str] = None) -> str:
        return self.resolve(group_id, user_id).render_system_prompt()
