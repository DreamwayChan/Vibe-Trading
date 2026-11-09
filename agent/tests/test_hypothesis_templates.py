"""Anomaly and behavioral templates pre-fill the five fields without an edge."""

from __future__ import annotations

import json
from pathlib import Path

from src.hypotheses.fields import assess_five_fields, looks_causal
from src.hypotheses.templates import list_templates

ROOT = Path(__file__).resolve().parents[2]
LOCALES = ROOT / "frontend" / "src" / "i18n" / "locales"
_PREFIX = {
    "anomaly": "journalSampleAnomaly",
    "behavioral": "journalSampleBehavioral",
}
_BANNED = ("sharpe", "夏普", "%")


def test_templates_cover_anomaly_and_behavioral_bias() -> None:
    rows = list_templates()
    assert {row["kind"] for row in rows} == {"anomaly", "behavioral"}
    for row in rows:
        for lang in ("en", "zh"):
            fields = row[lang]
            reasons = assess_five_fields(
                observation=fields["observation"],
                hypothesis=fields["hypothesis"],
                logic=fields["logic"],
                validation_plan=fields["validation_plan"],
                invalidation=fields["invalidation"],
            )
            assert reasons == [], row["id"]
            assert looks_causal(fields["hypothesis"])
            assert not looks_causal(fields["observation"])
            blob = " ".join(
                fields[key] for key in ("observation", "hypothesis", "logic", "thesis")
            ).casefold()
            for banned in _BANNED:
                assert banned not in blob


def test_locale_forms_match_the_template_copy() -> None:
    """The journal buttons fill the same sentences the gate accepts."""
    by_kind = {row["kind"]: row for row in list_templates()}
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))["monitor"]
    zh = json.loads((LOCALES / "zh-CN.json").read_text(encoding="utf-8"))["monitor"]
    for kind, prefix in _PREFIX.items():
        fields = by_kind[kind]
        assert en[f"{prefix}Observation"] == fields["en"]["observation"]
        assert en[f"{prefix}Hypothesis"] == fields["en"]["hypothesis"]
        assert en[f"{prefix}Logic"] == fields["en"]["logic"]
        assert en[f"{prefix}Validation"] == fields["en"]["validation_plan"]
        assert en[f"{prefix}Invalidation"] == fields["en"]["invalidation"]
        assert en[f"{prefix}Thesis"] == fields["en"]["thesis"]
        assert en[f"{prefix}Position"] == fields["en"]["planned_position"]
        assert en[f"{prefix}Risk"] == fields["en"]["planned_risk"]
        assert zh[f"{prefix}Observation"] == fields["zh"]["observation"]
        assert zh[f"{prefix}Hypothesis"] == fields["zh"]["hypothesis"]
        assert zh[f"{prefix}Logic"] == fields["zh"]["logic"]
        assert zh[f"{prefix}Validation"] == fields["zh"]["validation_plan"]
        assert zh[f"{prefix}Invalidation"] == fields["zh"]["invalidation"]
        assert zh[f"{prefix}Thesis"] == fields["zh"]["thesis"]
        assert zh[f"{prefix}Position"] == fields["zh"]["planned_position"]
        assert zh[f"{prefix}Risk"] == fields["zh"]["planned_risk"]


def test_other_locales_still_pass_the_five_field_gate() -> None:
    for name in ("ja.json", "ko.json", "ar.json"):
        monitor = json.loads((LOCALES / name).read_text(encoding="utf-8"))["monitor"]
        for prefix in _PREFIX.values():
            reasons = assess_five_fields(
                observation=monitor[f"{prefix}Observation"],
                hypothesis=monitor[f"{prefix}Hypothesis"],
                logic=monitor[f"{prefix}Logic"],
                validation_plan=monitor[f"{prefix}Validation"],
                invalidation=monitor[f"{prefix}Invalidation"],
            )
            assert reasons == [], (name, prefix, reasons)
            assert monitor[f"{prefix}Position"].strip()
            assert monitor[f"{prefix}Risk"].strip()


def test_capability_refuses_a_feature_that_skips_the_loop() -> None:
    en = (ROOT / "CAPABILITY.md").read_text(encoding="utf-8")
    zh = (ROOT / "CAPABILITY_zh.md").read_text(encoding="utf-8")
    assert "A new feature must attach to this loop or be refused." in en
    assert "新功能必须挂上这条闭环，否则拒绝。" in zh
    assert "uncausal_scan" in en
    assert "uncausal_scan" in zh
