'use client';

import React, { useMemo, useState, useEffect, useCallback } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { clusterApiUrl, Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import dynamic from 'next/dynamic';

// Стили для кошельков
import '@solana/wallet-adapter-react-ui/styles.css';

// --- КОНФИГУРАЦИЯ ---
const RECEIVER_WALLET_STR = "QVWqd5fSxaFfT1cdxcmNYofqKFM8tFBJMw97kwRWKpS"; // <--- ЗАМЕНИ НА СВОЙ АДРЕС!
const network = WalletAdapterNetwork.Devnet;

const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const [roundId, setRoundId] = useState<string | null>(null);
  const [guesses, setGuesses] = useState<any[]>([]);
  const [currentGuess, setCurrentGuess] = useState("");
  const [status, setStatus] = useState("Connect wallet to start");
  const [isProcessing, setIsProcessing] = useState(false);

  const connection = useMemo(() => new Connection(clusterApiUrl(network)), []);

  // 1. Загрузка данных раунда и Realtime подписка
  useEffect(() => {
    const initGame = async () => {
      const { data: round } = await supabase.from('global_rounds').select('id').eq('status', 'active').single();
      if (round) {
        setRoundId(round.id);
        const { data: history } = await supabase.from('guesses').select('*').eq('round_id', round.id).order('created_at', { ascending: true });
        if (history) setGuesses(history);
      }
    };
    initGame();

    const channel = supabase.channel('global_guesses')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'guesses' }, (payload) => {
        setGuesses(prev => [...prev, payload.new]);
      }).subscribe();

    return () => { supabase.removeChannel(channel); };
  }, []);

  // 2. Слушатель клавиатуры (только английские буквы)
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (!publicKey || isProcessing) return;
      if (e.key === 'Enter') handlePaymentAndSubmit();
      else if (e.key === 'Backspace') setCurrentGuess(prev => prev.slice(0, -1));
      else if (/^[a-zA-Z]$/.test(e.key) && currentGuess.length < 5) {
        setCurrentGuess(prev => (prev + e.key).toUpperCase());
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [currentGuess, publicKey, isProcessing]);

  // 3. Функция оплаты и отправки слова
  const handlePaymentAndSubmit = async () => {
    if (currentGuess.length < 5 || !publicKey || !roundId || isProcessing) return;

    try {
      setIsProcessing(true);
      setStatus("Confirming transaction in wallet...");
      
      const receiver = new PublicKey(RECEIVER_WALLET_STR);
      const transaction = new Transaction().add(
        SystemProgram.transfer({
          fromPubkey: publicKey,
          toPubkey: receiver,
          lamports: 0.001 * LAMPORTS_PER_SOL,
        })
      );

      const signature = await sendTransaction(transaction, connection);
      setStatus("Verifying on blockchain...");
      await connection.confirmTransaction(signature, 'processed');

      setStatus("Submitting your guess...");
      const res = await fetch('/api/check-word', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          roundId, 
          guess: currentGuess, 
          walletAddress: publicKey.toString() 
        }),
      });

      if (res.ok) {
        setCurrentGuess("");
        setStatus("Guess submitted! Wait for result.");
      } else {
        setStatus("Error submitting guess.");
      }

    } catch (e: any) {
      console.error(e);
      setStatus(e.message || "Transaction failed.");
    } finally {
      setIsProcessing(false);
    }
  };

  const shorten = (addr: string) => `${addr.slice(0, 4)}...${addr.slice(-4)}`;

  return (
    <div className="flex flex-col items-center min-h-screen bg-[#0a0a0a] text-white p-6 font-mono">
      {/* Header */}
      <div className="w-full max-w-2xl flex justify-between items-center mb-12 border-b border-zinc-800 pb-6">
        <div className="flex flex-col">
          <h1 className="text-3xl font-black tracking-tighter text-cyan-500">SOL WORDLE</h1>
          <span className="text-[10px] text-zinc-500 uppercase tracking-widest">Global Multiplayer Session</span>
        </div>
        <WalletMultiButton />
      </div>

      {/* Game Board (Previous Guesses) */}
      <div className="w-full max-w-sm space-y-3 mb-12">
        {guesses.map((g, i) => (
          <div key={i} className="flex items-center gap-4 group">
            <div className="flex gap-1.5 flex-grow">
              {g.word_attempt.split('').map((char: string, idx: number) => (
                <div key={idx} className={`w-12 h-12 flex items-center justify-center font-bold text-xl border-2 transition-all duration-500
                  ${g.result_colors[idx] === 'green' ? 'bg-green-600 border-green-600 shadow-[0_0_10px_rgba(22,163,74,0.4)]' : 
                    g.result_colors[idx] === 'yellow' ? 'bg-yellow-500 border-yellow-500 shadow-[0_0_10px_rgba(234,179,8,0.4)]' : 
                    'bg-zinc-900 border-zinc-800 text-zinc-400'}`}>
                  {char}
                </div>
              ))}
            </div>
            <div className="text-[10px] text-zinc-600 font-bold bg-zinc-900 px-2 py-1 rounded border border-zinc-800">
              {shorten(g.wallet_address)}
            </div>
          </div>
        ))}

        {/* Empty rows to maintain 6-row grid */}
        {Array.from({ length: Math.max(0, 6 - guesses.length) }).map((_, i) => (
          <div key={i} className="flex gap-1.5 opacity-10">
            {Array(5).fill(0).map((_, j) => (
              <div key={j} className="w-12 h-12 border-2 border-zinc-700 bg-transparent"></div>
            ))}
          </div>
        ))}
      </div>

      {/* Input Section */}
      <div className="w-full max-w-sm mt-auto flex flex-col gap-4">
        <div className="text-center text-xs text-zinc-500 uppercase tracking-tighter h-4">
          {status}
        </div>

        {publicKey ? (
          <>
            <div className="flex justify-center gap-2">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className={`w-14 h-14 flex items-center justify-center font-black text-2xl border-2 transition-all
                  ${currentGuess[i] ? 'border-cyan-400 bg-cyan-900/20' : 'border-zinc-800 bg-zinc-950'}`}>
                  {currentGuess[i] || ""}
                </div>
              ))}
            </div>
            
            <button
              onClick={handlePaymentAndSubmit}
              disabled={currentGuess.length < 5 || isProcessing}
              className={`w-full py-4 px-6 font-black text-lg tracking-[0.2em] transition-all
                ${currentGuess.length === 5 && !isProcessing
                  ? 'bg-cyan-500 text-black hover:bg-white' 
                  : 'bg-zinc-800 text-zinc-600 cursor-not-allowed'}`}
            >
              {isProcessing ? "PROCESSING..." : "SUBMIT GUESS"}
            </button>
          </>
        ) : (
          <div className="bg-zinc-900/50 border border-zinc-800 p-8 rounded-xl text-center">
            <p className="text-zinc-400 text-sm mb-6">Connect wallet to join the global hunt</p>
            <div className="flex justify-center"><WalletMultiButton /></div>
          </div>
        )}
      </div>
    </div>
  );
};

// Обертка с отключенным SSR для корректной работы кошельков
const GlobalWordleComponent = dynamic(() => Promise.resolve(AppContent), {
  ssr: false
});

export default function GlobalWordle() {
  const wallets = useMemo(() => [new PhantomWalletAdapter(), new SolflareWalletAdapter()], []);

  return (
    <ConnectionProvider endpoint={clusterApiUrl(network)}>
      <WalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider>
          <GlobalWordleComponent />
        </WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}