-- The scripts (service role) ran into PostgREST's default 8 s statement timeout on 6-8 Nov 2026: once
-- announcements (139 MB) outgrew the free plan's memory, unclassified_titles() and archive_page() read it
-- from disk right after the evening load and took longer than 8 s, so no title was classified for 3 days.
-- The web app (anon) keeps its own 3 s limit; this only gives the back-end scripts room.
alter role service_role set statement_timeout = '120s';
notify pgrst, 'reload config';
