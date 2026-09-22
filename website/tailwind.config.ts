import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0A0A0A',
        paper: '#FFFFFF',
        sand: '#F5F3EE',
        muted: '#6E6A62',
        faint: '#A8A39A',
        highlight: '#CBB2FF',
        pass: '#1E7A62',
        fail: '#B3261E',
        'hover-ink': '#2A2A2A',
        hairline: 'rgba(10,10,10,0.1)',
        control: 'rgba(10,10,10,0.15)',
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'system-ui', 'sans-serif'],
        mono: ['var(--font-mono)', 'ui-monospace', 'Menlo', 'monospace'],
      },
      fontSize: {
        xs: ['12px', { lineHeight: '1.5' }],
        sm: ['13px', { lineHeight: '1.5' }],
        base: ['15px', { lineHeight: '1.5' }],
        md: ['16px', { lineHeight: '1.6' }],
        lg: ['18px', { lineHeight: '1.5' }],
        xl: ['19px', { lineHeight: '1.5' }],
        '2xl': ['24px', { lineHeight: '1.25' }],
        '3xl': ['28px', { lineHeight: '1.2' }],
        '4xl': ['32px', { lineHeight: '1.1' }],
      },
      borderRadius: { DEFAULT: '6px', md: '6px', lg: '10px' },
      transitionDuration: { DEFAULT: '150ms' },
    },
  },
  plugins: [],
};

export default config;
