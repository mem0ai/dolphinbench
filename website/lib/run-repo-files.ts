import files from '@/content/run-repo-files.json';

type BundledFile = { content: string; sha256: string };

export const runRepoFileContents = files as Record<string, BundledFile>;
export const runRepoFiles = new Set(Object.keys(runRepoFileContents));
