import { redirect } from 'next/navigation';
import { currentSession } from '@/lib/current-session';
import { db } from '@/lib/db';
import Submissions from '@/components/Submissions';

export const dynamic = 'force-dynamic';
export const metadata = { title: 'Admin', robots: { index: false, follow: false } };

type AccountSummary = {
  id: string;
  username: string;
  label: string;
  role: string;
  active: boolean;
  created_at: string;
  last_login_at: string | null;
  event_count: number;
  last_activity_at: string | null;
};

type RecentActivity = {
  created_at: string;
  event_name: string;
  path: string | null;
  target: string | null;
  metadata: Record<string, unknown>;
  country: string | null;
  region: string | null;
  city: string | null;
  user_agent: string | null;
  referrer: string | null;
  ip_hash: string | null;
  label: string;
  username: string;
};

function formatDate(value: unknown) {
  return value ? new Date(value as string).toLocaleString('en-US', { timeZone: 'UTC' }) + ' UTC' : 'Never';
}

export default async function AdminPage() {
  const session = await currentSession();
  if (!session) redirect('/login/');
  if (session.role !== 'admin') redirect('/');

  const sql = db();
  const [accountResult, recentResult] = await Promise.all([
    sql`
      SELECT a.id, a.username, a.label, a.role, a.active, a.created_at, a.last_login_at,
             count(e.id)::int AS event_count, max(e.created_at) AS last_activity_at
      FROM partner_accounts a
      LEFT JOIN activity_events e ON e.account_id = a.id
      GROUP BY a.id
      ORDER BY a.created_at ASC
    `,
    sql`
      SELECT e.created_at, e.event_name, e.path, e.target, e.metadata,
             e.country, e.region, e.city, e.user_agent, e.referrer, e.ip_hash,
             a.label, a.username
      FROM activity_events e
      JOIN partner_accounts a ON a.id = e.account_id
      ORDER BY e.created_at DESC
      LIMIT 200
    `,
  ]);
  const accounts = accountResult as unknown as AccountSummary[];
  const recent = recentResult as unknown as RecentActivity[];

  return (
    <div className="container-x pb-24 pt-16">
      <header className="flex items-start justify-between gap-6">
        <div>
          <h1 className="page-title">Partner activity</h1>
          <p className="mt-3 text-sm text-muted">Account access and the latest recorded interactions.</p>
        </div>
        <form action="/api/auth/logout/" method="post">
          <button className="command">Sign out</button>
        </form>
      </header>

      <section className="mt-12"><Submissions mode="admin" /></section>
      <section className="mt-12 overflow-x-auto border-y border-hairline">
        <table className="w-full min-w-[800px] text-left text-sm">
          <thead className="text-xs text-muted">
            <tr>
              <th className="py-4 pr-5 font-medium">Account</th>
              <th className="py-4 pr-5 font-medium">Username</th>
              <th className="py-4 pr-5 font-medium">Status</th>
              <th className="py-4 pr-5 font-medium">Last login</th>
              <th className="py-4 pr-5 font-medium">Last activity</th>
              <th className="py-4 font-medium">Events</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-hairline">
            {accounts.map((account) => (
              <tr key={account.id}>
                <td className="py-4 pr-5 font-medium text-ink">{account.label}</td>
                <td className="py-4 pr-5 font-mono text-xs text-muted">{account.username}</td>
                <td className="py-4 pr-5 text-muted">{account.active ? account.role : 'disabled'}</td>
                <td className="py-4 pr-5 text-muted">{formatDate(account.last_login_at)}</td>
                <td className="py-4 pr-5 text-muted">{formatDate(account.last_activity_at)}</td>
                <td className="py-4 text-muted">{account.event_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="mt-16">
        <h2 className="text-xl font-semibold">Recent activity</h2>
        <div className="mt-6 divide-y divide-hairline border-y border-hairline">
          {recent.map((item, index) => (
            <div key={`${item.created_at}-${index}`} className="grid gap-2 py-4 text-sm md:grid-cols-[170px_150px_110px_1fr]">
              <div className="text-xs text-muted">{formatDate(item.created_at)}</div>
              <div className="font-medium text-ink">{item.label}</div>
              <div className="font-mono text-xs text-muted">{item.event_name}</div>
              <div className="min-w-0 text-muted">
                <span className="break-all">{item.path}</span>
                {item.target ? <span> · {item.target}</span> : null}
                {Object.keys(item.metadata || {}).length > 0 ? (
                  <code className="ml-2 break-all text-xs text-muted">{JSON.stringify(item.metadata)}</code>
                ) : null}
                <details className="mt-2 text-xs text-muted">
                  <summary className="cursor-pointer select-none">Request details</summary>
                  <div className="mt-2 space-y-1 break-all">
                    <div>Account: {item.username}</div>
                    <div>Location: {[item.city, item.region, item.country].filter(Boolean).join(', ') || 'Unavailable'}</div>
                    <div>Referrer: {item.referrer || 'Unavailable'}</div>
                    <div>User agent: {item.user_agent || 'Unavailable'}</div>
                    <div>Visitor hash: {item.ip_hash || 'Unavailable'}</div>
                  </div>
                </details>
              </div>
            </div>
          ))}
          {recent.length === 0 ? <p className="py-8 text-sm text-muted">No activity recorded yet.</p> : null}
        </div>
      </section>
    </div>
  );
}
