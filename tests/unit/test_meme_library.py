import json

from dududa.core.meme_library import MemeLibrary


def test_basic_and_pinyin_fuzzy_candidates_are_only_nominations():
    library = MemeLibrary()
    exact = library.match("这个真的 yyds")
    fuzzy = library.match("这也太绝绝紫了")
    assert exact is not None and exact.key == "yyds"
    assert exact.tier == "basic"
    assert fuzzy is not None and fuzzy.key == "绝绝子"
    assert fuzzy.confidence >= 0.88


def test_custom_meme_requires_explicit_admin_style_addition(tmp_path):
    path = str(tmp_path / "memes.json")
    library = MemeLibrary(path)
    assert library.match("轨道交通之神") is None
    assert library.add_custom(
        "g1", "轨道之神", "群内用于夸赞专业课答题很强的人",
        aliases=("轨交之神", "轨道交通之神"))
    assert library.match("今天轨道交通之神又来了", group_id="g2") is None
    matched = library.match("今天轨道交通之神又来了", group_id="g1")
    assert matched is not None
    assert matched.tier == "custom"

    restarted = MemeLibrary(path)
    assert restarted.match("轨交之神", group_id="g1") is not None
    assert restarted.remove_custom("g1", "轨道之神")
    assert restarted.match("轨交之神", group_id="g1") is None


def test_unknown_export_contains_phrase_and_count_but_no_identity(tmp_path):
    path = str(tmp_path / "memes.json")
    library = MemeLibrary(path)
    for _ in range(3):
        library.observe_unknown("轨信人集合")
    assert library.candidates(min_count=3) == (("轨信人集合", 3),)
    for _ in range(17):
        library.observe_unknown("另一个短语")
    payload = json.loads((tmp_path / "memes.json").read_text("utf-8"))
    assert "unknown_counts" in payload
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "group_id" not in serialized
    assert "sender_id" not in serialized
