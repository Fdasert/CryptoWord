# WORDGUESS — Guess the 7-letter word · Win SOL

> A competitive on-chain word game built on Solana. Pay 0.01 SOL per attempt, guess the secret 7-letter word first, win the entire prize pool.

🌐 **[wordguess.space](https://wordguess.space)**

---

## How it works

1. A random 7-letter word is selected from a custom dictionary at the start of each round
2. Players pay **0.01 SOL** per guess attempt
3. After each guess, tiles reveal color hints:
   - 🟩 **Green** — correct letter, correct position
   - 🟨 **Yellow** — correct letter, wrong position
   - ⬛ **Gray** — letter not in the word
4. The first player to guess the word correctly wins **95% of the prize pool**
5. A new round starts automatically after every win or timeout

---

## Features

- ⚡ Real-time live feed — see all players' guesses as they happen
- 🎮 Free demo mode — try the game without a wallet
- 🔒 Word validation against a 42,000+ word dictionary — no nonsense inputs
- 👥 Online counter — see how many players are active
- 🏆 Automatic on-chain payouts via Solana
- 📱 Fully responsive — works on mobile

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 14, React, TypeScript |
| Blockchain | Solana (web3.js, wallet-adapter) |
| Backend | Supabase (Postgres + Edge Functions + Realtime) |
| RPC | Helius |
| Hosting | Vercel |

---

## Architecture

```
Player browser
    │
    ├── Supabase Realtime ──── live feed, online counter
    │
    ├── Edge Functions
    │       ├── verify-payment    — confirms SOL tx on-chain
    │       ├── check-guess       — validates word + computes colors
    │       ├── end-round         — reveals secret word, triggers payout
    │       ├── start-round       — picks new random word, opens round
    │       ├── payout-winner     — sends SOL to winner wallet
    │       └── check-balance     — returns game wallet SOL balance
    │
    └── Postgres (Supabase)
            ├── global_rounds     — round state, secret word, prize pool
            ├── guesses           — all player attempts + color results
            ├── dictionary        — 632 curated 7-letter secret words
            ├── valid_words       — 42,002 words for input validation
            ├── pending_payouts   — payout queue
            └── site_presence     — online user tracking
```

---

## Anti-cheat

The game uses a **custom secret word dictionary** (632 words) that is separate from the validation dictionary. This means standard Wordle solvers and AI assistants cannot predict the answer — the only way to win is to think and use color hints from the live feed.

---

## Transparency

All payouts are on-chain and publicly verifiable:

- 🏦 [Game wallet on Solscan](https://solscan.io/account/6ei4xUpeKjKs3uHVkmbxcGvhczWrW8QJ2zTf9a4qUHfe)
- Prize pool balance is visible in real-time on the site
- 5% service fee on winnings only — no fee on losing attempts

---

## Local Development

```bash
# Clone
git clone https://github.com/Fdasert/CryptoWord.git
cd CryptoWord

# Install
npm install

# Set up environment variables
cp .env.example .env.local
# Fill in your Supabase and Helius credentials

# Run
npm run dev
```

### Environment Variables

```env
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
NEXT_PUBLIC_HELIUS_RPC=
```

---

## License

MIT
