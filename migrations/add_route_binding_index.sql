BEGIN;

CREATE INDEX IF NOT EXISTS messages_route_binding_lookup_idx
    ON assistant.messages(channel,chat_id,user_id,created_at DESC,message_id DESC)
    WHERE direction='system' AND message_type='route_binding';

WITH latest_handoff AS (
    SELECT DISTINCT ON (m.channel,m.chat_id,m.user_id)
        m.*,r.agent_key
    FROM assistant.messages m
    JOIN assistant.runs r ON r.run_id=m.run_id
    WHERE m.direction='inbound' AND m.message_type='handoff'
      AND r.status IN ('queued','running','waiting_user','reconciling')
      AND NOT EXISTS (
          SELECT 1 FROM assistant.messages binding
          WHERE binding.direction='system' AND binding.message_type='route_binding'
            AND binding.channel=m.channel AND binding.chat_id=m.chat_id
            AND binding.user_id IS NOT DISTINCT FROM m.user_id
      )
    ORDER BY m.channel,m.chat_id,m.user_id,m.created_at DESC,m.message_id DESC
)
INSERT INTO assistant.messages(
    message_id,project_id,channel,chat_id,user_id,direction,message_type,
    content_json,delivery_status,idempotency_key
)
SELECT
    md5('route-binding-backfill:' || message_id::text)::uuid,
    project_id,channel,chat_id,user_id,'system','route_binding',
    jsonb_build_object(
        'schema_version','route-binding.v1',
        'status','active',
        'project_id',project_id,
        'agent_key',agent_key,
        'source_run_id',content_json->>'source_run_id',
        'source_message_id',content_json->>'source_message_id',
        'reason','active_handoff_backfill'
    ),
    'received','route-binding-backfill:' || run_id::text
FROM latest_handoff
ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING;

COMMIT;
