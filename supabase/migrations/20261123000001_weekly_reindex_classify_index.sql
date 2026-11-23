-- Rebuild the classification covering index every week, one hour after the weekly cleanup
-- (Sat 19:00 UTC = Sun 02:00 Bangkok), so the space of the rows it deleted is given back.
-- Nightly classification updates bloat this index: rebuilt 54 -> 24 MB on 16 Nov 2026, back to 56 MB by
-- 23 Nov 2026. CONCURRENTLY keeps the table readable and writable during the rebuild; pg_cron runs the
-- single statement outside a transaction block, which REINDEX CONCURRENTLY requires.
select cron.schedule(
  'egp-weekly-reindex',
  '0 20 * * 6',
  'reindex index concurrently public.announcements_type_classify_idx'
);
