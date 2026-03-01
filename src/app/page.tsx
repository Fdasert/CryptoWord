'use client';

import { useState, useEffect, useCallback } from 'react';

type LetterStatus = 'empty' | 'gray' | 'yellow' | 'green';
interface Tile { char: string; status: LetterStatus; }

export default function WordleGame() {
  const [gameId, setGameId] = useState<string | null>(null);
  const [board, setBoard] = useState<Tile[][]>(
    Array(6).fill(null).map(() => Array(5).fill({ char: '', status: 'empty' }))
  );
  const [currentRow, setCurrentRow] = useState(0);
  const [currentCol, setCurrentCol] = useState(0);
  const [gameState, setGameState] = useState<'playing' | 'loading' | 'finished'>('loading');
  const [timeLeft, setTimeLeft] = useState(60); // Таймер на 60 секунд
  const [message, setMessage] = useState('Initializing session...');

  // Функция старта новой игры
  const startNewGame = useCallback(async () => {
    try {
      setGameState('loading');
      setTimeLeft(60); // Сброс таймера
      setBoard(Array(6).fill(null).map(() => Array(5).fill({ char: '', status: 'empty' })));
      setCurrentRow(0);
      setCurrentCol(0);

      const res = await fetch('/api/start-game', {
        method: 'POST',
        body: JSON.stringify({ walletAddress: "Phantom_User" }),
      });
      const data = await res.json();

      if (data.gameId) {
        setGameId(data.gameId);
        setGameState('playing');
        setMessage('New game started! Guess the word.');
      }
    } catch (err) {
      setMessage('Connection error...');
    }
  }, []);

  // Таймер автоматического перезапуска
  useEffect(() => {
    if (timeLeft <= 0) {
      startNewGame();
      return;
    }
    const timer = setInterval(() => setTimeLeft(prev => prev - 1), 1000);
    return () => clearInterval(timer);
  }, [timeLeft, startNewGame]);

  // Запуск первой игры
  useEffect(() => { startNewGame(); }, [startNewGame]);

  // Ввод (ТОЛЬКО АНГЛИЙСКИЙ)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (gameState !== 'playing') return;
      if (e.key === 'Enter') submitGuess();
      else if (e.key === 'Backspace') deleteLetter();
      else if (/^[a-zA-Z]$/.test(e.key) && currentCol < 5) addLetter(e.key.toUpperCase());
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [currentCol, currentRow, gameState, board]);

  const addLetter = (char: string) => {
    const newBoard = [...board];
    newBoard[currentRow][currentCol] = { char, status: 'empty' };
    setBoard(newBoard);
    setCurrentCol(currentCol + 1);
  };

  const deleteLetter = () => {
    if (currentCol === 0) return;
    const newBoard = [...board];
    newBoard[currentRow][currentCol - 1] = { char: '', status: 'empty' };
    setBoard(newBoard);
    setCurrentCol(currentCol - 1);
  };

  const submitGuess = async () => {
    if (currentCol < 5) return;
    const guess = board[currentRow].map(t => t.char).join('');
    
    try {
      const res = await fetch('/api/check-word', {
        method: 'POST',
        body: JSON.stringify({ gameId, guess }),
      });
      const { result, isWin } = await res.json();

      const newBoard = [...board];
      newBoard[currentRow] = board[currentRow].map((t, i) => ({ char: t.char, status: result[i] }));
      setBoard(newBoard);

      if (isWin) {
        setGameState('finished');
        setMessage('CORRECT! 🎉 Wait for next round.');
      } else if (currentRow === 5) {
        setGameState('finished');
        setMessage('GAME OVER. Next round soon.');
      } else {
        setCurrentRow(currentRow + 1);
        setCurrentCol(0);
      }
    } catch (err) { setMessage('API Error'); }
  };

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-black text-white p-4">
      <div className="text-sm font-mono text-cyan-500 mb-2">Next session in: {timeLeft}s</div>
      <h1 className="text-4xl font-black mb-8 tracking-tighter text-white">CRYPTO WORD</h1>

      <div className="mb-6 text-center text-slate-400 uppercase tracking-widest text-sm h-5">
        {message}
      </div>

      <div className="grid grid-rows-6 gap-1.5 mb-10">
        {board.map((row, i) => (
          <div key={i} className="grid grid-cols-5 gap-1.5">
            {row.map((tile, j) => (
              <div key={j} className={`w-14 h-14 border-2 flex items-center justify-center text-2xl font-bold transition-all duration-500
                ${tile.status === 'empty' ? 'border-zinc-800 bg-transparent' : ''}
                ${tile.status === 'green' ? 'bg-green-600 border-green-600' : ''}
                ${tile.status === 'yellow' ? 'bg-yellow-500 border-yellow-500' : ''}
                ${tile.status === 'gray' ? 'bg-zinc-700 border-zinc-700' : ''}
              `}>
                {tile.char}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}