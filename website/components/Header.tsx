'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Github } from 'lucide-react';
import { DiscordIcon } from './icons';
import DolphinMark from './DolphinMark';
import { discordUrl, paperUrl, repositoryUrl } from '@/lib/site';

const links = [
  ['/', 'Home'],
  ['/leaderboard/', 'Leaderboard'],
  ['/dataset/', 'Dataset'],
  ['/run/', 'Run and submit'],
] as const;

const pill =
  'inline-flex items-center gap-2 rounded-full border border-ink px-3.5 py-1.5 text-sm font-medium text-ink transition-colors hover:bg-ink hover:text-paper';
// External links open in a new tab so the benchmark stays open.
const external = { target: '_blank', rel: 'noopener noreferrer' } as const;

export default function Header() {
  const pathname = usePathname();
  const current = pathname.startsWith('/personas/') ? '/dataset/' : pathname;
  return (
    <header className="sticky top-0 z-40 border-b border-hairline bg-paper/85 backdrop-blur-md">
      <div className="container-x flex flex-wrap items-center justify-between gap-x-6 gap-y-0 py-3 md:h-[60px] md:flex-nowrap md:py-0">
        <Link
          href="/"
          className="inline-flex items-center gap-2 text-base font-semibold tracking-[-0.01em] text-ink"
        >
          <DolphinMark size={22} tile />
          DolphinBench
        </Link>
        <nav
          aria-label="Primary navigation"
          className="order-3 flex w-full flex-wrap items-center gap-x-5 text-sm md:order-none md:w-auto md:gap-x-7"
        >
          {links.map(([href, label]) => {
            const active = href === '/' ? current === '/' : current.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`border-b-[1.5px] py-2 transition-colors hover:text-ink md:py-5 ${
                  active ? 'border-ink text-ink' : 'border-transparent text-muted'
                }`}
              >
                {label}
              </Link>
            );
          })}
          <a
            href={paperUrl}
            {...external}
            className="border-b-[1.5px] border-transparent py-2 text-muted transition-colors hover:text-ink md:py-5"
          >
            Paper <span aria-hidden="true">↗</span>
          </a>
        </nav>
        <div className="flex items-center gap-2">
          <a href={discordUrl} {...external} className={pill} title="Join the DolphinBench Discord">
            <DiscordIcon size={16} />
            Discord
          </a>
          <a href={repositoryUrl} {...external} className={pill} title="DolphinBench on GitHub">
            <Github size={16} aria-hidden="true" />
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}
