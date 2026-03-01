import { createClient } from '@supabase/supabase-js';
import { NextResponse } from 'next/server';

const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
);

export async function POST(req: Request) {
  try {
    const { gameId, guess } = await req.json();

    // 1. Получаем секретное слово из базы данных
    const { data: game, error } = await supabase
      .from('games')
      .select('status, dictionary(word)')
      .eq('id', gameId)
      .single();

    if (error || !game) return NextResponse.json({ error: "Game not found" }, { status: 404 });

    const secretWord = (game.dictionary as any).word.toUpperCase();
    const currentGuess = guess.toUpperCase();

    // 2. Логика сравнения (Wordle Logic)
    const result = currentGuess.split('').map((char: string, i: number) => {
      if (char === secretWord[i]) return 'green';
      if (secretWord.includes(char)) return 'yellow';
      return 'gray';
    });

    const isWin = currentGuess === secretWord;

    // 3. (Опционально) Обновляем статус игры в базе, если выиграл
    if (isWin) {
      await supabase.from('games').update({ status: 'won' }).eq('id', gameId);
    }

    return NextResponse.json({ result, isWin });
  } catch (err) {
    return NextResponse.json({ error: "Internal Error" }, { status: 500 });
  }
}