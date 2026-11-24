"""The single transactional entry point for every points balance change."""

import uuid
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .pets import apply_points_effect


def _oid(value):
    try:
        return value if isinstance(value, ObjectId) else ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def _now():
    return datetime.now(timezone.utc)


def adjust_points(db, kid_id, delta, reason, actor, kind="parent_award",
                  *, idempotency_key=None, source_type="manual", source_id=None):
    """Atomically update balance, lifetime points, ledger and linked pet HP.

    Returns ``(updated_kid, transaction)`` or ``(None, None)`` on a rejected
    debit / unknown child. Positive points count toward lifetime earnings only
    for quest and parent award transactions.
    """
    kid_oid = _oid(kid_id)
    if not kid_oid:
        return None, None
    delta = int(delta)
    if delta == 0:
        return None, None
    key = str(idempotency_key or uuid.uuid4().hex)[:180]
    prior = db.transactions.find_one({"idempotency_key": key})
    if prior:
        return db.users.find_one({"_id": prior["kid_id"]}), prior

    actor = actor or {}
    stamp = _now()
    client = db.client

    def commit(session):
        return apply_points_in_session(
            db, session, kid_oid, delta, reason, actor, kind,
            idempotency_key=key, source_type=source_type,
            source_id=source_id, stamp=stamp)

    try:
        with client.start_session() as session:
            return session.with_transaction(commit)
    except DuplicateKeyError:
        prior = db.transactions.find_one({"idempotency_key": key})
        return (db.users.find_one({"_id": prior["kid_id"]}), prior) if prior else (None, None)


def apply_points_in_session(db, session, kid_id, delta, reason, actor, kind,
                            *, idempotency_key, source_type, source_id=None,
                            stamp=None, reversal_of=None):
    """Apply points from a caller-owned Mongo transaction."""
    kid_oid = _oid(kid_id)
    if not kid_oid or int(delta) == 0:
        return None, None
    key = str(idempotency_key)[:180]
    prior = db.transactions.find_one({"idempotency_key": key}, session=session)
    if prior:
        return db.users.find_one({"_id": prior["kid_id"]}, session=session), prior
    delta = int(delta)
    actor = actor or {}
    stamp = stamp or _now()
    inc = {"points": delta}
    if delta > 0 and kind in {"quest", "parent_award"}:
        inc["lifetime_points"] = delta
    query = {"_id": kid_oid, "role": "kid"}
    if delta < 0:
        query["points"] = {"$gte": -delta}
    updated = db.users.find_one_and_update(
        query, {"$inc": inc}, return_document=ReturnDocument.AFTER, session=session)
    if not updated:
        return None, None
    entry = {
        "kid_id": kid_oid, "kid_name": updated.get("name", "孩子"),
        "delta": delta, "balance_before": int(updated.get("points", 0)) - delta,
        "balance_after": int(updated.get("points", 0)),
        "reason": str(reason or ("积分奖励" if delta > 0 else "积分扣除"))[:240],
        "kind": kind, "source_type": source_type,
        "source_id": _oid(source_id) if source_id else None,
        "reversal_of": _oid(reversal_of) if reversal_of else None,
        "idempotency_key": key, "actor_id": actor.get("_id"),
        "actor_name": actor.get("name", "Questly"), "created_at": stamp,
        "_id": ObjectId(),
    }
    apply_points_effect(db, updated, entry, session)
    db.transactions.insert_one(entry, session=session)
    events = {"quest": "quest_approved", "parent_award": "points_awarded",
              "parent_deduct": "points_deducted"}
    if kind in events:
        pet_effect = entry.get("pet_effect", {})
        hp_delta = int(pet_effect.get("applied_hp_delta", 0))
        hp_text = (f"宠物生命值{'+' if hp_delta > 0 else ''}{hp_delta}" if hp_delta else "")
        db.notifications.insert_one({
            "user_id": kid_oid, "event": events[kind], "event_type": events[kind],
            "title": (f"获得 {delta} 积分" if delta > 0 else f"扣除 {abs(delta)} 积分"),
            "body": " · ".join(v for v in (entry["reason"], hp_text) if v),
            "read": False, "created_at": stamp,
        }, session=session)
    return updated, entry
