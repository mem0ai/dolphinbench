CREATE TABLE IF NOT EXISTS submissions (
  id uuid PRIMARY KEY,
  account_id uuid REFERENCES partner_accounts(id),
  browser_hash text,
  ip_hash text,
  contact_email text,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
  harness text NOT NULL CHECK (length(harness) BETWEEN 1 AND 120),
  model text NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
  memory text NOT NULL CHECK (length(memory) BETWEEN 1 AND 120),
  filename text NOT NULL CHECK (length(filename) BETWEEN 1 AND 200),
  expected_size bigint NOT NULL CHECK (expected_size > 0 AND expected_size <= 268435456),
  pathname text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'uploading'
    CHECK (status IN ('uploading', 'queued', 'validating', 'accepted', 'rejected', 'removed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  upload_expires_at timestamptz NOT NULL DEFAULT now() + interval '1 hour',
  blob_url text,
  blob_deleted boolean NOT NULL DEFAULT false,
  blob_sha256 text,
  release_sha256 text,
  validator_sha256 text,
  summary jsonb,
  error_code text,
  error_message text,
  attempts integer NOT NULL DEFAULT 0,
  lease uuid,
  lease_until timestamptz,
  retry_after timestamptz NOT NULL DEFAULT now(),
  removed_by uuid REFERENCES partner_accounts(id),
  removal_reason text,
  CHECK (status <> 'accepted' OR
    (blob_url IS NOT NULL AND blob_sha256 IS NOT NULL AND summary IS NOT NULL
      AND release_sha256 IS NOT NULL AND validator_sha256 IS NOT NULL))
);
ALTER TABLE submissions ALTER COLUMN account_id DROP NOT NULL;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS browser_hash text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS ip_hash text;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS contact_email text;
CREATE INDEX IF NOT EXISTS submissions_browser_created ON submissions (browser_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS submissions_ip_created ON submissions (ip_hash, created_at DESC);
CREATE INDEX IF NOT EXISTS submissions_account_created ON submissions (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS submissions_status_created ON submissions (status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS submissions_accepted_archive ON submissions (blob_sha256)
  WHERE status = 'accepted';
