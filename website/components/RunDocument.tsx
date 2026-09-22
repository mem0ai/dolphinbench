import Link from 'next/link';
import { ArrowLeft } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CopyText from './CopyText';
import { highlight } from '@/lib/highlight';
import { runRepoFileContents } from '@/lib/run-repo-files';
import { repositoryUrl, sourceBranch } from '@/lib/site';

function Code({ text, language }: { text: string; language: string }) {
  const html = highlight(text, language);
  if (html === null) return <code>{text}</code>;
  return <code className={`hljs language-${language}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

export default function RunDocument({ title, file, language }: {
  title: string; file: string; language?: string;
}) {
  const content = runRepoFileContents[file].content;
  const link = (href: string) => {
    if (!href || href.startsWith('#') || /^(?:[a-z]+:|\/)/i.test(href)) return href;
    const resolved = new URL(href, `https://repository.invalid/${file}`);
    const pages: Record<string, string> = {
      '/docs/DRIVER_CONTRACT.md': '/run/guide/',
      '/examples/harness_template.py': '/run/template/',
    };
    return (pages[resolved.pathname] || `${repositoryUrl}/blob/${sourceBranch}${resolved.pathname}`) + resolved.hash;
  };
  return (
    <div className="container-x pb-24 pt-10 sm:pt-14">
      <header className="run-header">
        <Link href="/run/#setup" className="text-link mb-5 inline-flex items-center gap-2 text-sm"><ArrowLeft size={16} aria-hidden="true" /> Run and submit</Link>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <h1 className="max-w-full text-2xl font-semibold sm:text-3xl">{title}</h1>
          <CopyText text={content} label={`Copy ${title.toLowerCase()} for agent`} showLabel />
        </div>
        <nav aria-label="Integration documents" className="mt-5 flex flex-wrap gap-x-6 gap-y-2 text-sm">
          <Link href="/run/guide/" aria-current={!language ? 'page' : undefined}
            className={!language ? 'font-medium text-ink' : 'text-link'}>Harness integration guide</Link>
          <Link href="/run/template/" aria-current={language ? 'page' : undefined}
            className={language ? 'font-medium text-ink' : 'text-link'}>Python template</Link>
        </nav>
      </header>
      <article className="run-document mx-auto max-w-4xl py-6 sm:py-8">
        {language ? <pre tabIndex={0} aria-label="Python template source"><Code text={content} language={language} /></pre>
          : <Markdown remarkPlugins={[remarkGfm]} components={{
            h1: () => null,
            a: ({ href, children }) => <a href={link(href || '')}>{children}</a>,
            table: ({ children }) => <div className="document-table"><table>{children}</table></div>,
            code: ({ className, children }) => className?.startsWith('language-')
              ? <Code text={String(children)} language={className.slice(9)} /> : <code>{children}</code>,
          }}>{content}</Markdown>}
      </article>
    </div>
  );
}
