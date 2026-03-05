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

const GAME_WALLET    = '6ei4xUpeKjKs3uHVkmbxcGvhczWrW8QJ2zTf9a4qUHfe';
const HELIUS_RPC     = 'https://mainnet.helius-rpc.com/?api-key=676b709c-1c3e-4fba-a47d-5cd3f2e78283';
const SUPABASE_URL   = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SUPABASE_ANON  = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;
const WORD_LENGTH    = 7;
const ENTRY_FEE_SOL  = 0.01;
const ENTRY_FEE_LAMP = ENTRY_FEE_SOL * LAMPORTS_PER_SOL;
const ROUND_SECS     = 90;

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
  return (
    <div style={{
      width:44,height:44,display:'flex',alignItems:'center',justifyContent:'center',
      background:bg,border:`2px solid ${color==='empty'?'#3a3a3c':'transparent'}`,
      borderRadius:4,fontSize:20,fontWeight:700,color:'#fff',textTransform:'uppercase',
      transition:'background 0.3s',
    }}>{letter}</div>
  );
}

function GuessRow({ guess, isMe }: { guess: Guess; isMe: boolean }) {
  const letters = guess.word_attempt.toUpperCase().split('');
  const isWin = guess.result_colors.every(c => c === 'green');
  return (
    <div style={{display:'flex',alignItems:'center',gap:8,padding:'5px 0',borderRadius:6,
      background:isWin?'rgba(83,141,78,0.1)':'transparent'}}>
      <span style={{fontSize:11,color:isMe?'#a78bfa':'#6b7280',fontFamily:'monospace',
        minWidth:76,textAlign:'right',fontWeight:isMe?700:400}}>
        {isWin?'🏆 ':''}{isMe?'YOU':shortWallet(guess.wallet_address)}
      </span>
      <div style={{display:'flex',gap:4}}>
        {letters.map((l,i)=><Tile key={i} letter={l} color={guess.result_colors[i]??'gray'}/>)}
      </div>
    </div>
  );
}

function InputRow({ value, shake }: { value: string; shake: boolean }) {
  const letters = value.toUpperCase().split('');
  return (
    <div style={{display:'flex',gap:4,animation:shake?'shake 0.4s':undefined}}>
      {Array.from({length:WORD_LENGTH}).map((_,i)=><Tile key={i} letter={letters[i]??''} color='empty'/>)}
    </div>
  );
}

const KB_ROWS = [['Q','W','E','R','T','Y','U','I','O','P'],['A','S','D','F','G','H','J','K','L'],['ENTER','Z','X','C','V','B','N','M','⌫']];
function Keyboard({ lc, onKey }: { lc: Record<string,Color>; onKey:(k:string)=>void }) {
  return (
    <div style={{display:'flex',flexDirection:'column',gap:6,alignItems:'center'}}>
      {KB_ROWS.map((row,ri)=>(
        <div key={ri} style={{display:'flex',gap:5}}>
          {row.map(k=>{
            const c=lc[k];
            const bg=c==='green'?'#538d4e':c==='yellow'?'#b59f3b':c==='gray'?'#3a3a3c':'#818384';
            const wide=k==='ENTER'||k==='⌫';
            return (
              <button key={k} onClick={()=>onKey(k)} style={{
                width:wide?60:36,height:54,background:bg,border:'none',borderRadius:4,
                color:'#fff',fontWeight:700,fontSize:wide?11:14,cursor:'pointer',transition:'background 0.2s',
              }}>{k}</button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────
const AppContent = () => {
  const { publicKey, sendTransaction } = useWallet();
  const connection = useMemo(()=>new Connection(HELIUS_RPC,'confirmed'),[]);

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
      .from('guesses').select('*').eq('round_id',data.id).order('created_at',{ascending:true});
    setAllGuesses((guesses??[]) as Guess[]);
    if (publicKey) {
      const mine = (guesses??[]).find(g=>g.wallet_address===publicKey.toString());
      setHasGuessed(!!mine);
    }
    setWinner(data.winner_wallet ?? null);
  }, [publicKey]);

  useEffect(()=>{ loadRound(); },[loadRound]);

  // Timer
  useEffect(()=>{
    if (timerRef.current) clearInterval(timerRef.current);
    if (!round || round.status!=='active' || round.winner_wallet) return;
    // set initial immediately
    setTimeLeft(Math.max(0,Math.floor((new Date(round.end_time).getTime()-Date.now())/1000)));
    timerRef.current = setInterval(async ()=>{
      const secs = Math.max(0,Math.floor((new Date(round.end_time).getTime()-Date.now())/1000));
      setTimeLeft(secs);
      if (secs===0){
        clearInterval(timerRef.current);
        await fetch(edgeFn('end-round'),{method:'POST',headers:apiHeaders(),body:JSON.stringify({round_id:round.id})});
        setTimeout(()=>{ setHasGuessed(false);setInput('');setWinner(null);setStatus('');loadRound(); },2000);
      }
    },1000);
    return ()=>clearInterval(timerRef.current);
  },[round?.id, round?.end_time]);

  // Realtime guesses
  useEffect(()=>{
    if (!round) return;
    const ch = supabase.channel(`g:${round.id}`)
      .on('postgres_changes',{event:'INSERT',schema:'public',table:'guesses',filter:`round_id=eq.${round.id}`},
        p=>{ const g=p.new as Guess; setAllGuesses(prev=>prev.find(x=>x.id===g.id)?prev:[...prev,g]); })
      .subscribe();
    return ()=>{ supabase.removeChannel(ch); };
  },[round?.id]);

  // Realtime winner
  useEffect(()=>{
    if (!round) return;
    const ch = supabase.channel(`rw:${round.id}`)
      .on('postgres_changes',{event:'UPDATE',schema:'public',table:'global_rounds',filter:`id=eq.${round.id}`},
        p=>{
          const u=p.new as Round; setRound(u);
          if (u.winner_wallet){
            setWinner(u.winner_wallet);
            if (payoutRef.current) clearInterval(payoutRef.current);
            setPayoutSecs(300);
            payoutRef.current=setInterval(()=>{
              setPayoutSecs(prev=>{
                if (prev<=1){ clearInterval(payoutRef.current); setTimeout(()=>{ setHasGuessed(false);setInput('');setWinner(null);setStatus('');loadRound(); },1000); return 0; }
                return prev-1;
              });
            },1000);
          }
        })
      .subscribe();
    return ()=>{ supabase.removeChannel(ch); };
  },[round?.id]);

  useEffect(()=>{ if (feedRef.current) feedRef.current.scrollTop=feedRef.current.scrollHeight; },[allGuesses]);

  const handleKey = useCallback((k:string)=>{
    if (busy||hasGuessed||!round||winner||round.status!=='active') return;
    if (k==='⌫'||k==='Backspace'){ setInput(p=>p.slice(0,-1)); }
    else if (k==='ENTER'||k==='Enter'){
      if (input.length===WORD_LENGTH) handlePayAndGuess();
      else { setShake(true); setTimeout(()=>setShake(false),500); }
    } else if (/^[A-Za-z]$/.test(k)&&input.length<WORD_LENGTH){
      setInput(p=>p+k.toUpperCase());
    }
  },[busy,hasGuessed,input,round,winner]);

  useEffect(()=>{
    const fn=(e:KeyboardEvent)=>handleKey(e.key);
    window.addEventListener('keydown',fn);
    return ()=>window.removeEventListener('keydown',fn);
  },[handleKey]);

  const handlePayAndGuess = async ()=>{
    if (!publicKey||!round||input.length!==WORD_LENGTH||busy) return;
    setBusy(true);
    setStatus('Confirm transaction in your wallet...');
    try {
      const tx = new Transaction().add(
        SystemProgram.transfer({fromPubkey:publicKey,toPubkey:new PublicKey(GAME_WALLET),lamports:ENTRY_FEE_LAMP})
      );
      const sig = await sendTransaction(tx,connection);
      setStatus('Waiting for Solana confirmation...');
      await new Promise(r=>setTimeout(r,4000));

      setStatus('Verifying payment...');
      const vRes = await fetch(edgeFn('verify-payment'),{method:'POST',headers:apiHeaders(),
        body:JSON.stringify({round_id:round.id,wallet_address:publicKey.toString(),tx_signature:sig})});
      const vData = await vRes.json();
      if (!vData.success){ setStatus(`⚠ ${vData.error}`); setBusy(false); return; }

      setStatus('Checking your word...');
      const gRes = await fetch(edgeFn('check-guess'),{method:'POST',headers:apiHeaders(),
        body:JSON.stringify({round_id:round.id,wallet_address:publicKey.toString(),word_attempt:input})});
      const gData = await gRes.json();
      if (!gData.success){ setStatus(`⚠ ${gData.error}`); setBusy(false); return; }

      setHasGuessed(true);
      setInput('');
      setStatus(gData.is_winner?'🏆 You won! Prize will be sent in 5 minutes.':'Attempt submitted! Wait for the next round.');
    } catch(e:any){
      const msg=e?.message??'';
      setStatus(msg.includes('rejected')?'Transaction cancelled.':`Error: ${msg}`);
    }
    setBusy(false);
  };

  const letterColors = useMemo<Record<string,Color>>(()=>{
    if (!publicKey) return {};
    const mine = allGuesses.filter(g=>g.wallet_address===publicKey.toString());
    const map: Record<string,Color>={};
    for (const g of mine){
      g.word_attempt.toUpperCase().split('').forEach((l,i)=>{
        const c=g.result_colors[i]; const prev=map[l];
        if (c==='green'||!prev||(prev==='gray'&&c==='yellow')) map[l]=c;
      });
    }
    return map;
  },[allGuesses,publicKey]);

  const isActive = round?.status==='active' && !winner;

  return (
    <div style={{minHeight:'100vh',background:'#09090b',color:'#fff',fontFamily:"'Space Grotesk',sans-serif",display:'flex',flexDirection:'column'}}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700;800&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        ::-webkit-scrollbar{width:4px}
        ::-webkit-scrollbar-track{background:#18181b}
        ::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:2px}
        @keyframes shake{0%,100%{transform:translateX(0)}20%,60%{transform:translateX(-6px)}40%,80%{transform:translateX(6px)}}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
        @keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
      `}</style>

      {/* Header */}
      <header style={{display:'flex',alignItems:'center',justifyContent:'space-between',padding:'14px 24px',borderBottom:'1px solid #27272a'}}>
        <div style={{display:'flex',alignItems:'baseline',gap:8}}>
          <span style={{fontSize:24,fontWeight:800,letterSpacing:-1}}>SOL</span>
          <span style={{fontSize:24,fontWeight:800,color:'#a78bfa',letterSpacing:-1}}>WORD</span>
          <span style={{fontSize:11,color:'#52525b',marginLeft:4}}>Guess the word first · win the pool</span>
        </div>
        <WalletMultiButton style={{height:38,fontSize:13,background:'#7c3aed'}}/>
      </header>

      {/* Thin progress bar */}
      <div style={{height:3,background:'#18181b',width:'100%'}}>
        {isActive && (
          <div style={{
            height:'100%',
            background: timeLeft<=15?'#ef4444':timeLeft<=30?'#f59e0b':'#22d3ee',
            width:`${(timeLeft/ROUND_SECS)*100}%`,
            transition:'width 1s linear, background 0.5s',
          }}/>
        )}
      </div>

      {/* Stats bar */}
      <div style={{display:'flex',gap:24,padding:'10px 24px',borderBottom:'1px solid #27272a',background:'#0f0f10',fontSize:13,flexWrap:'wrap',alignItems:'center'}}>
        <span>💰 Prize pool: <b style={{color:'#fbbf24'}}>{(round?.prize_pool??0).toFixed(4)} SOL</b></span>
        <span style={{color:'#3f3f46'}}>|</span>
        <span>1 attempt = 0.01 SOL</span>
        {winner && (
          <span style={{marginLeft:'auto',animation:'fadeIn 0.4s'}}>
            🏆 <b style={{color:'#a78bfa'}}>{shortWallet(winner)}</b> won!
            {payoutSecs>0&&<span style={{color:'#6b7280'}}> · payout in {payoutSecs}s</span>}
          </span>
        )}
      </div>

      {/* Body */}
      <div style={{flex:1,display:'flex',gap:0,justifyContent:'center',padding:'20px 16px',flexWrap:'wrap'}}>

        {/* Left: feed */}
        <div style={{flex:'1 1 320px',maxWidth:480,display:'flex',flexDirection:'column',gap:8,paddingRight:16}}>
          <div style={{fontSize:11,color:'#52525b',letterSpacing:1,textTransform:'uppercase',display:'flex',alignItems:'center',gap:6}}>
            <span style={{width:7,height:7,borderRadius:'50%',background:'#ef4444',display:'inline-block',animation:'pulse 1.2s infinite'}}/>
            Live attempts
          </div>
          <div ref={feedRef} style={{
            background:'#18181b',borderRadius:10,padding:'10px 14px',
            flex:1,minHeight:400,maxHeight:560,overflowY:'auto',border:'1px solid #27272a',
          }}>
            {allGuesses.length===0 ? (
              <div style={{color:'#3f3f46',textAlign:'center',marginTop:140,fontSize:14}}>
                No attempts yet. Be the first!
              </div>
            ) : allGuesses.map(g=>(
              <GuessRow key={g.id} guess={g} isMe={publicKey?.toString()===g.wallet_address}/>
            ))}
          </div>
        </div>

        {/* Right: timer + input */}
        <div style={{flex:'0 0 320px',display:'flex',flexDirection:'column',alignItems:'center',gap:16}}>

          {/* ── Big Timer ── */}
          {isActive && (
            <div style={{
              background:'#18181b',border:'1px solid #27272a',borderRadius:16,
              padding:'20px 32px',display:'flex',flexDirection:'column',alignItems:'center',gap:8,
              width:'100%',
            }}>
              <div style={{fontSize:11,color:'#52525b',textTransform:'uppercase',letterSpacing:1}}>Round ends in</div>
              <CircleTimer timeLeft={timeLeft} total={ROUND_SECS}/>
              <div style={{fontSize:11,color:'#3f3f46'}}>Round · {round?.id.slice(0,8)}</div>
            </div>
          )}

          {/* ── Winner banner ── */}
          {winner && (
            <div style={{
              background:'rgba(124,58,237,0.15)',border:'1px solid #7c3aed',borderRadius:16,
              padding:'20px 24px',textAlign:'center',width:'100%',animation:'fadeIn 0.5s',
            }}>
              <div style={{fontSize:32,marginBottom:8}}>🏆</div>
              {winner===publicKey?.toString()
                ? <><div style={{color:'#fbbf24',fontWeight:700,fontSize:18}}>You won!</div><div style={{color:'#6b7280',fontSize:13,marginTop:4}}>Prize arrives in {payoutSecs}s</div></>
                : <><div style={{color:'#a78bfa',fontWeight:700}}>{shortWallet(winner)} won!</div><div style={{color:'#6b7280',fontSize:13,marginTop:4}}>New round in {payoutSecs}s</div></>
              }
            </div>
          )}

          {/* ── Play area ── */}
          {!publicKey ? (
            <div style={{color:'#52525b',fontSize:14,textAlign:'center',padding:16}}>Connect your wallet to play</div>
          ) : !round || round.status!=='active' ? (
            <div style={{color:'#52525b',fontSize:14,textAlign:'center'}}>Waiting for next round...</div>
          ) : winner ? null : hasGuessed ? (
            <div style={{textAlign:'center',color:'#6b7280',fontSize:14,padding:16,background:'#18181b',borderRadius:12,width:'100%',border:'1px solid #27272a'}}>
              <div style={{fontSize:24,marginBottom:8}}>⏳</div>
              You already used your attempt.<br/>
              <span style={{fontSize:12,color:'#3f3f46'}}>Wait for the next round ({timeLeft}s)</span>
              {status && <div style={{marginTop:8,fontSize:13,color:'#a78bfa'}}>{status}</div>}
            </div>
          ) : (
            <div style={{width:'100%',display:'flex',flexDirection:'column',gap:12,alignItems:'center'}}>
              <InputRow value={input} shake={shake}/>

              <button
                onClick={handlePayAndGuess}
                disabled={busy||input.length!==WORD_LENGTH}
                style={{
                  width:'100%',padding:'15px 0',borderRadius:8,border:'none',
                  background:busy||input.length!==WORD_LENGTH?'#27272a':'linear-gradient(135deg,#7c3aed,#a855f7)',
                  color:busy||input.length!==WORD_LENGTH?'#52525b':'#fff',
                  fontWeight:700,fontSize:16,cursor:busy||input.length!==WORD_LENGTH?'not-allowed':'pointer',
                  transition:'all 0.2s',
                  boxShadow:input.length===WORD_LENGTH&&!busy?'0 0 24px rgba(124,58,237,0.35)':'none',
                }}
              >
                {busy?'⏳ Processing...':'🔮 PAY 0.01 SOL & GUESS'}
              </button>

              {status && <div style={{fontSize:13,color:'#a1a1aa',textAlign:'center'}}>{status}</div>}

              <Keyboard lc={letterColors} onKey={handleKey}/>

              <div style={{fontSize:11,color:'#3f3f46',textAlign:'center',lineHeight:1.7}}>
                Type a 7-letter word · press PAY & GUESS<br/>
                1 attempt per round · winner takes the pool
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

function CircleTimer({ timeLeft, total }: { timeLeft: number; total: number }) {
  const pct   = total>0 ? timeLeft/total : 0;
  const color = timeLeft<=15?'#ef4444':timeLeft<=30?'#f59e0b':'#22d3ee';
  const r=40, circ=2*Math.PI*r;
  return (
    <div style={{position:'relative',width:100,height:100,display:'flex',alignItems:'center',justifyContent:'center'}}>
      <svg width={100} height={100} style={{position:'absolute',top:0,left:0,transform:'rotate(-90deg)'}}>
        <circle cx={50} cy={50} r={r} fill="none" stroke="#27272a" strokeWidth={7}/>
        <circle cx={50} cy={50} r={r} fill="none" stroke={color} strokeWidth={7}
          strokeDasharray={`${pct*2*Math.PI*r} ${circ}`} strokeLinecap="round"
          style={{transition:'stroke-dasharray 1s linear,stroke 0.5s'}}
        />
      </svg>
      <div style={{position:'relative',textAlign:'center'}}>
        <div style={{fontSize:26,fontWeight:800,color,fontVariantNumeric:'tabular-nums',lineHeight:1}}>{timeLeft}</div>
        <div style={{fontSize:10,color:'#52525b',marginTop:2,letterSpacing:1}}>SEC</div>
      </div>
    </div>
  );
}

export default function Page() {
  const endpoint = useMemo(()=>HELIUS_RPC,[]);
  const wallets  = useMemo(()=>[new PhantomWalletAdapter(),new SolflareWalletAdapter()],[]);
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