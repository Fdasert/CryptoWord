import "./globals.css";
import { Inter } from "next/font/google";
import AppWalletProvider from "./AppWalletProvider";

const inter = Inter({ subsets: ["latin"] });

export const metadata = {
  metadataBase: new URL('https://your-domain.vercel.app'), // Замените на свой домен позже
  title: 'Sol Wordle | The Ultimate Solana Word Challenge',
  description: 'Connect your wallet and solve 7-letter puzzles on Solana.',
  openGraph: {
    title: 'Sol Wordle',
    images: '/og-image.png',
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className={inter.className} style={{ margin: 0, padding: 0, overflow: 'hidden', background: '#020617' }}>
        <AppWalletProvider>
          {children}
        </AppWalletProvider>
      </body>
    </html>
  );
}