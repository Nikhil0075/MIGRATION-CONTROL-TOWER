# Evidence frames

Eight captures of the console, each wrapped in a frame that states what it
proves and — the part that matters — **where it came from**.

Rebuild them with:

```bash
python tools/evidence_frames.py
```

## The label is the point

A screenshot on its own is an assertion with no provenance. A reader cannot
tell whether an image came from a real migration, a local run against
fixtures, or a mock-up, and those three carry very different weight in a
submission. Every frame therefore carries one of:

| Label | Meaning |
|---|---|
| **LIVE** | Captured from a real migration against real infrastructure. |
| **LOCAL** | Captured from the real console, driven by local fixture data. |
| **SIMULATED** | A mock-up. The software did not produce this image. |

`SIMULATED` renders amber rather than in the same calm blue as `LOCAL`,
because the weakest claim should be the loudest label — the failure this
whole set exists to prevent is a mocked screen being read as a live result.

**Every frame in this set is `LOCAL`.** They come from the Playwright
baselines, which drive the real console — the real components, the real
routing, the real rendering — against fixture data rather than a live
estate. That is exactly what `LOCAL` means, and it is deliberately not
upgraded: the capture path cannot authenticate against a real estate, so
no frame here is entitled to say `LIVE`.

A test enforces this rather than trusting the convention: anything sourced
from the fixture-driven baselines cannot be labelled `LIVE`, so a frame
cannot be promoted by editing a string.

## The set

| # | Frame | What it proves |
|---|---|---|
| 1 | Agent registry | Agents resolve from APPROVED registry cards by capability, never by direct import. |
| 2 | Run lifecycle | Stage status is derived from the run's own `state_history`, not rendered from a guess. |
| 3 | Policy denial | `policy_engine.py` takes no free-text estate content, so a hostile table comment cannot reach an authorization decision. |
| 4 | Failed reconciliation | The defect is caught by counts, aggregates and hashes in ordinary Python — measured, not judged. |
| 5 | Memory-assisted recovery | A confirmed remediation is recalled and cited by later runs, without replacing the re-validation that follows. |
| 6 | Approval gate | The approval token is bound to the plan hash it was issued against, and a stale binding is visible before cutover. |
| 7 | Dead letters | A message that defeated a consumer is readable, attributable, and replayable onto its original topic. |
| 8 | Runtime health | Each in-process consumer reports its own state and lease holder. |

## Producing LIVE evidence

If a frame needs to claim `LIVE`, capture it outside the Playwright
baselines — sign in to a console running against a real estate, capture
the page, and add an `Evidence(...)` entry pointing at that file with
`label="LIVE"`. The test will then pass, because the source is no longer a
fixture-driven baseline. Relabelling an existing frame will not work, and
that is intentional.
