-- unclassified_titles() reads only these columns; with them in an index it no longer scans the 112 MB
-- announcements heap (raw jsonb) after every evening load. Measured 9 Nov 2026: 735 ms -> 155 ms warm,
-- announcements buffers 14,301 -> 8,674. Applied by hand on 9 Nov (needs no lock beyond a few seconds).
create index if not exists announcements_type_classify_idx
  on public.announcements (announce_type, id) include (title, method, first_seen_at);
