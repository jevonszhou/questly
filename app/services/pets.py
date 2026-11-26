"""Pet health rules and immutable HP events."""

from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument


DEFAULT_PET_RULES = {
    "enabled": True,
    "max_hp": 100,
    "ratio_numerator": 1,
    "ratio_denominator": 1,
    "apply_kinds": ["quest", "parent_award", "parent_deduct"],
    "thresholds": [
        {"min": 80, "max": 100, "status": "healthy", "label_zh": "健康"},
        {"min": 50, "max": 79, "status": "good", "label_zh": "良好"},
        {"min": 20, "max": 49, "status": "care", "label_zh": "需要照顾"},
        {"min": 1, "max": 19, "status": "weak", "label_zh": "虚弱"},
        {"min": 0, "max": 0, "status": "recover", "label_zh": "需要恢复"},
    ],
}


def _now():
    return datetime.now(timezone.utc)


def _status(hp, thresholds):
    for row in thresholds:
        if int(row["min"]) <= hp <= int(row["max"]):
            return row["status"]
    return "recover" if hp <= 0 else "healthy"


def _rules(db, session=None):
    settings = db.family_settings.find_one({"_id": "family"}, session=session) or {}
    rules = dict(DEFAULT_PET_RULES)
    rules.update(settings.get("pet_rules") or {})
    return rules


def apply_points_effect(db, kid, transaction, session):
    """Apply configured HP changes in the points transaction."""
    rules = _rules(db, session)
    delta = int(transaction["delta"])
    kind = transaction.get("kind")
    pet = db.pets.find_one({"kid_id": kid["_id"], "is_active": True}, session=session)
    if not pet or not rules.get("enabled") or kind not in rules.get("apply_kinds", []):
        transaction["pet_effect"] = {"applied": False, "requested_hp_delta": 0,
                                     "applied_hp_delta": 0}
        return transaction

    denominator = max(1, int(rules.get("ratio_denominator", 1)))
    numerator = int(rules.get("ratio_numerator", 1))
    # Integer conversion truncates toward zero, including for negative values.
    product = delta * numerator
    requested = (abs(product) // denominator) * (1 if product >= 0 else -1)
    maximum = max(1, int(rules.get("max_hp", pet.get("max_hp", 100))))
    before = max(0, min(maximum, int(pet.get("hp", maximum))))
    after = min(maximum, max(0, before + requested))
    applied = after - before
    status = _status(after, rules.get("thresholds", DEFAULT_PET_RULES["thresholds"]))
    # Some pets created before versioning was added have no `version` field.
    # Filtering by a default version of 1 silently misses those documents while
    # allowing the points transaction to commit. Transactions already protect
    # concurrent writes, so update by _id and initialize/increment version here.
    try:
        next_version = int(pet.get("version") or 0) + 1
    except (TypeError, ValueError):
        next_version = 1
    changed = db.pets.update_one(
        {"_id": pet["_id"]},
        {"$set": {"hp": after, "max_hp": maximum, "status": status,
                  "updated_at": _now(), "version": next_version}},
        session=session,
    )
    if changed.matched_count != 1:
        raise RuntimeError("Active pet disappeared while applying its HP change.")
    transaction["pet_effect"] = {
        "applied": True, "requested_hp_delta": requested,
        "applied_hp_delta": applied, "hp_before": before, "hp_after": after,
    }
    db.pet_events.insert_one({
        "transaction_id": transaction["_id"], "kid_id": kid["_id"],
        "pet_id": pet["_id"], "event_type": "points_linked",
        "source_type": transaction.get("source_type", "transaction"),
        "source_id": transaction.get("source_id"), "points_delta": delta,
        "requested_hp_delta": requested, "applied_hp_delta": applied,
        "hp_before": before, "hp_after": after,
        "reason": transaction.get("reason", ""),
        "actor_id": transaction.get("actor_id"),
        "actor_name": transaction.get("actor_name", "Questly"),
        "created_at": transaction["created_at"],
    }, session=session)
    return transaction


def status_for(hp, rules):
    return _status(int(hp), rules.get("thresholds", DEFAULT_PET_RULES["thresholds"]))


def active_pet(db, kid_id):
    try:
        kid_oid = kid_id if isinstance(kid_id, ObjectId) else ObjectId(str(kid_id))
    except (InvalidId, TypeError):
        return None
    return db.pets.find_one({"kid_id": kid_oid, "is_active": True})


def pet_history(db, kid_id, limit=50):
    try:
        kid_oid = kid_id if isinstance(kid_id, ObjectId) else ObjectId(str(kid_id))
    except (InvalidId, TypeError):
        return []
    return list(db.pet_events.find({"kid_id": kid_oid}).sort("created_at", -1).limit(limit))


def ensure_pet(db, kid, *, name=None, species="cat", actor=None):
    """Create a starting pet only if the child has no active pet."""
    existing = active_pet(db, kid["_id"])
    if existing:
        if existing.get("version") is None:
            db.pets.update_one(
                {"_id": existing["_id"], "version": None},
                {"$set": {"version": 1}},
            )
            existing["version"] = 1
        db.users.update_one({"_id": kid["_id"]}, {"$set": {"active_pet_id": existing["_id"]}})
        return existing
    maximum = int((db.family_settings.find_one({"_id": "family"}) or {})
                  .get("pet_rules", {}).get("max_hp", 100))
    stamp = _now()
    pet = {
        "kid_id": kid["_id"], "name": (name or f"{kid.get('name', '孩子')}的小伙伴")[:40],
        "species": species if species in {"cat", "dog", "rabbit", "dinosaur"} else "cat",
        "avatar_key": species if species in {"cat", "dog", "rabbit", "dinosaur"} else "cat",
        "level": 1, "xp": 0, "hp": maximum, "max_hp": maximum,
        "status": "healthy", "is_active": True, "adopted_at": stamp,
        "created_at": stamp, "updated_at": stamp, "version": 1,
    }
    pet_id = db.pets.insert_one(pet).inserted_id
    db.users.update_one({"_id": kid["_id"], "role": "kid"},
                        {"$set": {"active_pet_id": pet_id}})
    return db.pets.find_one({"_id": pet_id})


def adopt_pet(db, kid, name, species, actor):
    """Replace the active pet while preserving the former pet and its events."""
    if species not in {"cat", "dog", "rabbit", "dinosaur"}:
        return None, "请选择有效的宠物种类。"
    if not str(name or "").strip():
        return None, "请填写宠物名字。"
    rules = _rules(db)
    maximum = max(1, int(rules.get("max_hp", 100)))
    stamp = _now()
    actor = actor or {}
    pet = {
        "kid_id": kid["_id"], "name": str(name).strip()[:40], "species": species,
        "avatar_key": species, "level": 1, "xp": 0, "hp": maximum,
        "max_hp": maximum, "status": "healthy", "is_active": True,
        "adopted_at": stamp, "created_at": stamp, "updated_at": stamp,
        "version": 1,
    }

    def commit(session):
        previous = db.pets.find_one({"kid_id": kid["_id"], "is_active": True},
                                    session=session)
        if previous:
            db.pets.update_one({"_id": previous["_id"], "is_active": True},
                               {"$set": {"is_active": False, "updated_at": stamp},
                                "$inc": {"version": 1}}, session=session)
        pet_id = db.pets.insert_one(pet, session=session).inserted_id
        db.users.update_one({"_id": kid["_id"], "role": "kid"},
                            {"$set": {"active_pet_id": pet_id}}, session=session)
        db.audit_logs.insert_one({
            "actor_id": actor.get("_id"), "actor_role": actor.get("role", "parent"),
            "action": "pet.adopted", "target_type": "user",
            "target_id": str(kid["_id"]),
            "before": {"active_pet_id": previous["_id"]} if previous else None,
            "after": {"active_pet_id": pet_id, "species": species},
            "reason": "领养或更换宠物", "created_at": stamp,
        }, session=session)
        pet["_id"] = pet_id
        return pet

    with db.client.start_session() as session:
        return session.with_transaction(commit), None


def customize_pet(db, kid, name, species):
    """Let a child choose only an active pet's safe cosmetic fields."""
    if species not in {"cat", "dog", "rabbit", "dinosaur"}:
        return False
    result = db.pets.update_one(
        {"kid_id": kid["_id"], "is_active": True},
        {"$set": {"name": str(name or "").strip()[:40] or "小伙伴",
                  "species": species, "avatar_key": species,
                  "updated_at": _now()}, "$inc": {"version": 1}},
    )
    return result.modified_count == 1


def adjust_pet_hp(db, kid, delta, reason, actor):
    """Manually adjust HP transactionally and leave an audit trail."""
    delta = int(delta)
    if not delta or not str(reason or "").strip():
        return None, "请输入非零生命值变化和原因。"
    rules = _rules(db)
    pet = active_pet(db, kid["_id"])
    if not pet:
        return None, "这个孩子还没有启用的宠物。"
    maximum = max(1, int(rules.get("max_hp", pet.get("max_hp", 100))))
    before = int(pet.get("hp", maximum))
    after = min(maximum, max(0, before + delta))
    stamp = _now()
    event = {
        "kid_id": kid["_id"], "pet_id": pet["_id"], "event_type": "manual_adjustment",
        "points_delta": 0, "requested_hp_delta": delta,
        "applied_hp_delta": after - before, "hp_before": before, "hp_after": after,
        "reason": str(reason).strip()[:240], "actor_id": actor.get("_id"),
        "actor_name": actor.get("name", "家长"), "created_at": stamp,
    }
    client = db.client

    def commit(session):
        changed = db.pets.update_one(
            {"_id": pet["_id"], "version": pet.get("version", 1)},
            {"$set": {"hp": after, "max_hp": maximum,
                      "status": status_for(after, rules), "updated_at": stamp},
             "$inc": {"version": 1}}, session=session)
        if changed.modified_count != 1:
            raise RuntimeError("宠物资料已更新，请刷新后重试。")
        db.pet_events.insert_one(event, session=session)
        db.audit_logs.insert_one({
            "actor_id": actor.get("_id"), "actor_role": "parent",
            "action": "pet.hp.adjusted", "target_type": "pet",
            "target_id": str(pet["_id"]), "before": {"hp": before},
            "after": {"hp": after}, "reason": event["reason"], "created_at": stamp,
        }, session=session)
        return event

    with client.start_session() as session:
        return session.with_transaction(commit), None


def update_pet_rules(db, actor, rules, reason):
    """Validate and audit an atomic family HP rule update."""
    if not str(reason or "").strip():
        return False, "请填写调整原因。"
    maximum = int(rules.get("max_hp", 0))
    numerator = int(rules.get("ratio_numerator", 0))
    denominator = int(rules.get("ratio_denominator", 0))
    if maximum < 1 or maximum > 10000 or numerator < 0 or numerator > 1000 or denominator < 1 or denominator > 1000:
        return False, "生命值上限或积分换算比例无效。"
    thresholds = rules.get("thresholds") or []
    ordered = sorted(thresholds, key=lambda row: int(row["min"]))
    if not ordered or int(ordered[0]["min"]) != 0 or int(ordered[-1]["max"]) != maximum:
        return False, "健康阈值必须从 0 连续覆盖到生命值上限。"
    previous = -1
    for row in ordered:
        low, high = int(row["min"]), int(row["max"])
        if low != previous + 1 or high < low:
            return False, "健康阈值不能重叠或留空。"
        previous = high
    before_settings = db.family_settings.find_one({"_id": "family"}) or {}
    before = before_settings.get("pet_rules", DEFAULT_PET_RULES)
    stamp = _now()
    client = db.client

    def commit(session):
        db.family_settings.update_one(
            {"_id": "family"},
            {"$set": {"pet_rules": rules, "updated_at": stamp,
                      "updated_by": actor.get("_id")},
             "$setOnInsert": {"schema_version": 1, "default_locale": "zh_CN",
                              "timezone": "Asia/Shanghai"}},
            upsert=True, session=session)
        db.audit_logs.insert_one({
            "actor_id": actor.get("_id"), "actor_role": "parent",
            "action": "pet_rules.updated", "target_type": "family_settings",
            "target_id": "family", "before": before, "after": rules,
            "reason": str(reason).strip()[:240], "created_at": stamp,
        }, session=session)
        for pet in db.pets.find({"is_active": True}, session=session):
            before_hp = int(pet.get("hp", maximum))
            hp = min(maximum, max(0, before_hp))
            db.pets.update_one(
                {"_id": pet["_id"]},
                {"$set": {"max_hp": maximum, "hp": hp,
                          "status": status_for(hp, rules), "updated_at": stamp},
                 "$inc": {"version": 1}}, session=session)
            if hp != before_hp:
                db.pet_events.insert_one({
                    "kid_id": pet["kid_id"], "pet_id": pet["_id"],
                    "event_type": "max_hp_changed", "points_delta": 0,
                    "requested_hp_delta": 0, "applied_hp_delta": hp - before_hp,
                    "hp_before": before_hp, "hp_after": hp,
                    "reason": "调整生命值上限后按新上限修正",
                    "actor_id": actor.get("_id"), "actor_name": actor.get("name", "家长"),
                    "created_at": stamp,
                }, session=session)
        return True

    with client.start_session() as session:
        session.with_transaction(commit)
    return True, None
