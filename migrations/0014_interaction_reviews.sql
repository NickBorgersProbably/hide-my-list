-- Post-send interaction review jobs and verdicts.
--
-- After a turn's reply is delivered, the listener re-reads the turn with the
-- medium model and may send one corrective follow-up
-- (app/graph/interaction_review.py). Each review is a durable job: the listener
-- inserts a 'pending' row before the review runs; the review claims it
-- ('executing') before its first external effect, and every exit path moves it
-- to a final state. Startup re-reviews or retires rows still 'pending' and
-- finalizes 'executing' rows as error(interrupted), never replaying them. The
-- table is also the rate-limit source (executed corrections per peer per hour),
-- the ops alert source (executed corrections per 24 h), and the corpus for
-- judging whether the reviewer helps or over-corrects.
--
-- verdict: the job state.
--   'pending'   — scheduled, not yet acting;
--   'executing' — claimed: its action and page are recorded and its
--                 effects may have run;
--   'ok'      — the turn was right;
--   'correct' — the reviewer's correction ran;
--   'skipped' — the review yielded: buffer_non_empty, superseded, cancelled,
--               rate_limited, disabled, or timeout (the code is in reason);
--   'error'   — invalid model output, a stale checkpoint, a failed send_only
--               delivery, an interrupted claimed job, or a failure.
-- action: the proposed correction (complete_task or send_only, or none for an
-- ok verdict); NULL while pending and when the review ended before a verdict.
-- executed: the correction took effect — the complete_task Notion write
-- (stored as soon as it succeeds), or a delivered send_only follow-up (whose
-- entire correction is delivery). Also set on a skipped or error row when the
-- review stopped after the effect. Rate limit and alert count it.
-- follow_up_sent: stored as soon as the follow-up is delivered.
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
                  CHECK (verdict IN
                         ('pending', 'executing', 'ok', 'correct', 'skipped', 'error')),
  reason          TEXT NOT NULL DEFAULT '',
  action          TEXT
                  CHECK (action IS NULL OR action IN
                         ('none', 'complete_task', 'send_only')),
  action_page_id  TEXT,
  executed        BOOLEAN NOT NULL DEFAULT false,
  follow_up_sent  BOOLEAN NOT NULL DEFAULT false,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS interaction_reviews_peer_created_at_idx
  ON interaction_reviews (peer, created_at);

CREATE INDEX IF NOT EXISTS interaction_reviews_unfinished_idx
  ON interaction_reviews (created_at) WHERE verdict IN ('pending', 'executing');

COMMIT;
