import type { Metadata } from 'next';
import { Analytics } from '@vercel/analytics/next';
import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://wordguess.space'),

  title: {
    default: 'WordGuess — Solana Word Game | Win SOL Prizes',
    template: '%s | WordGuess',
  },
  description:
    'Guess the 7-letter word, win real SOL. WordGuess is a provably fair on-chain word game on Solana. Play free demo or enter for 0.01 SOL and compete for the prize pool.',

  keywords: [
    'solana word game',
    'crypto wordle',
    'solana wordle',
    'win sol',
    'solana game',
    'blockchain word puzzle',
    'play to earn solana',
    'sol prize game',
    'crypto puzzle game',
    'word game crypto',
    'wordle solana',
    'sol reward game',
  ],

  authors: [{ name: 'WordGuess', url: 'https://wordguess.space' }],
  creator: 'WordGuess',
  publisher: 'WordGuess',

  openGraph: {
    type: 'website',
    locale: 'en_US',
    url: 'https://wordguess.space',
    siteName: 'WordGuess',
    title: 'WordGuess — Guess the Word, Win SOL',
    description:
      'Provably fair word game on Solana. Guess the 7-letter word and win the prize pool. 0.01 SOL entry fee. Play free demo now.',
    images: [
      {
        url: '/og-image.png',
        width: 1200,
        height: 630,
        alt: 'WordGuess — Solana Word Game',
      },
    ],
  },

  twitter: {
    card: 'summary_large_image',
    title: 'WordGuess — Guess the Word, Win SOL',
    description:
      'Provably fair word game on Solana. Guess the 7-letter word and win the prize pool.',
    images: ['/og-image.png'],
    creator: '@WordGuessSOL', // замени на свой твиттер если есть
  },

  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      'max-video-preview': -1,
      'max-image-preview': 'large',
      'max-snippet': -1,
    },
  },

  icons: {
    icon: '/favicon.ico',
    shortcut: '/favicon.ico',
    apple: '/apple-touch-icon.png',
  },

  manifest: '/site.webmanifest',

  alternates: {
    canonical: 'https://wordguess.space',
  },

  category: 'game',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        {/* JSON-LD structured data for Google */}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'WebApplication',
              name: 'WordGuess',
              url: 'https://wordguess.space',
              description:
                'Provably fair on-chain word game on Solana. Guess the 7-letter word and win SOL prizes.',
              applicationCategory: 'GameApplication',
              operatingSystem: 'Web Browser',
              offers: {
                '@type': 'Offer',
                price: '0.01',
                priceCurrency: 'SOL',
                description: 'Entry fee per round',
              },
              featureList: [
                'Provably fair on-chain verification',
                'Real SOL prize pool',
                'Free demo mode',
                'Multiplayer real-time',
              ],
            }),
          }}
        />
      </head>
      <body>
        {children}
        <Analytics />
      </body>
    </html>
  );
}
