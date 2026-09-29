# Handoff: Batch LLM Agent Calls + Herd Sentiment Fallback

## ⚠️ Working agreement — read first
This feature must not be implemented solo. Whoever picks this up (human or
agent) must **spawn a subagent for every request/step of this work** — e.g.
one subagent to implement Phase A, another to review/verify it, another for
Phase B, etc. Do not silently do multi-step implementation in a single pass.
This applies to every future session that touches this plan, not just the
first one.

## Goal
Cut Gemini 429s in market-sim's LLM advisor by:
1. Batching multiple agents' persona decisions into a single API call.
2. Letting agents that don't get a live LLM slot this tick still act on a
   decaying "herd sentiment" signal derived from recently applied LLM
   decisions of the same psych_profile.
3. Capping eligible agents per tick to the batch/slot budget (top-K by
   "interesting" + fairness — falls out of batching, not a separate feature).

Personality controls are continuous **population-share sliders**, not boolean
switches: `disciplined_ratio` and `bagholder_ratio` are slider values, and
averager is the remaining share. Each agent receives one realized profile label;
its pain threshold, greed threshold, and reaction delay remain continuous,
per-agent values. Do not bin/round the slider controls or replace them with
booleans for batching. Group requests by realized `(agent_type, psych_profile)`
and include each agent's individual continuous values in its situation block.

## Implementation status
- Phase A is implemented: requests are grouped by realized profile, with a
   configurable batch cap of 11 (`SIMPASAR_LLM_BATCH_SIZE`) and per-agent result
   mapping.
- Phase B is implemented: accepted LLM decisions feed decaying per-profile
   sentiment (`SIMPASAR_LLM_SENTIMENT_WEIGHT`, default 0.25;
   `SIMPASAR_LLM_SENTIMENT_HALFLIFE_S`, default 60 seconds).
- Batch size 11 is a maximum, not a promise that every profile group contains
   11 eligible candidates. Slider ratios determine actual group sizes.
- Groups rotate least-recently-served first; urgency selects agents within
   each selected group to avoid one group monopolizing the available slots.

Adaptive RPM auto-tuning / honoring `Retry-After` is explicitly OUT of scope
(not selected by the user) — batching is quota-agnostic and is the primary
lever.

## Current architecture (verified against the code)
- `LLMAdvisor` (`sim/llm_advisor.py`): token-bucket rate limiter
  (`rpm`/`concurrency`), per-agent `offer()` registers a candidate +
  prompt-builder callback without I/O; `end_tick()` sorts the pool
  (interesting-first, then least-recently-served) and submits up to
  `slots = min(floor(tokens), concurrency-inflight, remaining_budget)`
  **individual** requests, 1 agent per request, to a queue drained by
  `concurrency` daemon worker threads (`_worker` -> `_call`).
- `agents.py::_build_persona_prompt(agent, base_order, price, gut_seed)`
  builds the full prompt text (persona + situation + JSON instruction) per
  agent.
- `agents.py::Agent._psych_override`: if advisor present, calls
  `advisor.collect()` (non-blocking check of a previous future), else falls
  back to deterministic jittered rules per psych_profile
  (disciplined/bagholder/averager), and calls `advisor.offer()` to register
  itself for a future LLM slot.
- Response caching: per-agent `Future` in `agent._llm_future`; valid for
  `max_age_s=45s` and P&L drift <=15%, checked in `collect()`.
- Error handling: exponential backoff, 429 -> min 30s backoff, 401/403 ->
  permanent disable. SDK retries disabled (`attempts=1`) so the advisor's own
  budget accounting stays authoritative.
- The new `tests/test_llm_advisor.py` covers response mapping, profile isolation,
   sentiment expiry, and fallback blending.

## Steps

### Phase A — Batched multi-agent prompts, grouped by realized profile (primary fix)
Directly multiplies effective agents-served per API request; independent of
exact quota. Batch cap = **11** (9 possible combinations: 3 `agent_type` x 3
`psych_profile`; balanced slider ratios would yield ~11-12 agents per group
out of ~100 total, but actual group sizes vary with the sliders).
Batches never mix personalities, so the verbose persona paragraph is sent
ONCE per batch instead of once per agent — cuts prompt tokens substantially
in addition to cutting request count (helps TPM limits too, not just RPM).

1. `sim/agents.py`: replace `_build_persona_prompt` with two pieces:
   - `_persona_header(agent_type, psych_profile) -> str`: the shared persona
   paragraph + base-signal explanation, built once per batch (identical
   for every agent in the batch since they share the realized
   `(agent_type, psych_profile)`).
   - `_agent_situation_line(agent, base_order, price, gut_seed) -> str`:
   compact per-agent block tagged `[AGENT <id>]` with
   price/entry/pnl/position/capital/pain threshold/greed threshold/reaction
   delay/gut_seed — no persona prose repeated here.
2. `sim/llm_advisor.py::offer()`: extend candidate registration to also
   capture a `group_key = (agent.agent_type, agent.psych_profile)` and a
   `header_builder` callback, alongside the existing per-agent
   `fragment_builder`/price. `llm_advisor` stays persona-agnostic — it only
   treats `group_key` as an opaque grouping token and calls
   `header_builder()` once per batch (from the first candidate in the
   group).
3. `sim/llm_advisor.py`: add constructor param `batch_size: int = 11` (env
   `SIMPASAR_LLM_BATCH_SIZE`, read in `agents.py::get_llm_advisor()`, clamp
   e.g. 1-16).
4. `sim/llm_advisor.py::end_tick()`: bucket the deduped/filtered candidate
   pool by `group_key` (up to 9 buckets). Within each bucket, sort by the
   existing criteria (interesting-first, then least-recently-served). Rank
   buckets by least-recently-served group first (first-time groups first;
   urgency order breaks ties), then take buckets until `slots` (existing
   token/concurrency/budget-bounded batch count) is exhausted; from each
   selected bucket, take up to `batch_size` agents for one batch (a bucket
   with more than `batch_size` eligible agents spills into a later tick,
   same as today's fairness/staleness handling). Each batch = 1 token + 1
   concurrency slot + 1 `_stats["requested"]` (was previously 1 per agent)
   and this slice is also the top-K eligibility cap (Phase C).
5. Update `RESPONSE_FORMAT` schema (`llm_advisor.py`) to describe an array
   response: `{"decisions": [{"id": int, "order": number, "reason": string}, ...]}`.
6. `_jobs.put(...)` payload becomes
   `(batch_id, [(future, agent_local_id), ...], combined_prompt)`. Combined
   prompt = `header_builder()` (once) + join of each agent's situation line
   (tagged `[AGENT <id>]`) + one shared footer instructing the model to
   return a `decisions` array with one entry per agent id seen above.
7. `_worker()` / new `_call_batch()`: parse the array response, match
   entries by id, `future.set_result((order, reason))` per matched agent;
   any agent id missing from the response or malformed entry resolves to
   `None` (safe fallback — same behavior as today's per-agent parse
   failure). On exception, all futures in the batch resolve to `None` and
   exactly one error is counted / one backoff triggered (previously would
   have been N separate error counts if these had been N requests).
8. Update usage/token accounting: batch response usage recorded once per
   batch (already aggregate counters, no change needed beyond using the
   single interaction's usage).
9. `stats()`: expose `batch_size` for visibility in the browser/log.

### Phase B — Herd sentiment fallback for un-served agents
Depends on Phase A having applied decisions to react to; can be built in
parallel and wired in last.

1. `sim/llm_advisor.py`: add
   `self._sentiment: dict[str, tuple[float, float]]` =
   `psych_profile -> (ema_order, last_update_monotonic)`. Add constructor
   params `sentiment_weight: float = 0.25`
   (env `SIMPASAR_LLM_SENTIMENT_WEIGHT`, 0 disables) and
   `sentiment_halflife_s: float = 60.0`
   (env `SIMPASAR_LLM_SENTIMENT_HALFLIFE_S`).
2. In `collect()`, whenever a result is accepted (`why is None`), update the
   EMA for `agent.psych_profile` using the decayed-weighted average of
   `order` (exponential decay based on elapsed time since
   `last_update_monotonic`, half-life above).
3. Add `LLMAdvisor.sentiment(psych_profile: str) -> float | None`: returns
   the EMA value, or `None` if no update in the last e.g. 5x half-life
   (stale => no influence).
4. `sim/agents.py::_psych_override`: in the fallback branch (advisor
   present, no fresh `advice`, agent already offered), after computing the
   deterministic fallback `order`, blend in sentiment:
   `order = clip(order + sentiment_weight * advisor.sentiment(self.psych_profile) * rng.uniform(0.5, 1.0), -1.5, 1.5)`
   only when `advisor.sentiment(...)` is not `None`. This gives every
   "interesting" agent LLM-flavored reactivity even when it didn't win a
   batch slot this tick — without any extra network call.
5. Guard: sentiment blending only applies when `advisor is not None` (LLM
   enabled); LLM-off behavior is unchanged.

### Phase C — Top-K eligible capping (mostly free from Phase A step 4)
- The per-bucket slice in Phase A step 4 *is* the top-K cap: least-recently-
   served profile groups are selected first, with urgent agents prioritized
   inside each selected group; the rest stay on deterministic rules (now
   sentiment-blended per Phase B) until they resurface in a later tick.
- No new config needed beyond `batch_size`/`concurrency`/`rpm` already
  controlling the effective K = `slots * batch_size` per tick (up to 9
  buckets can be represented, bounded by `slots`).

## Relevant files
- `sim/llm_advisor.py` — `LLMAdvisor.__init__`, `end_tick`, `_worker`,
  `_call` (split into batch call), `RESPONSE_FORMAT`, `collect` (sentiment
  EMA update), new `sentiment()` method, `stats()`.
- `sim/agents.py` — `_build_persona_prompt` -> split into `_persona_header` +
  `_agent_situation_line`, `get_llm_advisor()` (read new env vars incl.
  `SIMPASAR_LLM_BATCH_SIZE=11`, pass to `LLMAdvisor(...)`),
  `Agent._psych_override` (pass `group_key=(agent_type, psych_profile)` to
  `advisor.offer()`; sentiment blend in fallback branch).
- `sim/market.py` — no change expected; `end_tick()` call site (around
  L207-240) stays the same.

## Verification
1. Manual: run server with `SIMPASAR_LLM=on` and a valid `GEMINI_API_KEY`,
   watch server logs for `[llm]` lines — request count per tick should drop
   while agents served per request rises (check `stats().requested` vs
   number of agents that got fresh `last_reason` text).
2. Add/extend a small unit test (no existing LLMAdvisor tests found) for
   batch parsing: fake client returning a `decisions` array, assert each
   agent's future resolves to the right (order, reason) by id, and assert a
   malformed/missing id resolves to `None` without crashing the batch.
3. Unit test for sentiment EMA: simulate `collect()` accepting a few
   decisions for the same psych_profile, assert `sentiment()` moves toward
   the recent order value and decays/returns `None` after enough simulated
   elapsed time.
4. Load check: with `SIMPASAR_LLM_RPM=12`, `SIMPASAR_LLM_CONCURRENCY=4`,
   `SIMPASAR_LLM_BATCH_SIZE=11`, confirm at most 4 requests in flight, each
   batch contains agents from exactly one `(agent_type, psych_profile)`
   bucket, and up to 11 agents per request (fewer for smaller eligible groups),
   reducing request count relative to the original 1:1 pattern.
5. Confirm 429 rate empirically over an extended run (compare
   `stats().errors` and time spent in backoff before/after).

## Decisions
- Batch size fixed at 11, matching the reasoning: 3 `agent_type` x 3
  `psych_profile` = 9 personality buckets, ~100 agents total => ~11-12
  agents per bucket, so one batch can usually cover an entire bucket's
  eligible agents.
- Batches are strictly single-personality (never mix
  `agent_type`/`psych_profile`) so the persona paragraph is sent once per
  batch, not once per agent — this is a token/TPM win on top of the RPM
  win.
- The UI sliders control continuous population shares, not boolean agent
   traits. Preserve those ratios and each agent's continuous thresholds;
   only group by the realized profile labels already assigned by `build_agents()`.
- Bucket priority when `slots < 9`: use least-recently-served group rotation,
   with urgency order breaking ties. This avoids starving groups while keeping
   urgency as the within-group selection rule.
- Adaptive RPM auto-tuning / honoring `Retry-After` header: explicitly OUT
  of scope (only batching + herd sentiment were chosen).
- Top-K capping is implemented implicitly via per-bucket batch sizing, not a
  separate config knob, to avoid over-engineering.
- Batch parse failures degrade per-agent to `None` (same as today's
  single-agent parse failure), not a whole-batch retry, to keep behavior
  predictable.
- Sentiment blending is additive/small-weight, not a full override —
  deterministic fallback rules remain the primary logic when no LLM answer
  is available.

## Further considerations
1. Default `SIMPASAR_LLM_BATCH_SIZE=11` per the user's request — validate
   empirically that Gemini flash-lite reliably returns an 11-entry
   structured array without truncation/latency issues once implemented;
   fall back to splitting a bucket into 2 sub-batches if not.
2. Sentiment weight default 0.25: tune after observing whether herd
   behavior looks too correlated (lower weight) or too inert (raise
   weight).
3. Observe whether least-recently-served bucket rotation provides enough
   urgency responsiveness; if not, consider a bounded urgency/fairness score
   rather than changing to strict urgency-only selection.
