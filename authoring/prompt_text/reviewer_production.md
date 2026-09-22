Decide whether this complete test is suitable for a memory benchmark. Four real
assistant attempts have run: both with-history attempts passed the checks and
both without-history attempts failed a source-backed check. Those results do not
prove that the task or grading requirements are valid. Review them independently.

Approve only when all of the following hold:
- The request is useful present-day work, not standalone recall or an artificial
  excuse to repeat facts. An email or document needing remembered factual content
  is valid even if the tool is unchanged.
- The supplied original messages establish every required remembered result for
  the test date. Respect later explicit instructions and limits. A past request
  does not prove a completed action. Evidence explanations may paraphrase; check
  their meaning against the original messages rather than their exact wording.
- The request and visible app state do not reveal the remembered answer. Needed
  non-memory inputs, tools, records, and requester identity are available. The
  requester name in the request is ordinary context supplied in both conditions.
- The request preserves the approved work, and the checks cover every necessary
  remembered result and requested final action without grading optional details,
  ordinary supporting reads, or redundant requirements. Every selected fact must
  change a necessary checked result. True historical details are not automatically
  required output. Do not require information in both title and body when either
  location communicates it correctly, unless the work independently needs both.
- The actual with-history calls really complete the work. The no-history calls
  fail because required remembered information is absent or wrong, not because of
  an unrelated ordinary detail, infrastructure failure, or a grading mistake.
- The checks allow correct alternatives and reject missing, contradictory, or
  negated required meaning. Graders see the complete same-call arguments. Different
  actions require different calls; all checks for one action must hold together.

Read the original sources, actual request and app state, checks, and complete
tool calls/results. Do not invent a hypothetical requirement solely to challenge
a test. Identify a real problem and explain its effect on correct completion.
Do not demand synthetic positive/negative examples, exact copied quotations,
prose keyword matches, a second audit table, or a particular writing style.

Return approve with no issues if the test is valid. Return correct for a specific
repair to the request, starting state, checks, or evidence of this same approved
work. Name existing affected check_ids; use an empty list for a missing check.
Evidence updates may accompany affected check changes, but unrelated checks are
protected. Cite relevant source_message_ids when historical support is at issue.
Return reject for a defect in the approved idea that requires different work or
facts. Return pending for missing information or a system/grader application
error that cannot be resolved by changing an incorrect test requirement. Do not
weaken a correct criterion to accommodate an assistant mistake. Use plain,
specific explanations and state the exact needed change; do not write examples
or new test content in this response.
