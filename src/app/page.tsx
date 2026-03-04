'use client';

import React, { useMemo, useState, useEffect } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import NextDynamic from 'next/dynamic'; // ПЕРЕИМЕНОВАЛИ ИМПОРТ
import '@solana/wallet-adapter-react-ui/styles.css';
export const dynamic = 'force-dynamic';




import '@solana/wallet-adapter-react-ui/styles.css';

const RECEIVER_WALLET_STR = "QVWqd5fSxaFfT1cdxcmNYofqKFM8tFBJMw97kwRWKpS"; 
const MAINNET_RPC = "https://api.mainnet-beta.solana.com";
const network = WalletAdapterNetwork.Mainnet;
const WORD_LENGTH = 7;

const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const [roundId, setRoundId] = useState<string | null>(null);
  const [guesses, setGuesses] = useState<any[]>([]);
  const [currentGuess, setCurrentGuess] = useState("");
  const [status, setStatus] = useState("Connect wallet and type 7 letters");
  const [isProcessing, setIsProcessing] = useState(false);

  const connection = useMemo(() => new Connection("https://mainnet.helius-rpc.com/?api-key=676b709c-1c3e-4fba-a47d-5cd3f2e78283"), []);

  // Загрузка данных
  useEffect(() => {
    const init = async () => {
      try {
        const { data: round } = await supabase.from('global_rounds').select('id').eq('status', 'active').single();
        if (round) {
          setRoundId(round.id);
          const { data: history } = await supabase.from('guesses').select('*').eq('round_id', round.id).order('created_at', { ascending: true });
          if (history) setGuesses(history);
        }
      } catch (e) { console.error("Database fetch error:", e); }
    };
    init();

    const channel = supabase.channel('global_guesses')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'guesses' }, (p) => setGuesses(prev => [...prev, p.new]))
      .subscribe();
    return () => { supabase.removeChannel(channel); };
  }, []);

  // Ввод букв
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (!publicKey || isProcessing) return;
      if (e.key === 'Enter') handlePaymentAndSubmit();
      else if (e.key === 'Backspace') setCurrentGuess(prev => prev.slice(0, -1));
      else if (/^[a-zA-Z]$/.test(e.key) && currentGuess.length < WORD_LENGTH) {
        setCurrentGuess(prev => (prev + e.key).toUpperCase());
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [currentGuess, publicKey, isProcessing]);

  const handlePaymentAndSubmit = async () => {
    console.log("Attempting payment..."); // Отладка в консоль
    if (currentGuess.length !== WORD_LENGTH || !publicKey) {
      setStatus(`Type exactly ${WORD_LENGTH} letters first!`);
      return;
    }

    try {
      setIsProcessing(true);
      setStatus("Confirming in your wallet...");

      const transaction = new Transaction().add(
        SystemProgram.transfer({
          fromPubkey: publicKey,
          toPubkey: new PublicKey(RECEIVER_WALLET_STR),
          lamports: 0.001 * LAMPORTS_PER_SOL
        })
      );

      const sig = await sendTransaction(transaction, connection);
      setStatus("Verifying transaction...");
      await connection.confirmTransaction(sig, 'processed');

      setStatus("Sending guess to server...");
      // Даже если roundId null, мы попробуем отправить, сервер разберется
      await fetch('/api/check-word', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ roundId, guess: currentGuess, walletAddress: publicKey.toString() }),
      });

      setCurrentGuess("");
      setStatus("Success! Word submitted.");
    } catch (e: any) {
      console.error("Payment error:", e);
      setStatus(e.message || "Transaction failed.");
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <div className="min-h-screen bg-[#050505] text-white font-mono flex flex-col items-center py-10 px-4">
      {/* Header */}
      <div className="w-full max-w-xl flex justify-between items-center mb-10 border-b border-zinc-800 pb-5">
        <h1 className="text-2xl font-black text-cyan-400">SOL_WORDLE_7</h1>
        <div className="scale-90"><WalletMultiButton /></div>
      </div>

      {/* Grid */}
      <div className="flex flex-col gap-2 mb-10">
        {guesses.map((g, i) => (
          <div key={i} className="flex gap-1">
            {g.word_attempt.split('').map((char: string, idx: number) => (
              <div key={idx} className={`w-10 h-10 flex items-center justify-center text-lg font-bold border-2 
                ${g.result_colors[idx] === 'green' ? 'bg-green-600 border-green-600' : 'bg-zinc-900 border-zinc-800'}`}>
                {char}
              </div>
            ))}
          </div>
        ))}

        {/* Текущий ввод (7 ячеек) */}
        {publicKey && (
          <div className="flex gap-1 mt-4">
            {Array.from({ length: WORD_LENGTH }).map((_, i) => (
              <div key={i} className={`w-12 h-12 flex items-center justify-center text-xl font-black border-2 transition-all
                ${currentGuess[i] ? 'border-cyan-400 bg-cyan-900/20 shadow-[0_0_10px_rgba(6,182,212,0.3)]' : 'border-zinc-800'}`}>
                {currentGuess[i] || ""}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Control Area */}
      <div className="w-full max-w-sm text-center">
        <p className="text-[10px] uppercase text-zinc-500 mb-6 tracking-[0.2em]">{status}</p>
        
        {publicKey ? (
          <button 
            onClick={handlePaymentAndSubmit}
            disabled={currentGuess.length !== WORD_LENGTH || isProcessing}
            className={`w-full py-4 font-black text-lg transition-all
              ${currentGuess.length === WORD_LENGTH && !isProcessing 
                ? 'bg-cyan-500 text-black shadow-xl shadow-cyan-500/20 cursor-pointer active:scale-95' 
                : 'bg-zinc-900 text-zinc-700 cursor-not-allowed'}`}
          >
            {isProcessing ? "PROCESSING..." : "PAY 0.001 SOL & SUBMIT"}
          </button>
        ) : (
          <div className="p-8 border-2 border-dashed border-zinc-800 rounded-xl">
            <p className="text-sm text-zinc-500 mb-4">CONNECT WALLET TO JOIN</p>
            <div className="flex justify-center"><WalletMultiButton /></div>
          </div>
        )}
      </div>
    </div>
  );
};

const GlobalWordleComponent = NextDynamic(() => Promise.resolve(AppContent), { ssr: false });

export default function GlobalWordle() {
  const wallets = useMemo(() => [new PhantomWalletAdapter(), new SolflareWalletAdapter()], []);
  
  // Используем прямой URL вместо clusterApiUrl для надежности
  return (
    <ConnectionProvider endpoint={MAINNET_RPC}>
      <WalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider><GlobalWordleComponent /></WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}