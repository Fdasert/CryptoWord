'use client';

import React, { useMemo, useState, useEffect, useCallback, useRef } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import { Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL } from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import '@solana/wallet-adapter-react-ui/styles.css';

export const dynamic = 'force-dynamic';

const GAME_WALLET    = '6ei4xUpeKjKs3uHVkmbxcGvhczWrW8QJ2zTf9a4qUHfe';
const HELIUS_RPC     = 'https://mainnet.helius-rpc.com/?api-key=676b709c-1c3e-4fba-a47d-5cd3f2e78283';
const SUPABASE_URL   = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SUPABASE_ANON  = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
const WORD_LENGTH    = 7;
const ENTRY_FEE_SOL  = 0.01;
const ENTRY_FEE_LAMP = ENTRY_FEE_SOL * LAMPORTS_PER_SOL;
const ROUND_SECS     = 120;

type Color = 'green' | 'yellow' | 'gray' | 'empty';
interface Round { id: string; status: string; prize_pool: number; entry_fee: number; end_time: string; winner_wallet: string | null; }
interface Guess  { id: string; wallet_address: string; word_attempt: string; result_colors: Color[]; created_at: string; }

const edgeFn = (n: string) => `${SUPABASE_URL}/functions/v1/${n}`;
const apiH   = () => ({ 'Content-Type': 'application/json', 'apikey': SUPABASE_ANON, 'Authorization': `Bearer ${SUPABASE_ANON}` });
const short  = (w: string) => w.slice(0,4) + '..' + w.slice(-4);

// ── Tile ──────────────────────────────────────────────────────────────────────
function Tile({ letter, color }: { letter: string; color: Color }) {
  const bg = color==='green'?'#538d4e': color==='yellow'?'#b59f3b': color==='gray'?'#3a3a3c': '#1a1a1b';
  return (
    <div style={{ width:42, height:42, display:'flex', alignItems:'center', justifyContent:'center',
      background:bg, border:`2px solid ${color==='empty'?'#3a3a3c':'transparent'}`,
      borderRadius:4, fontSize:19, fontWeight:700, color:'#fff', textTransform:'uppercase', transition:'background 0.3s' }}>
      {letter}
    </div>
  );
}

// ── Guess row in feed ─────────────────────────────────────────────────────────
function GuessRow({ guess, isMe }: { guess: Guess; isMe: boolean }) {
  const letters = guess.word_attempt.toUpperCase().split('');
  const isWin   = guess.result_colors.every(c => c === 'green');
  return (
    <div style={{ display:'flex', alignItems:'center', gap:8, padding:'5px 0',
      borderRadius:6, background: isWin ? 'rgba(83,141,78,0.12)' : 'transparent',
      animation: 'fadeIn 0.35s ease', }}>
      <span style={{ fontSize:11, fontFamily:'monospace', minWidth:80, textAlign:'right',
        color: isWin ? '#4ade80' : isMe ? '#a78bfa' : '#6b7280', fontWeight: isMe||isWin ? 700 : 400 }}>
        {isWin ? '🏆 ' : ''}{isMe ? 'YOU' : short(guess.wallet_address)}
      </span>
      <div style={{ display:'flex', gap:3 }}>
        {letters.map((l,i) => <Tile key={i} letter={l} color={guess.result_colors[i] ?? 'gray'} />)}
      </div>
    </div>
  );
}

// ── Input preview ─────────────────────────────────────────────────────────────
function InputRow({ value, shake }: { value: string; shake: boolean }) {
  return (
    <div style={{ display:'flex', gap:4, animation: shake ? 'shake 0.4s' : undefined }}>
      {Array.from({ length: WORD_LENGTH }).map((_,i) =>
        <Tile key={i} letter={value[i] ?? ''} color="empty" />
      )}
    </div>
  );
}

// ── Keyboard ─────────────────────────────────────────────────────────────────
const KB = [['Q','W','E','R','T','Y','U','I','O','P'],['A','S','D','F','G','H','J','K','L'],['ENTER','Z','X','C','V','B','N','M','⌫']];
function Keyboard({ lc, onKey }: { lc: Record<string,Color>; onKey:(k:string)=>void }) {
  return (
    <div style={{ display:'flex', flexDirection:'column', gap:6, alignItems:'center' }}>
      {KB.map((row,ri) => (
        <div key={ri} style={{ display:'flex', gap:5 }}>
          {row.map(k => {
            const c  = lc[k];
            const bg = c==='green'?'#538d4e': c==='yellow'?'#b59f3b': c==='gray'?'#3a3a3c': '#818384';
            return (
              <button key={k} onClick={() => onKey(k)} style={{
                width: k==='ENTER'||k==='⌫' ? 58 : 36, height:54, background:bg,
                border:'none', borderRadius:4, color:'#fff', fontWeight:700,
                fontSize: k==='ENTER' ? 11 : 14, cursor:'pointer', transition:'background 0.2s',
              }}>{k}</button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// ── Circle timer ──────────────────────────────────────────────────────────────
function CircleTimer({ timeLeft, total }: { timeLeft: number; total: number }) {
  const pct   = total > 0 ? timeLeft / total : 0;
  const color = timeLeft <= 15 ? '#ef4444' : timeLeft <= 30 ? '#f59e0b' : '#22d3ee';
  const r = 40, circ = 2 * Math.PI * r;
  return (
    <div style={{ position:'relative', width:100, height:100, display:'flex', alignItems:'center', justifyContent:'center' }}>
      <svg width={100} height={100} style={{ position:'absolute', top:0, left:0, transform:'rotate(-90deg)' }}>
        <circle cx={50} cy={50} r={r} fill="none" stroke="#27272a" strokeWidth={7}/>
        <circle cx={50} cy={50} r={r} fill="none" stroke={color} strokeWidth={7}
          strokeDasharray={`${pct * circ} ${circ}`} strokeLinecap="round"
          style={{ transition:'stroke-dasharray 1s linear, stroke 0.5s' }}/>
      </svg>
      <div style={{ position:'relative', textAlign:'center' }}>
        <div style={{ fontSize:26, fontWeight:800, color, fontVariantNumeric:'tabular-nums', lineHeight:1 }}>{timeLeft}</div>
        <div style={{ fontSize:10, color:'#52525b', marginTop:2, letterSpacing:1 }}>SEC</div>
      </div>
    </div>
  );
}

// ── App ───────────────────────────────────────────────────────────────────────
const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const connection = useMemo(() => new Connection(HELIUS_RPC, 'confirmed'), []);

  const [round,      setRound]      = useState<Round|null>(null);
  const [allGuesses, setAllGuesses] = useState<Guess[]>([]);
  const [input,      setInput]      = useState('');
  const [timeLeft,   setTimeLeft]   = useState(0);
  const [status,     setStatus]     = useState('');
  const [busy,       setBusy]       = useState(false);
  const [hasPending, setHasPending] = useState(false); // paid but not yet submitted guess
  const [myCount,    setMyCount]    = useState(0);     // guesses this round
  const [winner,     setWinner]     = useState<string|null>(null);
  const [payoutSecs, setPayoutSecs] = useState(0);
  const [shake,      setShake]      = useState(false);
  const [nextRoundIn, setNextRoundIn] = useState(0); // seconds until new round starts

  const timerRef  = useRef<any>(null);
  const payoutRef = useRef<any>(null);

  // ── Load round ──
  const loadRound = useCallback(async () => {
    const { data } = await supabase.from('active_round').select('*').maybeSingle();
    setRound(data ?? null);
    if (!data) { setAllGuesses([]); setMyCount(0); setHasPending(false); return; }

    const { data: guesses } = await supabase
      .from('guesses').select('*').eq('round_id', data.id).order('created_at', { ascending: false });
    setAllGuesses((guesses ?? []) as Guess[]);
    setWinner(data.winner_wallet ?? null);

    if (publicKey) {
      const mine = (guesses ?? []).filter(g => g.wallet_address === publicKey.toString());
      setMyCount(mine.length);
      // check if there's an unused payment
      const { count: txCount } = await supabase.from('transactions').select('id', { count:'exact', head:true })
        .eq('round_id', data.id).eq('wallet_address', publicKey.toString()).eq('verified', true);
      setHasPending((txCount ?? 0) > mine.length);
    }
  }, [publicKey]);

  useEffect(() => { loadRound(); }, [loadRound]);

  // ── Timer ──
  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (!round || round.status !== 'active' || round.winner_wallet) return;
    setTimeLeft(Math.max(0, Math.floor((new Date(round.end_time).getTime() - Date.now()) / 1000)));
    timerRef.current = setInterval(async () => {
      const secs = Math.max(0, Math.floor((new Date(round.end_time).getTime() - Date.now()) / 1000));
      setTimeLeft(secs);
      if (secs === 0) {
        clearInterval(timerRef.current);
        // End round on server
        await fetch(edgeFn('end-round'), { method:'POST', headers:apiH(), body:JSON.stringify({ round_id: round.id }) });
        // Show "next round" countdown for 10 seconds
        setNextRoundIn(10);
        const cd = setInterval(() => {
          setNextRoundIn(p => {
            if (p <= 1) {
              clearInterval(cd);
              setInput(''); setWinner(null); setStatus(''); setMyCount(0); setHasPending(false); setNextRoundIn(0);
              loadRound();
              return 0;
            }
            return p - 1;
          });
        }, 1000);
      }
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [round?.id, round?.end_time]);

  // ── Realtime: new guess → prepend to top ──
  useEffect(() => {
    if (!round) return;
    const ch = supabase.channel(`g:${round.id}`)
      .on('postgres_changes', { event:'INSERT', schema:'public', table:'guesses', filter:`round_id=eq.${round.id}` },
        payload => {
          const g = payload.new as Guess;
          setAllGuesses(prev => prev.find(x => x.id === g.id) ? prev : [g, ...prev]);
          if (publicKey && g.wallet_address === publicKey.toString()) {
            setMyCount(p => p + 1);
            setHasPending(false);
          }
        })
      .subscribe();
    return () => { supabase.removeChannel(ch); };
  }, [round?.id, publicKey]);

  // ── Realtime: winner ──
  useEffect(() => {
    if (!round) return;
    const ch = supabase.channel(`rw:${round.id}`)
      .on('postgres_changes', { event:'UPDATE', schema:'public', table:'global_rounds', filter:`id=eq.${round.id}` },
        payload => {
          const u = payload.new as Round;
          setRound(u);
          if (u.winner_wallet) {
            setWinner(u.winner_wallet);
            if (payoutRef.current) clearInterval(payoutRef.current);
            setPayoutSecs(300);
            payoutRef.current = setInterval(() => {
              setPayoutSecs(p => {
                if (p <= 1) {
                  clearInterval(payoutRef.current);
                  setTimeout(() => { setInput(''); setWinner(null); setStatus(''); setMyCount(0); setHasPending(false); loadRound(); }, 1000);
                  return 0;
                }
                return p - 1;
              });
            }, 1000);
          }
        })
      .subscribe();
    return () => { supabase.removeChannel(ch); };
  }, [round?.id]);

  // ── Keyboard ──
  const handleKey = useCallback((k: string) => {
    if (busy || !round || winner || round.status !== 'active') return;
    if (!hasPending) return; // need to pay first
    if (k === '⌫' || k === 'Backspace') { setInput(p => p.slice(0,-1)); }
    else if (k === 'ENTER' || k === 'Enter') {
      if (input.length === WORD_LENGTH) submitGuess();
      else { setShake(true); setTimeout(() => setShake(false), 500); }
    } else if (/^[A-Za-z]$/.test(k) && input.length < WORD_LENGTH) {
      setInput(p => p + k.toUpperCase());
    }
  }, [busy, hasPending, input, round, winner]);

  useEffect(() => {
    const fn = (e: KeyboardEvent) => handleKey(e.key);
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [handleKey]);

  // ── Pay ──
  const handlePay = async () => {
    if (!publicKey || !round || busy) return;
    setBusy(true);
    setStatus('Confirm transaction in your wallet...');
    try {
      const tx = new Transaction().add(
        SystemProgram.transfer({ fromPubkey: publicKey, toPubkey: new PublicKey(GAME_WALLET), lamports: ENTRY_FEE_LAMP })
      );
      const sig = await sendTransaction(tx, connection);
      setStatus('Waiting for Solana confirmation...');
      await new Promise(r => setTimeout(r, 4000));

      setStatus('Verifying payment...');
      const vRes  = await fetch(edgeFn('verify-payment'), { method:'POST', headers:apiH(),
        body: JSON.stringify({ round_id: round.id, wallet_address: publicKey.toString(), tx_signature: sig }) });
      const vData = await vRes.json();
      if (!vData.success) { setStatus(`⚠ ${vData.error}`); setBusy(false); return; }

      setHasPending(true);
      setStatus('✅ Paid! Type your word and press ENTER.');
      // update prize pool locally
      setRound(r => r ? { ...r, prize_pool: r.prize_pool + ENTRY_FEE_SOL } : r);
    } catch (e: any) {
      setStatus(e?.message?.includes('rejected') ? 'Transaction cancelled.' : `Error: ${e?.message}`);
    }
    setBusy(false);
  };

  // ── Submit guess ──
  const submitGuess = async () => {
    if (!publicKey || !round || input.length !== WORD_LENGTH || busy) return;
    setBusy(true);
    setStatus('Checking your word...');
    try {
      const gRes  = await fetch(edgeFn('check-guess'), { method:'POST', headers:apiH(),
        body: JSON.stringify({ round_id: round.id, wallet_address: publicKey.toString(), word_attempt: input }) });
      const gData = await gRes.json();
      if (!gData.success) { setStatus(`⚠ ${gData.error}`); setBusy(false); return; }

      setInput('');
      setStatus(gData.is_winner
        ? '🏆 You won! Prize will be sent in 5 minutes.'
        : `Attempt #${gData.attempts_used} done! Pay 0.01 SOL to try again.`
      );
    } catch (e: any) {
      setStatus(`Error: ${e?.message}`);
    }
    setBusy(false);
  };

  // ── Letter colors (from my guesses) ──
  const letterColors = useMemo<Record<string,Color>>(() => {
    if (!publicKey) return {};
    const mine = allGuesses.filter(g => g.wallet_address === publicKey.toString());
    const map: Record<string,Color> = {};
    for (const g of mine) {
      g.word_attempt.toUpperCase().split('').forEach((l,i) => {
        const c = g.result_colors[i], prev = map[l];
        if (c === 'green' || !prev || (prev === 'gray' && c === 'yellow')) map[l] = c;
      });
    }
    return map;
  }, [allGuesses, publicKey]);

  const isActive   = round?.status === 'active' && !winner && nextRoundIn === 0;
  const timerColor = timeLeft <= 15 ? '#ef4444' : timeLeft <= 30 ? '#f59e0b' : '#22d3ee';

  // ── Render ──
  return (
    <div style={{ minHeight:'100vh', background:'#09090b', color:'#fff', fontFamily:"'Space Grotesk',sans-serif", display:'flex', flexDirection:'column' }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700;800&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        ::-webkit-scrollbar{width:4px} ::-webkit-scrollbar-track{background:#18181b} ::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:2px}
        @keyframes shake{0%,100%{transform:translateX(0)}20%,60%{transform:translateX(-5px)}40%,80%{transform:translateX(5px)}}
        @keyframes fadeIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:translateY(0)}}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
      `}</style>

      {/* Header */}
      <header style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding:'14px 24px', borderBottom:'1px solid #27272a' }}>
        <div style={{ display:'flex', alignItems:'baseline', gap:8 }}>
          <span style={{ fontSize:24, fontWeight:800, letterSpacing:-1 }}>SOL</span>
          <span style={{ fontSize:24, fontWeight:800, color:'#a78bfa', letterSpacing:-1 }}>WORD</span>
          <span style={{ fontSize:11, color:'#52525b', marginLeft:4 }}>Guess the 7-letter word · win the pool</span>
        </div>
        <WalletMultiButton style={{ height:38, fontSize:13, background:'#7c3aed' }}/>
      </header>

      {/* Round Over overlay */}
      {nextRoundIn > 0 && (
        <div style={{
          position:'fixed', inset:0, zIndex:50,
          background:'rgba(9,9,11,0.92)', backdropFilter:'blur(8px)',
          display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center',
          gap:20, animation:'fadeIn 0.4s',
        }}>
          <div style={{ fontSize:64 }}>⏱</div>
          <div style={{ fontSize:28, fontWeight:800, color:'#fff' }}>Round Over</div>
          <div style={{ fontSize:15, color:'#71717a', textAlign:'center' }}>
            {winner
              ? <><b style={{color:'#a78bfa'}}>{short(winner)}</b> guessed the word!</>
              : 'No one guessed the word this round.'
            }
            {!winner && round && round.prize_pool > 0 &&
              <div style={{color:'#fbbf24', marginTop:4}}>Prize pool rolls over to next round!</div>
            }
          </div>
          <div style={{
            width:120, height:120, borderRadius:'50%',
            border:`6px solid #27272a`, display:'flex', flexDirection:'column',
            alignItems:'center', justifyContent:'center', position:'relative',
          }}>
            <svg width={120} height={120} style={{position:'absolute',top:0,left:0,transform:'rotate(-90deg)'}}>
              <circle cx={60} cy={60} r={54} fill="none" stroke="#7c3aed" strokeWidth={6}
                strokeDasharray={`${(nextRoundIn/10)*2*Math.PI*54} ${2*Math.PI*54}`}
                strokeLinecap="round" style={{transition:'stroke-dasharray 1s linear'}}/>
            </svg>
            <div style={{fontSize:36,fontWeight:800,color:'#a78bfa'}}>{nextRoundIn}</div>
            <div style={{fontSize:11,color:'#52525b',letterSpacing:1}}>NEW ROUND</div>
          </div>
        </div>
      )}

      {/* Progress bar */}
      <div style={{ height:3, background:'#18181b' }}>
        {isActive && (
          <div style={{ height:'100%', width:`${(timeLeft/ROUND_SECS)*100}%`,
            background: timerColor, transition:'width 1s linear, background 0.5s' }}/>
        )}
      </div>

      {/* Stats */}
      <div style={{ display:'flex', gap:20, padding:'9px 24px', borderBottom:'1px solid #27272a', background:'#0f0f10', fontSize:13, alignItems:'center', flexWrap:'wrap' }}>
        <span>💰 <b style={{ color:'#fbbf24' }}>{(round?.prize_pool ?? 0).toFixed(4)} SOL</b> prize pool</span>
        <span style={{ color:'#3f3f46' }}>·</span>
        <span style={{ color:'#71717a' }}>0.01 SOL / attempt · unlimited tries</span>
        {winner && (
          <span style={{ marginLeft:'auto', animation:'fadeIn 0.4s' }}>
            🏆 <b style={{ color:'#a78bfa' }}>{short(winner)}</b> won!
            {payoutSecs > 0 && <span style={{ color:'#6b7280' }}> · payout in {payoutSecs}s</span>}
          </span>
        )}
      </div>

      {/* Body: two-column */}
      <div style={{ flex:1, display:'flex', gap:0, justifyContent:'center', padding:'20px 16px', flexWrap:'wrap' }}>

        {/* LEFT: Live feed — newest on top */}
        <div style={{ flex:'1 1 320px', maxWidth:500, display:'flex', flexDirection:'column', gap:8, paddingRight:16 }}>
          <div style={{ fontSize:11, color:'#52525b', letterSpacing:1, textTransform:'uppercase', display:'flex', alignItems:'center', gap:6 }}>
            <span style={{ width:7, height:7, borderRadius:'50%', background:'#ef4444', display:'inline-block', animation:'pulse 1.2s infinite' }}/>
            Live feed · newest first
            {allGuesses.length > 0 && <span style={{ color:'#3f3f46', marginLeft:4 }}>{allGuesses.length} attempt{allGuesses.length !== 1 ? 's' : ''}</span>}
          </div>
          <div style={{ background:'#18181b', borderRadius:10, padding:'10px 14px',
            flex:1, minHeight:420, maxHeight:580, overflowY:'auto', border:'1px solid #27272a' }}>
            {allGuesses.length === 0
              ? <div style={{ color:'#3f3f46', textAlign:'center', marginTop:150, fontSize:14 }}>No attempts yet — be first!</div>
              : allGuesses.map(g => <GuessRow key={g.id} guess={g} isMe={publicKey?.toString() === g.wallet_address}/>)
            }
          </div>
        </div>

        {/* RIGHT: Timer + controls */}
        <div style={{ flex:'0 0 300px', display:'flex', flexDirection:'column', alignItems:'center', gap:14 }}>

          {/* Timer widget */}
          {isActive && (
            <div style={{ background:'#18181b', border:'1px solid #27272a', borderRadius:16,
              padding:'18px 32px', display:'flex', flexDirection:'column', alignItems:'center', gap:8, width:'100%' }}>
              <div style={{ fontSize:11, color:'#52525b', textTransform:'uppercase', letterSpacing:1 }}>Round ends in</div>
              <CircleTimer timeLeft={timeLeft} total={ROUND_SECS}/>
              <div style={{ fontSize:11, color:'#3f3f46' }}>{round?.id.slice(0,8)}</div>
            </div>
          )}

          {/* Winner banner */}
          {winner && (
            <div style={{ background:'rgba(124,58,237,0.15)', border:'1px solid #7c3aed', borderRadius:16,
              padding:'20px 24px', textAlign:'center', width:'100%', animation:'fadeIn 0.5s' }}>
              <div style={{ fontSize:32, marginBottom:8 }}>🏆</div>
              {winner === publicKey?.toString()
                ? <><div style={{ color:'#fbbf24', fontWeight:700, fontSize:18 }}>You won!</div><div style={{ color:'#6b7280', fontSize:13, marginTop:4 }}>Prize arrives in {payoutSecs}s</div></>
                : <><div style={{ color:'#a78bfa', fontWeight:700 }}>{short(winner)} won!</div><div style={{ color:'#6b7280', fontSize:13, marginTop:4 }}>New round in {payoutSecs}s</div></>
              }
            </div>
          )}

          {/* Play controls */}
          {!publicKey ? (
            <div style={{ color:'#52525b', fontSize:14, textAlign:'center', padding:16 }}>Connect your wallet to play</div>
          ) : !round || round.status !== 'active' || winner ? null : (
            <div style={{ width:'100%', display:'flex', flexDirection:'column', gap:11, alignItems:'center' }}>

              {/* Input preview — only shown when has a pending payment */}
              {hasPending && <InputRow value={input} shake={shake}/>}

              {/* PAY button — shown when no pending payment */}
              {!hasPending && (
                <button onClick={handlePay} disabled={busy} style={{
                  width:'100%', padding:'15px 0', borderRadius:8, border:'none',
                  background: busy ? '#27272a' : 'linear-gradient(135deg,#7c3aed,#a855f7)',
                  color: busy ? '#52525b' : '#fff', fontWeight:700, fontSize:16,
                  cursor: busy ? 'not-allowed' : 'pointer', transition:'all 0.2s',
                  boxShadow: !busy ? '0 0 24px rgba(124,58,237,0.3)' : 'none',
                }}>
                  {busy ? '⏳ Processing...' : `🔮 PAY 0.01 SOL${myCount > 0 ? ` · attempt #${myCount+1}` : ' & PLAY'}`}
                </button>
              )}

              {/* SUBMIT button — shown after paying */}
              {hasPending && (
                <button onClick={submitGuess} disabled={busy || input.length !== WORD_LENGTH} style={{
                  width:'100%', padding:'15px 0', borderRadius:8, border:'none',
                  background: busy || input.length !== WORD_LENGTH ? '#27272a' : 'linear-gradient(135deg,#059669,#10b981)',
                  color: busy || input.length !== WORD_LENGTH ? '#52525b' : '#fff',
                  fontWeight:700, fontSize:16,
                  cursor: busy || input.length !== WORD_LENGTH ? 'not-allowed' : 'pointer',
                  transition:'all 0.2s',
                  boxShadow: input.length===WORD_LENGTH&&!busy ? '0 0 20px rgba(16,185,129,0.3)' : 'none',
                }}>
                  {busy ? '⏳ Checking...' : '✅ SUBMIT GUESS'}
                </button>
              )}

              {status && <div style={{ fontSize:13, color:'#a1a1aa', textAlign:'center', lineHeight:1.5 }}>{status}</div>}

              {/* Keyboard always visible when round active */}
              <Keyboard lc={hasPending ? letterColors : {}} onKey={handleKey}/>

              <div style={{ fontSize:11, color:'#3f3f46', textAlign:'center', lineHeight:1.7 }}>
                Pay → type word → submit · unlimited attempts<br/>
                First to guess correctly wins the entire pool
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default function Page() {
  const endpoint = useMemo(() => HELIUS_RPC, []);
  const wallets  = useMemo(() => [new PhantomWalletAdapter(), new SolflareWalletAdapter()], []);
  return (
    <ConnectionProvider endpoint={endpoint}>
      <WalletProvider wallets={wallets} autoConnect>
        <WalletModalProvider>
          <AppContent/>
        </WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}