from dududa.core.profile import detect_emotional_tone


def test_laughter_about_media_is_positive_not_distress():
    assert detect_emotional_tone("这个视频绷不住了哈哈哈") == "positive"


def test_laughter_does_not_erase_a_concrete_setback():
    assert detect_emotional_tone("哈哈哈又挂科了") == "negative"


def test_ambiguous_emotion_conflict_fails_neutral():
    assert detect_emotional_tone("太好了但也有点绷不住") == ""
