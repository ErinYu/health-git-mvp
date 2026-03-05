from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "health_git.db"
WEB_PATH = ROOT / "web"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log_event(conn: sqlite3.Connection, event_type: str, entity_type: str, entity_id: int, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO events (event_type, entity_type, entity_id, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, entity_type, entity_id, json.dumps(payload, ensure_ascii=True), now_iso()),
    )


def require_api_key(role: Literal["consumer", "reviewer"], x_api_key: Optional[str]) -> None:
    auth_enabled = os.getenv("AUTH_ENABLED", "false").lower() == "true"
    consumer_api_key = os.getenv("CONSUMER_API_KEY", "")
    reviewer_api_key = os.getenv("REVIEWER_API_KEY", "")

    if not auth_enabled:
        return

    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing x-api-key")

    expected = consumer_api_key if role == "consumer" else reviewer_api_key
    if not expected:
        raise HTTPException(status_code=500, detail=f"{role} api key is not configured")
    if x_api_key != expected:
        raise HTTPException(status_code=403, detail="invalid api key")


def init_db() -> None:
    conn = db_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('consumer', 'reviewer')),
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS health_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            baseline_metric REAL,
            target_metric REAL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS care_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(issue_id) REFERENCES health_issues(id)
        );

        CREATE TABLE IF NOT EXISTS care_commits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            evidence_text TEXT NOT NULL,
            metric_value REAL,
            adherence_score INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(branch_id) REFERENCES care_branches(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS pull_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id INTEGER NOT NULL,
            requested_by INTEGER NOT NULL,
            summary TEXT NOT NULL,
            risk_level TEXT NOT NULL CHECK(risk_level IN ('low', 'medium', 'high')),
            status TEXT NOT NULL,
            check_status TEXT NOT NULL,
            check_reason TEXT,
            reviewed_by INTEGER,
            review_note TEXT,
            created_at TEXT NOT NULL,
            merged_at TEXT,
            FOREIGN KEY(branch_id) REFERENCES care_branches(id),
            FOREIGN KEY(requested_by) REFERENCES users(id),
            FOREIGN KEY(reviewed_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_id INTEGER NOT NULL,
            rule_id TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('passed', 'failed')),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(pr_id) REFERENCES pull_requests(id)
        );

        CREATE TABLE IF NOT EXISTS merges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_id INTEGER NOT NULL,
            branch_id INTEGER NOT NULL,
            approved_by INTEGER NOT NULL,
            rollback_condition TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(pr_id) REFERENCES pull_requests(id),
            FOREIGN KEY(branch_id) REFERENCES care_branches(id),
            FOREIGN KEY(approved_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            issue_id INTEGER NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            observed_at TEXT NOT NULL,
            note TEXT,
            FOREIGN KEY(issue_id) REFERENCES health_issues(id)
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS check_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL,
            description TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    default_rules = [
        (
            "MEDICATION_CHANGE_REVIEW",
            1,
            "Fail when medication escalation keywords are present",
            json.dumps({"keywords": ["increase medication", "new drug", "double dose", "insulin"]}, ensure_ascii=True),
            now_iso(),
            now_iso(),
        ),
        (
            "ADHERENCE_GATE",
            1,
            "Fail when latest adherence score is below threshold",
            json.dumps({"min_adherence": 50}, ensure_ascii=True),
            now_iso(),
            now_iso(),
        ),
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO check_rules(rule_id, enabled, description, config_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        default_rules,
    )
    conn.commit()
    conn.close()


class SeedResponse(BaseModel):
    message: str


class CommitCreate(BaseModel):
    branch_id: int
    user_id: int
    task_type: str
    evidence_text: str
    metric_value: Optional[float] = None
    adherence_score: int = Field(ge=0, le=100)


class PRCreate(BaseModel):
    branch_id: int
    requested_by: int
    summary: str
    risk_level: Literal["low", "medium", "high"]


class PRReview(BaseModel):
    reviewer_id: int
    action: Literal["approve", "reject"]
    review_note: str = ""
    force_override: bool = False


class OutcomeCreate(BaseModel):
    issue_id: int
    metric_name: str
    metric_value: float
    note: str = ""


class RuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    description: Optional[str] = None
    config_json: Optional[dict[str, Any]] = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Health Git MVP", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "time": now_iso()}


@app.post("/api/seed", response_model=SeedResponse)
def seed_data(x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> SeedResponse:
    require_api_key("consumer", x_api_key)
    conn = db_conn()

    user_count = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
    if user_count == 0:
        created = now_iso()
        conn.execute("INSERT INTO users(name, role, created_at) VALUES (?, ?, ?)", ("Alice", "consumer", created))
        conn.execute("INSERT INTO users(name, role, created_at) VALUES (?, ?, ?)", ("Dr. Chen", "reviewer", created))
        issue_id = conn.execute(
            """
            INSERT INTO health_issues(user_id, title, baseline_metric, target_metric, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (1, "8-week fasting glucose reduction", 112.0, 100.0, "open", created),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO care_branches(issue_id, name, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (issue_id, "diet-exercise-v1", "active", created),
        )
        conn.commit()

    conn.close()
    return SeedResponse(message="seed complete")


def _safety_checks(conn: sqlite3.Connection, pr_id: int, summary: str, branch_id: int) -> tuple[str, str]:
    lower_summary = summary.lower()
    blocked_reasons: list[str] = []
    rules = {
        row["rule_id"]: row
        for row in conn.execute(
            "SELECT rule_id, enabled, description, config_json FROM check_rules"
        ).fetchall()
    }

    med_rule = rules.get("MEDICATION_CHANGE_REVIEW")
    if med_rule and med_rule["enabled"]:
        med_config = json.loads(med_rule["config_json"])
        med_risky_words = med_config.get("keywords", [])
        if any(token in lower_summary for token in med_risky_words):
            reason = "Medication change requires manual clinical review"
            blocked_reasons.append(reason)
            conn.execute(
                "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (pr_id, "MEDICATION_CHANGE_REVIEW", "failed", reason, now_iso()),
            )
        else:
            conn.execute(
                "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (pr_id, "MEDICATION_CHANGE_REVIEW", "passed", "No medication escalation keywords", now_iso()),
            )
    else:
        conn.execute(
            "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (pr_id, "MEDICATION_CHANGE_REVIEW", "passed", "Rule disabled", now_iso()),
        )

    latest_commit = conn.execute(
        """
        SELECT adherence_score
        FROM care_commits
        WHERE branch_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (branch_id,),
    ).fetchone()

    adh_rule = rules.get("ADHERENCE_GATE")
    min_adherence = 50
    adh_enabled = True
    if adh_rule:
        adh_enabled = bool(adh_rule["enabled"])
        adh_config = json.loads(adh_rule["config_json"])
        min_adherence = int(adh_config.get("min_adherence", 50))

    if adh_enabled and latest_commit and latest_commit["adherence_score"] < min_adherence:
        reason = f"Latest adherence score below {min_adherence}; escalate coaching before merge"
        blocked_reasons.append(reason)
        conn.execute(
            "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (pr_id, "ADHERENCE_GATE", "failed", reason, now_iso()),
        )
    elif adh_enabled:
        conn.execute(
            "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (pr_id, "ADHERENCE_GATE", "passed", "Adherence gate passed", now_iso()),
        )
    else:
        conn.execute(
            "INSERT INTO checks(pr_id, rule_id, result, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (pr_id, "ADHERENCE_GATE", "passed", "Rule disabled", now_iso()),
        )

    if blocked_reasons:
        return "failed", " | ".join(blocked_reasons)
    return "passed", "All checks passed"


@app.get("/api/dashboard")
def dashboard(x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> dict[str, Any]:
    require_api_key("consumer", x_api_key)
    conn = db_conn()

    users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY id")]
    issues = [dict(row) for row in conn.execute("SELECT * FROM health_issues ORDER BY id")]
    branches = [dict(row) for row in conn.execute("SELECT * FROM care_branches ORDER BY id")]
    commits = [dict(row) for row in conn.execute("SELECT * FROM care_commits ORDER BY id DESC LIMIT 20")]
    prs = [dict(row) for row in conn.execute("SELECT * FROM pull_requests ORDER BY id DESC")]
    merges = [dict(row) for row in conn.execute("SELECT * FROM merges ORDER BY id DESC")]
    outcomes = [dict(row) for row in conn.execute("SELECT * FROM outcomes ORDER BY id DESC")]

    event_counts = [
        dict(row)
        for row in conn.execute(
            "SELECT event_type, COUNT(*) AS count FROM events GROUP BY event_type ORDER BY count DESC"
        )
    ]

    conn.close()
    return {
        "users": users,
        "issues": issues,
        "branches": branches,
        "commits": commits,
        "prs": prs,
        "merges": merges,
        "outcomes": outcomes,
        "event_counts": event_counts,
    }


@app.post("/api/commits")
def create_commit(
    payload: CommitCreate, x_api_key: Optional[str] = Header(default=None, alias="x-api-key")
) -> dict[str, Any]:
    require_api_key("consumer", x_api_key)
    conn = db_conn()

    branch = conn.execute("SELECT * FROM care_branches WHERE id = ?", (payload.branch_id,)).fetchone()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (payload.user_id,)).fetchone()
    if not branch or not user:
        conn.close()
        raise HTTPException(status_code=404, detail="branch or user not found")

    commit_id = conn.execute(
        """
        INSERT INTO care_commits(branch_id, user_id, task_type, evidence_text, metric_value, adherence_score, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.branch_id,
            payload.user_id,
            payload.task_type,
            payload.evidence_text,
            payload.metric_value,
            payload.adherence_score,
            now_iso(),
        ),
    ).lastrowid

    log_event(
        conn,
        "commit_submitted",
        "care_commit",
        int(commit_id),
        {
            "branch_id": payload.branch_id,
            "task_type": payload.task_type,
            "adherence_score": payload.adherence_score,
        },
    )
    conn.commit()

    created = dict(conn.execute("SELECT * FROM care_commits WHERE id = ?", (commit_id,)).fetchone())
    conn.close()
    return created


@app.post("/api/prs")
def open_pr(payload: PRCreate, x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> dict[str, Any]:
    require_api_key("consumer", x_api_key)
    conn = db_conn()

    branch = conn.execute("SELECT * FROM care_branches WHERE id = ?", (payload.branch_id,)).fetchone()
    if not branch:
        conn.close()
        raise HTTPException(status_code=404, detail="branch not found")

    pr_id = conn.execute(
        """
        INSERT INTO pull_requests(branch_id, requested_by, summary, risk_level, status, check_status, check_reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.branch_id,
            payload.requested_by,
            payload.summary,
            payload.risk_level,
            "open",
            "pending",
            "Awaiting checks",
            now_iso(),
        ),
    ).lastrowid

    check_status, check_reason = _safety_checks(conn, int(pr_id), payload.summary, payload.branch_id)

    conn.execute(
        "UPDATE pull_requests SET check_status = ?, check_reason = ? WHERE id = ?",
        (check_status, check_reason, pr_id),
    )

    log_event(
        conn,
        "pr_opened",
        "pull_request",
        int(pr_id),
        {
            "branch_id": payload.branch_id,
            "risk_level": payload.risk_level,
            "check_status": check_status,
        },
    )

    conn.commit()
    created = dict(conn.execute("SELECT * FROM pull_requests WHERE id = ?", (pr_id,)).fetchone())
    conn.close()
    return created


@app.post("/api/prs/{pr_id}/review")
def review_pr(
    pr_id: int, payload: PRReview, x_api_key: Optional[str] = Header(default=None, alias="x-api-key")
) -> dict[str, Any]:
    require_api_key("reviewer", x_api_key)
    conn = db_conn()
    pr = conn.execute("SELECT * FROM pull_requests WHERE id = ?", (pr_id,)).fetchone()
    reviewer = conn.execute("SELECT * FROM users WHERE id = ?", (payload.reviewer_id,)).fetchone()

    if not pr:
        conn.close()
        raise HTTPException(status_code=404, detail="pr not found")
    if not reviewer or reviewer["role"] != "reviewer":
        conn.close()
        raise HTTPException(status_code=400, detail="invalid reviewer")
    if pr["status"] not in ("open", "blocked"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"cannot review pr with status {pr['status']}")

    next_status = "rejected"
    merged_at = None

    if payload.action == "approve":
        if pr["check_status"] == "passed" or payload.force_override:
            next_status = "merged"
            merged_at = now_iso()
            conn.execute(
                """
                INSERT INTO merges(pr_id, branch_id, approved_by, rollback_condition, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    pr_id,
                    pr["branch_id"],
                    payload.reviewer_id,
                    "Rollback if 7-day trend worsens or adverse event is logged",
                    merged_at,
                ),
            )
            log_event(
                conn,
                "merge_completed",
                "pull_request",
                pr_id,
                {"reviewer_id": payload.reviewer_id, "override": payload.force_override},
            )
        else:
            next_status = "blocked"
            log_event(
                conn,
                "check_failed",
                "pull_request",
                pr_id,
                {"reason": pr["check_reason"]},
            )
    else:
        log_event(
            conn,
            "pr_rejected",
            "pull_request",
            pr_id,
            {"reviewer_id": payload.reviewer_id, "note": payload.review_note},
        )

    conn.execute(
        """
        UPDATE pull_requests
        SET status = ?, reviewed_by = ?, review_note = ?, merged_at = ?
        WHERE id = ?
        """,
        (next_status, payload.reviewer_id, payload.review_note, merged_at, pr_id),
    )
    conn.commit()

    updated = dict(conn.execute("SELECT * FROM pull_requests WHERE id = ?", (pr_id,)).fetchone())
    conn.close()
    return updated


@app.post("/api/outcomes")
def create_outcome(
    payload: OutcomeCreate, x_api_key: Optional[str] = Header(default=None, alias="x-api-key")
) -> dict[str, Any]:
    require_api_key("consumer", x_api_key)
    conn = db_conn()
    issue = conn.execute("SELECT * FROM health_issues WHERE id = ?", (payload.issue_id,)).fetchone()
    if not issue:
        conn.close()
        raise HTTPException(status_code=404, detail="issue not found")

    oid = conn.execute(
        """
        INSERT INTO outcomes(issue_id, metric_name, metric_value, observed_at, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        (payload.issue_id, payload.metric_name, payload.metric_value, now_iso(), payload.note),
    ).lastrowid

    log_event(
        conn,
        "outcome_observed",
        "outcome",
        int(oid),
        {"issue_id": payload.issue_id, "metric_name": payload.metric_name, "metric_value": payload.metric_value},
    )

    conn.commit()
    created = dict(conn.execute("SELECT * FROM outcomes WHERE id = ?", (oid,)).fetchone())
    conn.close()
    return created


@app.get("/api/metrics")
def metrics(x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> dict[str, float]:
    require_api_key("consumer", x_api_key)
    conn = db_conn()

    total_commits = conn.execute("SELECT COUNT(*) AS cnt FROM care_commits").fetchone()["cnt"]
    total_prs = conn.execute("SELECT COUNT(*) AS cnt FROM pull_requests").fetchone()["cnt"]
    merged_prs = conn.execute("SELECT COUNT(*) AS cnt FROM pull_requests WHERE status = 'merged'").fetchone()["cnt"]
    blocked_prs = conn.execute("SELECT COUNT(*) AS cnt FROM pull_requests WHERE status = 'blocked'").fetchone()["cnt"]

    avg_adherence_row = conn.execute("SELECT AVG(adherence_score) AS avg_v FROM care_commits").fetchone()
    avg_adherence = float(avg_adherence_row["avg_v"]) if avg_adherence_row["avg_v"] is not None else 0.0

    merge_rate = (merged_prs / total_prs) if total_prs else 0.0
    rollback_rate = 0.0

    conn.close()
    return {
        "commit_rate": float(total_commits),
        "merge_rate": round(merge_rate, 3),
        "blocked_rate": round((blocked_prs / total_prs), 3) if total_prs else 0.0,
        "avg_adherence": round(avg_adherence, 2),
        "rollback_rate": rollback_rate,
    }


@app.get("/api/events")
def list_events(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    require_api_key("reviewer", x_api_key)
    conn = db_conn()

    if event_type:
        rows = conn.execute(
            """
            SELECT id, event_type, entity_type, entity_id, payload_json, created_at
            FROM events
            WHERE event_type = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (event_type, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, event_type, entity_type, entity_id, payload_json, created_at
            FROM events
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    items = [dict(row) for row in rows]
    conn.close()
    return {"items": items, "limit": limit, "offset": offset}


@app.get("/api/rules")
def list_rules(x_api_key: Optional[str] = Header(default=None, alias="x-api-key")) -> dict[str, Any]:
    require_api_key("reviewer", x_api_key)
    conn = db_conn()
    rows = conn.execute(
        """
        SELECT rule_id, enabled, description, config_json, created_at, updated_at
        FROM check_rules
        ORDER BY rule_id
        """
    ).fetchall()
    items = []
    for row in rows:
        rule = dict(row)
        rule["enabled"] = bool(rule["enabled"])
        rule["config_json"] = json.loads(rule["config_json"])
        items.append(rule)
    conn.close()
    return {"items": items}


@app.patch("/api/rules/{rule_id}")
def update_rule(
    rule_id: str,
    payload: RuleUpdate,
    x_api_key: Optional[str] = Header(default=None, alias="x-api-key"),
) -> dict[str, Any]:
    require_api_key("reviewer", x_api_key)
    conn = db_conn()
    existing = conn.execute("SELECT * FROM check_rules WHERE rule_id = ?", (rule_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="rule not found")

    next_enabled = existing["enabled"] if payload.enabled is None else int(payload.enabled)
    next_description = existing["description"] if payload.description is None else payload.description
    next_config = existing["config_json"]
    if payload.config_json is not None:
        next_config = json.dumps(payload.config_json, ensure_ascii=True)

    conn.execute(
        """
        UPDATE check_rules
        SET enabled = ?, description = ?, config_json = ?, updated_at = ?
        WHERE rule_id = ?
        """,
        (next_enabled, next_description, next_config, now_iso(), rule_id),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT rule_id, enabled, description, config_json, created_at, updated_at
        FROM check_rules
        WHERE rule_id = ?
        """,
        (rule_id,),
    ).fetchone()
    conn.close()
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["config_json"] = json.loads(item["config_json"])
    return item


app.mount("/", StaticFiles(directory=str(WEB_PATH), html=True), name="web")
