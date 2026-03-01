'use client';

import { useState, useEffect } from 'react';
import { supabase } from '@/lib/supabase';

export default function GlobalWordle() {
  const [currentRoundId, setCurrentRoundId] = useState<string | null>(null);
  const [allGuesses, setAllGuesses] = useState<any[]>([]);
  const [timeLeft, setTimeLeft] = useState(60);

  // 1. Загружаем текущий раунд и все сделанные в нем попытки
  useEffect(() => {
    const fetchActiveRound = async () => {
      const { data: round } = await supabase
        .from('global_rounds')
        .select('id')
        .eq('status', 'active')
        .single();

      if (round) {
        setCurrentRoundId(round.id);
        // Подгружаем уже сделанные ходы
        const { data: guesses } = await supabase
          .from('guesses')
          .select('*')
          .eq('round_id', round.id)
          .order('created_at', { ascending: true });
        
        if (guesses) setAllGuesses(guesses);
      }
    };

    fetchActiveRound();

    // 2. ПОДПИСКА НА REALTIME (Видим чужие ходы мгновенно)
    const channel = supabase
      .channel('global_guesses')
      .on('postgres_changes', { event: 'INSERT', schema: 'public', table: 'guesses' }, 
      (payload) => {
        setAllGuesses((prev) => [...prev, payload.new]);
      })
      .subscribe();

    return () => { supabase.removeChannel(channel); };
  }, []);

  // Функция для сокращения адреса кошелька
  const shortenAddress = (addr: string) => `${addr.slice(0, 4)}...${addr.slice(-4)}`;

  return (
    <div className="flex flex-col items-center min-h-screen bg-[#050505] text-white p-6 font-mono">
      <div className="text-cyan-500 text-xs mb-4">GLOBAL LIVE SESSION | {timeLeft}s</div>
      <h1 className="text-3xl font-black tracking-tighter mb-8">CRYPTO WORDLE</h1>

      <div className="w-full max-w-md space-y-3">
        {/* Отрисовываем все попытки из базы */}
        {allGuesses.map((g, rowIndex) => (
          <div key={g.id} className="flex items-center gap-4">
            <div className="grid grid-cols-5 gap-1 flex-grow">
              {g.word_attempt.split('').map((char: string, i: number) => (
                <div key={i} className={`w-12 h-12 border flex items-center justify-center font-bold text-xl
                  ${g.result_colors[i] === 'green' ? 'bg-green-600 border-green-600' : ''}
                  ${g.result_colors[i] === 'yellow' ? 'bg-yellow-500 border-yellow-500' : ''}
                  ${g.result_colors[i] === 'gray' ? 'bg-zinc-800 border-zinc-800' : ''}
                `}>
                  {char}
                </div>
              ))}
            </div>
            {/* Имя игрока рядом с его попыткой */}
            <span className="text-[10px] text-zinc-500 vertical-text">
              {shortenAddress(g.wallet_address)}
            </span>
          </div>
        ))}

        {/* Пустые строки, если попыток меньше 6 */}
        {Array.from({ length: Math.max(0, 6 - allGuesses.length) }).map((_, i) => (
          <div key={i} className="grid grid-cols-5 gap-1 opacity-20">
            {Array(5).fill(0).map((_, j) => (
              <div key={j} className="w-12 h-12 border border-zinc-700"></div>
            ))}
          </div>
        ))}
      </div>

      {/* Кнопка оплаты для входа */}
      <div className="fixed bottom-10">
        <button className="bg-white text-black px-8 py-3 font-black hover:bg-cyan-400 transition-colors">
          JOIN ROUND (0.001 SOL)
        </button>
      </div>
    </div>
  );
}