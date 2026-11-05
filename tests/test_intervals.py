from datetime import date

import pytest
import requests

from garmin_intervals_bridge.intervals import IntervalsClient


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("API error")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.auth = None
        self.calls = []
        self.items = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/custom-item") and method == "GET":
            return Response(self.items)
        return Response({"id": "x"})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response({"id": "2026-10-08", "Existing": 1})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response([{"id": "i999"}], status=201)


def test_api_auth_and_paths():
    fake = FakeSession()
    api = IntervalsClient("secret", session=fake)
    assert fake.auth == ("API_KEY", "secret")
    assert api.wellness(date(2026, 10, 8))["Existing"] == 1
    api.write_wellness(date(2026, 10, 8), {"readiness": 44})
    method, url, req = fake.calls[-1]
    assert method == "PUT" and url.endswith("/wellness/2026-10-08")
    assert req["json"] == {"readiness": 44}


def test_a_carried_resting_hr_or_weight_reads_as_empty():
    """The day GET fills a day without a resting HR or weight of its own from another day and flags
    it; the bridge must still fill the day with Garmin's value, and keep a value of the day's own."""
    from garmin_intervals_bridge.mapping import merge_wellness

    class Day(FakeSession):
        def __init__(self, record):
            super().__init__()
            self.record = record

        def get(self, url, **kwargs):
            return Response(dict(self.record))

    carried = {"id": "2025-09-01", "restingHR": 42, "tempRestingHR": True, "weight": 68.7, "tempWeight": True,
               "hrv": 49}
    record = IntervalsClient("secret", session=Day(carried)).wellness(date(2025, 9, 1))
    assert record["restingHR"] is None and record["weight"] is None and record["hrv"] == 49
    assert merge_wellness(record, {"restingHR": 48, "weight": 68.2, "hrv": 50}, {}) == {"restingHR": 48,
                                                                                         "weight": 68.2}
    own = {"id": "2026-11-03", "restingHR": 50, "tempRestingHR": False, "weight": 82.5}     # no tempWeight: kept
    record = IntervalsClient("secret", session=Day(own)).wellness(date(2026, 11, 3))
    assert record["restingHR"] == 50 and record["weight"] == 82.5
    assert merge_wellness(record, {"restingHR": 48, "weight": 82.0}, {}) == {}
    mixed = {"id": "2025-09-02", "restingHR": 42, "tempRestingHR": True, "weight": 68.7, "tempWeight": False}
    record = IntervalsClient("secret", session=Day(mixed)).wellness(date(2025, 9, 2))
    assert record["restingHR"] is None and record["weight"] == 68.7
    mixed = {"id": "2025-09-03", "restingHR": 47, "tempRestingHR": False, "weight": 68.7, "tempWeight": True}
    record = IntervalsClient("secret", session=Day(mixed)).wellness(date(2025, 9, 3))
    assert record["restingHR"] == 47 and record["weight"] is None


def test_a_backfill_fills_a_carried_resting_hr_with_garmin_s(tmp_path):
    """End to end through the real client: the day's PUT carries Garmin's resting HR, not the weight
    the day holds of its own."""
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from test_mapping import sample

    from garmin_intervals_bridge.store import Store
    from garmin_intervals_bridge.sync import sync_wellness

    class Day(FakeSession):
        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            return Response({"id": "2026-10-07", "restingHR": 42, "tempRestingHR": True,
                             "weight": 70.0, "tempWeight": False})

    class Garmin:
        def snapshot(self, day, endpoints=None):
            return sample()

    fake, store = Day(), Store(tmp_path)
    settings = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), activity_days=3,
                               wellness_days=3, wellness_refresh_hours=8)
    sync_wellness(settings, Garmin(), IntervalsClient("secret", session=fake), store, apply=True,
                  days=[date(2026, 10, 7)], today=date(2026, 10, 8))
    puts = [req["json"] for method, url, req in fake.calls if method == "PUT" and "/wellness/" in url]
    assert len(puts) == 1 and puts[0]["restingHR"] == 49 and "weight" not in puts[0]
    store.close()


def test_a_backfill_fills_consecutive_days_though_intervals_carries_each_value_on(tmp_path):
    """Intervals carries a resting HR it is given on to the following days without one (flagged
    `tempRestingHR`); a backfill over consecutive days still writes Garmin's value on each day."""
    from datetime import timedelta
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from test_mapping import sample

    from garmin_intervals_bridge.store import Store
    from garmin_intervals_bridge.sync import sync_wellness

    class Carrying(FakeSession):
        def __init__(self):
            super().__init__()
            self.stored = {}

        def request(self, method, url, **kwargs):
            if method == "PUT" and "/wellness/" in url:
                self.stored[url.rsplit("/", 1)[1]] = kwargs["json"].get("restingHR")
            return super().request(method, url, **kwargs)

        def get(self, url, **kwargs):
            day = date.fromisoformat(url.rsplit("/", 1)[1])
            record = {"id": day.isoformat(), "restingHR": None, "tempRestingHR": False}
            if self.stored.get(day.isoformat()) is not None:
                record["restingHR"] = self.stored[day.isoformat()]
                return Response(record)
            for back in range(1, 31):
                carried = self.stored.get((day - timedelta(days=back)).isoformat())
                if carried is not None:
                    record.update(restingHR=carried, tempRestingHR=True)
                    break
            return Response(record)

    class Garmin:
        def snapshot(self, day, endpoints=None):
            return sample()

    fake, store = Carrying(), Store(tmp_path)
    settings = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), activity_days=3,
                               wellness_days=3, wellness_refresh_hours=8)
    days = [date(2025, 9, 3), date(2025, 9, 4), date(2025, 9, 5)]
    sync_wellness(settings, Garmin(), IntervalsClient("secret", session=fake), store, apply=True,
                  days=days, today=date(2026, 10, 8))
    assert fake.stored == {"2025-09-03": 49, "2025-09-04": 49, "2025-09-05": 49}
    store.close()


def test_private_custom_item_provision_idempotence():
    fake = FakeSession()
    fake.items = [{"type": "INPUT_FIELD", "content": {"code": "BodyBatteryMax", "type": "numeric"}}]
    api = IntervalsClient("secret", session=fake)
    plan = api.provision_fields(apply=False, needed={"GarminTrainingReadiness", "BodyBatteryMax"})
    assert plan == ["GarminTrainingReadiness"]
    assert all(method != "POST" for method, *_ in fake.calls)
    api.provision_fields(apply=True, needed={"GarminTrainingReadiness"})
    calls = [req for method, _, req in fake.calls if method == "POST"]
    assert len(calls) == 1
    assert calls[0]["json"]["visibility"] == "PRIVATE"
    assert calls[0]["json"]["content"]["type"] == "numeric"


def test_activity_upload_201_means_created(tmp_path):
    fake = FakeSession()
    api = IntervalsClient("secret", session=fake)
    fit_path = tmp_path / "123.fit"
    fit_path.write_bytes(b"synthetic-upload-body")
    result = api.upload_fit("123", fit_path)
    assert result["created"] is True
    assert result["items"][0]["id"] == "i999"
    method, url, kwargs = fake.calls[-1]
    assert method == "POST" and kwargs["params"]["external_id"] == "garmin:123"


def test_activity_upload_200_means_no_activity_created(tmp_path):
    class DuplicateSession(FakeSession):
        def post(self, url, **kwargs):
            return Response([], status=200)
    api = IntervalsClient("secret", session=DuplicateSession())
    path = tmp_path / "123.fit"
    path.write_bytes(b"test bytes")
    assert api.upload_fit("123", path) == {"created": False, "items": []}


def test_activity_upload_current_openapi_response(tmp_path):
    class CurrentResponseSession(FakeSession):
        def post(self, url, **kwargs):
            return Response({"icu_athlete_id": "i567", "id": "garmin:123",
                             "activities": [{"id": "i999"}]}, status=201)

    api = IntervalsClient("secret", session=CurrentResponseSession())
    path = tmp_path / "123.fit"
    path.write_bytes(b"synthetic test")
    out = api.upload_fit("123", path)
    assert out == {"created": True, "items": [{"id": "i999"}]}

def test_activity_upload_bad_current_response_fails_closed(tmp_path):
    class BrokenResponseSession(FakeSession):
        def post(self, url, **kwargs):
            return Response({"icu_athlete_id": "i567", "id": "garmin:123"}, status=201)

    api = IntervalsClient("secret", session=BrokenResponseSession())
    path = tmp_path / "123.fit"
    path.write_bytes(b"synthetic test")
    with pytest.raises(ValueError, match="activities missing"):
        api.upload_fit("123", path)


def test_a_wellness_write_answered_with_429_waits_and_is_sent_again(monkeypatch):
    from garmin_intervals_bridge import intervals as module

    class Limited(Response):
        def __init__(self):
            super().__init__(None, 429)
            self.headers = {"Retry-After": "2"}

        def raise_for_status(self):
            raise requests.HTTPError("429 Too Many Requests", response=self)

    class Session(FakeSession):
        answers = [Limited(), Response({"id": "2026-10-08"})]

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return self.answers.pop(0)

    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    session = Session()
    client = IntervalsClient("k", session=session)
    assert client.write_wellness(date(2026, 10, 8), {"HRVTrendAdvice": 3}) == {"id": "2026-10-08"}
    assert waits == [2.0] and [c[0] for c in session.calls] == ["PUT", "PUT"]
    assert session.calls[0][2]["json"] == session.calls[1][2]["json"] == {"HRVTrendAdvice": 3}
