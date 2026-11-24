"""Atomic reward redemption and refund operations."""

from datetime import datetime, timezone
import uuid

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .points import apply_points_in_session


class _RejectedRedemption(Exception):
    pass


def _oid(value):
    try:
        return value if isinstance(value, ObjectId) else ObjectId(str(value))
    except (InvalidId, TypeError):
        return None


def _period_key(period, local_now):
    if period == "weekly":
        year, week, _ = local_now.isocalendar()
        return f"{year}-W{week:02d}"
    if period == "monthly":
        return local_now.strftime("%Y-%m")
    return local_now.strftime("%Y-%m-%d")


def redeem(db, kid, reward, local_now=None, idempotency_key=None):
    """Reserve stock, debit points and create both records in one transaction."""
    key = str(idempotency_key or uuid.uuid4().hex)[:180]
    prior = db.redemptions.find_one({"idempotency_key": key})
    if prior:
        return prior, None
    reward_id = _oid(reward.get("_id"))
    kid_id = _oid(kid.get("_id"))
    if not reward_id or not kid_id or not reward.get("active", True):
        return None, "这个奖励当前不可兑换。"
    stamp = datetime.now(timezone.utc)
    local_now = local_now or stamp
    cost = int(reward["cost"])
    mode = reward.get("stock_mode") or ("unlimited" if reward.get("stock") is None else "fixed")
    if mode not in {"unlimited", "fixed", "periodic"}:
        mode = "unlimited"
    client = db.client

    def commit(session):
        prior_redemption = db.redemptions.find_one({"idempotency_key": key}, session=session)
        if prior_redemption:
            return prior_redemption, None
        current_reward = db.rewards.find_one({"_id": reward_id, "active": True}, session=session)
        if not current_reward:
            return None, "这个奖励当前不可兑换。"

        reservation = None
        if mode == "fixed":
            reserved = db.rewards.update_one(
                {"_id": reward_id, "stock": {"$gt": 0}},
                {"$inc": {"stock": -1}}, session=session)
            if reserved.modified_count != 1:
                return None, "奖励库存不足。"
        elif mode == "periodic":
            period = _period_key(current_reward.get("stock_period", "daily"), local_now)
            scope = current_reward.get("stock_scope", "child")
            scoped_kid = kid_id if scope == "child" else None
            reservation = {"reward_id": reward_id, "kid_id": scoped_kid, "period": period}
            limit = int(current_reward.get("stock_limit") or 0)
            try:
                usage = db.stock_usage.find_one_and_update(
                    {**reservation, "used": {"$lt": limit}},
                    {"$inc": {"used": 1}, "$setOnInsert": {"created_at": stamp}},
                    upsert=True, return_document=ReturnDocument.AFTER, session=session)
            except DuplicateKeyError:
                return None, "本周期的奖励数量已用完。"
            if not usage or int(usage.get("used", 0)) > limit:
                return None, "本周期的奖励数量已用完。"

        updated, transaction = apply_points_in_session(
            db, session, kid_id, -cost, f"兑换奖励：{current_reward['title']}",
            {"_id": kid_id, "name": kid.get("name", "孩子")}, "redeem",
            idempotency_key=f"redeem:{key}", source_type="redemption",
            source_id=None, stamp=stamp)
        if not updated:
            # Raising aborts stock reservations as well as any partial writes.
            raise _RejectedRedemption("积分不足，暂时还不能兑换。")
        doc = {
            "kid_id": kid_id, "kid_name": kid.get("name", "孩子"),
            "kid_avatar": kid.get("avatar"), "reward_id": reward_id,
            "reward_title": current_reward["title"],
            "reward_emoji": current_reward.get("emoji", "🎁"), "cost": cost,
            "status": "pending", "debit_transaction_id": transaction["_id"],
            "refund_transaction_id": None, "idempotency_key": key,
            "stock_reservation": reservation,
            "created_at": stamp, "decided_at": None, "decided_by": None,
            "decided_by_id": None,
        }
        doc["_id"] = db.redemptions.insert_one(doc, session=session).inserted_id
        transaction["source_id"] = doc["_id"]
        db.transactions.update_one({"_id": transaction["_id"]},
                                   {"$set": {"source_id": doc["_id"]}}, session=session)
        return doc, None

    try:
        with client.start_session() as session:
            return session.with_transaction(commit)
    except _RejectedRedemption as exc:
        return None, str(exc)
    except DuplicateKeyError:
        prior = db.redemptions.find_one({"idempotency_key": key})
        return (prior, None) if prior else (None, "兑换没有完成，请重试。")


def decide_redemption(db, redemption_id, approve, actor):
    """Deliver or reject an order; the refund and restock cannot split."""
    red_id = _oid(redemption_id)
    if not red_id:
        return None
    stamp = datetime.now(timezone.utc)
    actor = actor or {}
    client = db.client

    def commit(session):
        red = db.redemptions.find_one_and_update(
            {"_id": red_id, "status": "pending"},
            {"$set": {
                "status": "approved" if approve else "rejected",
                "decided_at": stamp, "decided_by": actor.get("name"),
                "decided_by_id": actor.get("_id"),
            }}, return_document=ReturnDocument.AFTER, session=session)
        if not red:
            return None
        if not approve:
            updated, refund = apply_points_in_session(
                db, session, red["kid_id"], int(red["cost"]),
                f"兑换退款：{red['reward_title']}", actor, "refund",
                idempotency_key=f"redemption-refund:{red['_id']}",
                source_type="redemption", source_id=red["_id"], stamp=stamp,
                reversal_of=red.get("debit_transaction_id"))
            if not updated:
                raise RuntimeError("无法退款：孩子账户不存在。")
            reward = db.rewards.find_one({"_id": red.get("reward_id")}, session=session)
            if reward and (reward.get("stock_mode") or
                           ("unlimited" if reward.get("stock") is None else "fixed")) == "fixed":
                db.rewards.update_one({"_id": reward["_id"]}, {"$inc": {"stock": 1}}, session=session)
            reservation = red.get("stock_reservation")
            if reservation:
                db.stock_usage.update_one(reservation, {"$inc": {"used": -1}}, session=session)
            red["refund_transaction_id"] = refund["_id"]
            db.redemptions.update_one({"_id": red_id},
                                      {"$set": {"refund_transaction_id": refund["_id"]}},
                                      session=session)
        db.notifications.insert_one({
            "user_id": red["kid_id"],
            "event": "purchase_approved" if approve else "purchase_rejected",
            "event_type": "purchase_approved" if approve else "purchase_rejected",
            "title": (f"{red.get('reward_emoji', '🎁')} {red['reward_title']} 已交付" if approve
                      else f"{red['reward_title']} 未获批准"),
            "body": ("家长已确认交付奖励。" if approve
                     else f"{red['cost']} 积分已退回。"),
            "read": False, "created_at": stamp,
        }, session=session)
        return red

    with client.start_session() as session:
        return session.with_transaction(commit)
