-- task_text_v1: source changes and their pending assessment are one transaction.
-- This file is frozen with migration 0130; later policy changes need a migration.
CREATE FUNCTION plane_activity_redact(event_ids uuid[]) RETURNS void AS $$
BEGIN
  UPDATE activity_assessment_revisions SET rationale = ''
    WHERE assessment_id IN (SELECT id FROM activity_assessments WHERE event_id = ANY(event_ids));
  UPDATE activity_assessments SET rationale = '', decision = 'needs_review', category = 'unknown'
    WHERE event_id = ANY(event_ids);
  UPDATE activity_source_events SET source_content = '{}', before_content = '{}', redacted = true
    WHERE id = ANY(event_ids);
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION plane_capture_task_text() RETURNS trigger AS $$
DECLARE
  row_new jsonb;
  row_old jsonb;
  task_id uuid;
  project_key uuid;
  source_key uuid;
  source_type text;
  next_content jsonb;
  previous_content jsonb;
  captured timestamptz := clock_timestamp();
  writer uuid := NULLIF(current_setting('plane.activity_writer', true), '')::uuid;
  producer text := current_setting('plane.activity_producer', true);
  quality text;
  source_time timestamptz;
  previous_event activity_source_events%ROWTYPE;
  next_revision bigint;
  provenance_changed boolean := false;
  hidden boolean := false;
  redact_ids uuid[];
BEGIN
  IF TG_OP <> 'DELETE' THEN row_new := to_jsonb(NEW); END IF;
  IF TG_OP <> 'INSERT' THEN row_old := to_jsonb(OLD); END IF;
  source_key := (COALESCE(row_new, row_old)->>'id')::uuid;
  project_key := (COALESCE(row_new, row_old)->>'project_id')::uuid;
  source_type := CASE WHEN TG_TABLE_NAME = 'issues' THEN 'description' ELSE 'comment' END;
  task_id := CASE WHEN source_type = 'description' THEN source_key
    ELSE (COALESCE(row_new, row_old)->>'issue_id')::uuid END;

  -- Serialize with assessments and all other text writers for this task.
  -- NO KEY UPDATE is compatible with the FK key-share locks of new comments.
  PERFORM id FROM issues WHERE id = task_id FOR NO KEY UPDATE;
  IF TG_OP = 'DELETE' OR row_new->>'deleted_at' IS NOT NULL THEN hidden := true; END IF;
  IF TG_OP = 'UPDATE' AND (
      row_old->>'project_id' IS DISTINCT FROM row_new->>'project_id'
      OR row_old->>'issue_id' IS DISTINCT FROM row_new->>'issue_id'
      OR row_old->>'access' IS DISTINCT FROM row_new->>'access'
  ) THEN hidden := true; END IF;

  IF hidden THEN
    SELECT array_agg(id) INTO redact_ids FROM activity_source_events
      WHERE (source_type = 'description' AND issue_id = task_id)
         OR (source_kind = source_type AND source_id = source_key);
    PERFORM plane_activity_redact(COALESCE(redact_ids, '{}'::uuid[]));
    UPDATE activity_coverage SET generation = generation + 1, captured_from = captured
      WHERE issue_id = task_id OR issue_id = (row_old->>'issue_id')::uuid;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
  END IF;

  quality := CASE WHEN writer IS NULL THEN 'unattributed'
    WHEN producer = 'native' THEN 'native' ELSE 'import' END;
  provenance_changed := TG_OP = 'UPDATE' AND (
      row_old->>'created_at' IS DISTINCT FROM row_new->>'created_at'
      OR row_old->>'created_by_id' IS DISTINCT FROM row_new->>'created_by_id'
      OR row_old->>'external_source' IS DISTINCT FROM row_new->>'external_source'
      OR row_old->>'external_id' IS DISTINCT FROM row_new->>'external_id'
  );
  IF provenance_changed OR (TG_OP = 'INSERT' AND (
    COALESCE(row_new->>'external_source', '') <> '' OR COALESCE(row_new->>'external_id', '') <> ''
  )) THEN quality := 'import'; END IF;
  source_time := CASE WHEN quality = 'native' THEN captured ELSE NULL END;

  INSERT INTO activity_coverage (
    issue_id, captured_from, known_existing_at, native_created_at, generation, source_schema_version
  ) VALUES (task_id, captured, captured,
    CASE WHEN source_type = 'description' AND TG_OP = 'INSERT' THEN source_time ELSE NULL END,
    0, 'task_text_v1') ON CONFLICT (issue_id) DO NOTHING;
  IF source_type = 'description' AND provenance_changed THEN
    UPDATE activity_coverage SET native_created_at = NULL WHERE issue_id = task_id;
  END IF;

  IF source_type = 'description' THEN
    next_content := jsonb_build_object('html', row_new->'description_html',
      'text', row_new->'description_stripped', 'json', row_new->'description_json',
      'binary_digest', md5(COALESCE(row_new->>'description_binary', '')));
    previous_content := CASE WHEN TG_OP = 'INSERT' THEN '{}'::jsonb ELSE
      jsonb_build_object('html', row_old->'description_html', 'text', row_old->'description_stripped',
        'json', row_old->'description_json',
        'binary_digest', md5(COALESCE(row_old->>'description_binary', ''))) END;
  ELSE
    next_content := jsonb_build_object('html', row_new->'comment_html',
      'text', row_new->'comment_stripped', 'json', row_new->'comment_json');
    previous_content := CASE WHEN TG_OP = 'INSERT' THEN '{}'::jsonb ELSE
      jsonb_build_object('html', row_old->'comment_html', 'text', row_old->'comment_stripped',
        'json', row_old->'comment_json') END;
  END IF;

  SELECT * INTO previous_event FROM activity_source_events
    WHERE source_kind = source_type AND source_id = source_key AND capture_transaction = txid_current();
  -- Late import fields in API serializers must downgrade the already captured
  -- revision without creating a fake progress event for audit-only changes.
  IF TG_OP = 'UPDATE' AND next_content = previous_content AND NOT hidden THEN
    IF provenance_changed AND previous_event.id IS NOT NULL THEN
      UPDATE activity_source_events SET time_quality = 'import', occurred_at = NULL
        WHERE id = previous_event.id;
    END IF;
    RETURN NEW;
  END IF;
  UPDATE activity_coverage SET generation = generation + 1 WHERE issue_id = task_id;
  IF previous_event.id IS NOT NULL THEN
    UPDATE activity_source_events SET source_content = next_content,
      occurred_at = CASE WHEN previous_event.time_quality <> 'native' THEN NULL ELSE source_time END,
      observed_at = captured,
      time_quality = CASE WHEN previous_event.time_quality <> 'native' THEN previous_event.time_quality ELSE quality END,
      is_baseline = (source_type = 'description' AND previous_event.before_content = '{}'::jsonb)
        OR previous_event.before_content = next_content,
      redacted = hidden OR previous_event.redacted
      WHERE id = previous_event.id;
  ELSE
    SELECT COALESCE(MAX(source_revision), 0) + 1 INTO next_revision FROM activity_source_events
      WHERE source_kind = source_type AND source_id = source_key;
    INSERT INTO activity_source_events (
      id, issue_id, project_id, source_kind, source_id, source_revision, capture_transaction,
      mutation_id, authenticated_writer_id, source_content, before_content,
      occurred_at, observed_at, time_quality, is_baseline, redacted, source_schema_version
    ) VALUES (gen_random_uuid(), task_id, project_key, source_type, source_key, next_revision, txid_current(),
      COALESCE(current_setting('plane.activity_mutation', true), ''), writer,
      next_content, previous_content, source_time, captured, quality,
      (source_type = 'description' AND TG_OP = 'INSERT'), hidden, 'task_text_v1');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Existing history has no trustworthy continuous observation boundary. New
-- capture begins now; historical contents aren't retrospectively called progress.
INSERT INTO activity_coverage (
  issue_id, captured_from, known_existing_at, native_created_at, generation, source_schema_version
) SELECT id, clock_timestamp(), clock_timestamp(), NULL, 0, 'task_text_v1'
  FROM issues WHERE deleted_at IS NULL;

CREATE TRIGGER plane_activity_issue_capture AFTER INSERT OR UPDATE OR DELETE ON issues
  FOR EACH ROW EXECUTE FUNCTION plane_capture_task_text();
CREATE TRIGGER plane_activity_comment_capture AFTER INSERT OR UPDATE OR DELETE ON issue_comments
  FOR EACH ROW EXECUTE FUNCTION plane_capture_task_text();
