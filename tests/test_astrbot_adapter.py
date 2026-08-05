import pytest, asyncio
from packages.adapters.astrbot.types import (
    AstrMessageEvent, AstrSender, MessageEventResult, CommandResult,
    EventMessageType, AstrBotPlatform, Plain, Image, At, Reply,
)
from packages.adapters.astrbot.input_adapter import AstrBotInputAdapter, ActorMappingConfig
from packages.adapters.astrbot.output_adapter import AstrBotOutputAdapter
from packages.adapters.astrbot.plugin import DududaPlugin, create_plugin, _is_duplicate, _processed_messages
from packages.core.envelope import Platform, MessageKind
from packages.core.delivery import RuntimeResult
from packages.core.state import RunOutcome
from packages.core.renderer import FinalResponse

def _evt(text='hello', uid='12345', nick='Test', gid='67890', priv=False):
    return AstrMessageEvent(
        message_str=text, message_id='m1', session_id='s1',
        sender=AstrSender(user_id=uid, nickname=nick),
        group_id='' if priv else gid,
        _message_type=EventMessageType.PRIVATE_MESSAGE if priv else EventMessageType.GROUP_MESSAGE,
        _platform=AstrBotPlatform.AIOCQHTTP,
    )

class TestInputAdapter:
    def test_group(self):
        env = AstrBotInputAdapter().to_envelope(_evt())
        assert env.platform == Platform.QQ and env.kind == MessageKind.GROUP

    def test_private(self):
        env = AstrBotInputAdapter().to_envelope(_evt(priv=True))
        assert env.kind == MessageKind.PRIVATE

    def test_hash(self):
        a = AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=True))
        env = a.to_envelope(_evt(uid='99999'))
        assert env.sender.actor_id != '99999' and len(env.sender.actor_id) == 16

    def test_nohash(self):
        a = AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=False))
        assert AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=False)).to_envelope(_evt(uid='99999')).sender.actor_id == '99999'

    def test_roles(self):
        c = ActorMappingConfig(owner_ids=('a1',), admin_ids=('m1',), muted_ids=('b1',))
        a = AstrBotInputAdapter(c)
        assert a.to_envelope(_evt(uid='a1')).sender.role == 'owner'
        assert a.to_envelope(_evt(uid='m1')).sender.role == 'admin'
        assert a.to_envelope(_evt(uid='b1')).sender.role == 'muted'
        assert a.to_envelope(_evt(uid='r')).sender.role == 'normal'

    def test_mentions(self):
        a = AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=False))
        e = _evt(); e._components = [At(qq='111'), At(qq='222')]
        assert '111' in a.to_envelope(e).mentions

    def test_attachment(self):
        a = AstrBotInputAdapter()
        e = _evt(); e._components = [Image(url='http://x.com/a.jpg')]
        assert a.to_envelope(e).has_attachment('image')

    def test_preprocessed(self):
        pp = AstrBotInputAdapter().to_preprocessed(_evt(text='hey'))
        assert pp.validated and pp.envelope.text == 'hey'

class TestOutputAdapter:
    def test_bind(self):
        a = AstrBotOutputAdapter()
        a.bind_event('r1', _evt())
        assert 'r1' in a._contexts
        a.unbind_event('r1')
        assert 'r1' not in a._contexts

    def test_send(self):
        a = AstrBotOutputAdapter()
        e = _evt()
        a.bind_event('r2', e)
        res = RuntimeResult(run_id='r2', outcome=RunOutcome.SUCCEEDED, final_response=FinalResponse(text='hi'))
        assert a.send(Platform.QQ, 'g', res) is not None

    def test_reaction(self):
        assert AstrBotOutputAdapter().send_reaction(Platform.QQ, 'g', 'like') is not None

    def test_chunk(self):
        a = AstrBotOutputAdapter()
        assert a._chunk_text('hi') == ['hi']
        chunks = a._chunk_text('A' * 5000)
        assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)

class TestPlugin:
    def test_create(self):
        p = create_plugin()
        h = p.health_check()
        assert p._enabled and h['services'] >= 6

    def test_group_msg(self):
        p = create_plugin()
        r = asyncio.run(p.on_group_message(_evt()))
        assert isinstance(r, MessageEventResult)

    def test_private_msg(self):
        p = create_plugin()
        r = asyncio.run(p.on_private_message(_evt(priv=True)))
        assert isinstance(r, MessageEventResult)

    def test_admin_status(self):
        p = create_plugin()
        r = asyncio.run(p.on_admin_command(_evt(text='/dududa_status', priv=True)))
        assert isinstance(r, CommandResult) and 'Dududa' in r.message_chain[0].text

    def test_admin_persona_list(self):
        p = create_plugin()
        r = asyncio.run(p.on_admin_command(_evt(text='/dududa_persona', priv=True)))
        assert 'dududa_default' in r.message_chain[0].text

    def test_admin_switch(self):
        p = create_plugin()
        r = asyncio.run(p.on_admin_command(_evt(text='/dududa_persona dududa_serious', priv=True)))
        assert 'Switched' in r.message_chain[0].text
        p.persona_registry.switch('dududa_default')

    def test_disable_enable(self):
        p = create_plugin()
        asyncio.run(p.on_admin_command(_evt(text='/dududa_disable', priv=True)))
        assert not p._enabled
        asyncio.run(p.on_admin_command(_evt(text='/dududa_enable', priv=True)))
        assert p._enabled

    def test_duplicate(self):
        _processed_messages.clear()
        assert not _is_duplicate('A')
        assert _is_duplicate('A')
        assert not _is_duplicate('B')

class TestTypes:
    def test_plain(self): assert Plain('hi').text == 'hi'
    def test_image(self): assert Image(url='x').url == 'x'
    def test_at(self): assert At(qq='123').qq == '123'
    def test_result(self):
        r = MessageEventResult(); r.set_text('ok')
        assert r.message_chain[0].text == 'ok'
    def test_cmd_result(self):
        assert CommandResult.from_text('cmd').message_chain[0].text == 'cmd'
    def test_event(self):
        e = _evt()
        assert e.get_platform_name() == 'aiocqhttp' and e.get_group_id() == '67890'

print('OK')
