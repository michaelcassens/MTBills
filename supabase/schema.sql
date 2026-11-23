-- Bill Watch: AIGPI review storage (Supabase / Postgres)
-- Paste this whole file into Supabase → SQL Editor → New query → Run.
-- Safe to run again; it only creates what is missing.

-- Who may review. Add reviewer emails at the bottom of this file (or in Table Editor).
create table if not exists public.reviewers (
  email text primary key,
  name  text,
  added_at timestamptz default now()
);

-- One row per bill or draft that AIGPI has reviewed. Public can read; reviewers can write.
create table if not exists public.reviews (
  item_key          text primary key,           -- e.g. lc:3:LC0465
  bill_label        text,                       -- e.g. HB 101 (LC0020), for humans browsing the table
  tribal_interest   text not null default 'unreviewed'
                    check (tribal_interest in ('unreviewed','confirmed','not_implicated')),
  consultation      text check (consultation in ('yes','partial','no')),
  aigpi_tracking    boolean generated always as (tribal_interest = 'confirmed') stored,
  public_note       text,
  reviewed_version  text,                       -- text version the reviewer read
  reviewer_email    text,
  updated_at        timestamptz default now()
);

-- Internal notes: reviewers only, never public.
create table if not exists public.review_notes (
  id           bigint generated always as identity primary key,
  item_key     text not null,
  note         text not null,
  author_email text,
  created_at   timestamptz default now()
);

-- Every change to a review, for an audit trail. Reviewers only.
create table if not exists public.review_history (
  id           bigint generated always as identity primary key,
  item_key     text not null,
  changed_by   text,
  changed_at   timestamptz default now(),
  old_row      jsonb,
  new_row      jsonb
);

create or replace function public.is_reviewer() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from public.reviewers
                 where lower(email) = lower(coalesce(auth.jwt() ->> 'email', '')));
$$;

create or replace function public.stamp_review() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  new.reviewer_email := auth.jwt() ->> 'email';
  new.updated_at := now();
  insert into public.review_history (item_key, changed_by, old_row, new_row)
  values (new.item_key, new.reviewer_email,
          case when tg_op = 'UPDATE' then to_jsonb(old) else null end, to_jsonb(new));
  return new;
end $$;

drop trigger if exists stamp_review on public.reviews;
create trigger stamp_review before insert or update on public.reviews
  for each row execute function public.stamp_review();

create or replace function public.stamp_note() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  new.author_email := auth.jwt() ->> 'email';
  return new;
end $$;

drop trigger if exists stamp_note on public.review_notes;
create trigger stamp_note before insert on public.review_notes
  for each row execute function public.stamp_note();

alter table public.reviewers      enable row level security;
alter table public.reviews        enable row level security;
alter table public.review_notes   enable row level security;
alter table public.review_history enable row level security;

drop policy if exists "reviewers see themselves" on public.reviewers;
create policy "reviewers see themselves" on public.reviewers
  for select using (lower(email) = lower(coalesce(auth.jwt() ->> 'email', '')));

drop policy if exists "anyone can read reviews" on public.reviews;
create policy "anyone can read reviews" on public.reviews for select using (true);

drop policy if exists "reviewers add reviews" on public.reviews;
create policy "reviewers add reviews" on public.reviews for insert with check (public.is_reviewer());

drop policy if exists "reviewers change reviews" on public.reviews;
create policy "reviewers change reviews" on public.reviews for update
  using (public.is_reviewer()) with check (public.is_reviewer());

drop policy if exists "reviewers read notes" on public.review_notes;
create policy "reviewers read notes" on public.review_notes for select using (public.is_reviewer());

drop policy if exists "reviewers add notes" on public.review_notes;
create policy "reviewers add notes" on public.review_notes for insert with check (public.is_reviewer());

drop policy if exists "reviewers read history" on public.review_history;
create policy "reviewers read history" on public.review_history for select using (public.is_reviewer());

-- Nobody can delete reviews or notes from the app; do that in the Supabase dashboard if needed.

-- Add your reviewers here (one line each), then run again:
-- insert into public.reviewers (email, name) values ('reviewer@umontana.edu', 'Reviewer Name')
--   on conflict (email) do nothing;
