-- 크레딧 원장. 0001_init.sql 다음에 실행한다(staging, production 각각 한 번).
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행하거나, supabase CLI로 적용한다.
--
-- 잔액은 따로 저장하지 않고 이 사용자의 delta 를 더해서 구한다. 기록이 한 곳에만 있게 하기 위해서다.
-- 0001 과 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key = service_role)만 읽고 쓴다.

create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  delta integer not null check (delta <> 0),                                   -- +충전·지급, -차감
  reason text not null check (reason in ('signup', 'chat', 'refund', 'purchase')),
  reference text not null unique,                                              -- 같은 일을 두 번 쌓지 않게(signup:사용자ID, chat:무작위 값)
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user_id_idx on public.credit_ledger (user_id, id);

alter table public.credit_ledger enable row level security;

-- 잔액 확인과 차감을 한 번에 한다.
-- 같은 사용자의 요청을 advisory lock 으로 줄 세운다(트랜잭션이 끝나면 풀린다).
-- VOLATILE 함수는 SQL 문마다 새 스냅샷을 잡으므로, 기다린 뒤의 합계는 앞 요청이 넣은 차감을 본다.
-- 그래서 잔액 1 에 두 요청이 동시에 와도 한쪽만 통과한다.
create or replace function public.spend_credit(p_user_id uuid, p_amount integer, p_reason text, p_reference text)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = ''
as $$
declare
  v_balance bigint;
begin
  if p_amount is null or p_amount <= 0 then
    raise exception 'p_amount must be positive';
  end if;
  perform pg_advisory_xact_lock(hashtextextended('credit:' || p_user_id::text, 0));
  select coalesce(sum(delta), 0) into v_balance from public.credit_ledger where user_id = p_user_id;
  if v_balance < p_amount then
    return jsonb_build_object('ok', false, 'balance', v_balance);
  end if;
  insert into public.credit_ledger (user_id, delta, reason, reference)
  values (p_user_id, -p_amount, p_reason, p_reference);
  return jsonb_build_object('ok', true, 'balance', v_balance - p_amount);
end;
$$;

create or replace function public.credit_balance(p_user_id uuid)
returns bigint
language sql
stable
security invoker
set search_path = ''
as $$
  select coalesce(sum(delta), 0) from public.credit_ledger where user_id = p_user_id;
$$;

-- Supabase 에서 함수는 기본으로 아무 역할이나(anon 포함) 실행할 수 있다.
-- 브라우저에 공개된 키(anon, authenticated 역할)로는 부르지 못하게 회수하고, 서버(service_role)에만 준다.
revoke execute on function public.spend_credit(uuid, integer, text, text) from public, anon, authenticated;
revoke execute on function public.credit_balance(uuid) from public, anon, authenticated;
grant execute on function public.spend_credit(uuid, integer, text, text) to service_role;
grant execute on function public.credit_balance(uuid) to service_role;
