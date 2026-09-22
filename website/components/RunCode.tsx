import CopyText from './CopyText';
import { highlight, type Language } from '@/lib/highlight';

export default function RunCode({ label, language = 'cli', children }: {
  label: string; language?: Language; children: string;
}) {
  const html = highlight(children, language);
  return <div className="run-code-block">
    <div className="flex min-h-9 items-center justify-between gap-3 border-b border-hairline px-3.5 text-xs text-muted">
      <span>{label}</span>
      <CopyText text={children} label={`Copy ${label}`} />
    </div>
    <pre className="run-code" tabIndex={0}>
      {html === null ? <code>{children}</code>
        : <code className={`hljs language-${language}`} dangerouslySetInnerHTML={{ __html: html }} />}
    </pre>
  </div>;
}
