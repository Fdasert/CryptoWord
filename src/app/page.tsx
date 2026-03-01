'use client';

import React, { FC, useMemo, useState, useEffect, useCallback } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { clusterApiUrl, Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { supabase } from '@/lib/supabase';

// Импортируем стили для модального окна кошельков
import '@solana/wallet-adapter-react-ui/styles.css';

// --- КОНФИГУРАЦИЯ ---
const RECEIVER_WALLET = new PublicKey("QVWqd5fSxaFfT1cdxcmNYofqKFM8tFBJMw97kwRWKpS"); 
const network = WalletAdapterNetwork.Devnet;

const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const [roundId, setRoundId] = useState<string | null>(null);
  const [guesses, setGuesses] = useState<any[]>([]);
  const [currentGuess, setCurrentGuess] = useState("");
  const [status, setStatus] = useState("Connect wallet & pay 0.001 SOL to guess");

  const connection = useMemo(() => new Connection(clusterApiUrl(network)), []);

  // 1. Загрузка раунда и Realtime
  useEffect(() => {
    const init = async () => {
      const { data: round } = await supabase.from('global_rounds').select('id').eq('status', 'active').single();
      if (round) {
        setRoundId(round.id);
        const { data: history } = await supabase.from('guesses').select('*').eq('round_id', round.id).order('created_at', { ascending: true });
        if (history) setGuesses(history);
      }
    };
    init();

    const channel = supabase.channel('global_guesses')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'guesses' }, (payload) => {
        setGuesses(prev => [...prev, payload.new]);
      }).subscribe();

    return () => { supabase.removeChannel(channel); };
  }, []);

  // 2. Логика ввода букв
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (!publicKey) return;
      if (e.key === 'Enter') handlePaymentAndSubmit();
      else if (e.key === 'Backspace') setCurrentGuess(prev => prev.slice(0, -1));
      else if (/^[a-zA-Z]$/.test(e.key) && currentGuess.length < 5) setCurrentGuess(prev => (prev + e.key).toUpperCase());
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [currentGuess, publicKey]);

  // 3. Главная функция: Оплата + Ход
  const handlePaymentAndSubmit = async () => {
  if (currentGuess.length < 5 || !publicKey || !roundId) return;

  try {
    // Создаем объект PublicKey прямо здесь
    const receiver = new PublicKey(RECEIVER_WALLET); 
    
    setStatus("Processing transaction...");
    
    const transaction = new Transaction().add(
      SystemProgram.transfer({
        fromPubkey: publicKey,
        toPubkey: receiver, // Используем созданный объект
        lamports: 0.001 * LAMPORTS_PER_SOL,
      })
    );

      const signature = await sendTransaction(transaction, connection);
      await connection.confirmTransaction(signature, 'processed');

      setStatus("Verifying guess...");
      await fetch('/api/check-word', {
        method: 'POST',
        body: JSON.stringify({ 
          roundId, 
          guess: currentGuess, 
          walletAddress: publicKey.toString() 
        }),
      });

      setCurrentGuess("");
      setStatus("Success! Wait for next turn.");
    } catch (e) {
      console.error(e);
      setStatus("Transaction failed.");
    }
  };

  const shorten = (addr: string) => `${addr.slice(0, 4)}...${addr.slice(-4)}`;

  return (
    <div className="flex flex-col items-center min-h-screen bg-black text-white p-6 font-mono">
      <div className="w-full flex justify-between items-center mb-12">
        <h1 className="text-2xl font-black">SOL WORDLE</h1>
        <WalletMultiButton />
      </div>

      <p className="text-cyan-400 text-sm mb-6 uppercase tracking-widest">{status}</p>

      {/* История ходов всех игроков */}
      <div className="space-y-3 mb-8">
        {guesses.map((g, i) => (
          <div key={i} className="flex items-center gap-4">
            <div className="flex gap-1">
              {g.word_attempt.split('').map((char: string, idx: number) => (
                <div key={idx} className={`w-12 h-12 flex items-center justify-center font-bold text-xl border
                  ${g.result_colors[idx] === 'green' ? 'bg-green-600 border-green-600' : 'bg-zinc-900 border-zinc-700'}`}>
                  {char}
                </div>
              ))}
            </div>
            <span className="text-[10px] text-zinc-500">{shorten(g.wallet_address)}</span>
          </div>
        ))}

        {/* Текущая строка ввода */}
        {publicKey && (
          <div className="flex gap-1 border-t border-zinc-800 pt-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="w-12 h-12 flex items-center justify-center font-bold text-xl border border-cyan-500 animate-pulse">
                {currentGuess[i] || ""}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

// 4. Обертка провайдеров (необходима для работы Wallet Adapter)
export default function GlobalWordle() {
  const wallets = useMemo(() => [new PhantomWalletAdapter(), new SolflareWalletAdapter()], []);

  return (
    <ConnectionProvider endpoint={clusterApiUrl(network)}>
      <WalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider>
          <AppContent />
        </WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}