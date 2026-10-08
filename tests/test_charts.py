from garmin_intervals_bridge.charts import CHARTS, plan_charts, setup_charts


def items(*codes, charts=()):
    out = [{"type": "INPUT_FIELD", "content": {"code": c, "name": c, "type": "numeric"}} for c in codes]
    out += [{"type": "FITNESS_CHART", "name": n} for n in charts]
    return out


def test_plan_uses_only_fields_that_exist_and_skips_existing_charts():
    plan = plan_charts(items("GarminTrainingReadiness", "BodyBatteryMax", charts=["HRV detail (Garmin)"]))
    names = {c["name"]: c for c in plan["create"]}
    assert "HRV detail (Garmin)" in plan["skipped"]
    assert "Garmin sleep stages" in plan["skipped"]                      # none of its fields exist
    readiness = names["Garmin readiness & recovery"]
    assert [p["field"] for p in readiness["content"]["plots"]] == ["GarminTrainingReadiness"]
    assert readiness["_missing_fields"] == ["GarminRecoveryTimeMinutes", "GarminAcuteLoad"]
    assert readiness["visibility"] == "PRIVATE" and readiness["type"] == "FITNESS_CHART"
    plot = readiness["content"]["plots"][0]
    assert plot["filter"] == "customInput" and plot["customInput"]["content"]["code"] == "GarminTrainingReadiness"


def test_native_plots_carry_scale_and_moving_average_args():
    plan = plan_charts(items("GarminHRV7DayAvg"))
    hrv = next(c for c in plan["create"] if c["name"] == "HRV detail (Garmin)")
    native = hrv["content"]["plots"][0]
    assert native["field"] == "hrv" and native["scale"] == "ms" and native["filter"] == "dec0"
    plan2 = plan_charts(items("GarminAcuteLoad"))
    load = next(c for c in plan2["create"] if c["name"] == "Garmin readiness & recovery")
    assert load["content"]["plots"][0]["aggArgs"] == {"days": 7}


class Fake:
    def __init__(self):
        self.posted = []

    def custom_items(self):
        return items("GarminSleepDeepMinutes", "GarminSleepLightMinutes")

    def create_custom_item(self, item):
        self.posted.append(item)
        return {"id": 999}


def test_setup_charts_dry_run_posts_nothing_and_apply_posts_clean_bodies():
    f = Fake()
    out = setup_charts(f, apply=False)
    assert out["created"] == [] and f.posted == []
    # The HRV chart always has its native plot, so it is created even without custom fields.
    assert [c["name"] for c in out["charts"]] == ["Garmin sleep stages", "HRV detail (Garmin)"]
    out = setup_charts(f, apply=True)
    assert [c["name"] for c in out["created"]] == ["Garmin sleep stages", "HRV detail (Garmin)"]
    assert "_missing_fields" not in f.posted[0] and len(f.posted[0]["content"]["plots"]) == 2
    assert len(CHARTS) == 4
