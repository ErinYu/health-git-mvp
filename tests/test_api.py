from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(monkeypatch, auth_enabled: bool = False):
    monkeypatch.setenv("AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("CONSUMER_API_KEY", "consumer-secret")
    monkeypatch.setenv("REVIEWER_API_KEY", "reviewer-secret")

    from app.main import DB_PATH, app

    db_file = Path(DB_PATH)
    if db_file.exists():
        db_file.unlink()

    return TestClient(app)


def test_main_flow_without_auth(monkeypatch):
    client = _fresh_client(monkeypatch, auth_enabled=False)

    with client as c:
        assert c.get("/api/health").status_code == 200
        assert c.post("/api/seed").status_code == 200

        commit = c.post(
            "/api/commits",
            json={
                "branch_id": 1,
                "user_id": 1,
                "task_type": "meal_log",
                "evidence_text": "walk 30m",
                "metric_value": 106,
                "adherence_score": 88,
            },
        )
        assert commit.status_code == 200

        pr = c.post(
            "/api/prs",
            json={
                "branch_id": 1,
                "requested_by": 1,
                "summary": "Continue low-carb dinner and evening walk",
                "risk_level": "low",
            },
        )
        assert pr.status_code == 200
        pr_id = pr.json()["id"]

        review = c.post(
            f"/api/prs/{pr_id}/review",
            json={
                "reviewer_id": 2,
                "action": "approve",
                "review_note": "ok",
                "force_override": False,
            },
        )
        assert review.status_code == 200
        assert review.json()["status"] == "merged"

        metrics = c.get("/api/metrics")
        assert metrics.status_code == 200
        assert metrics.json()["merge_rate"] >= 1.0


def test_auth_guardrails(monkeypatch):
    client = _fresh_client(monkeypatch, auth_enabled=True)

    with client as c:
        assert c.post("/api/seed").status_code == 401
        assert c.post("/api/seed", headers={"x-api-key": "bad"}).status_code == 403
        assert c.post("/api/seed", headers={"x-api-key": "consumer-secret"}).status_code == 200

        c.post(
            "/api/commits",
            headers={"x-api-key": "consumer-secret"},
            json={
                "branch_id": 1,
                "user_id": 1,
                "task_type": "meal_log",
                "evidence_text": "walk",
                "metric_value": 109,
                "adherence_score": 70,
            },
        )
        pr = c.post(
            "/api/prs",
            headers={"x-api-key": "consumer-secret"},
            json={
                "branch_id": 1,
                "requested_by": 1,
                "summary": "Increase medication dose",
                "risk_level": "high",
            },
        )
        pr_id = pr.json()["id"]

        blocked = c.post(
            f"/api/prs/{pr_id}/review",
            headers={"x-api-key": "consumer-secret"},
            json={
                "reviewer_id": 2,
                "action": "approve",
                "review_note": "try",
                "force_override": False,
            },
        )
        assert blocked.status_code == 403

        reviewed = c.post(
            f"/api/prs/{pr_id}/review",
            headers={"x-api-key": "reviewer-secret"},
            json={
                "reviewer_id": 2,
                "action": "approve",
                "review_note": "override",
                "force_override": True,
            },
        )
        assert reviewed.status_code == 200

        assert c.get("/api/events", headers={"x-api-key": "consumer-secret"}).status_code == 403
        events = c.get("/api/events", headers={"x-api-key": "reviewer-secret"})
        assert events.status_code == 200
        assert len(events.json()["items"]) >= 1


def test_rule_configuration_changes_check_behavior(monkeypatch):
    client = _fresh_client(monkeypatch, auth_enabled=True)

    with client as c:
        c.post("/api/seed", headers={"x-api-key": "consumer-secret"})
        c.post(
            "/api/commits",
            headers={"x-api-key": "consumer-secret"},
            json={
                "branch_id": 1,
                "user_id": 1,
                "task_type": "meal_log",
                "evidence_text": "tired today",
                "metric_value": 111,
                "adherence_score": 45,
            },
        )

        # Default adherence gate is 50, so this should fail checks.
        pr1 = c.post(
            "/api/prs",
            headers={"x-api-key": "consumer-secret"},
            json={
                "branch_id": 1,
                "requested_by": 1,
                "summary": "Continue current plan",
                "risk_level": "low",
            },
        )
        assert pr1.status_code == 200
        assert pr1.json()["check_status"] == "failed"

        rules = c.get("/api/rules", headers={"x-api-key": "reviewer-secret"})
        assert rules.status_code == 200
        assert len(rules.json()["items"]) >= 2

        # Lower min adherence threshold to 40 and verify check now passes.
        upd = c.patch(
            "/api/rules/ADHERENCE_GATE",
            headers={"x-api-key": "reviewer-secret"},
            json={"config_json": {"min_adherence": 40}},
        )
        assert upd.status_code == 200
        assert upd.json()["config_json"]["min_adherence"] == 40

        pr2 = c.post(
            "/api/prs",
            headers={"x-api-key": "consumer-secret"},
            json={
                "branch_id": 1,
                "requested_by": 1,
                "summary": "Continue current plan",
                "risk_level": "low",
            },
        )
        assert pr2.status_code == 200
        assert pr2.json()["check_status"] == "passed"
