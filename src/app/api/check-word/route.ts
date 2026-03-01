import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';

const supabase = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!);

export async function POST(req: Request) {
  try {
    const { roundId, guess, walletAddress } = await req.json();

    // 1. Получаем секретное слово текущего раунда
    const { data: round } = await supabase
      .from('global_rounds')
      .select('*, dictionary(word)')
      .eq('id', roundId)
      .single();

    if (!round || round.status !== 'active') {
      return NextResponse.json({ error: "Round not active" }, { status: 403 });
    }

    const secretWord = (round.dictionary as any).word.toUpperCase();
    const currentGuess = guess.toUpperCase();

    // 2. Считаем цвета
    const colors = currentGuess.split('').map((char: string, i: number) => {
      if (char === secretWord[i]) return 'green';
      if (secretWord.includes(char)) return 'yellow';
      return 'gray';
    });

    const isWin = currentGuess === secretWord;

    // 3. Сохраняем попытку в базу (это триггернет Realtime на фронтенде)
    const { error: insertError } = await supabase
      .from('guesses')
      .insert([{
        round_id: roundId,
        wallet_address: walletAddress,
        word_attempt: currentGuess,
        result_colors: colors
      }]);

    if (insertError) throw insertError;

    // 4. Если угадали — закрываем раунд
    if (isWin) {
      await supabase.from('global_rounds').update({ status: 'finished' }).eq('id', roundId);
    }

    return NextResponse.json({ success: true, isWin });
  } catch (err: any) {
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}