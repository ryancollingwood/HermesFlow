-- Raw layer is append-only (a story can appear once per extraction run), so
-- dedupe to the latest snapshot per story here rather than relying on the
-- source to merge — keeps the mart idempotent across repeated extractions.
with ranked as (
    select
        id as story_id,
        title,
        url,
        score,
        "by" as author,
        time as posted_at_epoch,
        descendants as comment_count,
        _extracted_at,
        _job_id,
        _pipeline,
        _batch_id,
        _source_url,
        row_number() over (partition by id order by _extracted_at desc) as rn
    from {{ source('datalake', 'hn_stories') }}
)

select
    story_id,
    title,
    url,
    score,
    author,
    posted_at_epoch,
    comment_count,
    _extracted_at,
    _job_id,
    _pipeline,
    _batch_id,
    _source_url
from ranked
where rn = 1
