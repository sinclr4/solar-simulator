from solarsim.service import credit_sessions


def _rows(values, first_hour=7):
    return [{"interval_start": f"2026-11-16T{first_hour + i // 2:02d}:{'30' if i % 2 else '00'}:00+00:00", "consumption": v}
            for i, v in enumerate(values)]


def test_session_energy_goes_to_the_slots_with_excess_usage():
    rows = _rows([0.4, 2.2, 2.1, 0.4])
    typical = {r["interval_start"][11:16]: 0.4 for r in rows}
    credit_sessions(rows, [{"start": "2026-11-15T16:04:00+00:00", "end": "2026-11-16T09:00:00+00:00", "kwh": 3.0}], typical)
    assert [r.get("ev", 0) for r in rows] == [0, 1.3, 1.7, 0]


def test_latest_charging_is_credited_first():
    rows = _rows([0.4, 2.6, 2.6, 0.4, 0.4, 2.2, 2.2])
    typical = {r["interval_start"][11:16]: 0.4 for r in rows}
    credit_sessions(rows, [{"start": "2026-11-16T07:00:00+00:00", "end": "2026-11-16T11:00:00+00:00", "kwh": 3.0}], typical)
    assert [r.get("ev", 0) for r in rows] == [0, 0, 0, 0, 0, 1.2, 1.8]


def test_modelled_ev_readings_count_towards_the_session_total():
    rows = _rows([0.4, 3.5, 3.5, 0.4])
    rows[1]["ev"] = 3.1
    typical = {r["interval_start"][11:16]: 0.4 for r in rows}
    credit_sessions(rows, [{"start": "2026-11-16T07:00:00+00:00", "end": "2026-11-16T09:00:00+00:00", "kwh": 3.5}], typical)
    assert rows[1]["ev"] == 3.1
    assert rows[2]["ev"] == 0.4


def test_usage_outside_the_session_and_below_typical_is_left_alone():
    rows = _rows([3.0, 0.2, 3.0, 3.0])
    typical = {r["interval_start"][11:16]: 0.4 for r in rows}
    credit_sessions(rows, [{"start": "2026-11-16T07:30:00+00:00", "end": "2026-11-16T08:30:00+00:00", "kwh": 5.0}], typical)
    assert [r.get("ev", 0) for r in rows] == [0, 0, 3.0 - 0.4, 0]
