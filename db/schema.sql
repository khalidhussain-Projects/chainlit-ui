-- Chainlit data-layer schema for the official ChainlitDataLayer.
-- Run this ONCE per database, against your Postgres instance (Neon, Render,
-- local, etc.). It is idempotent: every CREATE uses IF NOT EXISTS.
--
-- Why this file exists: Chainlit's official data layer assumes the tables
-- exist; it does not auto-create them. See docs/persistence.md for context.
--
-- To run on Neon: open the SQL editor in the Neon dashboard, paste this file's
-- contents, and execute. To run on Render: use the Render Shell for the
-- Postgres instance, or `psql` from a local terminal against the External URL.
--
-- Notes on column types:
--   * All ids are TEXT (Chainlit uses UUIDs as strings).
--   * Timestamps are TIMESTAMPTZ (Chainlit passes timezone-aware datetimes).
--   * JSON columns are JSONB for indexability and parity with the data layer's
--     `metadata::jsonb` casts.
--   * `Element.size` is TEXT — the data layer does not write to it, so we keep
--     it permissive. Same for any column marked "not written by the SDK".

-- ----------------------------------------------------------------------------
-- User
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "User" (
    id          TEXT PRIMARY KEY,
    identifier  TEXT NOT NULL UNIQUE,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- Thread
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "Thread" (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    "userId"    TEXT REFERENCES "User"(id) ON DELETE CASCADE,
    tags        JSONB,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
    "deletedAt" TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_thread_user_id
    ON "Thread" ("userId");

CREATE INDEX IF NOT EXISTS idx_thread_deleted_at
    ON "Thread" ("deletedAt");

-- ----------------------------------------------------------------------------
-- Step
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "Step" (
    id           TEXT PRIMARY KEY,
    "threadId"   TEXT NOT NULL REFERENCES "Thread"(id) ON DELETE CASCADE,
    "parentId"   TEXT,
    input        TEXT,
    output       TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    name         TEXT,
    type         TEXT,
    "startTime"  TIMESTAMPTZ,
    "endTime"    TIMESTAMPTZ,
    "createdAt"  TIMESTAMPTZ NOT NULL DEFAULT now(),
    "showInput"  TEXT,                -- 'json' or null; not written by SDK today
    "isError"    BOOLEAN              -- sent as a Python bool by the data layer
);

CREATE INDEX IF NOT EXISTS idx_step_thread_id
    ON "Step" ("threadId");

CREATE INDEX IF NOT EXISTS idx_step_start_time
    ON "Step" ("startTime");

-- ----------------------------------------------------------------------------
-- Element
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "Element" (
    id            TEXT PRIMARY KEY,
    "threadId"    TEXT NOT NULL REFERENCES "Thread"(id) ON DELETE CASCADE,
    "stepId"      TEXT REFERENCES "Step"(id) ON DELETE CASCADE,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    mime          TEXT,
    name          TEXT,
    "objectKey"   TEXT,
    url           TEXT,
    "chainlitKey" TEXT,
    display       TEXT,
    size          TEXT,
    language      TEXT,
    page          TEXT,
    props         JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_element_thread_id
    ON "Element" ("threadId");

CREATE INDEX IF NOT EXISTS idx_element_step_id
    ON "Element" ("stepId");

-- ----------------------------------------------------------------------------
-- Feedback
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "Feedback" (
    id        TEXT PRIMARY KEY,
    "stepId"  TEXT NOT NULL REFERENCES "Step"(id) ON DELETE CASCADE,
    name      TEXT,
    value     INTEGER,            -- 0 / 1, per the data layer code
    comment   TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_step_id
    ON "Feedback" ("stepId");

-- ----------------------------------------------------------------------------
-- Idempotent repairs for tables created by an earlier version of this file.
-- Safe to re-run on a fresh DB (each block no-ops if the column is already
-- correct).
-- ----------------------------------------------------------------------------

-- Repair: convert "Step"."isError" to BOOLEAN if it was created as TEXT.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'Step'
          AND column_name = 'isError'
          AND data_type <> 'boolean'
    ) THEN
        -- Translate any pre-existing string values to booleans before the
        -- type cast. NULLs stay NULL; unrecognized strings become NULL.
        UPDATE "Step"
        SET "isError" = CASE
            WHEN LOWER("isError") IN ('true', 't', '1', 'yes', 'y') THEN TRUE
            WHEN LOWER("isError") IN ('false', 'f', '0', 'no', 'n') THEN FALSE
            ELSE NULL
        END
        WHERE "isError" IS NOT NULL;

        ALTER TABLE "Step"
            ALTER COLUMN "isError" TYPE BOOLEAN
            USING "isError"::BOOLEAN;
    END IF;
END $$;
