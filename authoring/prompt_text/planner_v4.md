Propose useful work that a personal assistant can perform on the evaluation date.
Correct completion must require information from the supplied earlier facts that
the new request and readable app records will not disclose.

Use the current facts and available tools. You may reuse a fact for genuinely
different work. Prefer an unused fact only when ideas are otherwise equally
useful. Treat previous rejections as explanations of particular failures, not
as a list of forbidden facts or subjects.

Prefer focused work with a direct practical purpose over exhaustive archival
compilations. A fact record can contain many details; selecting it does not mean
the task must reproduce all of them. Choose work that needs its relevant content
or constraint, and select additional facts only when each changes necessary work.
For example, sending a useful update can require a remembered funding amount and
lead investor without also requiring the dates and narrative of earlier updates.
Documents and historical comparisons remain valid when that is genuinely what
the user needs. Do not add a checklist of historical milestones merely to cover
more facts, or turn a narrow operational request into a comprehensive evidence
package. Include necessary non-memory routing and execution inputs in the proposed
situation so the writer does not have to invent them.

When planning_shard is present, each current_facts row says whether it is
available_for_new_idea. Select only rows where that value is true. Rows where it
is false are related context supplied so that you can respect later information
about the same subject. Replacement chains stay in one shard. Other planner calls
handle the other selectable facts, so do not try to cover facts outside this
shard.

Before selecting each fact, apply an omission test: name the necessary observable
result that would become wrong if that fact were absent. Do not select a fact when
the requested work can remain fully correct without its distinct contribution,
including a more detailed fact that would only elaborate an otherwise complete
rule or result.

For each idea, state the present situation, the work to perform, the selected
fact IDs, the intended tools, what history supplies, and the most likely result
from a capable assistant without history. Do not invent a preference, completed
action, identity, or standing rule that the supplied facts do not establish.
Respect later updates and explicit newer instructions.

Remembered information may determine a tool argument or necessary content in
an otherwise identical tool call. The tools need not differ between conditions.
Do not propose standalone recall questions or create a document solely to turn
a recall question into a tool action. The work must have its own practical use.

Describe the work without giving away its remembered answer or announcing that
the assistant must retrieve a hidden preference. Consider whether ordinary
practice already gives a capable stranger the right answer. Do not keep an idea
whose likely no-history completion already satisfies the intended work.

Do not write the final request, source quotations, grading checks, or a detailed
answer for the writer to copy. The writer will verify the idea against the actual
messages. Return up to requested_count ideas. Do not fill unused slots with
unsupported ideas; explain any shortfall briefly in shortfall_reason. Use an
empty shortfall_reason when you return the full count. Do not generate IDs or
checkpoint metadata. Return only the supplied response shape.
