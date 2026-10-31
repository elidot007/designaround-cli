from designaround_engine.advisor import phrases_for_lang


def test_english_drops_chinese_observer_notes():
    notes = phrases_for_lang(["相同的圆角矩形箱体和外伸箱盖"], "en")
    assert notes and not any("圆角" in n for n in notes)
    assert "English" in notes[0]


def test_chinese_report_keeps_chinese_notes():
    assert phrases_for_lang(["相同的圆角矩形箱体"], "zh") == ["相同的圆角矩形箱体"]


def test_english_keeps_english_notes():
    assert phrases_for_lang(["rounded rectangular body"], "en") == ["rounded rectangular body"]
