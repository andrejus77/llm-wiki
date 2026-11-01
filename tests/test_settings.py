"""Settings: every field documented (the Settings page is generated from this), presets, save/load."""
from llmwiki.conf import PRESETS, Budgets, Settings, get_settings, save_settings


def test_every_field_has_a_description():
    missing = []
    for gname, gfield in Settings.model_fields.items():
        for fname, f in gfield.annotation.model_fields.items():
            if not f.description and gname not in ("budgets",):
                missing.append(f"{gname}.{fname}")
    assert not missing, missing


def test_presets_are_70_105_150():
    assert [PRESETS[p]["cap"] for p in ("standard", "large", "max")] == [70000, 105000, 150000]
    b = Budgets().apply_preset("max")
    assert b.cap == 150000 and b.s1["new"] > Budgets().s1["new"]


def test_save_and_reload():
    s = get_settings().model_copy(deep=True)
    s.schedule.live_tick_minutes = 7
    save_settings(s, user="test", note="unit test")
    assert get_settings().schedule.live_tick_minutes == 7
