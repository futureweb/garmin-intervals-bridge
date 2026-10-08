from garmin_intervals_bridge.charts import BRIDGE_MARK, CHARTS, plan_charts, setup_charts


def items(*codes, charts=()):
    out = [{"type": "INPUT_FIELD", "content": {"code": c, "name": c, "type": "numeric"}} for c in codes]
    out += [{"type": "FITNESS_CHART", "name": n, "id": 100 + i} for i, n in enumerate(charts)]
    return out


def bridge_chart(name, *fields, item_id=500):
    return {"type": "FITNESS_CHART", "name": name, "id": item_id, "description": BRIDGE_MARK, "visibility": "PRIVATE",
            "content": {"id": "abcd1234", "name": name, "plots": [{"id": i, "field": f} for i, f in enumerate(fields, 1)]}}


def test_plan_uses_only_fields_that_exist_and_skips_existing_charts():
    plan = plan_charts(items("GarminTrainingReadiness", "BodyBatteryMax", charts=["HRV detail (Garmin)"]))
    names = {c["name"]: c for c in plan["create"]}
    assert plan["skipped"]["HRV detail (Garmin)"] == "exists and was not created by the bridge"
    assert "Garmin sleep stages" in plan["skipped"]                      # none of its fields exist
    readiness = names["Garmin readiness & recovery"]
    assert [p["field"] for p in readiness["content"]["plots"]] == ["GarminTrainingReadiness"]
    assert readiness["_missing_fields"] == ["GarminRecoveryTimeMinutes", "GarminAcuteLoad"]
    assert readiness["visibility"] == "PRIVATE" and readiness["type"] == "FITNESS_CHART"
    plot = readiness["content"]["plots"][0]
    assert plot["filter"] == "dec0" and plot["customInput"]["content"]["code"] == "GarminTrainingReadiness"


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

    def reorder_custom_items(self, items):
        return {}


def test_setup_charts_dry_run_posts_nothing_and_apply_posts_clean_bodies():
    f = Fake()
    out = setup_charts(f, apply=False)
    assert out["created"] == [] and f.posted == []
    names = [c["name"] for c in out["charts"]]
    # Charts with a native plot are created even without custom fields; custom-only ones are not.
    assert names[0] == "Garmin sleep stages" and "HRV detail (Garmin)" in names
    assert "Garmin readiness & recovery" in out["skipped"] and "Garmin race predictions" in out["skipped"]
    out = setup_charts(f, apply=True)
    assert [c["name"] for c in out["created"]] == names
    assert "_missing_fields" not in f.posted[0] and len(f.posted[0]["content"]["plots"]) == 2
    assert len(CHARTS) == 10


def test_own_charts_are_completed_when_fields_appear_and_foreign_ones_untouched():
    own = bridge_chart("Garmin VO2max & fitness age", "vo2max")                 # created before the fields existed
    foreign = {"type": "FITNESS_CHART", "name": "Garmin sleep stages", "id": 7, "description": "mine",
               "content": {"plots": []}}
    plan = plan_charts(items("GarminVO2MaxCycling", "GarminFitnessAge", "GarminSleepDeepMinutes") + [own, foreign])
    upd = {u["name"]: u for u in plan["update"]}
    assert upd["Garmin VO2max & fitness age"]["_gained_fields"] == ["GarminVO2MaxCycling", "GarminFitnessAge"]
    assert upd["Garmin VO2max & fitness age"]["id"] == 500
    assert [p["field"] for p in upd["Garmin VO2max & fitness age"]["content"]["plots"]] == \
        ["vo2max", "GarminVO2MaxCycling", "GarminFitnessAge"]
    assert plan["skipped"]["Garmin sleep stages"] == "exists and was not created by the bridge"
    complete = plan_charts(items("GarminVO2MaxCycling", "GarminFitnessAge") +
                           [bridge_chart("Garmin VO2max & fitness age", "vo2max", "GarminVO2MaxCycling", "GarminFitnessAge")])
    assert "already" in complete["skipped"].get("Garmin VO2max & fitness age", "") or \
        [x["name"] for x in complete["update"]] == ["Garmin VO2max & fitness age"]


def test_setup_charts_applies_updates_through_put():
    class F(Fake):
        def __init__(self):
            super().__init__()
            self.puts = []
        def custom_items(self):
            return items("GarminHRV7DayAvg") + [bridge_chart("HRV detail (Garmin)", "hrv", item_id=42)]
        def update_custom_item(self, item_id, item):
            self.puts.append((item_id, item))
            return {"id": item_id}
    f = F()
    out = setup_charts(f, apply=True)
    assert out["updated"] == [{"name": "HRV detail (Garmin)", "id": 42, "adds": ["GarminHRV7DayAvg"]}]
    assert f.puts[0][0] == 42 and "_gained_fields" not in f.puts[0][1]


def test_new_charts_get_unique_indexes_after_creation():
    class F(Fake):
        def __init__(self):
            super().__init__()
            self.reordered = None
            self.created_items = []

        def custom_items(self):
            return (items("GarminSleepDeepMinutes") + [{"type": "FITNESS_CHART", "name": "Theirs", "id": 1, "index": 12}]
                    + self.created_items)

        def create_custom_item(self, item):
            self.created_items.append({**item, "id": 900 + len(self.created_items), "index": 0})
            return {"id": self.created_items[-1]["id"]}

        def reorder_custom_items(self, its):
            self.reordered = [(i["id"], i["index"]) for i in its]
            return {}

    f = F()
    out = setup_charts(f, apply=True)
    assert out["reindexed"] == [i for i, _ in f.reordered]
    assert f.reordered[0][1] == 13 and len({idx for _, idx in f.reordered}) == len(f.reordered)


def test_own_chart_is_updated_when_its_definition_changed():
    own = bridge_chart("Garmin sleep stages", "GarminSleepDeepMinutes", "GarminSleepLightMinutes",
                       "GarminSleepREMMinutes", "GarminSleepAwakeMinutes")
    for plot in own["content"]["plots"]:
        plot.update({"text": "An old, very long label", "type": "bars", "agg": "none", "filter": "customInput",
                     "stack": "sleep"})
    plan = plan_charts(items("GarminSleepDeepMinutes", "GarminSleepLightMinutes", "GarminSleepREMMinutes",
                             "GarminSleepAwakeMinutes") + [own])
    upd = next(x for x in plan["update"] if x["name"] == "Garmin sleep stages")
    assert upd["_gained_fields"] == [] and upd["content"]["plots"][0]["text"] == "Deep"
    assert all(len(p["text"]) <= 12 for c in CHARTS for p in [{"text": spec[2] if spec[0] == "custom" else spec[4]}
                                                             for spec in c["plots"]])
