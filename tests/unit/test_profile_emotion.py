from dududa.core.profile import detect_emotional_tone


def test_laughter_about_media_is_positive_not_distress():
    assert detect_emotional_tone("这个视频绷不住了哈哈哈") == "positive"


def test_laughter_does_not_erase_a_concrete_setback():
    assert detect_emotional_tone("哈哈哈又挂科了") == "negative"


def test_ambiguous_emotion_conflict_fails_neutral():
    assert detect_emotional_tone("太好了但也有点绷不住") == ""


def test_qq_pain_signals_are_negative():
    for text in ("😭", "有点想哭😔", "呜呜呜", "唉，真不知道怎么办", "你咋这样"):
        assert detect_emotional_tone(text) == "negative", text


def test_surprise_and_laughing_sigh_are_not_false_distress():
    assert detect_emotional_tone("😮") == ""
    assert detect_emotional_tone("哎，笑死我了哈哈哈") == "positive"
