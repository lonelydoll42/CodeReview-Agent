# 2026-10-09 A/B Pilot: Human Blind Review Preparation

This packet prepares a condition-blind review of the ten saved predictions. It contains no agent verdicts, known-issue mapping, or human conclusions. No human review has been performed, and the packet does not certify the cases as a blind benchmark.

## Frozen Inputs

- [Condition-blind prediction input](2026-10-09-ab-pilot-blind-review-input.json) contains the ten prediction texts, attached to anonymous case keys. Condition, agent confidence, verdict, raw host output, and oracle labels are omitted.
- [Frozen source index](2026-10-09-ab-pilot-blind-review-sources.json) records the base/head revisions, scope, scoped diff digest, local patch location, and patch byte digest for all eleven cases.
- [Empty human label template](2026-10-09-ab-pilot-blind-review-template.json) has null verdicts and case classifications. It is pending input, not a label file.

These files were prepared from `/root/project/review-benchmark-operator-20261009/adjudication-input.json`, whose SHA-256 is `5cddda038fd695db0d2809cdf83ca2de54fc3e60422b64446e8a10ce46f4bdc2`. The frozen patches and checkouts are under `/root/project/review-benchmark-operator-20261009/grader-sources/`. The condition-blind input is derived only from the anonymous prediction input; the grader result v1/v2, annotations, private mapping, and run records are not included.

To regenerate the packet from the preserved operator files:

```sh
python eval/export_ab_pilot.py prepare-blind-review \
  --anonymous-input /root/project/review-benchmark-operator-20261009/adjudication-input.json \
  --operator-root /root/project/review-benchmark-operator-20261009 \
  --input-output eval/results/2026-10-09-ab-pilot-blind-review-input.json \
  --sources-output eval/results/2026-10-09-ab-pilot-blind-review-sources.json \
  --template-output eval/results/2026-10-09-ab-pilot-blind-review-template.json
```

The command rejects a changed anonymous input digest, missing source patch, or a count other than eleven cases and ten predictions. It fingerprints each patch's actual bytes in the source index. For a local checkout, verify the recorded head revision and scoped diff before review; do not substitute a newly generated patch for the frozen file.

## Review Procedure

1. Give the reviewer only the three packet files and the frozen source changes. Do not give access to the pilot report/evidence, source run records, agent grader results, annotations, private mapping, or A/B condition labels. Existing public artifacts include a prediction-to-condition projection, so access to the repository itself would break condition blinding.
2. Ask the reviewer to assess each prediction against its cited scope and base-to-head change. A prediction verdict is `valid`, `false_positive`, `duplicate`, or `unresolved`. Use `unresolved` when the available source is insufficient; do not turn uncertainty or a missing citation into a false positive. Record duplicate targets and the reasoning in the returned copy of the template.
3. Keep the known-finding match separate from the initial blind pass. Only after the blind verdicts are frozen should the operator compare them with the separately controlled case annotations and resolve the `known_findings` references. Do not put agent candidate judgments into those annotations.
4. Record reviewer identity and review time, preserve the untouched template and returned file, and check that every prediction and case has an explicit human decision before marking a result adjudicated.

The packet labels are prediction-level assessments, not a substitute for independent file-read/tool-call traces. They cannot establish review coverage, whether a Skill was loaded, or which agent condition produced a prediction. Do not calculate human precision, known-defect recall, clean-case false alarms, or wrong-resolution from the blank template. Those fields remain unknown until completed human labels pass validation.
