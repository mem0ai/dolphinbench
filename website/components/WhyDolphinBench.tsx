import type { ReactNode } from 'react';
import Link from 'next/link';
import { formatDate, messageHref, number, testHref } from '@/lib/data';
import { datasetFacts, getWorkedExample, numberWord } from '@/lib/example';

function ToolCall({
  tool,
  field,
  value,
  wrong,
}: {
  tool: string;
  field: string;
  value: string;
  wrong?: boolean;
}) {
  return (
    <div className={`font-mono text-sm leading-[1.8] ${wrong ? 'text-muted' : ''}`}>
      <div>{tool}(</div>
      <div className="pl-[18px]">
        <span className="text-muted">{field}</span> ={' '}
        {wrong ? (
          <span className="text-fail line-through">
            &quot;<span data-example-wrong>{value}</span>&quot;
          </span>
        ) : (
          <span className="marker">
            &quot;<span data-example-outcome>{value}</span>&quot;
          </span>
        )}
        ,
      </div>
      <div className="pl-[18px]">
        <span className="text-muted">content</span> = &quot;…&quot;
      </div>
      <div>)</div>
    </div>
  );
}

// One step of the worked example: a number, a one-line claim, and the evidence for it.
function Step({ number, title, children }: { number: string; title: string; children: ReactNode }) {
  return (
    <li className="grid gap-x-6 gap-y-3 border-t border-ink/15 py-8 sm:grid-cols-[64px_minmax(0,1fr)] sm:py-10">
      <span className="eyebrow pt-1" aria-hidden="true">{number}</span>
      <div className="min-w-0">
        <h3 className="mb-5 text-lg font-semibold tracking-[-0.015em] sm:text-xl">{title}</h3>
        {children}
      </div>
    </li>
  );
}

function Quote({ heading, meta, children, note }: {
  heading: string; meta: ReactNode; children: ReactNode; note?: string;
}) {
  return (
    <article className="max-w-[640px] rounded-lg border border-hairline bg-paper px-7 py-6">
      <div className="mb-3.5 flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-semibold">{heading}</span>
        <span className="eyebrow">{meta}</span>
      </div>
      <blockquote className="text-lg leading-[1.5]">{children}</blockquote>
      {note && <p className="mt-3 text-sm text-muted">{note}</p>}
    </article>
  );
}

const monthYear = (iso: string) => formatDate(iso, { day: undefined, month: 'short', year: 'numeric' });
const capitalize = (word: string) => word.charAt(0).toUpperCase() + word.slice(1);

export default function WhyDolphinBench() {
  const example = getWorkedExample();
  const facts = datasetFacts();
  const { source, test, value, wrongValue, persona } = example;
  const firstName = persona.name.split(' ')[0];
  const parts = source.content.split(value);
  return (
    <section id="example" className="border-y border-hairline bg-sand">
      <div className="container-x py-16 sm:py-24">
        <div className="mb-14 grid items-end gap-8 md:grid-cols-[minmax(0,1.2fr)_minmax(0,0.8fr)] md:gap-12">
          <div>
            <p className="eyebrow mb-4">Why DolphinBench</p>
            <h2 className="section-title">
              We evaluate agent memory directly through <span className="marker">future task completion</span>
            </h2>
          </div>
          <p className="text-[17px] leading-[1.55]">
            Most memory benchmarks ask a question and check the answer. DolphinBench gives the agent real tools and a
            request, then grades what it <em>does</em>. The only way to pass is to have retained the right rule and to
            apply it. Here is one test, start to finish.
          </p>
        </div>

        <ol className="border-b border-ink/15">
          <Step number="01" title={`${firstName} states a rule once`}>
            <Quote
              heading={`What ${firstName} said`}
              meta={
                <>
                  <time dateTime={source.date}>{formatDate(source.date)}</time> ·{' '}
                  <Link href={messageHref(persona.id, source.id)} className="transition-colors hover:text-ink">
                    message {source.id}
                  </Link>
                </>
              }
            >
              “
              <span data-example-source>
                {parts.map((part, index) => (
                  <span key={index}>
                    {index > 0 && <code className="inline-code">{value}</code>}
                    {part}
                  </span>
                ))}
              </span>
              ”
            </Quote>
          </Step>

          <Step number="02" title="Years of unrelated conversation pile up on top of it">
            <div className="relative">
              <div className="mb-2.5 flex justify-between gap-4 font-mono text-[11px] text-muted">
                <span>
                  {monthYear(example.firstMessage.date)} · message {example.firstMessage.id}
                </span>
                <span className="hidden sm:inline">
                  {number(example.messageCount)} messages · {example.months} months
                </span>
                <span>
                  {monthYear(example.lastMessage.date)} · message {example.lastMessage.id}
                </span>
              </div>
              <div
                className="relative h-7 rounded-sm"
                style={{ background: 'repeating-linear-gradient(90deg, rgba(10,10,10,0.28) 0 1px, transparent 1px 5px)' }}
                data-timeline="strip"
              >
                <div className="absolute -bottom-2 -top-2 w-[3px] rounded-sm bg-ink" style={{ left: `${example.markerPercent}%` }} />
                <div className="absolute -bottom-2 -top-2 right-0 w-[3px] rounded-sm bg-pass" />
                <div
                  className="absolute right-0 top-1/2 h-px opacity-50"
                  style={{ left: `${example.markerPercent}%`, background: 'linear-gradient(90deg, #0A0A0A, #1E7A62)' }}
                />
              </div>
              <div className="mt-3 flex flex-wrap justify-between gap-x-4 gap-y-1 font-mono text-[11px] sm:block sm:h-4">
                <span
                  data-timeline="rule"
                  className="whitespace-nowrap sm:absolute"
                  style={{ left: `${example.markerPercent}%` }}
                >
                  #{source.id} · the rule
                </span>
                <span data-timeline="request" className="whitespace-nowrap text-pass sm:absolute sm:right-0">
                  test {test.id} · the request
                </span>
              </div>
              <p data-timeline="caption" className="mt-4 max-w-[640px] text-sm text-muted">
                {number(example.between)} unrelated messages in between: fundraising, hiring, a dog named Kibo, a condo
                tour. Nothing in them repeats the rule.
              </p>
            </div>
          </Step>

          <Step number="03" title={`The request arrives ${example.yearsWord} years later, with no reminder`}>
            <Quote
              heading={`What ${firstName} asks today`}
              meta={
                <>
                  <time dateTime={test.date}>{formatDate(test.date)}</time> · test {test.id}
                </>
              }
              note="No channel named. No reminder of the rule. Just the request."
            >
              “<span data-example-request>{test.request}</span>”
            </Quote>
          </Step>

          <Step number="04" title="The agent acts, and the action is graded">
            <div className="grid gap-8 md:grid-cols-2">
              <div className="border-t-2 border-pass pt-[18px]">
                <div className="mb-3 flex items-baseline justify-between">
                  <span className="text-sm font-semibold text-pass">Correct action</span>
                  <span className="font-mono text-xs text-pass">PASS</span>
                </div>
                <ToolCall tool={example.tool} field={example.field} value={value} />
                <p className="mt-3 text-sm text-muted">
                  Recognized that a {example.yearsWord}‑year‑old channel rule governs today’s request.
                </p>
              </div>
              <div className="border-t-2 border-ink/25 pt-[18px]">
                <div className="mb-3 flex items-baseline justify-between">
                  <span className="text-sm font-semibold text-muted">A plausible action without the rule</span>
                  <span className="font-mono text-xs text-fail">FAIL</span>
                </div>
                <ToolCall tool={example.tool} field={example.field} value={wrongValue} wrong />
                <p className="mt-3 text-sm text-muted">
                  Plausible, polite, and wrong: it would broadcast pre‑green release notes to the whole org.
                </p>
              </div>
            </div>
          </Step>
        </ol>

        <div className="mt-14 grid gap-8 md:grid-cols-3">
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Graded on action, not recall</div>
            <p className="text-sm leading-[1.55] text-muted">
              Every test checks the tool called, the target, and the content. Knowing the fact isn’t enough. The agent
              has to act on it.
            </p>
          </div>
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Years of history, not a session</div>
            <p className="text-sm leading-[1.55] text-muted">
              {capitalize(numberWord(facts.personas))} personas, up to {number(facts.maxMessages)} messages each,
              spanning {facts.spanLabel} of work and life. The signal is buried.
            </p>
          </div>
          <div>
            <div className="mb-2 text-lg font-semibold tracking-[-0.015em]">Real tools, simulated world</div>
            <p className="text-sm leading-[1.55] text-muted">
              Email, Slack, Discord, calendar, CRM and more run as MCP servers with state, so a wrong action has
              consequences the grader can see.
            </p>
          </div>
        </div>
        <div className="mt-10 flex flex-wrap gap-7 text-base font-medium">
          <Link href={testHref(persona.id, test.id)} className="text-link">
            View test {test.id} <span aria-hidden="true">→</span>
          </Link>
          <Link href="/dataset/" className="text-link">
            Explore the dataset
          </Link>
        </div>
      </div>
    </section>
  );
}
