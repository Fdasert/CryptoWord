import type { Metadata } from 'next';

export const metadata: Metadata = {
  title: 'WordGuess — Guess the 7-letter word · Win SOL',
  description: 'Competitive Solana word game. Pay 0.01 SOL per attempt, guess the 7-letter word first and win the entire prize pool.',
  openGraph: {
    title: 'WordGuess',
    description: 'Guess the 7-letter word. Win the SOL prize pool.',
    url: 'https://wordguess.space',
    siteName: 'WordGuess',
    locale: 'en_US',
    type: 'website',
  },
  twitter: {
    card: 'summary',
    title: 'WordGuess — Win SOL by guessing words',
    description: 'Pay 0.01 SOL per attempt. First to guess wins the pool.',
  },
  icons: {
    icon: [
      { url: '/favicon.svg', type: 'image/svg+xml' },
      { url: '/favicon.ico', sizes: '32x32' },
    ],
    apple: '/favicon.svg',
  },
  metadataBase: new URL('https://wordguess.space'),
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, padding: 0, background: '#09090b' }}>
        {children}
      </body>
    </html>
  );
}