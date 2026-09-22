'use client';

export default function RunNavigation({ sections }: {
  sections: readonly (readonly [string, string])[];
}) {
  return (
    <nav aria-label="Run and submit sections" className="run-contents">
      <label htmlFor="run-section-select" className="sr-only">Jump to section</label>
      <select
        id="run-section-select"
        defaultValue=""
        className="field mt-0 w-full lg:hidden"
        onChange={event => { window.location.hash = event.target.value; }}
      >
        <option value="" disabled>On this page</option>
        {sections.map(([id, label], index) => (
          <option key={id} value={id}>{index + 1}. {label}</option>
        ))}
      </select>
      <ol className="hidden lg:flex">
        {sections.map(([id, label], index) => (
          <li key={id}>
            <a href={`#${id}`}>
              <span>0{index + 1}</span>
              {label}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}
