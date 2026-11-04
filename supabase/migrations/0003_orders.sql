-- 크레딧 주문. 0002_credits.sql 다음에 실행한다(staging, production 각각 한 번).
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행하거나, supabase CLI로 적용한다.
--
-- 주문은 서버(POST /api/orders)가 상품표에서 금액과 크레딧 수를 찾아 대기(pending)로 만든다.
-- 결제 승인(POST /api/payments/confirm)이 성공하면 완료(done), 토스가 거절하면 실패(failed)가 된다.
-- 크레딧은 이 표가 아니라 원장(credit_ledger)에 reference 'order:주문번호' 로 한 번만 쌓인다.
-- 0001, 0002 와 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key = service_role)만 읽고 쓴다.
-- 로그인 계정을 지우면 함께 지워진다(on delete cascade). 「내 데이터 전체 삭제」로는 지우지 않는다(원장과 같음).

create table if not exists public.orders (
  id bigint generated always as identity primary key,
  order_id text not null unique check (order_id ~ '^[A-Za-z0-9_=-]{6,64}$'),  -- 토스 orderId 규칙. 서버가 무작위로 만든다
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id text not null,                                                     -- app/config.py 의 PRODUCTS 키
  amount integer not null check (amount > 0),                                   -- 원. 승인 때 이 값과 비교한다
  credits integer not null check (credits > 0),                                 -- 승인되면 쌓을 크레딧 수
  status text not null default 'pending' check (status in ('pending', 'done', 'failed')),
  payment_key text,                                                             -- 승인된 결제의 토스 paymentKey
  approved_at timestamptz,                                                      -- 토스가 알려 준 승인 시각
  created_at timestamptz not null default now()
);
create index if not exists orders_user_id_idx on public.orders (user_id, id);

alter table public.orders enable row level security;
