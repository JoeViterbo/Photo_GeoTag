import geotag_cascade_gcv_multi as g


class DummyLoc:
    def __init__(self, lat, lon):
        self.latitude = lat
        self.longitude = lon


def test_build_index_hint_map_last_simple(monkeypatch):
    # geocode full names
    def geocode(q, timeout=None):
        if q == "Madrid":
            return DummyLoc(40.0, -3.0)
        if q == "Barcelona":
            return DummyLoc(41.0, 2.0)
        return None

    monkeypatch.setattr(g, "_geolocator", type("X", (), {"geocode": staticmethod(geocode)}))

    data = [{"last": 2, "hint": "Madrid"}, {"last": 4, "hint": "Barcelona"}]
    per_index, errs = g.build_index_hint_map_from_data(data)
    assert per_index[1][0] == 40.0 and per_index[1][1] == -3.0 and per_index[1][2] == "Madrid"
    assert per_index[2][0] == 40.0 and per_index[2][1] == -3.0
    assert per_index[3][0] == 41.0 and per_index[3][1] == 2.0 and per_index[4][2] == "Barcelona"
    assert not errs


def test_build_index_hint_map_fallback_country(monkeypatch):
    # full hint fails, but country geocodes
    def geocode(q, timeout=None):
        if q == "Spain":
            return DummyLoc(40.5, -4.0)
        return None

    monkeypatch.setattr(g, "_geolocator", type("X", (), {"geocode": staticmethod(geocode)}))

    data = [{"last": 34, "hint": "Mostoles, Spain"}]
    per_index, errs = g.build_index_hint_map_from_data(data)
    # should have country coords and used name set to 'Spain'
    assert per_index[1][0] == 40.5 and per_index[1][1] == -4.0 and per_index[1][2] == "Spain"
    assert any("hint_fallback_used" in e for e in errs)


def test_build_index_hint_map_unresolved_keeps_country_token(monkeypatch):
    # no geocode for either full hint or country
    def geocode(q, timeout=None):
        return None

    monkeypatch.setattr(g, "_geolocator", type("X", (), {"geocode": staticmethod(geocode)}))

    data = [{"last": 10, "hint": "UnknownPlace, Noland"}]
    per_index, errs = g.build_index_hint_map_from_data(data)
    assert per_index[1][0] is None and per_index[1][1] is None and per_index[1][2] == "Noland"
    assert any(e.startswith("hint_unresolved") for e in errs)


def test_detect_json_type_last():
    assert g.detect_json_type([{"last": 10, "hint": "X"}]) == 'single'