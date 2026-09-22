import mem0 from '@/assets/logos/mem0.svg';
import honcho from '@/assets/logos/honcho.png';
import hindsight from '@/assets/logos/hindsight.png';
import supermemory from '@/assets/logos/supermemory.svg';
import nous from '@/assets/logos/nousresearch.png';
import openai from '@/assets/logos/openai.png';

// Keyed by the display names in lib/results.ts. Missing keys render a monogram instead.
export const memoryLogos: Record<string, string> = {
  Mem0: mem0.src, Honcho: honcho.src, Hindsight: hindsight.src, Supermemory: supermemory.src,
};
export const harnessLogos: Record<string, string> = { Hermes: nous.src };
export const providerLogos: Record<string, string> = { OpenAI: openai.src };
