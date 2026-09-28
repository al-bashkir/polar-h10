import json

import pandas as pd
import pytest

from h10_analysis.loader import LoadError, load_session
from h10_analysis.models import AnalysisConfig
from h10_analysis.validation import has_errors, validate
from synth import make_session

CFG = AnalysisConfig()


def checks(issues, severity=None):
    return {i.check for i in issues if severity is None or i.severity == severity}


def test_clean_session_loads_and_validates(tmp_path):
    d, _ = make_session(tmp_path)
    s = load_session(d)
    assert len(s.hr) > 100 and len(s.rr) > 100 and len(s.ecg) > 10000
    assert str(s.ecg["ts"].dtype) == "datetime64[ns, UTC]"
    assert s.ecg["elapsed_ns"].dtype == "int64"
    assert s.ecg_sample_rate_hz == 130
    issues = validate(s, CFG)
    assert issues == [], [i.message for i in issues]


def test_missing_metadata(tmp_path):
    d, _ = make_session(tmp_path)
    (d / "metadata.json").unlink()
    with pytest.raises(LoadError, match="metadata.json"):
        load_session(d)


def test_unsupported_schema(tmp_path):
    d, _ = make_session(tmp_path)
    m = json.loads((d / "metadata.json").read_text())
    m["schema_version"] = 2
    (d / "metadata.json").write_text(json.dumps(m))
    with pytest.raises(LoadError, match="schema_version"):
        load_session(d)


def test_malformed_csv_missing_column(tmp_path):
    d, _ = make_session(tmp_path)
    (d / "rr.csv").write_text("timestamp,elapsed_ns,rr\n2026-01-01T00:00:01.000000000Z,1000000000,800\n")
    with pytest.raises(LoadError, match="rr_ms"):
        load_session(d)


def test_malformed_values_reported_not_fatal(tmp_path):
    d, _ = make_session(tmp_path)
    lines = (d / "hr.csv").read_text().splitlines()
    parts = lines[5].split(",")
    lines[5] = ",".join(parts[:2] + ["abc"])          # non-numeric HR (row 4)
    lines[7] = "," + ",".join(lines[7].split(",")[1:])  # missing timestamp (row 6)
    (d / "hr.csv").write_text("\n".join(lines) + "\n")
    s = load_session(d)
    by_check = {i.check: i for i in s.issues}
    assert by_check["hr.malformed_values"].details["rows"] == [5]
    assert by_check["hr.timestamp_unparseable"].details["rows"] == [7]
    assert pd.isna(s.hr.loc[s.hr["row"] == 5, "heart_rate_bpm"]).all()
    assert has_errors(validate(s, CFG))


def test_non_utc_timestamp(tmp_path):
    d, _ = make_session(tmp_path)
    lines = (d / "rr.csv").read_text().splitlines()
    ts, rest = lines[3].split(",", 1)
    lines[3] = ts.replace("Z", "+00:00").replace("2026-01-01T00", "2026-01-01T05").replace("+00:00", "+05:00") + "," + rest
    (d / "rr.csv").write_text("\n".join(lines) + "\n")
    s = load_session(d)
    assert "rr.timestamp_not_utc" in checks(s.issues, "warning")
    # Converted value equals the original instant, so it still matches elapsed_ns.
    assert "rr.timestamp_elapsed_mismatch" not in checks(validate(s, CFG))


def test_metadata_counter_mismatch(tmp_path):
    d, _ = make_session(tmp_path)
    m = json.loads((d / "metadata.json").read_text())
    actual = m["statistics"]["ecg_samples"]
    m["statistics"]["ecg_samples"] = actual + 5
    (d / "metadata.json").write_text(json.dumps(m))
    issues = validate(load_session(d), CFG)
    mm = [i for i in issues if i.check == "counters.mismatch"]
    assert len(mm) == 1 and mm[0].severity == "error"
    assert mm[0].details == {"metadata": actual + 5, "actual": actual, "difference": -5}
    assert f"ecg_samples = {actual + 5}, file has {actual} rows" in mm[0].message


def _edit_ecg(d, fn):
    df = pd.read_csv(d / "ecg.csv", dtype=str)
    df = fn(df)
    df.to_csv(d / "ecg.csv", index=False)
    m = json.loads((d / "metadata.json").read_text())
    m["statistics"]["ecg_samples"] = len(df)
    (d / "metadata.json").write_text(json.dumps(m))


def test_duplicate_ecg_samples(tmp_path):
    d, _ = make_session(tmp_path)
    _edit_ecg(d, lambda df: pd.concat([df.iloc[:1000], df.iloc[999:1000], df.iloc[1000:]]).reset_index(drop=True))
    c = checks(validate(load_session(d), CFG), "error")
    assert {"ecg.index_duplicate", "ecg.elapsed_duplicate"} <= c


def test_missing_ecg_index(tmp_path):
    d, _ = make_session(tmp_path)
    _edit_ecg(d, lambda df: df.drop(index=range(500, 510)).reset_index(drop=True))
    issues = validate(load_session(d), CFG)
    miss = [i for i in issues if i.check == "ecg.index_missing"]
    assert miss and "skips 10 index value(s) at 1 place(s)" in miss[0].message


def test_backwards_and_inconsistent_time(tmp_path):
    d, _ = make_session(tmp_path)

    def swap(df):
        df.loc[[200, 201], "elapsed_ns"] = df.loc[[201, 200], "elapsed_ns"].to_numpy()
        return df
    _edit_ecg(d, swap)
    c = checks(validate(load_session(d), CFG), "error")
    assert "ecg.elapsed_backwards" in c
    assert "ecg.timestamp_elapsed_mismatch" in c


def test_status_and_event_counters(tmp_path):
    d, _ = make_session(tmp_path, gap=(50, 55))
    m = json.loads((d / "metadata.json").read_text())
    m["status"], m["statistics"]["disconnects"] = "recording", 2
    (d / "metadata.json").write_text(json.dumps(m))
    c = checks(validate(load_session(d), CFG), "warning")
    assert {"metadata.status", "events.counter_mismatch", "metadata.disconnects"} <= c
