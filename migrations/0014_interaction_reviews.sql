-- Post-send interaction review verdicts.
--
-- After a turn's reply is delivered, the listener re-reads the turn with the
-- medium model and may send one corrective follow-up
-- (app/graph/interaction_review.py). Every review outcome lands here: it is
-- the rate-limit source (executed corrections per peer per hour), the ops
-- alert source (executed corrections per 24 h), and the corpus for judging
-- whether the reviewer helps or over-corrects.
--
-- verdict: 'ok' (the turn was right), 'correct' (the reviewer proposed a
-- correction), 'skipped' (the review yielded: newer messages, rate limit),
-- 'error' (invalid model output or a failure while reviewing).
-- action: the proposed correction, NULL for skipped/error rows.
-- reason: the model's own explanation for ok/correct rows, a fixed reason
-- code for skipped/error rows. It can name a task, so it is private data:
-- ops queries read it here, never from logs.
-- turn_ref: the LangGraph checkpoint id of the reviewed turn, '' when unknown.

BEGIN;

CREATE TABLE IF NOT EXISTS interaction_reviews (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  peer            TEXT NOT NULL,
  turn_ref        TEXT NOT NULL DEFAULT '',
  intent          TEXT,
  verdict         TEXT NOT NULL
                  CHECK (verdict IN ('ok', 'correct', 'skipped', 'error')),
  reason          TEXT NOT NULL DEFAULT '',
  action          TEXT
                  CHECK (action IS NULL OR action IN
                         ('none', 'complete_task', 'create_task', 'reopen_task', 'send_only')),
  action_page_id  TEXT,
  executed        BOOLEAN NOT NULL DEFAULT false,
  follow_up_sent  BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS interaction_reviews_peer_created_at_idx
  ON interaction_reviews (peer, created_at);

COMMIT;
