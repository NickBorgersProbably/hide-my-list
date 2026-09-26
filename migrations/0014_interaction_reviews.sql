-- Post-send interaction review jobs and verdicts.
--
-- After a turn's reply is delivered, the listener re-reads the turn with the
-- medium model and may send one corrective follow-up
-- (app/graph/interaction_review.py). Each review is a durable job: the listener
-- inserts a 'pending' row before the review runs, and every exit path moves it
-- to a final state. Startup resumes or retires the rows still 'pending'. The
-- table is also the rate-limit source (executed corrections per peer per hour),
-- the ops alert source (executed corrections per 24 h), and the corpus for
-- judging whether the reviewer helps or over-corrects.
--
-- verdict: the job state.
--   'pending' — scheduled, not finished;
--   'ok'      — the turn was right;
--   'correct' — the reviewer's correction ran;
--   'skipped' — the review yielded: buffer_non_empty, superseded, cancelled,
--               rate_limited, disabled, or timeout (the code is in reason);
--   'error'   — invalid model output, a stale checkpoint, or a failure.
-- action: the proposed correction; NULL while pending and when the review
-- ended before a verdict.
-- executed: the corrective action ran — any Notion write, or send_only (whose
-- entire correction is delivery). Also set on a skipped or error row when the
-- review stopped after the action. Rate limit and alert count it.
-- reason: the model's own explanation for ok/correct rows, a fixed reason
-- code otherwise. It can name a task, so it is private data: ops queries read
-- it here, never from logs.
-- turn_ref: the LangGraph checkpoint id of the reviewed turn, '' when unknown.

BEGIN;

CREATE TABLE IF NOT EXISTS interaction_reviews (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  peer            TEXT NOT NULL,
  turn_ref        TEXT NOT NULL DEFAULT '',
  intent          TEXT,
  verdict         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (verdict IN ('pending', 'ok', 'correct', 'skipped', 'error')),
  reason          TEXT NOT NULL DEFAULT '',
  action          TEXT
                  CHECK (action IS NULL OR action IN
                         ('none', 'complete_task', 'create_task', 'reopen_task', 'send_only')),
  action_page_id  TEXT,
  executed        BOOLEAN NOT NULL DEFAULT false,
  follow_up_sent  BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS interaction_reviews_peer_created_at_idx
  ON interaction_reviews (peer, created_at);

CREATE INDEX IF NOT EXISTS interaction_reviews_pending_idx
  ON interaction_reviews (created_at) WHERE verdict = 'pending';

COMMIT;
