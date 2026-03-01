import { supabase } from '@/lib/supabase';
import { NextResponse } from 'next/server';

export async function POST(req: Request) {
  try {
    const { walletAddress } = await req.json();

    // 1. Берем случайное слово из словаря
    const { data: words } = await supabase.from('dictionary').select('id');
    if (!words || words.length === 0) throw new Error("Словарь пуст");
    
    const randomWord = words[Math.floor(Math.random() * words.length)];

    // 2. Создаем запись об игре (пока в статусе 'active', потом прикрутим 'pending' для оплаты)
    const { data: game, error } = await supabase
      .from('games')
      .insert([
        { 
          wallet_address: walletAddress, 
          secret_word_id: randomWord.id, 
          status: 'active' 
        }
      ])
      .select()
      .single();

    if (error) throw error;

    // 3. Отдаем клиенту только ID игры
    return NextResponse.json({ gameId: game.id });
    
  } catch (err: any) {
    console.error("DETAILED ERROR:", err); // <-- Эта строка покажет причину в терминале VS Code
    return NextResponse.json({ error: err.message }, { status: 500 });
  }
}