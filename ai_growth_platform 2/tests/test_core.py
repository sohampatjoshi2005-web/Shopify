import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database as db  # noqa: E402
from marketing_module import find_risky_claims, predict_campaign_performance  # noqa: E402
from recommendation_module import get_personalized_recommendations, normalise_profile  # noqa: E402
from sdr_module import (generate_outreach, parse_sequence, qualification_label,  # noqa: E402
                        score_bant, synthesize_leads)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.seed_demo_data()
    return db


def test_bant_clamps_and_labels():
    assert score_bant(99, 99, 99, 99) == 100
    assert score_bant(-5, 0, 0, 0) == 0
    assert [qualification_label(s) for s in (80, 60, 40, 10)] == ["SQL", "MQL", "Nurture", "Disqualified"]


def test_synthesize_is_deterministic_and_unique():
    a = synthesize_leads("Acme", "SaaS", "VP Sales", "US", "a, b", 10)
    b = synthesize_leads("Acme", "SaaS", "VP Sales", "US", "a, b", 10)
    assert a == b
    assert len({x["email"] for x in a}) == len(a)
    assert all(x["email"].endswith(".example") for x in a)


def test_parse_sequence_and_template_fallback():
    text = "EMAIL_1: one\nEMAIL_2: two\nEMAIL_3: three\nLINKEDIN: hi"
    assert parse_sequence(text)["email_step_3"] == "three"
    assert parse_sequence("just prose") is None
    out = generate_outreach({"name": "Ava Sharma", "title": "VP", "company": "Acme"}, "slow follow-up")
    assert out["source"] == "template" and "Ava" in out["email_step_1"]


def test_forecast_uses_real_channel_cpc_and_validates():
    email = predict_campaign_performance(1000, "Medium", "Email", 7, 7)
    assert email["clicks"] == pytest.approx(1000 / 0.70)
    assert predict_campaign_performance(0, "Low", "Meta", 5, 5)["roi"] == 0.0
    with pytest.raises(ValueError):
        predict_campaign_performance(100, "Medium", "TikTok", 5, 5)


def test_risky_claims():
    assert find_risky_claims("Guaranteed results, risk-free!") == ["Guaranteed", "risk-free"]
    assert find_risky_claims("A practical guide") == []


def test_recommendations_exclude_history_and_respect_diversity(fresh_db):
    profile = {"segment": "silver", "lifecycle": "at_risk", "churn_risk": 0.8, "history": ["P002", "P010", "BAD"]}
    p = normalise_profile(profile)
    assert p["ignored_ids"] == ["BAD"]
    recs = get_personalized_recommendations(profile, top_n=5)
    ids = [r["product_id"] for r in recs]
    assert len(recs) == 5 and "P002" not in ids and "P010" not in ids
    cats = [r["category"] for r in recs]
    assert max(cats.count(c) for c in set(cats)) <= 2
    assert recs[0]["category"] == "retention"


def test_cold_start_user_still_gets_results(fresh_db):
    assert len(get_personalized_recommendations({"history": []}, top_n=4)) == 4


def test_lead_upsert_and_draft_workflow(fresh_db):
    lead = {"company": "X", "name": "A B", "email": "A.B@x.example", "title": "VP"}
    id1 = db.save_lead(lead, {"score": 80, "label": "SQL"})
    id2 = db.save_lead(lead, {"score": 90, "label": "SQL"})
    assert id1 == id2
    assert db.save_drafts(id1, {"email_step_1": "hi", "linkedin": "yo"}) == 2
    did = int(db.query_df("SELECT id FROM outreach_drafts LIMIT 1").iloc[0, 0])
    db.set_draft_status(did, "approved")
    with pytest.raises(ValueError):
        db.set_draft_status(did, "sent")
    with pytest.raises(ValueError):
        db.table_df("sqlite_master")
