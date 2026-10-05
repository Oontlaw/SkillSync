"""Unaddressed-ping risk model.

Predicts at send time whether a directed 1:1 ping will end up
unaddressed-after-return. Features are cumulative counters over strictly
earlier pings plus fields from the message itself; the label is the resolved
`addressed` state.

Unaddressed pings are rare (under 1% of resolved pings), so the model is
class-weighted and evaluated by average precision rather than accuracy.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline

from database import PingEvent, db

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
RISK_MODEL_PATH = os.path.join(MODELS_DIR, "unaddressed_risk.joblib")
MODEL_VERSION = 1
TRAIN_WINDOW_DAYS = 30
MIN_POSITIVES = 5

FEATURE_NAMES = [
    "pinger_prior_unaddressed_rate",  # cumulative, strictly before this ping
    "pinger_prior_count",
    "pair_prior_count",
    "pair_prior_unaddressed_rate",
    "pair_gap_minutes",               # minutes since the pair's previous ping (capped)
    "hour_of_day",
    "requires_response",
    "is_reply",
]


def _build_training_data(days=TRAIN_WINDOW_DAYS):
    """Chronological walk over resolved pings. Returns X, y, n_pos."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    pings = (
        PingEvent.query.filter(
            PingEvent.created_at >= cutoff,
            PingEvent.addressed.isnot(None),
        )
        .order_by(PingEvent.created_at.asc())
        .all()
    )
    pinger_stats = defaultdict(lambda: [0, 0])   # pinger -> [resolved, unaddressed]
    pair_stats = defaultdict(lambda: [0, 0])     # (guild, a, b) -> [resolved, unaddressed]
    pair_last = {}                               # (guild, a, b) -> last created_at

    X, y = [], []
    for p in pings:
        pair = (p.guild_id, min(p.pinger_id, p.pingee_id), max(p.pinger_id, p.pingee_id))
        p_key = (p.guild_id, p.pinger_id)
        p_res, p_un = pinger_stats[p_key]
        pr_res, pr_un = pair_stats[pair]
        gap_minutes = 0.0
        last = pair_last.get(pair)
        if last is not None:
            gap_minutes = min((p.created_at - last).total_seconds() / 60.0, 60.0 * 24.0)

        X.append([
            (p_un / p_res) if p_res else 0.0,
            p_res,
            pr_res,
            (pr_un / pr_res) if pr_res else 0.0,
            gap_minutes,
            float(p.created_at.hour),
            1.0 if p.requires_response else 0.0,
            1.0 if (p.ping_type or "") == "reply" else 0.0,
        ])
        y.append(0 if p.addressed else 1)

        pinger_stats[p_key][0] += 1
        pair_stats[pair][0] += 1
        if p.addressed is False:
            pinger_stats[p_key][1] += 1
            pair_stats[pair][1] += 1
        pair_last[pair] = p.created_at

    return np.array(X), np.array(y), int(sum(y))


def train(days=TRAIN_WINDOW_DAYS):
    """Train and report PR-AUC via stratified 5-fold CV."""
    X, y, n_pos = _build_training_data(days=days)
    if len(y) < 50 or n_pos < MIN_POSITIVES:
        return {
            "status": "skipped",
            "reason": f"insufficient data: {len(y)} resolved pings, {n_pos} unaddressed",
        }

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        (
            "clf",
            LogisticRegression(
                class_weight="balanced", max_iter=1000, random_state=42
            ),
        ),
    ])
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    try:
        ap = float(
            np.mean(cross_val_score(pipe, X, y, cv=cv, scoring="average_precision"))
        )
    except Exception:
        ap = None
    pipe.fit(X, y)

    os.makedirs(MODELS_DIR, exist_ok=True)
    joblib.dump(
        {
            "version": MODEL_VERSION,
            "trained_at": datetime.utcnow().isoformat(),
            "feature_names": FEATURE_NAMES,
            "n_samples": int(len(y)),
            "n_positives": n_pos,
            "cv_average_precision": round(ap, 4) if ap is not None else None,
            "pipeline": pipe,
        },
        RISK_MODEL_PATH,
    )
    return {
        "status": "trained",
        "samples": int(len(y)),
        "positives": n_pos,
        "cv_average_precision": round(ap, 4) if ap is not None else None,
        "n_features": X.shape[1],
    }


def get_stats():
    if not os.path.exists(RISK_MODEL_PATH):
        return {"trained": False}
    meta = joblib.load(RISK_MODEL_PATH)
    return {
        "trained": True,
        "version": meta.get("version"),
        "trained_at": meta.get("trained_at"),
        "n_samples": meta.get("n_samples"),
        "n_positives": meta.get("n_positives"),
        "cv_average_precision": meta.get("cv_average_precision"),
        "feature_names": meta.get("feature_names"),
    }
