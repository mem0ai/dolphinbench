import Link from 'next/link';
import ResultsExplorer from '@/components/results/ResultsExplorer';
import WhyDolphinBench from '@/components/WhyDolphinBench';
import HowItWorks from '@/components/HowItWorks';
import StructuredData, { datasetSchema } from '@/components/StructuredData';
import DolphinMark from '@/components/DolphinMark';
import { data } from '@/lib/data';

export default function HomePage() {
  return (
    <>
      <StructuredData data={[datasetSchema(data)]} />
      <section className="container-x pb-16 pt-12 sm:pb-24 sm:pt-28">
        <div className="mb-7 flex items-center gap-3">
          <DolphinMark size={40} tile />
          <span className="eyebrow tracking-[0.02em]">
            DolphinBench · {data.personas.length} personas · {data.tests} tasks
          </span>
        </div>
        <h1
          className="mb-8 max-w-[15ch] font-semibold leading-[0.98] tracking-[-0.04em]"
          style={{ fontSize: 'clamp(38px, 6vw, 84px)' }}
        >
          Mapping the <span className="marker">Pareto frontier</span> of agent memory
        </h1>
        <div className="grid items-end gap-8 md:grid-cols-2 md:gap-12">
          <p className="max-w-[34em] text-md leading-[1.5] sm:text-xl">
            DolphinBench compares agent configurations across memory systems, models, providers, and harnesses. Agents
            complete tasks that depend on past conversations and must recognize which earlier information matters to
            guide their decisions and actions.
          </p>
          <div className="flex flex-wrap items-center gap-7 md:justify-end">
            <Link href="/leaderboard/" className="pill-button">
              View leaderboard <span aria-hidden="true">→</span>
            </Link>
            <Link href="/dataset/" className="text-link text-base">Explore dataset</Link>
            <Link href="/run/" className="text-link text-base">Run and submit</Link>
          </div>
        </div>
      </section>
      <section id="results" className="container-x pb-24">
        <ResultsExplorer showLeaderboardLink />
      </section>
      <WhyDolphinBench />
      <HowItWorks />
    </>
  );
}
