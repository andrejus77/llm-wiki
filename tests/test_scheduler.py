"""Scheduler: the nightly batch runs once per day; live mode switches itself off after N failed ticks."""
from llmwiki import scheduler
from llmwiki.conf import get_settings, invalidate, save_settings
from llmwiki.models import KV


def test_batch_once_per_day_and_live_auto_off(monkeypatch):
    s = get_settings().model_copy(deep=True)
    s.schedule.batch_enabled = True
    s.schedule.batch_time = "00:00"
    s.schedule.live_enabled = True
    s.schedule.live_tick_minutes = 1
    s.schedule.live_max_failed_ticks = 3
    save_settings(s, user="test", note="scheduler test")
    batches = []
    monkeypatch.setattr(scheduler, "run_batch", lambda force=False: batches.append(1))

    def boom():
        raise RuntimeError("API down")
    monkeypatch.setattr(scheduler, "live_tick", boom)
    monkeypatch.setattr("llmwiki.rag.index.sync_vectors", lambda: 0)
    KV.set("last_batch_day", "")
    KV.set("live_failures", 0)
    sch = scheduler.Scheduler()
    for _ in range(3):
        KV.set("last_live_tick", 0)
        invalidate()
        sch.tick()
    invalidate()
    assert len(batches) == 1                      # once per day, not every tick
    assert get_settings().schedule.live_enabled is False
    assert KV.get("live_failures") == 3
