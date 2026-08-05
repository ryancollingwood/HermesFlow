# Tuning Hindsight's LLM config

Operational guide for the `hindsight` service — the memory layer behind Hermes's
retain and recall tools. Read this before changing any `HINDSIGHT_*` variable in
`.env` or `docker-compose.yml`.

The retain path fails *silently*: it reports success while writing nothing, or
while writing memories with no entity graph. Most of this document is about
detecting that, because nothing in the container's health, logs or job status
will tell you.

**Conventions in the examples below.** Substitute your own values:

| placeholder | meaning |
|---|---|
| `$API` | the Hindsight API container name |
| `$DB` | the Postgres container name |
| `$BANK` | the memory bank id |
| `$BASE` | the API base URL, e.g. `http://127.0.0.1:8888` |

**On the numbers.** Measurements here come from a local single-slot deployment
running a small (4B-class) instruct model against an OpenAI-compatible server.
Absolute latencies and token counts will differ on your hardware and model — the
*ratios* and the *direction* are what transfer. Re-measure before treating any
figure as a target.

---

## 1. Failures here are silent

The retain path fails *successfully* in several ways. None produce an error the
caller or the container can see:

| failure | what you observe | what actually happened |
|---|---|---|
| thinking/reasoning runaway | nothing; writes stop | reasoning tokens consumed the whole completion budget, `finish_reason=length`, empty content |
| zero-fact extraction | nothing; document row exists | extraction returned **0 facts** and still reported success |
| entities omitted | nothing — **freshness looks perfectly healthy** | facts extracted fine, but most returned `entities: []`; the entity graph never populates |
| model not loaded | nothing; writes stop | the inference server returns `400 Model unloaded`; the retain fails and the caller swallows it |

A retain that fails still returns to the caller, and `async_operations` records
`completed`. A deployment exhibiting the entity failure showed **hundreds of
retain and consolidation operations, all `status=completed`, zero failures** —
while its entity graph was empty. **Job status is not a health signal here.**

Data freshness is a better signal, but it is *necessary, not sufficient*: it
catches writes stopping, never writes arriving incomplete. The entity failure
passes a freshness check with flying colours. Use all the queries in §7.

---

## 2. A known-good baseline

Start here and change one thing at a time.

| variable (`.env`) | value | rationale |
|---|---|---|
| `HINDSIGHT_LLM_MODEL` | a small **Instruct** model | non-reasoning variants remove the runaway failure class by construction (§3) |
| `HINDSIGHT_RETAIN_LLM_MODEL` | *same as above* | one model everywhere — see §3 |
| `HINDSIGHT_CONSOLIDATION_LLM_MODEL` | *same as above* | ditto |
| `HINDSIGHT_REFLECT_LLM_MODEL` | *same as above* | ditto |
| `HINDSIGHT_API_LLM_EXTRA_BODY` | `{"chat_template_kwargs": {"enable_thinking": false}}` | belt-and-braces thinking disable for models that support the flag |
| `HINDSIGHT_LLM_STRICT_SCHEMA` | `true` | many local servers reject the soft `json_object` fallback; also what makes entity extraction work on ≥0.8.5 (§4) |
| `HINDSIGHT_LLM_MAX_CONCURRENT` | `1` | for a single-slot local server; the image default is 32, which is wildly wrong there |
| `HINDSIGHT_LLM_TIMEOUT` | generous, but not hours | must exceed real latency; an over-long timeout lets a wedged call pin the only slot |
| `HINDSIGHT_RETAIN_EXTRACTION_MODE` | `concise` | correct **and** cheapest on ≥0.8.5 — see §4 |
| `HINDSIGHT_RETAIN_CHUNK_SIZE` | a few thousand chars | small models extract poorly from very large chunks |
| `HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS` | **>** chunk size, with headroom | see §4 for how mode affects this |
| `HINDSIGHT_API_WORKER_MAX_SLOTS` | `2` | reserved pools run concurrently even at 1 each; a spare shared slot adds a third contender for one inference slot |
| `HINDSIGHT_LOG_LEVEL` | `info` | **lowercase** — uppercase crash-loops the container on ≥0.8.5 (§8) |

**Pin the image tag.** `:latest` means the config surface — which variables exist
and what they default to — can change under you silently, with no code change on
your side.

---

## 3. One model, one slot

If you are running a single local inference slot, configuring different models
per operation guarantees eviction and reload thrash. That is what
`400: Model has not started loading/has been unloaded` errors are. Many local
servers also disable just-in-time loading by default, so requesting an unloaded
model returns an error rather than loading it on demand.

**Avoid reasoning models for retain.** Their reasoning tokens can consume the
entire completion budget, leaving empty content and `finish_reason=length` — and
the retain still reports success. One measured comparison, same corpus, same
single-slot server:

| | per successful extraction | failure mode |
|---|---|---|
| 27B reasoning model | ~69s (and ~383s before erroring) | empty extractions, `Model unloaded` errors |
| 4B instruct model | ~12–18s | none observed |

An Instruct variant removes the failure class by construction rather than by
config, and is small enough to stay resident.

**Upstream publishes no guidance that consolidation needs a stronger model than
retain** — the docs treat the LLM as a single role, and the published benchmarks
cover retain and reflect only. Using one model everywhere is a reasonable default
until you have measured otherwise.

**Always unload before loading a different model.** Loading a second large model
alongside a resident one can exhaust memory and take the inference server down,
which takes *all* inference with it. Check what is resident before switching.

---

## 4. Extraction mode and the entity graph

**`concise` is correct on image ≥0.8.5.** This section exists because on
**≤0.8.4 the same setting silently produced no entity graph at all**, and the
failure is invisible — so anyone pinned to an older image, or investigating a
historically empty graph, needs to recognise it.

Measured on one deployment, same document, same model:

| image | mode | entities | output tokens | wall time |
|---|---|---|---|---|
| 0.8.4 | `concise` | **0** | 214 | 12s |
| 0.8.4 | `verbose` | 25 | ~2,200 | — |
| **≥0.8.5** | **`concise`** | **8**, all correctly named | 409 | 18s |
| ≥0.8.5 | `verbose` | 31 — but most extras are abstract noise (`data integrity`, `data continuity`) | 1,484 | **157s** |

Under 0.8.4 that bank had accumulated tens of entities and a handful of
`unit_entities` links across **thousands** of memory units — effectively no
graph — with every retain reporting success. Roughly **90% of extracted facts**
came back with `entities: []`.

**The obvious explanation is wrong.** `concise`'s prompt *does* contain a
dedicated `ENTITIES` section with worked examples. The instruction was present
and ignored. The mechanism: 0.8.4's retain path sent
`response_format.model_json_schema()` — in which `entities` carries a default and
is therefore never in the schema's `required` set — together with
`"strict": true`. The grammar was enforcing a schema that did not require
entities, so the model omitted the field and validation filled `[]`.

0.8.5 fixes it three ways: a new `engine/structured_output.py` regenerates the
schema with `required = list(properties)` whenever `STRICT_SCHEMA` is on; the
prompt gained *"Use an empty array [] only when the fact truly names nothing"*;
and `entities` became plain `list[str]` instead of nested objects.

**Do not switch to `verbose` on ≥0.8.5.** It buys abstract-concept noise for
several times the tokens and roughly an order of magnitude more wall time. It is
only the right call if you are pinned below 0.8.5 and need a graph.

> **Completion-token headroom scales with extraction mode**, not just chunk size.
> `verbose` emitted ~4x the output tokens of `concise` on the same input. If you
> change mode, re-check `RETAIN_MAX_COMPLETION_TOKENS` — it must stay above chunk
> size, with room for the mode you actually run, or output truncates mid-JSON.

> **The fix is forward-only.** Memory units retained while the bug was active
> keep their empty graph until the source documents are re-retained.

---

## 5. Verify settings against the image, not the docs

The authority for env-var names and defaults is the **running image's own**
`config.py` — its docstring says all environment variables and their defaults are
defined there, and it is more reliable than published documentation, which does
not always match:

```bash
docker exec $API sh -c \
  'grep -nE "^(ENV|DEFAULT)_[A-Z0-9_]+ *=" /app/api/hindsight_api/config.py'
```

Two settings that commonly appear in configs do **nothing**:

- `HINDSIGHT_LLM_RESPONSE_FORMAT` — `RESPONSE_FORMAT` appears **zero** times in
  `config.py`. `LLM_STRICT_SCHEMA` is the real control.
- `HINDSIGHT_LLM_REASONING_EFFORT` — `_supports_reasoning_model()` in
  `engine/providers/openai_compatible_llm.py` only sends `reasoning_effort` when
  the model name contains `gpt-5`, `o1` or `o3` (or a thinking DeepSeek variant).
  For every other model it is silently ignored. To influence reasoning on a model
  outside that allow-list, use `LLM_EXTRA_BODY`.

### The prefix remap, and how it bites

The image reads `HINDSIGHT_API_*`. Configs frequently use bare `HINDSIGHT_*`
names and remap them in compose. **A partial remap is worse than none.** A single
line reading `${HINDSIGHT_API_LLM_TIMEOUT}` while `.env` defines
`HINDSIGHT_LLM_TIMEOUT` looks correct next to its working neighbours, but that
value is never wired and the hardcoded compose default silently wins. Verify
**per variable**, against the container:

```bash
docker exec $API env | grep '^HINDSIGHT_API_' | sort
```

### The `LLM_EXTRA_BODY` compose trap

`HINDSIGHT_API_LLM_EXTRA_BODY` **cannot** be given a literal default in compose:
`${VAR:-...}` ends at the first unmatched `}`, and the bare `: ` inside the JSON
breaks YAML parsing (`mapping values are not allowed in this context`). Use
`'${HINDSIGHT_API_LLM_EXTRA_BODY:-null}'` in compose and put the real value in
`.env`.

---

## 6. Bank config vs. environment

Bank config is snapshotted at bank creation. A bank whose `config` is `{}` falls
back to the environment; one with an explicit value ignores it. **Changing an env
var and restarting does nothing for a bank that already has that key set.**

```sql
select bank_id, config from banks;
```

To change an existing bank, PATCH it — note the `{"updates": {...}}` wrapper, as
a bare body 422s:

```bash
curl -s -X PATCH "$BASE/v1/default/banks/$BANK/config" \
  -H 'Content-Type: application/json' \
  -d '{"updates": {"retain_extraction_mode": "concise"}}'
```

---

## 7. Diagnose with `llm_requests`, not by inference

`GET /v1/default/banks/{id}/llm-requests` — and the `llm_requests` table behind
it — return the **actual system prompt, user message and raw model output** per
call. This is the only way to distinguish entity omission from truncation from
degenerate repetition, and it is what identifies problems like §4. Use it
*before* inferring model behaviour from fact counts.

Two non-obvious details: `input` is a jsonb **array of messages** (not an object
with a `.messages` key), and `output` is a JSON **string** needing a second
decode via `#>> '{}'`.

```sql
-- how often is the model returning facts with NO entities?
with f as (
  select jsonb_array_elements((lr.output #>> '{}')::jsonb -> 'facts') fx
  from llm_requests lr
  where lr.scope = 'retain_extract_facts' and lr.status = 'success')
select count(*) facts,
       count(*) filter (where jsonb_array_length(fx->'entities') = 0) empty_entities
from f;

-- read the actual prompt that was sent
select m->>'role', m->>'content'
from llm_requests lr, lateral jsonb_array_elements(lr.input) m
order by lr.started_at desc limit 2;
```

### Health queries

Run magnitude **before** purity — the pollution query cannot distinguish a clean
graph from a non-existent one, since zero rows is both the pass condition and the
symptom of having no entities at all.

```sql
-- 1. FRESHNESS. Stale while the container is healthy = a §1 failure.
select count(*), max(created_at) from memory_units where bank_id = '<bank>';

-- 2. MAGNITUDE. A links:units ratio near zero means entities are being
--    omitted entirely (§1, §4) — purity is meaningless until this passes.
select (select count(*) from entities     where bank_id = '<bank>') entities,
       (select count(*) from unit_entities)                         links,
       (select count(*) from memory_units where bank_id = '<bank>') units;

-- 3. PURITY. Generic-entity pollution; should be zero.
select canonical_name, mention_count from entities
where bank_id = '<bank>'
  and canonical_name ~* '^(the )?(user|users|assistant|agent|someone)s?$';

-- 4. SILENTLY LOST DOCUMENTS — extraction returned nothing, caller saw success.
select d.id from documents d
left join memory_units m on m.document_id = d.id and m.bank_id = d.bank_id
where d.bank_id = '<bank>'
group by d.id having count(m.id) = 0;
```

---

## 8. Upgrading the image

Diff the config surface between the old and new image before upgrading — new
variables, removed variables, and above all **changed defaults**, which alter
behaviour with no change on your side:

```bash
docker exec $API sh -c \
  'grep -oE "^DEFAULT_[A-Z0-9_]+ *= *[^#]*" /app/api/hindsight_api/config.py' | sort
```

Migrations apply automatically on boot (`RUN_MIGRATIONS_ON_STARTUP` defaults to
**true**), so a new container is also a schema change. **Back up first** — that
is the only hard-to-reverse step:

```bash
docker exec $DB pg_dump -U <user> <db> | gzip > hindsight-pre-upgrade.sql.gz
```

> ⚠️ **A config diff is not sufficient.** Both problems below were invisible to a
> diff of variable names and defaults, because neither is a config-surface
> change. Budget for a real boot plus a probe (§9).

### `LOG_LEVEL` must be lowercase on ≥0.8.5

0.8.5 passes the value straight to uvicorn, whose `LOG_LEVELS` dict is keyed on
lowercase names. An uppercase `INFO` crash-loops the container:

```
File "uvicorn/config.py", line 390, in configure_logging
    log_level = LOG_LEVELS[self.log_level]
KeyError: 'INFO'
```

Earlier images tolerated uppercase. It dies **before the HTTP server binds**, so
it presents as "the container never becomes healthy" rather than as a config
error — check the container logs, not the healthcheck.

### Strict-schema grammar can crash a local inference server

0.8.5's all-fields-`required` schema (§4) makes grammar-constrained decoding much
stricter, and not every local backend survives it. Observed on an MLX backend
with a 4B model, roughly **1 call in 3**:

```
Encountered fatal exception in the backend scheduler:
  ... outlines/processors/structured.py, line 112, in process_logits
      prev_state = self._guide_states[hash(tuple(...))]
  KeyError: <n>
```

A cache miss in `outlines`' grammar guide-state table, most likely tied to the
server's **batched generation** setting. Observed behaviour:

- **Flaky, not deterministic.** The same document failed once, then succeeded on
  two immediate retries and a clean run. Do not condemn a model or a schema on a
  single failure.
- It surfaces as a **hard HTTP 400 to the caller**, not a silent empty
  extraction — better than failing quietly, but it does mean occasional loud
  retain failures.

**Mitigation:** lower the inference server's parallel/batch setting first. The
per-operation `HINDSIGHT_API_LLM_STRICT_SCHEMA_RETAIN=false` also silences it,
but it restores the loose schema on the retain path *and with it the
empty-entities bug from §4* — self-defeating unless you also go back to
`verbose`.

### `FAIL_ON_EXTRACTION_ERRORS` does not close §1

0.8.5 adds this opt-in flag, which flips a retain to `failed` instead of
`completed` when chunks error. Worth enabling — but it counts only *hard* errors:
missing batch result, API error, no `choices`, JSON parse failure, non-dict JSON.
A valid `{"facts": []}` response is logged at debug only, and `entities: []` is
not an error at all. It would catch **neither** the zero-fact row nor the entity
failure in §1. Keep the §7 queries.

---

## 9. Probing safely

Never probe against a production bank. Use a scratch bank and delete it
afterwards.

Two undocumented API gotchas: **there is no create endpoint** — `POST
/v1/default/banks` returns `405 Method Not Allowed` — and a plain `GET
.../config` **silently creates** the bank with defaults. So the sequence is GET
(which creates it), then PATCH, then retain. Retain also needs an `items` array
wrapper; a bare `{"content": ...}` 422s.

```bash
PROBE=probe-$(date +%s)
curl -s "$BASE/v1/default/banks/$PROBE/config" > /dev/null     # creates it
curl -s -X PATCH "$BASE/v1/default/banks/$PROBE/config" \
  -H 'Content-Type: application/json' \
  -d '{"updates": {"retain_extraction_mode": "concise"}}'
curl -s -X POST "$BASE/v1/default/banks/$PROBE/memories" \
  -H 'Content-Type: application/json' \
  -d '{"items": [{"content": "…test document…"}]}'
# … inspect via §7, then:
curl -s -X DELETE "$BASE/v1/default/banks/$PROBE"
```

Protocol that avoids wasted runs:

1. **Quiet any scheduled ingest first.** Contention through a single inference
   slot will dominate your timings.
2. **Use a fresh bank per run** — bank config snapshots at creation, and content
   hashing will otherwise skip your test document on a re-run.
3. **Compare against the same source document**, and check the extracted facts
   against the source text. Fact *counts* alone hide corrupt fields and wrong
   dates.
4. **Change one variable at a time.**
5. **Clean up**, and restore any schedule, model and `.env` you touched.

> ⚠️ If you ever do write test data into a real bank, note that `DELETE`ing a
> document does **not** cascade to the entities it created. They survive as
> orphans with stale `mention_count`s and corrupt every later entity
> measurement:
>
> ```sql
> delete from entities e
> where e.bank_id = '<bank>'
>   and e.first_seen > now() - interval '30 minutes'   -- scope to YOUR probe
>   and not exists (select 1 from unit_entities ue where ue.entity_id = e.id);
> ```

---

## 10. Things to check in any deployment

- **Is the entity graph actually populated?** Query 2 in §7. A near-zero
  links:units ratio is the single most common silent failure.
- **Are there documents with zero memory units?** Query 4 in §7. Each one is a
  document whose content was lost while the caller saw success.
- **Are there orphaned entities** with no `unit_entities` links, inflating entity
  counts and hiding the ratio above?
- **Does a graph fix need backfilling?** Extraction fixes are forward-only.
  Memory units written while a bug was active keep their bad state until the
  source documents are re-retained.
- **Is the consolidation model choice justified?** There is no upstream guidance
  and no published consolidation benchmark, so whatever you have chosen is a
  default, not a measurement.
