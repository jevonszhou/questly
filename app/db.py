"""MongoDB connection handling and index setup."""

from flask import current_app
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import PyMongoError


def init_mongo(app):
    """Attach a shared MongoClient to the app. Connection is lazy, so this
    never blocks on startup even if Mongo is not up yet."""
    app.extensions["mongo_client"] = MongoClient(
        app.config["MONGO_URI"],
        serverSelectionTimeoutMS=app.config["MONGO_TIMEOUT_MS"],
        tz_aware=True,
    )
    app.extensions["mongo_indexed"] = False


def get_db():
    db = current_app.extensions["mongo_client"][current_app.config["MONGO_DB"]]
    if not current_app.extensions.get("mongo_indexed"):
        try:
            ensure_indexes(db)
            current_app.extensions["mongo_indexed"] = True
        except PyMongoError:
            # Mongo not ready yet; try again on the next request.
            pass
    return db


def ensure_indexes(db):
    db.users.create_index([("role", ASCENDING), ("name", ASCENDING)])
    db.users.create_index(
        [("email", ASCENDING)],
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    db.rewards.create_index([("active", DESCENDING), ("cost", ASCENDING)])
    db.transactions.create_index([("kid_id", ASCENDING), ("created_at", DESCENDING)])
    db.transactions.create_index([("created_at", DESCENDING)])
    db.transactions.create_index(
        [("idempotency_key", ASCENDING)], unique=True,
        partialFilterExpression={"idempotency_key": {"$type": "string"}},
    )
    db.redemptions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    db.redemptions.create_index([("kid_id", ASCENDING), ("created_at", DESCENDING)])
    db.redemptions.create_index(
        [("idempotency_key", ASCENDING)], unique=True,
        partialFilterExpression={"idempotency_key": {"$type": "string"}},
    )
    db.stock_usage.create_index(
        [("reward_id", ASCENDING), ("kid_id", ASCENDING), ("period", ASCENDING)],
        unique=True,
    )
    db.quests.create_index([("active", DESCENDING), ("title", ASCENDING)])
    db.quest_claims.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
    db.quest_claims.create_index(
        [("idempotency_key", ASCENDING)], unique=True,
        partialFilterExpression={"idempotency_key": {"$type": "string"}},
    )
    db.quest_progress.create_index(
        [("quest_id", ASCENDING), ("kid_id", ASCENDING), ("period", ASCENDING)],
        unique=True,
    )
    db.quest_progress.create_index([("updated_at", ASCENDING)],
                                   expireAfterSeconds=90 * 24 * 60 * 60)

    db.notifications.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    db.notifications.create_index([("user_id", ASCENDING), ("read", ASCENDING)])
    # Keep the feed from growing forever; 90 days is plenty of history.
    db.notifications.create_index([("created_at", ASCENDING)],
                                  expireAfterSeconds=90 * 24 * 60 * 60)
    db.auth_attempts.create_index([("expires_at", ASCENDING)],
                                  expireAfterSeconds=0)

    # A quest can be worth doing more than once a day, so uniqueness includes
    # an occurrence number. Two simultaneous claims race for the same slot and
    # one loses on the unique index, which is what keeps it honest.
    if "quest_id_1_kid_id_1_period_1" in db.quest_claims.index_information():
        db.quest_claims.drop_index("quest_id_1_kid_id_1_period_1")
    db.quest_claims.create_index(
        [("quest_id", ASCENDING), ("kid_id", ASCENDING),
         ("period", ASCENDING), ("seq", ASCENDING)],
        unique=True,
    )
    db.pets.create_index(
        [("kid_id", ASCENDING)], unique=True,
        partialFilterExpression={"is_active": True},
    )
    db.pet_events.create_index([("transaction_id", ASCENDING)], unique=True,
                               partialFilterExpression={"transaction_id": {"$type": "objectId"}})
    db.pet_events.create_index([("kid_id", ASCENDING), ("created_at", DESCENDING)])
    db.pet_events.create_index([("pet_id", ASCENDING), ("created_at", DESCENDING)])
    db.audit_logs.create_index([("actor_id", ASCENDING), ("created_at", DESCENDING)])
    db.audit_logs.create_index([("target_type", ASCENDING), ("target_id", ASCENDING),
                                ("created_at", DESCENDING)])
    db.outbox.create_index([("event_key", ASCENDING)], unique=True)
    db.outbox.create_index([("status", ASCENDING), ("next_attempt_at", ASCENDING)])
    db.family_settings.update_one(
        {"_id": "family"},
        {"$setOnInsert": {
            "schema_version": 1,
            "family_name": "我们的家",
            "default_locale": "zh_CN",
            "timezone": "Asia/Shanghai",
            "points_rules": {"min_balance": 0, "max_manual_delta": 1000,
                             "allow_negative_balance": False},
            "pet_rules": {
                "enabled": True, "max_hp": 100,
                "ratio_numerator": 1, "ratio_denominator": 1,
                "apply_kinds": ["quest", "parent_award", "parent_deduct"],
                "thresholds": [
                    {"min": 80, "max": 100, "status": "healthy", "label_zh": "健康"},
                    {"min": 50, "max": 79, "status": "good", "label_zh": "良好"},
                    {"min": 20, "max": 49, "status": "care", "label_zh": "需要照顾"},
                    {"min": 1, "max": 19, "status": "weak", "label_zh": "虚弱"},
                    {"min": 0, "max": 0, "status": "recover", "label_zh": "需要恢复"},
                ],
            },
        }},
        upsert=True,
    )
