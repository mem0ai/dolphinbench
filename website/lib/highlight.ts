import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import json from 'highlight.js/lib/languages/json';
import python from 'highlight.js/lib/languages/python';
import yaml from 'highlight.js/lib/languages/yaml';

// Terminal commands as they appear on the Run page: the command, its flags, and any
// URL or quoted argument. Plain bash leaves most of these lines unstyled.
const cli = () => ({
  name: 'cli',
  contains: [
    { className: 'comment', begin: /#/, end: /$/ },
    { className: 'built_in', begin: /^[\w./-]+/ },
    { className: 'string', begin: /https?:\/\/\S+/ },
    { className: 'string', begin: /"/, end: /"/ },
    { className: 'string', begin: /'/, end: /'/ },
    { className: 'params', begin: /(?<=\s)-{1,2}[\w-]+/ },
  ],
});

hljs.registerLanguage('bash', bash);
hljs.registerLanguage('cli', cli);
hljs.registerLanguage('json', json);
hljs.registerLanguage('python', python);
hljs.registerLanguage('yaml', yaml);

export type Language = 'bash' | 'cli' | 'json' | 'python' | 'yaml';

/** HTML for `text` with highlight.js token spans, or null when the language is unknown. */
export function highlight(text: string, language: string): string | null {
  if (!hljs.getLanguage(language)) return null;
  return hljs.highlight(text, { language }).value;
}
