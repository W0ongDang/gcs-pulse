import { Suspense } from 'react';
import CheckIn from './CheckIn';

export default async function Page({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <Suspense fallback={<p className="p-8 text-center">출석 확인 중…</p>}><CheckIn token={token} /></Suspense>;
}
