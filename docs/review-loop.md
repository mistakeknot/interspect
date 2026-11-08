# Review loop (weekly job)

Samples the BLOCK verdicts of one finished ISO week, has a judge on a different
lab label each finding, and reports bounds on the share of BLOCK receipts that
carried a confirmed warranted defect, plus backlog proposals per mechanism.
It files nothing and edits no policy: its output is a report that a person turns
into issues and pull requests.

## Steps

Each step reads and writes JSON files so a week replays byte for byte from its snapshot.

    scripts/review_loop/run.sh freeze --receipts DIR --week 2026-W40 --run-at 2026-10-25T00:00:00Z --out snap.json
    scripts/review_loop/run.sh sample --snapshot snap.json --out sample.json
    (judge each sampled PR: see review_loop/judge.py for the prompt and reply format)
    scripts/review_loop/run.sh aggregate --snapshot snap.json --sample sample.json --judgments j.json --report report.md --digest digest.txt

`freeze` refuses to run until 14 days after the week closes.

## Weekly driver

`run` does all of the above in one command and is what a timer calls:

    scripts/review_loop/run.sh run --receipts DIR --out-root DIR --clavain-root DIR \
        --decision-context FILE [--week 2026-W40] [--text-root DIR ...] [--no-calibration]

With no `--week` it takes the latest week whose 14-day horizon has elapsed. Output goes to
`OUT_ROOT/<week>/`: `population.json`, `sample.json`, `judgments-a.json` (Stage A, headline),
`judgments-b.json` (Stage B, comparison), `judgments-state.json` (resume state and the judge
log), `report.md` and `digest.txt`. The driver sends nothing; whatever runs it forwards the
digest.

- Judge: packaged `dispatch.sh --role validation --producer-identity <reviewer>`, so the seat
  lands on a different lab than the reviewer and the dispatch ledger records the real model and
  effort. Same-lab or unrecorded identities are declared in the report. The judge runs in an
  empty scratch directory, read-only, and its prompt says the embedded text is data.
- Stage A sees the original head's diff, the PR description and the stored finding text. Stage B
  also sees the earlier Stage A labels and what landed. Every changed label is counted.
- Finding text comes from `--text-root` directories: review output files that name exactly one
  full head sha and contain a VERDICT line. A receipt with no text is not sent to a judge; it is
  unresolved with reason `text_unrecovered` and widens the upper bound. This is the fallback until
  receipts carry their findings.
- A second judge re-judges 5 text-recovered receipts, blind to the first judge; a fixed set of 10
  synthetic items with known outcomes (`calibration.json`, extended only by adding) is judged
  every run, and fewer than 8 matches is reported as drift.
- Quota: before every judge call the driver reads the account pool and pauses (exit 3) when no
  enabled account has headroom below 80% in every limit window, or the reading is unavailable.
  Judgments already made are kept; rerun to resume.
- A judge reply that cannot be parsed leaves its receipts unresolved; it does not stop the week.
- The judge seat can use tools in its own sandbox. Diffs and review text are untrusted input, so
  they are sanitised and size-capped, and the scratch directory holds nothing of value.

## Design

- A PR belongs to the week of its first review of any verdict. The frame is the
  PRs with at least one BLOCK inside the 14-day horizon. A PR is the sampling and
  analysis unit; its stratum is its first BLOCK's reviewer and producer.
- Budget 30 PRs; the whole frame is taken when it has 30 or fewer.
- Lower bound L = weighted confirmed / weighted sampled receipts (Hajek ratio).
  Upper bound U adds weighted unresolved receipts without a confirmed defect over
  the same denominator, so 0 <= L <= U <= 100%.
- The interval is a Rao-Wu rescaled bootstrap with finite-population correction,
  recomputing both bounds in each replicate. Under 8 sampled PRs only counts and
  bounds are printed.
- A backlog proposal needs 3 defects across 2 PRs for one mechanism, or one
  high-severity defect. A proposal whose change would relax a check is rejected.
- The report does not rank reviewer or author models.

## Tests

    pytest tests/review_loop
