Review grading in an unaccepted new test using its four already-saved oracle
attempts. The attempts may fail the current checks. This is diagnosis, not
approval to run the assistant again or to make a failing answer pass.

First derive the necessary observable results from the approved work, actual
request, app state, and original dated messages. Then compare those requirements
with every check and the actual calls and results. Do not infer a requirement
from what an oracle happened to say, or infer correctness from its history label.

Return correct only for a demonstrated defect in checks or their dependent
source evidence. Name each affected check, the exact saved attempt and call (or
missing result), its current grading result, and why that result is wrong under
the necessary work. Preserve every necessary remembered result, target and final
effect. A missing check needs the source-backed result or distinct requested
action it would prove. Do not merely make checks easier: fix incomplete coverage,
false positives, optional requirements, duplicate checks, inappropriate exact
matching, and disallowed narrowing of legitimate alternatives where evidenced.

Use semantic judging for meaning that can be expressed in many ways. Exact
identifiers, amounts, dates, and literal wording remain exact when correctness
requires them. Semantic judging must reject missing, incomplete, contradictory,
negated or merely promised meaning. The complete same-call arguments supply
context but the request or criterion cannot stand in for actual completion.

An assistant's genuine mistake does not justify a check change. If the checks
are already correct but the attempts fail, return pending and explain the
assistant failure. A grader application or infrastructure error is also pending,
not permission to weaken a valid criterion. Request, app state, tools, history,
facts and approved work are immutable in this pass; report their defects without
trying to compensate in grading. Never invent history or weaken the memory
requirement. Accepting remains conditional on both with-history attempts passing,
both no-history attempts failing a necessary remembered result, and independent
review of the revised test. A failed set cannot be approved unchanged.
