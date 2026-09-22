import Link from 'next/link';
import { ExternalLink } from 'lucide-react';
import {
  HistoryMessage,
  formatDate,
  formatTime,
  testHref,
  messageHref,
} from '@/lib/types';

export default function SessionCard({
  personaId,
  session,
  open = false,
}: {
  personaId: string;
  session: HistoryMessage;
  open?: boolean;
}) {
  return (
    <details
      id={`message-${session.id}`}
      open={open}
      className="group border-b border-hairline py-4"
    >
      <summary className="cursor-pointer text-sm">
        <span className="ml-2 font-mono text-xs text-muted">
          {session.id}
        </span>
        <span className="ml-3">{formatDate(session.date)}</span>
        <span className="ml-3 text-xs text-muted">
          {formatTime(session.date)}
        </span>
        <span className="mt-2 block line-clamp-2 text-muted group-open:hidden">
          {session.content}
        </span>
      </summary>
      <div className="pt-4">
        <p className="source-text" data-message-content>
          {session.content}
        </p>
        <div className="mt-5 flex flex-wrap gap-x-4 gap-y-2 text-xs">
          <Link
            href={messageHref(personaId, session.id)}
            className="text-link inline-flex items-center gap-1"
          >
            <ExternalLink size={13} />
            Message {session.id}
          </Link>
          {session.source_test_ids.length > 0 && (
            <span>
              Source for:{' '}
              {session.source_test_ids.map((id, index) => (
                <span key={id}>
                  {index > 0 && ', '}
                  <Link className="text-link" href={testHref(personaId, id)}>
                    Test {id}
                  </Link>
                </span>
              ))}
            </span>
          )}
          {session.context_test_ids.length > 0 && (
            <span>
              Related context for:{' '}
              {session.context_test_ids.map((id, index) => (
                <span key={id}>
                  {index > 0 && ', '}
                  <Link className="text-link" href={testHref(personaId, id)}>
                    Test {id}
                  </Link>
                </span>
              ))}
            </span>
          )}
        </div>
      </div>
    </details>
  );
}
