import Link from 'next/link';
import ResultsExplorer from '@/components/results/ResultsExplorer';
import Submissions from '@/components/Submissions';

export const metadata = {
  title: 'Leaderboard',
  description: 'Official DolphinBench results: accuracy on 600 memory-dependent tasks, total cost, and latency ' +
    'for every evaluated memory system, model, and harness, plus the accuracy vs. cost Pareto frontier.',
  alternates: { canonical: '/leaderboard/' },
};

export default function LeaderboardPage() {
  return (
    <div className="container-x pb-24 pt-14 sm:pt-20">
      <header className="pb-10">
        <h1 className="page-title mb-5">Leaderboard</h1>
        <p className="max-w-[36em] text-lg text-muted">
          Compare evaluated configurations across action accuracy, cost, and task latency.
        </p>
      </header>
      <section aria-label="Benchmark results">
        <ResultsExplorer />
      </section>
      <section aria-label="Self-submitted results" className="mt-16 border-t border-hairline pt-8">
        <Submissions mode="public" />
      </section>
      <nav aria-label="Leaderboard resources" className="mt-16 flex flex-wrap gap-7 border-t border-hairline pt-8 text-base font-medium">
        <Link href="/dataset/" className="text-link">
          Dataset <span aria-hidden="true">→</span>
        </Link>
      </nav>
    </div>
  );
}
