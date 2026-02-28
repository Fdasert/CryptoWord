import { NextResponse } from 'next/server';

export async function GET() {
  try {
    const response = await fetch('https://api.datamuse.com/words?sp=???????&max=50');
    const data = await response.json();
    
    const validWords = data.filter((w: any) => /^[a-zA-Z]{7}$/.test(w.word));
    const randomWord = validWords[Math.floor(Math.random() * validWords.length)].word.toUpperCase();


    return NextResponse.json({ word: randomWord });
  } catch (error) {
    return NextResponse.json({ word: "SOLANAS" }, { status: 500 });
  }
}