# Write a credible assistant test

Create one test from the approved idea. The test gives an assistant a message the
user could reasonably send now. Completing that work must require information
from the user's earlier messages.

You write two different things in one response: the message the assistant receives,
and the evidence and checks used to judge its work. Keep those purposes separate.
The user's message explains the job. It is not a description of the benchmark.

## Understand the person and the job

Read the present situation and requested work first. Identify what the person
wants to accomplish and what the assistant needs to understand that request.
Use the supplied situation and messages to understand the person; do not invent
a biography, preference, or communication style from their name.

The approved requirements describe what a correct result must do. The source
messages establish why, and the tool contracts establish what is possible.
Neither the requirements nor the source wording is a script for the user's message.

## Write the message

In `query`, speak as the user addressing their assistant. State the action and the
context needed to understand it. Use concrete names, dates, targets, and conditions
when they identify the work. Use language appropriate to that relationship.

Ask for the result at the level the user cares about. Let the assistant determine
the remembered details needed to deliver it. Do not give the answer away, direct
the assistant to retrieve a particular remembered rule, or enumerate hidden
requirements merely to make the test easier to grade.

Detail is useful when it explains the work. A technical colleague may need service
names and rollback conditions; a family message may not. A longer request can be
natural. A short request can be vague. Preserve meaning rather than aiming for a
length or replacing necessary terms with generic language.

## Examples of the intended voice

These are illustrations, not facts or tasks to copy into the current test.

**Investor email.** The work is an update; history supplies the funding details.

> Email Priya an update on our recent wins and funding.

The request identifies the work and recipient. The amount, round, and lead investor
belong in the completed email. Asking the user to list those values in the request
would remove the need for memory. Priya's identity and address must be available
in the actual test; the name in this example is not evidence.

**Technical guide.** The reader is an on-call operator who needs to act on a problem.

> Write an ingest-edge runbook for the on-call team. Explain the production limits,
> how to check request handling, when to roll back, and who makes that decision.

Compare that with: "Cover every mandatory rollback condition including the
fixed-baseline percentage checks and durations." The latter tells the assistant
about the shape of the expected answer. The former describes the operator's job.
The actual checks must still be necessary for that job; broad wording does not
justify grading every detail from a source.

**A detailed request.** The current situation supplies these travel details;
history supplies the budget that determines the bookings.

> Book my flight from Boston to Chicago for October 12 and a hotel for two nights.
> I need to arrive before 3 p.m. and stay within walking distance of the conference.

Keep the route, dates, timing, and location condition when the task supplies them.
Removing those details would make the work unclear, not more natural. Grade the
remembered budget where it changes the bookings; do not turn every new instruction
into a separate memory check. The venue and bookable options must be available.

## Build the test around the work

Verify the source meanings and dates. Newer explicit instructions take precedence.
An earlier request is not proof of its outcome, and permission is not an obligation.

Choose the necessary starting records from the supplied catalog. Only selected
and newly created records become visible to the assistant. Keep required targets
and other non-memory inputs available. Judge answer leakage in that actual state
and the final message, not in unused catalog records.

Measure each distinct remembered result the work needs and prove that the requested
final actions occurred. Accept different words that communicate the required
meaning. Supporting reads, background information, ordinary request details, and
optional elaboration do not become separate grading obligations.

Distinguish a necessary constraint from one way to satisfy it. A maximum duration
does not require exactly that duration; flexible scheduling does not require one
chosen set of dates. Grade the result the approved work needs, including required
conditions, and accept other completions that satisfy it. Information in a table
need not be repeated in a checklist unless that checklist must stand on its own.

Before returning, consider a capable assistant completing the message as written.
Would a correct completion necessarily satisfy the approved requirements? If not,
do not add instructions just to justify the checks or silently remove a requirement.
Reject the idea and identify the mismatch so a human can approve different work.

Also consider what that assistant could do without the earlier messages. If the
message, starting records, or ordinary practice already determines the required
answer, this is not a valid memory test. Reject the test, not the underlying fact.

Return one complete response using the supplied schema and the output reference.
The reference specifies the format; it does not supply wording for `query`.
