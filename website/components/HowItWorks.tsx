import type { ReactNode } from 'react';
import Link from 'next/link';
import { Check } from 'lucide-react';
import { data, formatDate, number } from '@/lib/data';
import { datasetFacts, getWorkedExample, numberWord } from '@/lib/example';
import { evaluationUrl, methodologyUrl } from '@/lib/site';

const capitalize = (word: string) => word.charAt(0).toUpperCase() + word.slice(1);

function Stage({ number, title, children, figure }: {
  number: string; title: string; children: ReactNode; figure: ReactNode;
}) {
  return (
    <li className="flex min-w-0 flex-col rounded-lg border border-hairline bg-sand">
      <div className="flex h-[132px] items-center justify-center border-b border-hairline px-5" aria-hidden="true">
        {figure}
      </div>
      <div className="flex flex-1 flex-col px-5 pb-6 pt-5">
        <span className="eyebrow mb-2">{number}</span>
        <h3 className="mb-2 text-md font-semibold tracking-[-0.01em]">{title}</h3>
        <p className="text-sm leading-[1.55] text-muted">{children}</p>
      </div>
    </li>
  );
}

function Arrow() {
  return (
    <li className="flex items-center justify-center text-faint" aria-hidden="true">
      <span className="font-mono text-lg md:hidden">↓</span>
      <span className="hidden font-mono text-lg md:inline">→</span>
    </li>
  );
}

// A strip of message rows getting denser over time, with the years underneath.
function HistoryFigure({ start, end }: { start: string; end: string }) {
  const widths = [64, 40, 76, 52, 30, 68, 44, 80];
  return (
    <div className="w-full max-w-[200px]">
      <div className="mb-3 flex flex-col gap-[5px]">
        {widths.map((width, index) => (
          <div key={index} className="flex items-center gap-1.5">
            <span className="h-1.5 w-1.5 rounded-full bg-ink/20" />
            <span className="h-[5px] rounded-full bg-ink/25" style={{ width: `${width}%` }} />
          </div>
        ))}
      </div>
      <div className="flex justify-between font-mono text-[10px] text-muted">
        <span>{start.slice(0, 4)}</span>
        <span>{end.slice(0, 4)}</span>
      </div>
    </div>
  );
}

// Messages flowing into one memory store, in order.
function IngestFigure() {
  return (
    <div className="flex items-center gap-3">
      <div className="flex flex-col gap-1.5">
        {[0, 1, 2, 3].map((index) => (
          <span key={index} className="h-[5px] rounded-full bg-ink/25" style={{ width: 34 - index * 6 }} />
        ))}
      </div>
      <span className="font-mono text-sm text-faint">→</span>
      <div className="flex h-[76px] w-[68px] flex-col items-center justify-center rounded-md border border-ink/40 bg-paper">
        <span className="mb-1 grid grid-cols-3 gap-1">
          {[...Array(9)].map((_, index) => (
            <span key={index} className={`h-2 w-2 rounded-sm ${index % 4 === 0 ? 'bg-highlight' : 'bg-ink/15'}`} />
          ))}
        </span>
        <span className="font-mono text-[10px] text-muted">memory</span>
      </div>
    </div>
  );
}

// The request the agent sees, and the apps it can act in.
function ActFigure({ request, tools }: { request: string; tools: string[] }) {
  return (
    <div className="w-full max-w-[220px]">
      <div className="mb-2 rounded-md border border-hairline bg-paper px-3 py-2 text-[11px] leading-snug">
        <span className="line-clamp-2">“{request}”</span>
      </div>
      <div className="flex flex-wrap gap-1">
        {tools.map((tool) => (
          <span key={tool} className="rounded-full border border-ink/20 px-2 py-px font-mono text-[10px] text-muted">{tool}</span>
        ))}
      </div>
    </div>
  );
}

// The checks a grader runs on the recorded action.
function GradeFigure({ tool, field, value }: { tool: string; field: string; value: string }) {
  const rows: [string, string][] = [['tool', tool], [field, value], ['content', 'as requested']];
  return (
    <div className="w-full max-w-[210px] rounded-md border border-hairline bg-paper px-3 py-2 font-mono text-[10px]">
      {rows.map(([label, text]) => (
        <div key={label} className="flex items-center justify-between gap-2 py-[3px]">
          <span className="truncate"><span className="text-muted">{label}</span> {text}</span>
          <Check size={11} className="shrink-0 text-pass" />
        </div>
      ))}
      <div className="mt-1 flex justify-end border-t border-hairline pt-1.5 text-pass">PASS</div>
    </div>
  );
}

export default function HowItWorks() {
  const facts = datasetFacts();
  const example = getWorkedExample();
  const start = data.personas.map((persona) => persona.start).sort()[0];
  const end = data.personas.map((persona) => persona.end).sort().at(-1)!;
  const testsPerPersona = data.personas[0].tests;
  return (
    <section id="evaluation" className="container-x py-16 sm:py-24 sm:pb-28">
      <div className="mb-12 grid items-end gap-6 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] md:gap-12">
        <div>
          <p className="eyebrow mb-3">How it works</p>
          <h2 className="font-semibold leading-[1.1] tracking-[-0.03em]" style={{ fontSize: 'clamp(28px, 3.4vw, 40px)' }}>
            From years of history to one graded action
          </h2>
        </div>
        <p className="text-lg leading-[1.55]">
          Each memory system processes a user’s message history. An agent then uses that memory to complete tasks with
          the available tools. A task passes when every required check passes.
        </p>
      </div>

      <ol className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)_auto_minmax(0,1fr)]" aria-label="Evaluation pipeline">
        <Stage number="01 · History" title="Years of conversation" figure={<HistoryFigure start={start} end={end} />}>
          {capitalize(numberWord(facts.personas))} simulated users, up to {number(facts.maxMessages)} messages each,
          spanning {facts.spanLabel} of work and life. The rules that matter later are stated once and buried.
        </Stage>
        <Arrow />
        <Stage number="02 · Ingest" title="Memory reads the history" figure={<IngestFigure />}>
          Each memory system processes the history message by message, in order, starting from empty memory for
          every user. Reported cost includes ingestion and testing.
        </Stage>
        <Arrow />
        <Stage number="03 · Act" title="The agent gets a request and tools" figure={<ActFigure request={example.test.request} tools={['email', 'slack', 'discord', 'calendar', 'crm']} />}>
          {testsPerPersona} tests per user, each in a fresh conversation with isolated app state and read-only
          memory. The agent sees the request and the simulated apps, never a reminder of the rule.
        </Stage>
        <Arrow />
        <Stage number="04 · Grade" title="The action is checked, not the recall" figure={<GradeFigure tool={example.tool} field={example.field} value={example.value} />}>
          Every test checks the tool called, its target, and its content. Accuracy, total cost, and latency
          are reported for each memory system, model, and harness.
        </Stage>
      </ol>

      <p className="mt-6 text-sm text-muted">
        Histories run {formatDate(start, { day: undefined })} to {formatDate(end, { day: undefined })}. Facts and expected
        answers stay outside the agent’s context. The evaluated release is pinned by hash, so every configuration
        answers the same {number(data.tests)} tasks.
      </p>
      <div className="mt-8 flex flex-wrap gap-7 text-base font-medium">
        <a href={methodologyUrl} className="text-link">Methodology</a>
        <a href={evaluationUrl} className="text-link">Evaluation protocol</a>
        <Link href="/dataset/" className="text-link">
          Dataset <span aria-hidden="true">→</span>
        </Link>
      </div>
    </section>
  );
}
