import { createClient } from '@supabase/supabase-js';

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

// Если ключи не загрузились, мы увидим это в консоли сразу
if (!supabaseUrl || !supabaseAnonKey) {
  console.error("MISSING SUPABASE KEYS! Check .env.local");
}

console.log("Supabase URL:", supabaseUrl);
console.log("Supabase Key Loaded:", !!supabaseAnonKey);

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
  auth: {
    persistSession: false // Для игры нам не нужно хранить сессию юзера
  }
});