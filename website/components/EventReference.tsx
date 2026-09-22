import Link from 'next/link';
import { ExternalLink } from 'lucide-react';
import {
  Fact,
  HistoryMessage,
  formatDate,
  formatTime,
  messageHref,
} from '@/lib/types';

export default function EventReference({
  personaId,
  fact,
  messages,
}: {
  personaId: string;
  fact: Fact;
  messages: Map<string, HistoryMessage>;
}) {
  const sources = (ids: string[]) =>
    ids.map((id) => {
      const message = messages.get(id);
      if (!message) throw new Error(`Unresolved source ${personaId}/${id}`);
      return (
        <details key={id} className="border-t border-hairline py-3">
          <summary className="text-xs text-muted">
            <span className="ml-2 font-mono">{id}</span>
            <span className="ml-3">
              {formatDate(message.date)} / {formatTime(message.date)}
            </span>
          </summary>
          <p className="source-text mt-4">{message.content}</p>
          <Link
            href={messageHref(personaId, id)}
            className="text-link mt-3 inline-flex items-center gap-1 text-xs"
          >
            <ExternalLink size={13} />
            Message {id} in history
          </Link>
        </details>
      );
    });
  return (
    <article id={`fact-${fact.id}`} className="border-b border-hairline py-6">
      <h3 className="eyebrow mb-2">Fact {fact.id}</h3>
      <p className="source-text">{fact.statement}</p>
      {fact.applies_when && <p className="source-text mt-3 text-muted">
        <span className="font-medium">Applies when: </span>
        {fact.applies_when}
      </p>}
      <h4 className="mb-2 mt-5 text-xs font-medium">
        Source evidence ({fact.source_session_ids.length})
      </h4>
      {sources(fact.source_session_ids)}
      {fact.related_history_session_ids.length > 0 && (
        <details className="mt-3 text-xs">
          <summary>
            Related history ({fact.related_history_session_ids.length})
          </summary>
          <div className="mt-3">
            {sources(fact.related_history_session_ids)}
          </div>
        </details>
      )}
    </article>
  );
}
