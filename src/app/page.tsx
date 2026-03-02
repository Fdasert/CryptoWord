'use client';

import React, { useMemo, useState, useEffect } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { clusterApiUrl, Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import dynamic from 'next/dynamic';

import '@solana/wallet-adapter-react-ui/styles.css';

const RECEIVER_WALLET_STR = "QVWqd5fSxaFfT1cdxcmNYofqKFM8tFBJMw97kwRWKpS"; // <--- ПРОВЕРЬ ЭТО
const network = WalletAdapterNetwork.Devnet;

const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const [roundId, setRoundId] = useState<string | null>(null);
  const [guesses, setGuesses] = useState<any[]>([]);
  const [currentGuess, setCurrentGuess] = useState("");
  const [status, setStatus] = useState("Connect wallet to play");
  const [isProcessing, setIsProcessing] = useState(false);

  const connection = useMemo(() => new Connection(clusterApiUrl(network)), []);

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
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'guesses' }, (p) => setGuesses(prev => [...prev, p.new]))
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, []);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (!publicKey || isProcessing) return;
      if (e.key === 'Enter') handlePaymentAndSubmit();
      else if (e.key === 'Backspace') setCurrentGuess(prev => prev.slice(0, -1));
      else if (/^[a-zA-Z]$/.test(e.key) && currentGuess.length < 5) setCurrentGuess(prev => (prev + e.key).toUpperCase());
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [currentGuess, publicKey, isProcessing]);

  const handlePaymentAndSubmit = async () => {
    if (currentGuess.length < 5 || !publicKey || !roundId || isProcessing) return;
    try {
      setIsProcessing(true);
      setStatus("Confirming payment...");
      const transaction = new Transaction().add(
        SystemProgram.transfer({ fromPubkey: publicKey, toPubkey: new PublicKey(RECEIVER_WALLET_STR), lamports: 0.001 * LAMPORTS_PER_SOL })
      );
      const sig = await sendTransaction(transaction, connection);
      await connection.confirmTransaction(sig, 'processed');
      await fetch('/api/check-word', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roundId, guess: currentGuess, walletAddress: publicKey.toString() }),
      });
      setCurrentGuess("");
      setStatus("Success!");
    } catch (e) { setStatus("Error occurred."); } finally { setIsProcessing(false); }
  };

  const shorten = (a: string) => `${a.slice(0, 4)}..${a.slice(-3)}`;

  return (
    <div className="min-h-screen bg-black text-white font-mono flex flex-col items-center py-10 px-4">
      {/* Header */}
      <div className="w-full max-w-md flex justify-between items-center mb-10 border-b border-zinc-800 pb-5">
        <h1 className="text-xl font-bold tracking-tighter text-cyan-400">SOL_WORDLE</h1>
        <div className="scale-75 origin-right"><WalletMultiButton /></div>
      </div>

      {/* Grid */}
      <div className="flex flex-col gap-2 mb-10">
        {guesses.map((g, i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="flex gap-1">
              {g.word_attempt.split('').map((char: any, idx: number) => (
                <div key={idx} className={`w-12 h-12 flex items-center justify-center text-xl font-bold border-2 
                  ${g.result_colors[idx] === 'green' ? 'bg-green-600 border-green-600' : 
                    g.result_colors[idx] === 'yellow' ? 'bg-yellow-500 border-yellow-500' : 'bg-zinc-900 border-zinc-800'}`}>
                  {char}
                </div>
              ))}
            </div>
            <span className="text-[10px] text-zinc-600">{shorten(g.wallet_address)}</span>
          </div>
        ))}

        {/* Текущий ввод */}
        {publicKey && (
          <div className="flex gap-1 mt-2">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className={`w-12 h-12 flex items-center justify-center text-xl font-bold border-2 
                ${currentGuess[i] ? 'border-cyan-500 shadow-[0_0_10px_rgba(6,182,212,0.5)]' : 'border-zinc-800'}`}>
                {currentGuess[i] || ""}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Footer / Button */}
      <div className="w-full max-w-xs text-center">
        <p className="text-[10px] uppercase text-zinc-500 mb-4 tracking-widest">{status}</p>
        {publicKey ? (
          <div className="w-full max-w-xs text-center mt-10">
  <p className="text-[10px] text-zinc-500 mb-4 uppercase tracking-widest">
    {currentGuess.length < 5 ? `Type ${5 - currentGuess.length} more letters` : status}
  </p>

  <button 
    onClick={handlePaymentAndSubmit}
    // Кнопка станет активной, как только введено 5 букв, даже если база еще тупит
    disabled={currentGuess.length !== 5 || isProcessing}
    className={`w-full py-4 font-black transition-all border-2 
      ${currentGuess.length === 5 && !isProcessing 
        ? 'bg-cyan-500 border-cyan-500 text-black shadow-[0_0_20px_rgba(6,182,212,0.4)] cursor-pointer' 
        : 'bg-zinc-900 border-zinc-800 text-zinc-700 cursor-not-allowed'}`}
  >
    {isProcessing ? "WAIT..." : "PAY 0.001 SOL & SUBMIT"}
  </button>
</div>
        ) : (
          <div className="p-5 border border-zinc-800 rounded">
            <p className="text-xs text-zinc-500 mb-3">Login to participate</p>
            <div className="flex justify-center"><WalletMultiButton /></div>
          </div>
        )}
      </div>
    </div>
  );
};

// Обертка с отключенным SSR
const GlobalWordleComponent = dynamic(() => Promise.resolve(AppContent), { ssr: false });

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