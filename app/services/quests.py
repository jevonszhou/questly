"""Atomic quest claim approval and reward payout."""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument

from .points import apply_points_in_session


def decide_quest_claim(db, claim_id, approve, actor):
    try:
        claim_oid = claim_id if isinstance(claim_id, ObjectId) else ObjectId(str(claim_id))
    except (InvalidId, TypeError):
        return None
    actor = actor or {}
    stamp = datetime.now(timezone.utc)

    def commit(session):
        claim = db.quest_claims.find_one_and_update(
            {"_id": claim_oid, "status": "pending"},
            {"$set": {"status": "approved" if approve else "rejected",
                      "decided_at": stamp, "decided_by": actor.get("name"),
                      "decided_by_id": actor.get("_id")}},
            return_document=ReturnDocument.AFTER, session=session)
        if not claim:
            return None
        if approve:
            kid, transaction = apply_points_in_session(
                db, session, claim["kid_id"], int(claim["points"]),
                f"任务奖励：{claim['quest_title']}", actor, "quest",
                idempotency_key=f"quest-approve:{claim['_id']}",
                source_type="quest_claim", source_id=claim["_id"], stamp=stamp)
            if not kid:
                raise RuntimeError("无法发放任务积分：孩子账户不存在。")
            claim["transaction_id"] = transaction["_id"]
            db.quest_claims.update_one({"_id": claim_oid},
                                       {"$set": {"transaction_id": transaction["_id"]}},
                                       session=session)
        else:
            db.notifications.insert_one({
                "user_id": claim["kid_id"], "event": "quest_rejected",
                "event_type": "quest_rejected",
                "title": f"{claim['quest_title']} 未通过",
                "body": claim.get("review_note") or "请根据家长提示再次尝试。",
                "read": False, "created_at": stamp,
            }, session=session)
        return claim

    with db.client.start_session() as session:
        return session.with_transaction(commit)
