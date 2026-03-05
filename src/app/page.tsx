'use client';

import React, { useMemo, useState, useEffect, useCallback, useRef } from 'react';
import { ConnectionProvider, WalletProvider, useWallet } from '@solana/wallet-adapter-react';
import { WalletAdapterNetwork } from '@solana/wallet-adapter-base';
import { PhantomWalletAdapter, SolflareWalletAdapter } from '@solana/wallet-adapter-wallets';
import { WalletModalProvider, WalletMultiButton } from '@solana/wallet-adapter-react-ui';
import {
  Connection, Transaction, SystemProgram, PublicKey, LAMPORTS_PER_SOL,
} from '@solana/web3.js';
import { supabase } from '@/lib/supabase';
import '@solana/wallet-adapter-react-ui/styles.css';

export const dynamic = 'force-dynamic';

const GAME_WALLET   = '6ei4xUpeKjKs3uHVkmbxcGvhczWrW8QJ2zTf9a4qUHfe';
const HELIUS_RPC    = 'https://mainnet.helius-rpc.com/?api-key=676b709c-1c3e-4fba-a47d-5cd3f2e78283';
const SUPABASE_URL  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SUPABASE_ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
const WORD_LENGTH   = 7;
const ENTRY_FEE_SOL = 0.01;
const ENTRY_FEE_LAMP = ENTRY_FEE_SOL * LAMPORTS_PER_SOL;

type Color = 'green' | 'yellow' | 'gray' | 'empty';

interface Round {
  id: string; status: string; prize_pool: number;
  entry_fee: number; end_time: string; winner_wallet: string | null;
}
interface Guess {
  id: string; wallet_address: string; word_attempt: string;
  result_colors: Color[]; created_at: string;
}

function edgeFn(n: string) { return `${SUPABASE_URL}/functions/v1/${n}`; }
function apiHeaders() {
  return { 'Content-Type': 'application/json', 'apikey': SUPABASE_ANON, 'Authorization': `Bearer ${SUPABASE_ANON}` };
}
function shortWallet(w: string) { return w.slice(0,4) + '..' + w.slice(-4); }

function Tile({ letter, color }: { letter: string; color: Color }) {
  const bg = color==='green'?'#538d4e':color==='yellow'?'#b59f3b':color==='gray'?'#3a3a3c':'#1a1a1b';
  const border = color === 'empty' ? '2px solid #3a3a3c' : '2px solid transparent';
  return (
    <div style={{
      width:44,height:44,display:'flex',alignItems:'center',justifyContent:'center',
      background:bg,border,borderRadius:4,fontSize:20,fontWeight:700,color:'#fff',
      textTransform:'uppercase',transition:'background 0.3s',
    }}>{letter}</div>
  );
}

function GuessRow({ guess, isMe }: { guess: Guess; isMe: boolean }) {
  const letters = guess.word_attempt.toUpperCase().split('');
  const isWin = guess.result_colors.every(c => c === 'green');
  return (
    <div style={{display:'flex',alignItems:'center',gap:8,padding:'5px 0',borderRadius:6,
      background: isWin ? 'rgba(83,141,78,0.1)' : 'transparent',
    }}>
      <span style={{
        fontSize:11,color:isMe?'#a78bfa':'#6b7280',fontFamily:'monospace',
        minWidth:76,textAlign:'right',fontWeight:isMe?700:400,
      }}>
        {isWin ? '🏆' : ''}{isMe ? 'YOU' : shortWallet(guess.wallet_address)}
      </span>
      <div style={{display:'flex',gap:4}}>
        {letters.map((l,i) => <Tile key={i} letter={l} color={guess.result_colors[i]??'gray'} />)}
      </div>
    </div>
  );
}

function InputRow({ value }: { value: string }) {
  const letters = value.toUpperCase().split('');
  return (
    <div style={{display:'flex',gap:4}}>
      {Array.from({length:WORD_LENGTH}).map((_,i)=><Tile key={i} letter={letters[i]??''} color='empty'/>)}
    </div>
  );
}

const ROWS = [['Q','W','E','R','T','Y','U','I','O','P'],['A','S','D','F','G','H','J','K','L'],['ENTER','Z','X','C','V','B','N','M','⌫']];
function Keyboard({ lc, onKey }: { lc: Record<string,Color>; onKey:(k:string)=>void }) {
  return (
    <div style={{display:'flex',flexDirection:'column',gap:6,alignItems:'center'}}>
      {ROWS.map((row,ri) => (
        <div key={ri} style={{display:'flex',gap:5}}>
          {row.map(k => {
            const c=lc[k];
            const bg=c==='green'?'#538d4e':c==='yellow'?'#b59f3b':c==='gray'?'#3a3a3c':'#818384';
            const wide = k==='ENTER'||k==='⌫';
            return (
              <button key={k} onClick={()=>onKey(k)} style={{
                width:wide?60:36,height:54,background:bg,border:'none',borderRadius:4,
                color:'#fff',fontWeight:700,fontSize:wide?11:14,cursor:'pointer',
              }}>{k}</button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const connection = useMemo(() => new Connection(HELIUS_RPC, 'confirmed'), []);

  const [round, setRound]           = useState<Round|null>(null);
  const [allGuesses, setAllGuesses] = useState<Guess[]>([]);
  const [input, setInput]           = useState('');
  const [timeLeft, setTimeLeft]     = useState(0);
  const [status, setStatus]         = useState('');
  const [busy, setBusy]             = useState(false);
  const [hasGuessed, setHasGuessed] = useState(false);
  const [winner, setWinner]         = useState<string|null>(null);
  const [payoutSecs, setPayoutSecs] = useState(0);
  const [shake, setShake]           = useState(false);

  const timerRef  = useRef<any>(null);
  const payoutRef = useRef<any>(null);
  const feedRef   = useRef<HTMLDivElement>(null);

  const loadRound = useCallback(async () => {
    const { data } = await supabase.from('active_round').select('*').maybeSingle();
    setRound(data ?? null);
    if (!data) { setAllGuesses([]); return; }

    const { data: guesses } = await supabase
      .from('guesses').select('*').eq('round_id', data.id).order('created_at',{ascending:true});
    setAllGuesses((guesses ?? []) as Guess[]);

    if (publicKey) {
      const mine = (guesses ?? []).find(g => g.wallet_address === publicKey.toString());
      setHasGuessed(!!mine);
    }
    if (data.winner_wallet) setWinner(data.winner_wallet);
    else setWinner(null);
  }, [publicKey]);

  useEffect(() => { loadRound(); }, [loadRound]);

  // Timer
  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (!round || round.status !== 'active' || round.winner_wallet) return;
    timerRef.current = setInterval(async () => {
      const secs = Math.max(0, Math.floor((new Date(round.end_time).getTime() - Date.now()) / 1000));
      setTimeLeft(secs);
      if (secs === 0) {
        clearInterval(timerRef.current);
        await fetch(edgeFn('end-round'), { method:'POST', headers:apiHeaders(), body:JSON.stringify({round_id:round.id}) });
        setTimeout(() => { setHasGuessed(false); setInput(''); setWinner(null); setStatus(''); loadRound(); }, 2000);
      }
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [round?.id, round?.end_time]);

  // Realtime guesses
  useEffect(() => {
    if (!round) return;
    const ch = supabase.channel(`g:${round.id}`)
      .on('postgres_changes',{event:'INSERT',schema:'public',table:'guesses',filter:`round_id=eq.${round.id}`},
        payload => {
          const g = payload.new as Guess;
          setAllGuesses(prev => prev.find(x=>x.id===g.id) ? prev : [...prev, g]);
        })
      .subscribe();
    return () => { supabase.removeChannel(ch); };
  }, [round?.id]);

  // Realtime winner
  useEffect(() => {
    if (!round) return;
    const ch = supabase.channel(`rw:${round.id}`)
      .on('postgres_changes',{event:'UPDATE',schema:'public',table:'global_rounds',filter:`id=eq.${round.id}`},
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
                  setTimeout(() => { setHasGuessed(false); setInput(''); setWinner(null); setStatus(''); loadRound(); }, 1000);
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

  // Auto-scroll feed
  useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [allGuesses]);

  // Keyboard handler
  const handleKey = useCallback((k: string) => {
    if (busy || hasGuessed || !round || winner || round.status !== 'active') return;
    if (k === '⌫' || k === 'Backspace') {
      setInput(p => p.slice(0,-1));
    } else if (k === 'ENTER' || k === 'Enter') {
      if (input.length === WORD_LENGTH) handlePayAndGuess();
      else { setShake(true); setTimeout(()=>setShake(false),500); }
    } else if (/^[A-Za-z]$/.test(k) && input.length < WORD_LENGTH) {
      setInput(p => p + k.toUpperCase());
    }
  }, [busy, hasGuessed, input, round, winner]);

  useEffect(() => {
    const fn = (e: KeyboardEvent) => handleKey(e.key);
    window.addEventListener('keydown', fn);
    return () => window.removeEventListener('keydown', fn);
  }, [handleKey]);

  // Pay & Guess
  const handlePayAndGuess = async () => {
    if (!publicKey || !round || input.length !== WORD_LENGTH || busy) return;
    setBusy(true);
    setStatus('Подтверди транзакцию в кошельке...');
    try {
      const tx = new Transaction().add(
        SystemProgram.transfer({ fromPubkey:publicKey, toPubkey:new PublicKey(GAME_WALLET), lamports:ENTRY_FEE_LAMP })
      );
      const sig = await sendTransaction(tx, connection);
      setStatus('Ждём подтверждения Solana...');
      await new Promise(r => setTimeout(r, 4000));

      setStatus('Верификация оплаты...');
      const vRes = await fetch(edgeFn('verify-payment'), {
        method:'POST', headers:apiHeaders(),
        body: JSON.stringify({ round_id:round.id, wallet_address:publicKey.toString(), tx_signature:sig }),
      });
      const vData = await vRes.json();
      if (!vData.success) { setStatus(`⚠ ${vData.error}`); setBusy(false); return; }

      setStatus('Проверяем слово...');
      const gRes = await fetch(edgeFn('check-guess'), {
        method:'POST', headers:apiHeaders(),
        body: JSON.stringify({ round_id:round.id, wallet_address:publicKey.toString(), word_attempt:input }),
      });
      const gData = await gRes.json();
      if (!gData.success) { setStatus(`⚠ ${gData.error}`); setBusy(false); return; }

      setHasGuessed(true);
      setInput('');
      setStatus(gData.is_winner ? '🏆 Ты угадал! Приз придёт через 5 минут.' : 'Попытка принята! Жди следующего раунда.');
    } catch(e: any) {
      const msg = e?.message ?? '';
      setStatus(msg.includes('rejected') ? 'Транзакция отменена.' : `Ошибка: ${msg}`);
    }
    setBusy(false);
  };

  // Letter colors from my guesses
  const letterColors = useMemo<Record<string,Color>>(() => {
    if (!publicKey) return {};
    const mine = allGuesses.filter(g => g.wallet_address === publicKey.toString());
    const map: Record<string,Color> = {};
    for (const g of mine) {
      g.word_attempt.toUpperCase().split('').forEach((l,i) => {
        const c = g.result_colors[i];
        const prev = map[l];
        if (c==='green' || !prev || (prev==='gray' && c==='yellow')) map[l] = c;
      });
    }
    return map;
  }, [allGuesses, publicKey]);

  const timerColor = timeLeft<=15?'#ef4444':timeLeft<=30?'#f59e0b':'#22d3ee';
  const timerPct   = round ? timeLeft / 90 : 0;

  return (
    <div style={{ minHeight:'100vh', background:'#09090b', color:'#fff', fontFamily:"'Space Grotesk',sans-serif", display:'flex', flexDirection:'column' }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: #18181b; }
        ::-webkit-scrollbar-thumb { background: #3f3f46; border-radius: 2px; }
        @keyframes shake { 0%,100%{transform:translateX(0)} 20%,60%{transform:translateX(-6px)} 40%,80%{transform:translateX(6px)} }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.5} }
      `}</style>

      {/* Header */}
      <header style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding:'14px 24px', borderBottom:'1px solid #27272a' }}>
        <div>
          <span style={{fontSize:24,fontWeight:800,letterSpacing:-1}}>SOL</span>
          <span style={{fontSize:24,fontWeight:800,color:'#a78bfa',letterSpacing:-1}}>WORD</span>
          <span style={{fontSize:11,color:'#52525b',marginLeft:8}}>Угадай слово первым</span>
        </div>
        <WalletMultiButton style={{height:38,fontSize:13,background:'#7c3aed'}} />
      </header>

      {/* Timer bar */}
      {round && round.status === 'active' && !winner && (
        <div style={{height:3,background:'#27272a',width:'100%'}}>
          <div style={{height:'100%',background:timerColor,width:`${timerPct*100}%`,transition:'width 1s linear'}} />
        </div>
      )}

      {/* Stats */}
      <div style={{ display:'flex', gap:20, padding:'10px 24px', borderBottom:'1px solid #27272a', background:'#0f0f10', fontSize:13, flexWrap:'wrap', alignItems:'center' }}>
        <span>💰 <b style={{color:'#fbbf24'}}>{(round?.prize_pool ?? 0).toFixed(4)} SOL</b> prize pool</span>
        {round && round.status==='active' && !winner && (
          <span style={{display:'flex',alignItems:'center',gap:6}}>
            <span style={{width:8,height:8,borderRadius:'50%',background:timerColor,display:'inline-block',animation:'pulse 1s infinite'}}/>
            <b style={{color:timerColor}}>{timeLeft}s</b>
          </span>
        )}
        {winner && (
          <span>🏆 <b style={{color:'#a78bfa'}}>{shortWallet(winner)}</b> победил{payoutSecs>0&&` · выплата через ${payoutSecs}s`}</span>
        )}
        <span style={{color:'#3f3f46',marginLeft:'auto',fontSize:11,fontFamily:'monospace'}}>
          {round ? round.id.slice(0,8) : '—'}
        </span>
      </div>

      {/* Body */}
      <div style={{flex:1,display:'flex',flexDirection:'column',alignItems:'center',padding:'20px 16px',gap:18}}>

        {/* Feed */}
        <div style={{width:'100%',maxWidth:500}}>
          <div style={{fontSize:11,color:'#52525b',marginBottom:6,letterSpacing:1,textTransform:'uppercase'}}>
            🔴 Live attempts — все видят попытки друг друга
          </div>
          <div ref={feedRef} style={{
            background:'#18181b',borderRadius:10,padding:'10px 14px',
            height:320,overflowY:'auto',border:'1px solid #27272a',
          }}>
            {allGuesses.length === 0 ? (
              <div style={{color:'#3f3f46',textAlign:'center',marginTop:110,fontSize:14}}>
                Попыток пока нет. Будь первым!
              </div>
            ) : (
              allGuesses.map(g => (
                <GuessRow key={g.id} guess={g} isMe={publicKey?.toString()===g.wallet_address} />
              ))
            )}
          </div>
        </div>

        {/* Play area */}
        {!publicKey ? (
          <div style={{textAlign:'center',color:'#52525b',fontSize:14,padding:20}}>
            Подключи кошелёк чтобы играть
          </div>
        ) : !round || round.status !== 'active' ? (
          <div style={{textAlign:'center',color:'#52525b',fontSize:14}}>Ожидание нового раунда...</div>
        ) : winner ? (
          <div style={{textAlign:'center',padding:16}}>
            {winner===publicKey.toString()
              ? <><div style={{fontSize:36}}>🏆</div><div style={{color:'#fbbf24',fontWeight:700,fontSize:18}}>Ты победил!</div><div style={{color:'#6b7280',fontSize:13,marginTop:4}}>Приз придёт через {payoutSecs}s</div></>
              : <><div style={{fontSize:28}}>😔</div><div style={{color:'#6b7280',fontSize:14}}>Победил {shortWallet(winner)}<br/>Новый раунд через {payoutSecs}s</div></>
            }
          </div>
        ) : hasGuessed ? (
          <div style={{textAlign:'center',color:'#6b7280',fontSize:14,padding:16}}>
            Ты уже сделал попытку.<br/>
            <span style={{fontSize:12,color:'#3f3f46'}}>Жди следующего раунда через {timeLeft}s</span>
          </div>
        ) : (
          <div style={{width:'100%',maxWidth:500,display:'flex',flexDirection:'column',gap:12,alignItems:'center'}}>
            {/* Input preview */}
            <div style={{animation: shake ? 'shake 0.4s' : 'none'}}>
              <InputRow value={input} />
            </div>

            {/* Button */}
            <button
              onClick={handlePayAndGuess}
              disabled={busy || input.length !== WORD_LENGTH}
              style={{
                width:'100%',padding:'15px 0',borderRadius:8,border:'none',
                background: busy||input.length!==WORD_LENGTH ? '#27272a' : 'linear-gradient(135deg,#7c3aed,#a855f7)',
                color: busy||input.length!==WORD_LENGTH ? '#52525b' : '#fff',
                fontWeight:700,fontSize:16,cursor:busy||input.length!==WORD_LENGTH?'not-allowed':'pointer',
                transition:'all 0.2s',boxShadow: input.length===WORD_LENGTH&&!busy ? '0 0 20px rgba(124,58,237,0.3)' : 'none',
              }}
            >
              {busy ? '⏳ Обработка...' : `🔮 PAY 0.01 SOL & GUESS`}
            </button>

            {status && <div style={{fontSize:13,color:'#a1a1aa',textAlign:'center'}}>{status}</div>}

            <Keyboard lc={letterColors} onKey={handleKey} />

            <div style={{fontSize:11,color:'#3f3f46',textAlign:'center',lineHeight:1.6}}>
              1 попытка = 0.01 SOL · 90 секунд на раунд<br/>
              Угадавший забирает весь prize pool
            </div>
          </div>
        )}
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
          <AppContent />
        </WalletModalProvider>
      </WalletProvider>
    </ConnectionProvider>
  );
}