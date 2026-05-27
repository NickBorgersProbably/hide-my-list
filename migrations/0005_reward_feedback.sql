-- Signal-reaction feedback collection: add feedback_at and feedback_emoji columns
-- to reward_manifests, plus a fast lookup index for the feedback window query.
--
-- feedback_score (INT, nullable) already exists from migration 0002.
-- feedback_at and feedback_emoji are new.

BEGIN;

ALTER TABLE reward_manifests
    ADD COLUMN IF NOT EXISTS feedback_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS feedback_emoji TEXT NULL;

-- Index for fast "most recent reward per peer" lookup at feedback time.
-- reward_manifests_peer_delivered (from 0002) covers the same columns but
-- a distinct index name is used here so IF NOT EXISTS is safe to add.
CREATE INDEX IF NOT EXISTS reward_manifests_peer_delivered_idx
    ON reward_manifests (peer, delivered_at DESC);

COMMIT;
