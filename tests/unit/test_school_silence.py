import pytest

from dududa.application.school_silence import (
    is_school_related_message,
    school_silence_reason,
)


@pytest.mark.parametrize("text", [
    "老师今天留作业了吗",
    "学校什么时候开学",
    "帮我查一下课表",
    "这门课期末怎么考",
    "中科大评课社区评分怎么样",
    "我的绩点是多少",
    "Which university course should I take?",
])
def test_school_messages_are_out_of_scope(text):
    assert is_school_related_message(text)
    assert school_silence_reason(text)


@pytest.mark.parametrize("text", [
    "解释一下机器学习",
    "帮我查上海天气",
    "今天有什么新闻",
    "翻译一下 hello world",
    "这个 AI 模型怎么部署",
    "中午吃什么",
])
def test_general_chat_and_tools_remain_in_scope(text):
    assert not is_school_related_message(text)
    assert school_silence_reason(text) == ""
