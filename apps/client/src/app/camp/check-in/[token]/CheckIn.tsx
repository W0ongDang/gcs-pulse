'use client';
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/auth-context';
import { api, ApiError } from '@/lib/api';

type Receipt = { status: 'success' | 'duplicate'; name: string; affiliation: string; checked_at: string;
  session: { number: number; title: string; date: string } };

export default function CheckIn({ token }: { token: string }) {
  const { isAuthenticated, isLoading, authError } = useAuth();
  const router = useRouter();
  const pending = useRef(false);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (isLoading || authError) return;
    if (!isAuthenticated) {
      router.replace('/login?next=' + encodeURIComponent('/camp/check-in/' + token));
      return;
    }
    if (pending.current) return;
    pending.current = true;
    api.post<Receipt>('/camp-attendance/check-in/' + encodeURIComponent(token), {})
      .then(setReceipt).catch((e) => {
        if (e instanceof ApiError && e.status === 401) {
          router.replace('/login?next=' + encodeURIComponent('/camp/check-in/' + token));
        } else setError(e instanceof Error ? e.message : '출석을 확인하지 못했습니다. 다시 시도해 주세요.');
      });
  }, [isAuthenticated, isLoading, authError, token, router, retry]);
  return <main className="mx-auto flex min-h-[75vh] max-w-lg items-center p-6">
    <section className="w-full rounded-3xl border border-border bg-card p-8 text-center shadow-sm" aria-live="polite">
      <p className="text-sm text-muted-foreground">ENGLISH CAMP</p>
      <div className="my-6 text-5xl" aria-hidden>{receipt ? '✓' : error || authError ? '!' : '…'}</div>
      <h1 className="text-2xl font-bold">{receipt ? receipt.status === 'success' ? '출석 완료!' : '이미 출석했습니다' : error || authError ? '출석 확인이 필요합니다' : '출석 확인 중…'}</h1>
      {receipt ? <><p className="mt-4 text-xl font-semibold">{receipt.name} 님</p><p className="mt-2">{receipt.affiliation}</p>
        <p className="mt-6">{receipt.session.number}회차 · {receipt.session.title}</p><p>{receipt.session.date}</p>
        <p className="mt-2 text-muted-foreground">{new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(receipt.checked_at))} 출석</p></> : <p className="mt-4">{error || authError || '잠시만 기다려 주세요.'}</p>}
      {error && <button className="mt-6 rounded-xl bg-primary px-6 py-3 text-primary-foreground" onClick={() => { pending.current = false; setError(''); setRetry(v => v + 1); }}>다시 확인</button>}
    </section>
  </main>;
}
