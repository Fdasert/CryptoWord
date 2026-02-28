"use client";

import React, { useState, useEffect, useCallback } from 'react';
import { WalletMultiButton } from "@solana/wallet-adapter-react-ui";
import { useWallet } from "@solana/wallet-adapter-react";
import { useWalletModal } from "@solana/wallet-adapter-react-ui";

const CONFIG = {
  WORD_LENGTH: 7,
  MAX_ATTEMPTS: 6,
  INITIAL_TIME: 60,
  RESTART_DELAY: 3000,
};

export default function Home() {
  const { connected, publicKey } = useWallet();
  const { setVisible } = useWalletModal();
  
  const [mounted, setMounted] = useState(false);
  const [secretWord, setSecretWord] = useState("");
  const [guesses, setGuesses] = useState<string[]>(Array(CONFIG.MAX_ATTEMPTS).fill(""));
  const [currentAttempt, setCurrentAttempt] = useState(0);
  const [currentGuess, setCurrentGuess] = useState("");
  const [isGameOver, setIsGameOver] = useState(false);
  const [gameResult, setGameResult] = useState<'win' | 'loss' | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [timeLeft, setTimeLeft] = useState(CONFIG.INITIAL_TIME);

  const fetchNewWord = useCallback(async () => {
  setIsLoading(true);
  try {
    const response = await fetch('/api/word'); 
    const data = await response.json();
    setSecretWord(data.word.toUpperCase());
  } catch (error) {
    console.error("API Error:", error);
    setSecretWord("SOLANAS");
  } finally {
    setIsLoading(false);
  }
}, []);

  const startNewGame = useCallback(() => {
    setGuesses(Array(CONFIG.MAX_ATTEMPTS).fill(""));
    setCurrentAttempt(0);
    setCurrentGuess("");
    setIsGameOver(false);
    setGameResult(null);
    setTimeLeft(CONFIG.INITIAL_TIME);
    fetchNewWord();
  }, [fetchNewWord]);

  useEffect(() => {
    setMounted(true);
    fetchNewWord();
  }, [fetchNewWord]);

  useEffect(() => {
    if (!mounted || !connected || isLoading) return;

    let timer: NodeJS.Timeout;
    if (!isGameOver && timeLeft > 0) {
      timer = setInterval(() => setTimeLeft(prev => prev - 1), 1000);
    } else if (timeLeft === 0 && !isGameOver) {
      setIsGameOver(true);
      setGameResult('loss');
    }

    let restartTimer: NodeJS.Timeout;
    if (isGameOver) {
      restartTimer = setTimeout(startNewGame, CONFIG.RESTART_DELAY);
    }

    return () => {
      clearInterval(timer);
      clearTimeout(restartTimer);
    };
  }, [mounted, connected, isGameOver, isLoading, timeLeft, startNewGame]);

  useEffect(() => {
    const handleKeyUp = (e: KeyboardEvent) => {
      if (!connected || isGameOver || isLoading || currentAttempt >= CONFIG.MAX_ATTEMPTS) return;
      
      const key = e.key.toUpperCase();
      if (key === 'ENTER' && currentGuess.length === CONFIG.WORD_LENGTH) {
        const newGuesses = [...guesses];
        newGuesses[currentAttempt] = currentGuess;
        setGuesses(newGuesses);
        
        if (currentGuess === secretWord) {
          setGameResult('win');
          setIsGameOver(true);
        } else if (currentAttempt === CONFIG.MAX_ATTEMPTS - 1) {
          setGameResult('loss');
          setIsGameOver(true);
        }
        setCurrentAttempt(prev => prev + 1);
        setCurrentGuess("");
      } else if (key === 'BACKSPACE') {
        setCurrentGuess(prev => prev.slice(0, -1));
      } else if (currentGuess.length < CONFIG.WORD_LENGTH && /^[A-Z]$/.test(key)) {
        setCurrentGuess(prev => prev + key);
      }
    };
    window.addEventListener('keyup', handleKeyUp);
    return () => window.removeEventListener('keyup', handleKeyUp);
  }, [currentGuess, currentAttempt, connected, isGameOver, guesses, secretWord, isLoading]);

  const getBoxStyle = (rowIndex: number, colIndex: number, char: string): React.CSSProperties => {
    const base: React.CSSProperties = {
      width: 'min(50px, 12vw)', height: 'min(50px, 12vw)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: 'min(24px, 6vw)', fontWeight: '900', borderRadius: '6px',
      borderWidth: '2px', borderStyle: 'solid', borderColor: '#1e293b',
      transition: 'all 0.3s ease', color: 'white'
    };
    if (rowIndex === currentAttempt && char) return { ...base, borderColor: '#22d3ee', transform: 'scale(1.05)' };
    if (rowIndex < currentAttempt && char) {
      const isCorrect = secretWord[colIndex] === char;
      const isPresent = secretWord.includes(char);
      const style = { ...base, animation: `flip 0.5s ease forwards ${colIndex * 0.1}s` };
      if (isCorrect) return { ...style, backgroundColor: '#10b981', borderColor: '#10b981' };
      if (isPresent) return { ...style, backgroundColor: '#f59e0b', borderColor: '#f59e0b' };
      return { ...style, backgroundColor: '#334155', borderColor: '#334155', color: '#94a3b8' };
    }
    return base;
  };

  if (!mounted) return <div style={{ backgroundColor: '#020617', height: '100vh' }} />;

  return (
    <div style={{ backgroundColor: '#020617', height: '100vh', width: '100vw', color: 'white', display: 'flex', flexDirection: 'column', padding: '20px', fontFamily: 'Inter, sans-serif', overflow: 'hidden', boxSizing: 'border-box' }}>
      <style>{`
        @keyframes flip { 0% { transform: rotateX(0deg); } 50% { transform: rotateX(90deg); } 100% { transform: rotateX(0deg); } }
        @keyframes pop { 0% { transform: scale(0.9); } 50% { transform: scale(1.1); } 100% { transform: scale(1); } }
      `}</style>

      <header style={{ height: '60px', width: '100%', maxWidth: '600px', margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <span style={{ fontSize: '20px', fontWeight: '900', color: '#22d3ee', letterSpacing: '-1px' }}>SOL WORDLE</span>
          {connected && <span style={{ fontSize: '10px', color: '#475569', fontFamily: 'monospace' }}>{publicKey?.toBase58().slice(0, 4)}...{publicKey?.toBase58().slice(-4)}</span>}
        </div>
        <WalletMultiButton />
      </header>

      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
        {connected && (
          <div style={{ width: '100%', maxWidth: '400px', marginBottom: '25px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', fontWeight: 'bold', color: timeLeft <= 10 ? '#ef4444' : '#475569', marginBottom: '6px' }}>
              <span>{isGameOver ? 'PREPARING NEXT ROUND...' : 'MISSION TIME'}</span>
              <span>{timeLeft}s</span>
            </div>
            <div style={{ height: '4px', background: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
              <div style={{ width: `${(timeLeft/CONFIG.INITIAL_TIME)*100}%`, height: '100%', background: timeLeft <= 10 ? '#ef4444' : '#22d3ee', transition: 'width 1s linear' }} />
            </div>
          </div>
        )}

        <div style={{ display: 'grid', gap: '8px', opacity: connected ? 1 : 0.15, filter: connected ? 'none' : 'blur(8px)', transition: 'all 0.5s' }}>
          {guesses.map((guess, rIndex) => (
            <div key={rIndex} style={{ display: 'flex', gap: '8px' }}>
              {Array.from({ length: CONFIG.WORD_LENGTH }).map((_, cIndex) => (
                <div key={cIndex} style={getBoxStyle(rIndex, cIndex, rIndex === currentAttempt ? currentGuess[cIndex] : guess[cIndex])}>
                  {rIndex === currentAttempt ? currentGuess[cIndex] : guess[cIndex]}
                </div>
              ))}
            </div>
          ))}
        </div>

        <div style={{ marginTop: '30px', textAlign: 'center', minHeight: '100px' }}>
          {!connected ? (
            <div style={{ animation: 'pop 0.3s ease' }}>
              <button onClick={() => setVisible(true)} style={{ background: '#22d3ee', color: '#020617', border: 'none', padding: '12px 24px', borderRadius: '8px', fontWeight: 'bold', cursor: 'pointer', fontSize: '16px' }}>
                CONNECT TO PLAY
              </button>
            </div>
          ) : isGameOver ? (
            <div style={{ animation: 'pop 0.3s ease' }}>
              <h2 style={{ fontSize: '24px', margin: '0 0 5px 0', color: gameResult === 'win' ? '#10b981' : '#ef4444', fontWeight: '900' }}>
                {gameResult === 'win' ? 'SUCCESS! 🎉' : `FAILED: ${secretWord}`}
              </h2>
              <p style={{ fontSize: '12px', color: '#475569', margin: 0 }}>New word in 3 seconds...</p>
            </div>
          ) : (
            !isLoading && <p style={{ color: '#475569', letterSpacing: '4px', fontSize: '14px', fontWeight: 'bold' }}>ATTEMPT {currentAttempt + 1} / {CONFIG.MAX_ATTEMPTS}</p>
          )}
        </div>
      </main>

      <footer style={{ height: '40px', display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '20px', borderTop: '1px solid #0f172a' }}>
        <span style={{ fontSize: '10px', color: '#334155' }}>v1.2.0-STABLE</span>
        <span style={{ fontSize: '10px', color: '#334155' }}>NETWORK: MAINNET-BETA</span>
      </footer>
    </div>
  );
}