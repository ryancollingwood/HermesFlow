{{ config(database='pg', schema='data_platform') }}

select
    story_id,
    title,
    url,
    score,
    author,
    to_timestamp(posted_at_epoch) as posted_at,
    comment_count,
    _batch_id as extraction_batch_id,
    _extracted_at as extracted_at
from {{ ref('stg_hn_stories') }}
