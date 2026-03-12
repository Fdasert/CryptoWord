import type { Metadata } from 'next';
import { Analytics } from '@vercel/analytics/next';
import './globals.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://wordguess.space'),

  title: {
    default: 'WordGuess — Solana Wordle | Crypto Word Game, Win SOL',
    template: '%s | WordGuess',
  },
  description:
    'WordGuess is a Solana wordle — guess the 7-letter word and win real SOL. The first provably fair crypto word game on Solana blockchain. Free demo available. Entry 0.01 SOL.',

  keywords: [
    'solana wordle',
    'solana word game',
    'crypto wordle',
    'crypto word game',
    'play to earn word game',
    'win sol crypto',
    'solana game win money',
    'blockchain wordle',
    'word game sol prize',
    'solana puzzle game',
    'provably fair word game',
    'on-chain word game',
    'solana play to earn',
    'sol word game',
    'crypto word puzzle',
  ],

  authors: [{ name: 'WordGuess', url: 'https://wordguess.space' }],
  creator: 'WordGuess',
  publisher: 'WordGuess',

  openGraph: {
    type: 'website',
    locale: 'en_US',
    url: 'https://wordguess.space',
    siteName: 'WordGuess',
    title: 'WordGuess — Solana Wordle | Win Real SOL',
    description:
      'The first Solana wordle. Guess the 7-letter word, win real SOL prizes. Provably fair, on-chain. Free demo available — no wallet needed.',
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
    title: 'WordGuess — Solana Wordle | Win Real SOL',
    description:
      'The first Solana wordle. Guess the 7-letter word and win SOL prizes. Provably fair, free demo available.',
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
                'The first provably fair Solana wordle. Guess the 7-letter word and win real SOL prizes on-chain.',
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