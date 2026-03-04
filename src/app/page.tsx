'use client';

import React, { useMemo, useState, useEffect, useCallback, useRef } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import {
  Connection,
  Transaction,
  SystemProgram,
  PublicKey,
  LAMPORTS_PER_SOL,
} from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import NextDynamic from 'next/dynamic';
import '@solana/wallet-adapter-react-ui/styles.css';

export const dynamic = 'force-dynamic';

// ─── Constants ───────────────────────────────────────────────────────────────
const GAME_WALLET    = '6ei4xUpeKjKs3uHVkmbxcGvhczWrW8QJ2zTf9a4qUHfe';
const HELIUS_RPC     = 'https://mainnet.helius-rpc.com/?api-key=676b709c-1c3e-4fba-a47d-5cd3f2e78283';
const SUPABASE_URL   = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SUPABASE_ANON  = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
const WORD_LENGTH    = 7;
const MAX_GUESSES    = 6;
const ENTRY_FEE_SOL  = 0.01;
const ENTRY_FEE_LAMP = ENTRY_FEE_SOL * LAMPORTS_PER_SOL;

type Color = 'green' | 'yellow' | 'gray' | 'empty';
type GamePhase =
  | 'idle'          // нет активного раунда
  | 'connecting'    // идёт init
  | 'ready'         // раунд активен, кошелёк не подключён
  | 'paying'        // идёт транзакция
  | 'playing'       // оплачено, вводим слова
  | 'won'           // победил
  | 'lost'          // 6 попыток исчерпано
  | 'round_over'    // раунд закончился по таймеру
  | 'waiting_payout'; // ждём выплаты

interface Round {
  id: string;
  status: string;
  prize_pool: number;
  entry_fee: number;
  end_time: string;
  winner_wallet: string | null;
}

interface Guess {
  id: string;
  wallet_address: string;
  word_attempt: string;
  result_colors: Color[];
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function edgeFn(name: string) {
  return `${SUPABASE_URL}/functions/v1/${name}`;
}

function serviceHeaders() {
  return {
    'Content-Type': 'application/json',
    'apikey': SUPABASE_ANON,
    'Authorization': `Bearer ${SUPABASE_ANON}`,
  };
}

// ─── Main App ─────────────────────────────────────────────────────────────────
const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const connection = useMemo(() => new Connection(HELIUS_RPC, 'confirmed'), []);

  // ── State ──
  const [round, setRound] = useState<Round | null>(null);
  const [allGuesses, setAllGuesses] = useState<Guess[]>([]);   // все угадывания раунда (все игроки)
  const [myGuesses, setMyGuesses]   = useState<Guess[]>([]);   // только мои
  const [currentInput, setCurrentInput] = useState('');
  const [phase, setPhase] = useState<GamePhase>('connecting');
  const [statusMsg, setStatusMsg] = useState('');
  const [timeLeft, setTimeLeft] = useState(60);
  const [shake, setShake] = useState(false);
  const [winnerWallet, setWinnerWallet] = useState<string | null>(null);
  const [payoutCountdown, setPayoutCountdown] = useState(300); // 5 мин в секундах
  const [hasPaid, setHasPaid] = useState(false);
  const [flipRow, setFlipRow] = useState<number | null>(null);

  const roundRef    = useRef<Round | null>(null);
  const phaseRef    = useRef<GamePhase>('connecting');
  const timerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const payoutTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  roundRef.current = round;
  phaseRef.current = phase;

  // ── Load round ──
  const loadRound = useCallback(async () => {
    try {
      const { data } = await supabase
        .from('global_rounds')
        .select('id, status, prize_pool, entry_fee, end_time, winner_wallet')
        .eq('status', 'active')
        .maybeSingle();

      if (!data) {
        setPhase('idle');
        setRound(null);
        return;
      }

      setRound(data as Round);

      const secs = Math.max(0, Math.floor((new Date(data.end_time).getTime() - Date.now()) / 1000));
      setTimeLeft(secs);

      if (data.winner_wallet) {
        setWinnerWallet(data.winner_wallet);
        setPhase('waiting_payout');
        startPayoutCountdown();
        return;
      }

      setPhase(publicKey ? 'paying' : 'ready'); // will refine below
    } catch (e) {
      console.error('loadRound:', e);
    }
  }, [publicKey]);

  // ── Load my guesses for current round ──
  const loadMyGuesses = useCallback(async (roundId: string, wallet: string) => {
    const { data } = await supabase
      .from('guesses')
      .select('*')
      .eq('round_id', roundId)
      .eq('wallet_address', wallet)
      .order('created_at', { ascending: true });
    if (data) setMyGuesses(data as Guess[]);
  }, []);

  // ── Check if already paid ──
  const checkPaid = useCallback(async (roundId: string, wallet: string): Promise<boolean> => {
    const { data } = await supabase
      .from('transactions')
      .select('id')
      .eq('round_id', roundId)
      .eq('wallet_address', wallet)
      .eq('verified', true)
      .maybeSingle();
    return !!data;
  }, []);

  // ── Init ──
  useEffect(() => {
    loadRound();
  }, [loadRound]);

  // ── When wallet connects or round loads — update phase ──
  useEffect(() => {
    if (!round || round.status !== 'active') return;
    if (round.winner_wallet) return;

    (async () => {
      if (!publicKey) {
        setPhase('ready');
        return;
      }

      const wallet = publicKey.toString();
      const [paid, guesses] = await Promise.all([
        checkPaid(round.id, wallet),
        supabase.from('guesses').select('*').eq('round_id', round.id).eq('wallet_address', wallet).order('created_at', { ascending: true }),
      ]);

      setMyGuesses((guesses.data ?? []) as Guess[]);
      setHasPaid(paid);

      if (!paid) {
        setPhase('paying');
      } else if ((guesses.data?.length ?? 0) >= MAX_GUESSES) {
        setPhase('lost');
      } else {
        setPhase('playing');
      }
    })();
  }, [round?.id, publicKey]);

  // ── Round timer ──
  useEffect(() => {
    if (!round || round.status !== 'active' || round.winner_wallet) return;

    if (timerRef.current) clearInterval(timerRef.current);

    timerRef.current = setInterval(async () => {
      const secs = Math.max(0, Math.floor((new Date(round.end_time).getTime() - Date.now()) / 1000));
      setTimeLeft(secs);

      if (secs === 0) {
        clearInterval(timerRef.current!);
        // Вызываем end-round
        try {
          const res = await fetch(edgeFn('end-round'), {
            method: 'POST',
            headers: serviceHeaders(),
            body: JSON.stringify({ round_id: round.id }),
          });
          const data = await res.json();
          if (data.new_round) {
            setRound(data.new_round);
            setAllGuesses([]);
            setMyGuesses([]);
            setCurrentInput('');
            setHasPaid(false);
            setPhase(publicKey ? 'paying' : 'ready');
            setTimeLeft(60);
            setStatusMsg('⏱ Round expired! New round started. Prize rolled over.');
          } else {
            setPhase('round_over');
          }
        } catch (e) {
          setPhase('round_over');
        }
      }
    }, 1000);

    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [round?.id, round?.end_time]);

  // ── All guesses realtime subscription ──
  useEffect(() => {
    if (!round) return;

    // load all current guesses
    (async () => {
      const { data } = await supabase
        .from('guesses')
        .select('*')
        .eq('round_id', round.id)
        .order('created_at', { ascending: true });
      if (data) setAllGuesses(data as Guess[]);
    })();

    const channel = supabase
      .channel(`guesses:${round.id}`)
      .on('postgres_changes', {
        event: 'INSERT', schema: 'public', table: 'guesses',
        filter: `round_id=eq.${round.id}`,
      }, (payload) => {
        const g = payload.new as Guess;
        setAllGuesses(prev => [...prev, g]);
        if (publicKey && g.wallet_address === publicKey.toString()) {
          setMyGuesses(prev => {
            const updated = [...prev, g];
            setFlipRow(updated.length - 1);
            setTimeout(() => setFlipRow(null), 600);
            return updated;
          });
        }
      })
      .subscribe();

    // Watch for winner
    const roundChannel = supabase
      .channel(`round:${round.id}`)
      .on('postgres_changes', {
        event: 'UPDATE', schema: 'public', table: 'global_rounds',
        filter: `id=eq.${round.id}`,
      }, (payload) => {
        const updated = payload.new as Round;
        if (updated.winner_wallet) {
          setWinnerWallet(updated.winner_wallet);
          if (publicKey && updated.winner_wallet === publicKey.toString()) {
            setPhase('won');
            startPayoutCountdown();
          } else {
            setPhase('waiting_payout');
            startPayoutCountdown();
          }
        }
      })
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
      supabase.removeChannel(roundChannel);
    };
  }, [round?.id, publicKey]);

  const startPayoutCountdown = () => {
    if (payoutTimer.current) clearInterval(payoutTimer.current);
    setPayoutCountdown(300);
    payoutTimer.current = setInterval(() => {
      setPayoutCountdown(prev => {
        if (prev <= 1) {
          clearInterval(payoutTimer.current!);
          // Новый раунд стартует — перезагружаем
          setTimeout(() => {
            setPhase('connecting');
            loadRound();
          }, 2000);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
  };

  // ── Payment ──
  const handlePay = async () => {
    if (!publicKey || !round) return;
    setPhase('paying');
    setStatusMsg('Confirm in your wallet...');

    try {
      const tx = new Transaction().add(
        SystemProgram.transfer({
          fromPubkey: publicKey,
          toPubkey: new PublicKey(GAME_WALLET),
          lamports: ENTRY_FEE_LAMP,
        })
      );

      const sig = await sendTransaction(tx, connection);
      setStatusMsg('Verifying on Solana...');
      await connection.confirmTransaction(sig, 'confirmed');

      // Верифицируем через Edge Function
      const res = await fetch(edgeFn('verify-payment'), {
        method: 'POST',
        headers: serviceHeaders(),
        body: JSON.stringify({
          round_id: round.id,
          wallet_address: publicKey.toString(),
          tx_signature: sig,
        }),
      });

      const result = await res.json();
      if (!result.success) {
        setStatusMsg(`⚠ ${result.error}`);
        setPhase('paying');
        return;
      }

      // Обновляем prize_pool локально
      setRound(prev => prev ? { ...prev, prize_pool: prev.prize_pool + ENTRY_FEE_SOL } : prev);
      setHasPaid(true);
      setPhase('playing');
      setStatusMsg('Paid! Type your guess and press Enter.');
    } catch (e: any) {
      setStatusMsg(e?.message?.includes('rejected') ? 'Transaction cancelled.' : `Error: ${e?.message ?? 'Unknown'}`);
      setPhase('paying');
    }
  };

  // ── Submit guess ──
  const handleSubmit = async () => {
    if (!publicKey || !round || currentInput.length !== WORD_LENGTH) return;
    if (phase !== 'playing') return;

    setStatusMsg('Checking...');

    try {
      const res = await fetch(edgeFn('check-guess'), {
        method: 'POST',
        headers: serviceHeaders(),
        body: JSON.stringify({
          round_id: round.id,
          wallet_address: publicKey.toString(),
          word_attempt: currentInput,
        }),
      });

      const result = await res.json();

      if (!result.success) {
        if (result.round_ended) {
          setPhase('round_over');
          setStatusMsg('Round expired!');
        } else {
          setShake(true);
          setTimeout(() => setShake(false), 500);
          setStatusMsg(result.error ?? 'Error');
        }
        return;
      }

      setCurrentInput('');

      if (result.is_winner) {
        setPhase('won');
        setStatusMsg(`🏆 You got it! ${result.prize_pool} SOL incoming in 5 min.`);
        startPayoutCountdown();
      } else if (result.attempts_remaining === 0) {
        setPhase('lost');
        setStatusMsg('No attempts left. Better luck next round!');
      } else {
        setStatusMsg(`${result.attempts_remaining} attempt${result.attempts_remaining !== 1 ? 's' : ''} left`);
      }
    } catch (e: any) {
      setStatusMsg(`Error: ${e?.message ?? 'Unknown'}`);
    }
  };

  // ── Keyboard input ──
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (phase !== 'playing') return;
      if (e.key === 'Enter') {
        if (currentInput.length === WORD_LENGTH) handleSubmit();
        else { setShake(true); setTimeout(() => setShake(false), 500); }
      } else if (e.key === 'Backspace') {
        setCurrentInput(prev => prev.slice(0, -1));
      } else if (/^[a-zA-Z]$/.test(e.key) && currentInput.length < WORD_LENGTH) {
        setCurrentInput(prev => (prev + e.key).toUpperCase());
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [phase, currentInput, handleSubmit]);

  // ── On-screen keyboard ──
  const KEYBOARD_ROWS = [
    ['Q','W','E','R','T','Y','U','I','O','P'],
    ['A','S','D','F','G','H','J','K','L'],
    ['ENTER','Z','X','C','V','B','N','M','⌫'],
  ];

  const keyColors = useMemo(() => {
    const map: Record<string, Color> = {};
    for (const g of myGuesses) {
      g.word_attempt.split('').forEach((ch, i) => {
        const cur = map[ch];
        const next = g.result_colors[i];
        if (cur === 'green') return;
        if (next === 'green') { map[ch] = 'green'; return; }
        if (cur === 'yellow') return;
        if (next === 'yellow') { map[ch] = 'yellow'; return; }
        map[ch] = 'gray';
      });
    }
    return map;
  }, [myGuesses]);

  const onKeyPress = (key: string) => {
    if (phase !== 'playing') return;
    if (key === 'ENTER') { if (currentInput.length === WORD_LENGTH) handleSubmit(); }
    else if (key === '⌫') setCurrentInput(prev => prev.slice(0, -1));
    else if (currentInput.length < WORD_LENGTH) setCurrentInput(prev => prev + key);
  };

  // ── Timer color ──
  const timerColor = timeLeft > 20 ? '#22d3ee' : timeLeft > 10 ? '#f59e0b' : '#ef4444';
  const timerPct = (timeLeft / 60) * 100;

  // ── Cell component ──
  const Cell = ({ char, color, revealed }: { char: string; color: Color; revealed: boolean }) => {
    const bg: Record<Color, string> = {
      green: '#16a34a',
      yellow: '#ca8a04',
      gray: '#374151',
      empty: 'transparent',
    };
    const border: Record<Color, string> = {
      green: '#16a34a',
      yellow: '#ca8a04',
      gray: '#374151',
      empty: char ? '#22d3ee' : '#1f2937',
    };
    return (
      <div style={{
        width: 44, height: 44,
        background: revealed ? bg[color] : bg['empty'],
        border: `2px solid ${border[color]}`,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 18, fontWeight: 900, color: '#fff',
        fontFamily: "'Space Grotesk', monospace",
        transition: 'background 0.2s',
        boxShadow: revealed && color === 'green' ? '0 0 12px rgba(22,163,74,0.5)' : 'none',
      }}>
        {char}
      </div>
    );
  };

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div style={{
      minHeight: '100vh',
      background: '#09090b',
      color: '#fff',
      fontFamily: "'Space Grotesk', 'Courier New', monospace",
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      padding: '0 16px 40px',
    }}>

      {/* ── Header ── */}
      <div style={{
        width: '100%', maxWidth: 520,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '20px 0 16px',
        borderBottom: '1px solid #18181b',
      }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 900, letterSpacing: '0.08em', color: '#22d3ee' }}>
            SOL_WORD
          </div>
          <div style={{ fontSize: 10, color: '#52525b', letterSpacing: '0.15em', marginTop: 2 }}>
            CRYPTO WORDLE · 7 LETTERS
          </div>
        </div>
        <div style={{ transform: 'scale(0.88)', transformOrigin: 'right center' }}>
          <WalletMultiButton />
        </div>
      </div>

      {/* ── Prize Pool + Timer ── */}
      {round && (
        <div style={{
          width: '100%', maxWidth: 520,
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          padding: '14px 0 10px',
        }}>
          {/* Prize */}
          <div style={{ textAlign: 'left' }}>
            <div style={{ fontSize: 10, color: '#52525b', letterSpacing: '0.15em', marginBottom: 4 }}>PRIZE POOL</div>
            <div style={{ fontSize: 28, fontWeight: 900, color: '#fbbf24', lineHeight: 1 }}>
              {Number(round.prize_pool).toFixed(3)}
              <span style={{ fontSize: 14, color: '#78716c', marginLeft: 4 }}>SOL</span>
            </div>
          </div>

          {/* Timer */}
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 10, color: '#52525b', letterSpacing: '0.15em', marginBottom: 4 }}>TIME LEFT</div>
            <div style={{ fontSize: 28, fontWeight: 900, color: timerColor, lineHeight: 1 }}>
              {timeLeft}
              <span style={{ fontSize: 14, color: '#52525b', marginLeft: 4 }}>SEC</span>
            </div>
            {/* Timer bar */}
            <div style={{ width: 100, height: 3, background: '#18181b', borderRadius: 2, marginTop: 6, marginLeft: 'auto' }}>
              <div style={{ width: `${timerPct}%`, height: '100%', background: timerColor, borderRadius: 2, transition: 'width 1s linear, background 1s' }} />
            </div>
          </div>
        </div>
      )}

      {/* Live players count */}
      {round && (
        <div style={{ width: '100%', maxWidth: 520, fontSize: 10, color: '#3f3f46', letterSpacing: '0.1em', paddingBottom: 12 }}>
          {allGuesses.length} GUESS{allGuesses.length !== 1 ? 'ES' : ''} THIS ROUND ·{' '}
          {new Set(allGuesses.map(g => g.wallet_address)).size} PLAYER{new Set(allGuesses.map(g => g.wallet_address)).size !== 1 ? 'S' : ''}
        </div>
      )}

      {/* ── Guess Grid ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
        {Array.from({ length: MAX_GUESSES }).map((_, rowIdx) => {
          const guess = myGuesses[rowIdx];
          const isActive = rowIdx === myGuesses.length && phase === 'playing';
          const isFlipping = flipRow === rowIdx;
          return (
            <div
              key={rowIdx}
              style={{
                display: 'flex', gap: 5,
                opacity: rowIdx > myGuesses.length ? 0.25 : 1,
                animation: isFlipping ? 'flipIn 0.5s ease' : shake && isActive ? 'shake 0.4s ease' : undefined,
              }}
            >
              {Array.from({ length: WORD_LENGTH }).map((_, colIdx) => {
                if (guess) {
                  return <Cell key={colIdx} char={guess.word_attempt[colIdx]} color={guess.result_colors[colIdx]} revealed />;
                }
                if (isActive) {
                  const ch = currentInput[colIdx] ?? '';
                  return <Cell key={colIdx} char={ch} color="empty" revealed={false} />;
                }
                return <Cell key={colIdx} char="" color="empty" revealed={false} />;
              })}
            </div>
          );
        })}
      </div>

      {/* ── Status / Action area ── */}
      <div style={{ width: '100%', maxWidth: 380, textAlign: 'center', margin: '8px 0 16px' }}>

        {/* Status message */}
        {statusMsg && (
          <div style={{ fontSize: 11, color: '#71717a', letterSpacing: '0.12em', marginBottom: 10, textTransform: 'uppercase' }}>
            {statusMsg}
          </div>
        )}

        {/* IDLE */}
        {phase === 'idle' && (
          <div style={{ padding: '24px', border: '1px dashed #27272a', borderRadius: 12 }}>
            <div style={{ fontSize: 13, color: '#52525b', marginBottom: 12 }}>No active round</div>
            <button onClick={loadRound} style={btnStyle('#22d3ee')}>REFRESH</button>
          </div>
        )}

        {/* CONNECTING */}
        {phase === 'connecting' && (
          <div style={{ color: '#52525b', fontSize: 12 }}>Loading...</div>
        )}

        {/* READY (wallet not connected) */}
        {phase === 'ready' && (
          <div style={{ padding: '20px', border: '1px dashed #27272a', borderRadius: 12 }}>
            <div style={{ fontSize: 12, color: '#52525b', marginBottom: 16 }}>CONNECT WALLET TO JOIN</div>
            <div style={{ display: 'flex', justifyContent: 'center' }}><WalletMultiButton /></div>
          </div>
        )}

        {/* PAYING */}
        {phase === 'paying' && publicKey && !hasPaid && (
          <div>
            <div style={{ fontSize: 11, color: '#52525b', marginBottom: 12, letterSpacing: '0.1em' }}>
              ENTRY FEE: {ENTRY_FEE_SOL} SOL → {GAME_WALLET.slice(0,6)}…{GAME_WALLET.slice(-4)}
            </div>
            <button onClick={handlePay} style={btnStyle('#22d3ee')}>
              PAY {ENTRY_FEE_SOL} SOL & PLAY
            </button>
          </div>
        )}

        {/* PLAYING */}
        {phase === 'playing' && (
          <button
            onClick={handleSubmit}
            disabled={currentInput.length !== WORD_LENGTH}
            style={btnStyle(currentInput.length === WORD_LENGTH ? '#22d3ee' : '#27272a', currentInput.length !== WORD_LENGTH)}
          >
            SUBMIT GUESS ↵
          </button>
        )}

        {/* WON */}
        {phase === 'won' && (
          <div style={{ padding: 24, background: 'rgba(22,163,74,0.1)', border: '1px solid rgba(22,163,74,0.3)', borderRadius: 12 }}>
            <div style={{ fontSize: 32, marginBottom: 8 }}>🏆</div>
            <div style={{ fontSize: 18, fontWeight: 900, color: '#22c55e', marginBottom: 8 }}>YOU WON!</div>
            <div style={{ fontSize: 12, color: '#86efac', marginBottom: 16 }}>
              {Number(round?.prize_pool ?? 0).toFixed(3)} SOL will arrive in your wallet
            </div>
            <div style={{ fontSize: 11, color: '#52525b' }}>
              PAYOUT IN {Math.floor(payoutCountdown / 60)}:{String(payoutCountdown % 60).padStart(2, '0')}
            </div>
          </div>
        )}

        {/* LOST */}
        {phase === 'lost' && (
          <div style={{ padding: 20, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)', borderRadius: 12 }}>
            <div style={{ fontSize: 14, color: '#f87171', marginBottom: 8 }}>OUT OF ATTEMPTS</div>
            <div style={{ fontSize: 11, color: '#52525b' }}>Wait for the next round to play again</div>
          </div>
        )}

        {/* ROUND_OVER */}
        {phase === 'round_over' && (
          <div style={{ padding: 20, background: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)', borderRadius: 12 }}>
            <div style={{ fontSize: 14, color: '#fbbf24', marginBottom: 8 }}>ROUND EXPIRED</div>
            <div style={{ fontSize: 11, color: '#78716c', marginBottom: 16 }}>Nobody guessed. Prize rolls over to next round.</div>
            <button onClick={loadRound} style={btnStyle('#fbbf24')}>JOIN NEW ROUND</button>
          </div>
        )}

        {/* WAITING_PAYOUT (someone else won) */}
        {phase === 'waiting_payout' && (
          <div style={{ padding: 20, background: 'rgba(234,179,8,0.06)', border: '1px solid #27272a', borderRadius: 12 }}>
            <div style={{ fontSize: 14, color: '#fbbf24', marginBottom: 8 }}>ROUND WON!</div>
            <div style={{ fontSize: 11, color: '#78716c', marginBottom: 6 }}>
              Winner: {winnerWallet?.slice(0,6)}…{winnerWallet?.slice(-4)}
            </div>
            <div style={{ fontSize: 11, color: '#52525b', marginBottom: 16 }}>
              New round starts in {Math.floor(payoutCountdown / 60)}:{String(payoutCountdown % 60).padStart(2, '0')}
            </div>
          </div>
        )}
      </div>

      {/* ── On-screen Keyboard ── */}
      {(phase === 'playing') && (
        <div style={{ width: '100%', maxWidth: 480, marginTop: 8 }}>
          {KEYBOARD_ROWS.map((row, ri) => (
            <div key={ri} style={{ display: 'flex', justifyContent: 'center', gap: 5, marginBottom: 5 }}>
              {row.map(key => {
                const color = keyColors[key];
                const bg = color === 'green' ? '#16a34a'
                  : color === 'yellow' ? '#ca8a04'
                  : color === 'gray' ? '#27272a'
                  : '#3f3f46';
                const isWide = key === 'ENTER' || key === '⌫';
                return (
                  <button
                    key={key}
                    onClick={() => onKeyPress(key)}
                    style={{
                      width: isWide ? 62 : 36,
                      height: 46,
                      background: bg,
                      border: 'none',
                      borderRadius: 6,
                      color: '#fff',
                      fontSize: isWide ? 10 : 14,
                      fontWeight: 700,
                      cursor: 'pointer',
                      fontFamily: 'inherit',
                      transition: 'background 0.15s, transform 0.1s',
                    }}
                    onMouseDown={e => (e.currentTarget.style.transform = 'scale(0.92)')}
                    onMouseUp={e => (e.currentTarget.style.transform = 'scale(1)')}
                  >
                    {key}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {/* ── Recent guesses ticker (all players) ── */}
      {allGuesses.length > 0 && (
        <div style={{ width: '100%', maxWidth: 520, marginTop: 24, borderTop: '1px solid #18181b', paddingTop: 16 }}>
          <div style={{ fontSize: 10, color: '#3f3f46', letterSpacing: '0.15em', marginBottom: 10 }}>LIVE GUESSES</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 160, overflowY: 'auto' }}>
            {[...allGuesses].reverse().slice(0, 15).map((g, i) => (
              <div key={g.id} style={{ display: 'flex', alignItems: 'center', gap: 8, opacity: i === 0 ? 1 : 0.5 }}>
                <span style={{ fontSize: 10, color: '#52525b', minWidth: 80, fontFamily: 'monospace' }}>
                  {g.wallet_address.slice(0, 4)}…{g.wallet_address.slice(-4)}
                </span>
                <div style={{ display: 'flex', gap: 2 }}>
                  {g.word_attempt.split('').map((ch, ci) => (
                    <div key={ci} style={{
                      width: 22, height: 22, fontSize: 11, fontWeight: 700,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      background: g.result_colors[ci] === 'green' ? '#16a34a' : g.result_colors[ci] === 'yellow' ? '#ca8a04' : '#27272a',
                      color: '#fff', borderRadius: 3,
                    }}>{ch}</div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── CSS animations ── */}
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;700;900&display=swap');
        @keyframes shake {
          0%,100% { transform: translateX(0); }
          20%      { transform: translateX(-6px); }
          40%      { transform: translateX(6px); }
          60%      { transform: translateX(-4px); }
          80%      { transform: translateX(4px); }
        }
        @keyframes flipIn {
          0%   { transform: rotateX(0deg); }
          50%  { transform: rotateX(-90deg); }
          100% { transform: rotateX(0deg); }
        }
        @keyframes pulse {
          0%,100% { opacity:1; }
          50%     { opacity:0.4; }
        }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: #09090b; }
        ::-webkit-scrollbar-thumb { background: #27272a; border-radius: 2px; }
        .wallet-adapter-button { font-family: 'Space Grotesk', monospace !important; }
      `}</style>
    </div>
  );
};

// ─── Button style helper ──────────────────────────────────────────────────────
function btnStyle(color: string, disabled = false): React.CSSProperties {
  return {
    width: '100%',
    padding: '14px 0',
    background: disabled ? '#18181b' : color,
    color: disabled ? '#3f3f46' : '#000',
    border: 'none',
    fontWeight: 900,
    fontSize: 13,
    letterSpacing: '0.1em',
    cursor: disabled ? 'not-allowed' : 'pointer',
    fontFamily: "'Space Grotesk', monospace",
    transition: 'opacity 0.15s, transform 0.1s',
    borderRadius: 8,
  };
}

// ─── Root wrapper ─────────────────────────────────────────────────────────────
const GameDynamic = NextDynamic(() => Promise.resolve(AppContent), { ssr: false });

export default function SolWord() {
  const wallets = useMemo(() => [new PhantomWalletAdapter(), new SolflareWalletAdapter()], []);
  return (
    <ConnectionProvider endpoint={HELIUS_RPC}>
      <WalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider>
          <GameDynamic />
        </WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}
